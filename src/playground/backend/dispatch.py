"""Backend selection for the apply / destroy / reset / status lifecycle.

The CLI and TUI call these functions instead of reaching into a specific
backend package. Each one routes on ``ResolvedLab.backend`` to the
matching adapter:

- ``local-libvirt``       → :mod:`playground.backend.local_libvirt`
- ``local-vbox``          → :mod:`playground.backend.local_vbox`
- ``cloud-digitalocean``  → :mod:`playground.backend.cloud_digitalocean`

``local-libvirt`` is tofu-centric and takes a ``tofu_dir``; ``local-vbox``
drives VBoxManage directly and ignores it. The dispatch functions present
the libvirt-style signature (with ``tofu_dir``) and simply drop it for
vbox and cloud-digitalocean, so callers don't branch on backend.

Unknown backends are rejected at the CLI via
:func:`unsupported_backend_diagnostic` before any of these are called
(the validator already guarantees the backend has a ProviderConfig, but
that's not the same as having an implemented adapter).
"""

from __future__ import annotations

from pathlib import Path

from playground.backend import cloud_digitalocean, local_libvirt, local_vbox
from playground.events import EventBus
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus
from playground.planner.plan import CostEstimate
from playground.runs import OperationRun

LIBVIRT = "local-libvirt"
VBOX = "local-vbox"
DIGITALOCEAN = "cloud-digitalocean"
SUPPORTED_BACKENDS = (LIBVIRT, VBOX, DIGITALOCEAN)


def is_supported(backend: str) -> bool:
    return backend in SUPPORTED_BACKENDS


def unsupported_backend_diagnostic(backend: str) -> Diagnostic:
    return Diagnostic(
        id="runtime.backend.unsupported",
        severity="error",
        message=(
            f"backend {backend!r} has no implemented adapter "
            f"(supported: {', '.join(SUPPORTED_BACKENDS)})"
        ),
        source=SourceLocation(path="config"),
        suggestion=(
            "set spec.backend to one of the supported backends, or "
            "implement an adapter under src/playground/backend/"
        ),
    )


def verb_not_supported_diagnostic(verb: str, backend: str) -> Diagnostic:
    """Return an error diagnostic for a verb that is not supported by ``backend``.

    Used by :func:`execute_suspend` and :func:`execute_resume` when called
    with a local backend that has no concept of suspend/resume.
    """
    return Diagnostic(
        id="runtime.backend.verb_not_supported",
        severity="error",
        message=(
            f"backend {backend!r} does not support {verb!r} "
            "(only cloud backends do)"
        ),
        source=SourceLocation(path="config"),
        suggestion=(
            f"use `playground destroy` then `playground apply` to cycle a "
            f"local lab; {verb!r} is only meaningful for cloud backends that "
            "charge for idle compute"
        ),
    )


