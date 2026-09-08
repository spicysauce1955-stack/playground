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
from typing import TYPE_CHECKING

from playground.backend import cloud_digitalocean, local_libvirt, local_vbox
from playground.devices.adb import list_attached
from playground.devices.binding import read_binding
from playground.devices.registry import read_registry
from playground.events import EventBus
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedDevice, ResolvedLab
from playground.models.status import DeviceStatus, LabStatus
from playground.planner.plan import CostEstimate
from playground.runs import OperationRun

if TYPE_CHECKING:
    from playground.config.loader import LoadedConfig

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
    config_dir: Path | None = None,
) -> tuple[OperationRun, list[Diagnostic]]:
    if resolved.backend == VBOX:
        return local_vbox.execute_destroy(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )
    if resolved.backend == DIGITALOCEAN:
        return cloud_digitalocean.execute_destroy(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
            config_dir=config_dir,
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
    config_dir: Path | None = None,
) -> tuple[OperationRun, list[Diagnostic]]:
    if resolved.backend == VBOX:
        return local_vbox.execute_reset(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )
    if resolved.backend == DIGITALOCEAN:
        return cloud_digitalocean.execute_reset(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
            config_dir=config_dir,
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
    config_dir: Path | None = None,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    """Suspend a cloud lab by destroying its Droplets to stop billing.

    Returns ``(None, [diag])`` for local backends that do not support suspend;
    ``diag.id`` will be ``"runtime.backend.verb_not_supported"``.
    """
    if resolved.backend == DIGITALOCEAN:
        return cloud_digitalocean.execute_suspend(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
            config_dir=config_dir,
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
    resolved: ResolvedLab, tofu_dir: Path, state_dir: Path = Path(".playground"),
) -> tuple[LabStatus, list[Diagnostic]]:
    """Backend-observed status, plus BACKEND-NEUTRAL device status.

    No backend provisions a physical handset -- `local-libvirt`,
    `local-vbox` and `cloud-digitalocean` all delegate their VM-shaped
    status to the matching adapter below, then this function alone fills
    in ``LabStatus.devices`` from the same device machinery every other
    device-aware verb uses, so a handset works identically regardless of
    which backend the rest of the lab uses. ``state_dir`` defaults to
    the same ``.playground`` every other command defaults to, so callers
    that pass none still see live device state for the common case.
    """
    if resolved.backend == VBOX:
        status, diagnostics = local_vbox.query_status(resolved)
    elif resolved.backend == DIGITALOCEAN:
        status, diagnostics = cloud_digitalocean.query_status(resolved)
    else:
        status, diagnostics = local_libvirt.query_status(resolved, tofu_dir)

    if resolved.devices:
        status = status.model_copy(
            update={
                "devices": _device_statuses(state_dir, resolved.lab_name, resolved.devices)
            }
        )
    return status, diagnostics


def _device_statuses(
    state_dir: Path, lab: str, devices: list[ResolvedDevice]
) -> list[DeviceStatus]:
    """Read-only ``DeviceStatus`` for every declared slot.

    Deliberately does NOT call ``resolve_binding`` -- that function can
    auto-bind (and persist) an unbound slot with exactly one matching
    candidate, which is exactly the mutation a `status` query must never
    cause as a side effect. Instead it reads the already-persisted
    binding (`read_binding`) and cross-checks it against a live `adb
    devices -l` (`list_attached`) for the slot's CURRENT connectivity, and
    the registry (`read_registry`) for the facts recorded at the slot's
    last adoption -- no `probe_facts` re-probe, so this stays as cheap as
    any other status query. A slot with no persisted binding, or whose
    bound serial is not currently attached, reads as ``missing``: an
    un-adopted device is not a live target yet, matching the same
    "never adopted/probed" semantics `android.targets.resolve_targets`
    already applies to a slot absent from `status.devices` entirely.
    """
    attached, _adb_diagnostics = list_attached()
    attached_by_serial = {candidate.serial: candidate for candidate in attached}
    registry = read_registry(state_dir)

    statuses: list[DeviceStatus] = []
    for device in devices:
        binding = read_binding(state_dir, lab, device.name)
        if binding is None:
            statuses.append(DeviceStatus(name=device.name, state="missing"))
            continue
        candidate = attached_by_serial.get(binding.serial)
        if candidate is None:
            statuses.append(
                DeviceStatus(name=device.name, state="missing", serial=binding.serial)
            )
            continue
        entry = registry.get(binding.serial)
        facts = entry.facts if entry is not None else None
        statuses.append(
            DeviceStatus(
                name=device.name,
                state=candidate.state,
                serial=binding.serial,
                model=facts.model if facts is not None else candidate.model,
                manufacturer=facts.manufacturer if facts is not None else None,
                sdk=facts.sdk if facts is not None else None,
                abi=facts.abi if facts is not None else None,
                rooted=facts.rooted if facts is not None else None,
                last_verified=entry.last_verified if entry is not None else None,
            )
        )
    return statuses


def estimate_cost(
    resolved: ResolvedLab,
    *,
    config_dir: Path | None = None,
    loaded: LoadedConfig | None = None,
) -> CostEstimate | None:
    """Return an advisory :class:`CostEstimate` for ``resolved``, or ``None``.

    Only ``cloud-digitalocean`` labs produce a cost estimate; local backends
    return ``None`` because there is no per-VM usage charge.

    ``config_dir`` is forwarded to :func:`merge_provider_settings` so the
    estimate reflects the merged provider-config defaults, not just lab
    overrides.  Pass ``loaded`` to reuse an already-parsed config and avoid
    re-loading the config tree.
    """
    if resolved.backend == DIGITALOCEAN:
        provider_settings = cloud_digitalocean.merge_provider_settings(
            resolved, config_dir=config_dir, loaded=loaded,
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
    loaded: LoadedConfig | None = None,
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
    :param loaded: Already-loaded config to reuse.  When provided,
        *config_dir* is ignored, avoiding redundant I/O.
    :returns: Ordered dict or ``None`` for non-DO backends.
    """
    if resolved.backend != DIGITALOCEAN:
        return None

    provider_settings = cloud_digitalocean.merge_provider_settings(
        resolved, config_dir=config_dir, loaded=loaded,
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