def execute_apply(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    ansible_dir: Path,
    config_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    if resolved.backend == VBOX:
        return local_vbox.execute_apply(
            resolved=resolved, state_dir=state_dir,
            ansible_dir=ansible_dir, config_dir=config_dir, bus=bus,
        )
    if resolved.backend == DIGITALOCEAN:
        return cloud_digitalocean.execute_apply(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir,
            ansible_dir=ansible_dir, config_dir=config_dir, bus=bus,
        )
    if resolved.backend == LIBVIRT:
        return local_libvirt.execute_apply(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir,
            ansible_dir=ansible_dir, config_dir=config_dir, bus=bus,
        )
    return None, [unsupported_backend_diagnostic(resolved.backend)]


def execute_destroy(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    if resolved.backend == VBOX:
        return local_vbox.execute_destroy(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )
    if resolved.backend == DIGITALOCEAN:
        return cloud_digitalocean.execute_destroy(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
        )
    # CLI guards unsupported backends before here; libvirt is the default.
    return local_libvirt.execute_destroy(
        resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
    )


def execute_reset(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    if resolved.backend == VBOX:
        return local_vbox.execute_reset(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )
    if resolved.backend == DIGITALOCEAN:
        return cloud_digitalocean.execute_reset(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
        )
    return local_libvirt.execute_reset(
        resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
    )


def execute_suspend(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    """Suspend a cloud lab by destroying its Droplets to stop billing.

    Returns ``(None, [diag])`` for local backends that do not support suspend;
    ``diag.id`` will be ``"runtime.backend.verb_not_supported"``.
    """
    if resolved.backend == DIGITALOCEAN:
        return cloud_digitalocean.execute_suspend(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
        )
    return None, [verb_not_supported_diagnostic("suspend", resolved.backend)]


def execute_resume(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    ansible_dir: Path,
    config_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    """Resume a suspended cloud lab by rebuilding its Droplets.

    Returns ``(None, [diag])`` for local backends that do not support resume;
    ``diag.id`` will be ``"runtime.backend.verb_not_supported"``.
    """
    if resolved.backend == DIGITALOCEAN:
        return cloud_digitalocean.execute_resume(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir,
            ansible_dir=ansible_dir, config_dir=config_dir, bus=bus,
        )
    return None, [verb_not_supported_diagnostic("resume", resolved.backend)]


def query_status(
    resolved: ResolvedLab, tofu_dir: Path,
) -> tuple[LabStatus, list[Diagnostic]]:
    if resolved.backend == VBOX:
        return local_vbox.query_status(resolved)
    if resolved.backend == DIGITALOCEAN:
        return cloud_digitalocean.query_status(resolved)
    return local_libvirt.query_status(resolved, tofu_dir)


def estimate_cost(
    resolved: ResolvedLab,
    *,
    config_dir: Path | None = None,
) -> CostEstimate | None:
    """Return an advisory :class:`CostEstimate` for ``resolved``, or ``None``.

    Only ``cloud-digitalocean`` labs produce a cost estimate; local backends
    return ``None`` because there is no per-VM usage charge.

    ``config_dir`` is forwarded to :func:`merge_provider_settings` so the
    estimate reflects the merged provider-config defaults, not just lab
    overrides.
    """
    if resolved.backend == DIGITALOCEAN:
        provider_settings = cloud_digitalocean.merge_provider_settings(
            resolved, config_dir=config_dir
        )
        plan = cloud_digitalocean.build_do_plan(
            resolved,
            provider_settings=provider_settings,
        )
        return cloud_digitalocean.estimate_cost(plan.size, plan.vm_count)
    return None


def plan_provider_summary(
    resolved: ResolvedLab,
    *,
    config_dir: Path | None = None,
) -> dict[str, str] | None:
    """Return an ordered provider-detail dict for the plan display, or ``None``.

    Only ``cloud-digitalocean`` labs return a non-``None`` value; local
    backends return ``None`` (no per-VM cloud settings to show).

    The returned dict contains:

    ``region``
        DigitalOcean region slug.
    ``size``
        Droplet size slug.
    ``image``
        Droplet base image slug.
    ``ssh_exposure``
        Human-readable SSH CIDR allowlist: ``"open to all (0.0.0.0/0,
        ::/0)"`` when ``firewall_ssh_cidrs`` is empty; otherwise the
        joined CIDR list.

    :param resolved: Fully resolved lab model.
    :param config_dir: Config directory to load provider defaults from.
    :returns: Ordered dict or ``None`` for non-DO backends.
    """
    if resolved.backend != DIGITALOCEAN:
        return None

    provider_settings = cloud_digitalocean.merge_provider_settings(
        resolved, config_dir=config_dir
    )
    plan = cloud_digitalocean.build_do_plan(
        resolved, provider_settings=provider_settings
    )

    if plan.firewall_ssh_cidrs:
        ssh_exposure: str = ", ".join(plan.firewall_ssh_cidrs)
    else:
        ssh_exposure = "open to all (0.0.0.0/0, ::/0)"

    return {
        "region": plan.region,
        "size": plan.size,
        "image": plan.image,
        "ssh_exposure": ssh_exposure,
    }


__all__ = [
    "DIGITALOCEAN",
    "LIBVIRT",
    "SUPPORTED_BACKENDS",
    "VBOX",
    "estimate_cost",
    "execute_apply",
    "execute_destroy",
    "execute_reset",
    "execute_resume",
    "execute_suspend",
    "is_supported",
    "plan_provider_summary",
    "query_status",
    "unsupported_backend_diagnostic",
    "verb_not_supported_diagnostic",
]
