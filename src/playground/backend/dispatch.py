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
from playground.devices.binding import match_candidates, read_binding
from playground.devices.registry import read_registry
from playground.events import EventBus
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedDevice, ResolvedLab
from playground.models.status import DeviceStatus, LabStatus
from playground.planner.plan import CostEstimate
from playground.runs import OperationRun
from playground.state.isolation import (
    IsolationRecord,
    clear_isolation,
    now_iso,
    read_isolation,
    write_isolation,
)

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


_ISOLATION_VERBS = ("isolate", "connect")


def verb_not_supported_diagnostic(verb: str, backend: str) -> Diagnostic:
    """Return an error diagnostic for a verb that is not supported by ``backend``.

    Used by :func:`execute_suspend` / :func:`execute_resume` when called with
    a local backend that has no concept of suspend/resume, and by
    :func:`execute_isolate` / :func:`execute_connect` for a backend with no
    egress-isolation mechanism (``local-vbox``).
    """
    if verb in _ISOLATION_VERBS:
        return Diagnostic(
            id="runtime.backend.verb_not_supported",
            severity="error",
            message=(
                f"backend {backend!r} does not support {verb!r} "
                "(no egress-isolation mechanism is implemented for it)"
            ),
            source=SourceLocation(path="config"),
            suggestion=(
                "egress isolation is available on local-libvirt and "
                "cloud-digitalocean labs only"
            ),
        )
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


def lab_isolated_diagnostic(
    verb: str, lab: str, record: IsolationRecord, *, read_error: str | None = None,
) -> Diagnostic:
    """Error for a ``verb`` (apply / resume) refused on an isolated lab."""
    if read_error is not None:
        why = (
            f"its isolation record could not be read ({read_error}); "
            "treating it as isolated"
        )
    elif record.state == "isolating":
        why = (
            f"an isolate started {record.isolated_at} did not finish "
            "cleanly, so it may be partially isolated"
        )
    else:
        why = f"it is egress-isolated (since {record.isolated_at})"
    return Diagnostic(
        id="runtime.apply.lab_isolated",
        severity="error",
        message=(
            f"lab {lab!r}: {why}; refusing to {verb}: provisioning needs "
            "internet access and a re-render must not silently reopen egress"
        ),
        source=SourceLocation(path="config"),
        suggestion=(
            f"run `playground connect {lab}` first, then `playground {verb} "
            f"{lab}`, then `playground isolate {lab}` again"
        ),
    )


def _refuse_if_isolated(
    verb: str, resolved: ResolvedLab, state_dir: Path,
) -> list[Diagnostic] | None:
    """Refuse on any record, in either state; an unreadable record fails closed."""
    lab = resolved.lab_name
    try:
        record = read_isolation(state_dir, lab)
    except OSError as exc:
        placeholder = IsolationRecord(
            lab=lab, backend=resolved.backend, isolated_at="unknown",
            lab_cidrs=[], run_id="unknown",
        )
        return [lab_isolated_diagnostic(verb, lab, placeholder, read_error=str(exc))]
    if record is None:
        return None
    return [lab_isolated_diagnostic(verb, lab, record)]


# Steps that mutate enforcement. A failed isolate that never ran one of these
# changed nothing, whatever the exit codes of the other steps:
#   - local-libvirt (local_libvirt/isolation.py): ``attach-filter``
#   - cloud-digitalocean (cloud_digitalocean/isolation.py): ``tofu-apply``
# Preflight, init, plan and plan-time refusals (unexpected_changes, token
# missing, not provisioned, ...) all precede them.
_MUTATING_STEPS = frozenset({"attach-filter", "tofu-apply"})


def _nothing_changed(run: OperationRun) -> bool:
    return not any(step.name in _MUTATING_STEPS for step in run.steps)


def execute_apply(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    ansible_dir: Path,
    config_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    refusal = _refuse_if_isolated("apply", resolved, state_dir)
    if refusal is not None:
        return None, refusal
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
        run, diagnostics = local_vbox.execute_destroy(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )
    elif resolved.backend == DIGITALOCEAN:
        run, diagnostics = cloud_digitalocean.execute_destroy(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
            config_dir=config_dir,
        )
    else:
        # CLI guards unsupported backends before here; libvirt is the default.
        run, diagnostics = local_libvirt.execute_destroy(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
        )
    return run, _after_teardown(resolved, state_dir, run, diagnostics)


def execute_reset(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
    config_dir: Path | None = None,
) -> tuple[OperationRun, list[Diagnostic]]:
    if resolved.backend == VBOX:
        run, diagnostics = local_vbox.execute_reset(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )
    elif resolved.backend == DIGITALOCEAN:
        run, diagnostics = cloud_digitalocean.execute_reset(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
            config_dir=config_dir,
        )
    else:
        run, diagnostics = local_libvirt.execute_reset(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
        )
    return run, _after_teardown(resolved, state_dir, run, diagnostics)


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
        run, diagnostics = cloud_digitalocean.execute_suspend(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
            config_dir=config_dir,
        )
        return run, _after_teardown(resolved, state_dir, run, diagnostics)
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
        refusal = _refuse_if_isolated("resume", resolved, state_dir)
        if refusal is not None:
            return None, refusal
        return cloud_digitalocean.execute_resume(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir,
            ansible_dir=ansible_dir, config_dir=config_dir, bus=bus,
        )
    return None, [verb_not_supported_diagnostic("resume", resolved.backend)]


def _had_record(state_dir: Path, lab: str) -> bool:
    try:
        return read_isolation(state_dir, lab) is not None
    except OSError:
        return True  # unreadable: assume isolated, clean up the filter


def _after_teardown(
    resolved: ResolvedLab,
    state_dir: Path,
    run: OperationRun | None,
    diagnostics: list[Diagnostic],
) -> list[Diagnostic]:
    """Post-teardown bookkeeping shared by destroy / reset / suspend.

    After a non-failed run the resources carrying the isolation are gone, so
    the record is cleared. For local-libvirt the host-level nwfilter is also
    removed best-effort; its warnings are appended, never fatal.
    """
    if run is None or run.status == "failed":
        return diagnostics
    had_record = _had_record(state_dir, resolved.lab_name)
    clear_isolation(state_dir, resolved.lab_name)
    if resolved.backend == LIBVIRT and had_record:
        try:
            diagnostics = [
                *diagnostics,
                *local_libvirt.remove_isolation_filter(resolved.lab_name),
            ]
        except Exception as exc:  # noqa: BLE001 -- best-effort, must not fail teardown
            diagnostics = [
                *diagnostics,
                Diagnostic(
                    id="runtime.destroy.isolation_filter_cleanup_failed",
                    severity="warning",
                    message=(
                        f"could not remove the isolation nwfilter for lab "
                        f"{resolved.lab_name!r}: {exc}"
                    ),
                    source=SourceLocation(path="config"),
                    suggestion=(
                        "inspect with `virsh nwfilter-list` and remove any "
                        f"leftover `playground-isolate-{resolved.lab_name}` "
                        "filter with `virsh nwfilter-undefine`"
                    ),
                ),
            ]
    return diagnostics


def _partial_diagnostic(state_dir: Path, lab: str) -> Diagnostic:
    return Diagnostic(
        id="runtime.isolate.partially_isolated",
        severity="warning",
        message=(
            f"isolate failed; lab {lab!r} may be partially isolated and "
            "is still guarded against apply"
        ),
        source=SourceLocation(path=str(state_dir / "isolation")),
        suggestion=(
            f"run `playground connect {lab}` to undo, or "
            f"`playground isolate {lab}` to retry"
        ),
    )


def execute_isolate(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
    tofu_dir: Path | None = None,
    config_dir: Path | None = None,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    """Cut the lab's egress outside its own networks (post-provision).

    ``tofu_dir`` / ``config_dir`` are accepted for a uniform signature with
    the other dispatch verbs and are unused. The isolation record is written
    (state ``isolating``) BEFORE the backend runs and promoted to
    ``isolated`` on success; a failed run keeps it unless the diagnostics
    prove nothing changed. If the record cannot be written the backend is not
    called. Returns ``(None, [diag])`` for ``local-vbox``
    (``runtime.backend.verb_not_supported``).
    """
    if resolved.backend not in (LIBVIRT, DIGITALOCEAN):
        if resolved.backend == VBOX:
            return None, [verb_not_supported_diagnostic("isolate", resolved.backend)]
        return None, [unsupported_backend_diagnostic(resolved.backend)]

    lab = resolved.lab_name
    cidrs = [n.cidr for n in resolved.networks]
    try:
        prior = read_isolation(state_dir, lab)
    except OSError as exc:
        return None, [Diagnostic(
            id="runtime.isolate.record_write_failed",
            severity="error",
            message=(
                f"could not read the isolation record for lab {lab!r}: {exc}; "
                "not isolating, because the existing record must not be lost"
            ),
            source=SourceLocation(path=str(state_dir / "isolation")),
            suggestion="fix permissions on the state directory and retry",
        )]
    try:
        write_isolation(state_dir, IsolationRecord(
            lab=lab, backend=resolved.backend, isolated_at=now_iso(),
            lab_cidrs=cidrs, run_id="pending", state="isolating",
        ))
    except OSError as exc:
        return None, [Diagnostic(
            id="runtime.isolate.record_write_failed",
            severity="error",
            message=(
                f"could not write the isolation record for lab {lab!r}: {exc}; "
                "not isolating, because an unrecorded isolation would let a "
                "later apply silently reopen egress"
            ),
            source=SourceLocation(path=str(state_dir / "isolation")),
            suggestion="fix permissions on the state directory and retry",
        )]

    if resolved.backend == LIBVIRT:
        run, diagnostics = local_libvirt.execute_isolate(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )
    else:
        run, diagnostics = cloud_digitalocean.execute_isolate(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )

    if run.status == "succeeded":
        try:
            write_isolation(state_dir, IsolationRecord(
                lab=lab, backend=resolved.backend, isolated_at=now_iso(),
                lab_cidrs=cidrs, run_id=run.run_id, state="isolated",
            ))
        except OSError as exc:
            # The "isolating" record is still on disk, so the guard holds.
            diagnostics = [*diagnostics, Diagnostic(
                id="runtime.isolate.record_update_failed",
                severity="warning",
                message=(
                    f"lab {lab!r} is isolated but its record could not be "
                    f"updated to 'isolated': {exc}"
                ),
                source=SourceLocation(path=str(state_dir / "isolation")),
                suggestion="the lab stays guarded; `playground connect` clears it",
            )]
        return run, diagnostics

    changed = not _nothing_changed(run)
    if prior is not None:
        # The previous isolation is still in force: leave the record as it
        # was before this run.
        try:
            write_isolation(state_dir, prior)
        except OSError:
            pass  # the overwriting "isolating" record still guards
        if changed:
            diagnostics = [*diagnostics, _partial_diagnostic(state_dir, lab)]
    elif not changed:
        clear_isolation(state_dir, lab)
    else:
        diagnostics = [*diagnostics, _partial_diagnostic(state_dir, lab)]
    return run, diagnostics


def execute_connect(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
    tofu_dir: Path | None = None,
    config_dir: Path | None = None,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    """Undo :func:`execute_isolate`; clears the record after a succeeded run."""
    if resolved.backend == LIBVIRT:
        run, diagnostics = local_libvirt.execute_connect(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )
    elif resolved.backend == DIGITALOCEAN:
        run, diagnostics = cloud_digitalocean.execute_connect(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )
    elif resolved.backend == VBOX:
        return None, [verb_not_supported_diagnostic("connect", resolved.backend)]
    else:
        return None, [unsupported_backend_diagnostic(resolved.backend)]

    if run.status == "succeeded":
        clear_isolation(state_dir, resolved.lab_name)
    return run, diagnostics


def query_status(
    resolved: ResolvedLab,
    tofu_dir: Path,
    state_dir: Path = Path(".playground"),
    *,
    include_devices: bool = True,
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

    ``include_devices=False`` is the explicit opt-out for a caller that
    never renders ``status.devices`` at all (the TUI's lab-highlight
    panel is the one case today): it skips `_device_statuses` --
    and so the `adb devices -l` + registry read it does -- entirely,
    rather than paying for data nothing on screen shows. Every other
    caller keeps the default and is unaffected.
    """
    if resolved.backend == VBOX:
        status, diagnostics = local_vbox.query_status(resolved)
    elif resolved.backend == DIGITALOCEAN:
        status, diagnostics = cloud_digitalocean.query_status(resolved)
    else:
        status, diagnostics = local_libvirt.query_status(resolved, tofu_dir)

    if include_devices and resolved.devices:
        device_statuses, device_diagnostics = _device_statuses(
            state_dir, resolved.lab_name, resolved.devices
        )
        status = status.model_copy(update={"devices": device_statuses})
        diagnostics = [*diagnostics, *device_diagnostics]
    return status, diagnostics


def _device_statuses(
    state_dir: Path, lab: str, devices: list[ResolvedDevice]
) -> tuple[list[DeviceStatus], list[Diagnostic]]:
    """Read-only ``DeviceStatus`` for every declared slot, plus any
    diagnostic explaining why a slot reads as ``missing``.

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

    `list_attached`'s own diagnostics (e.g. `device.adb_unavailable` for
    a missing or hung `adb`) are severity=error, but propagating that
    unchanged into `query_status`'s return would make `_exit_on_errors`
    hard-fail every caller of `query_status` -- `status`, `exec`, `cp`,
    `app`, `adb`, `capture` -- for a reason that has nothing to do with
    what any of them are actually doing. So they are downgraded to a
    warning and returned rather than dropped: every device then reads
    `state=missing` exactly as it would if genuinely unplugged, but the
    warning names the real cause (no adb / adb timed out) instead of
    leaving the operator to conclude "no devices attached" -- the same
    confusion handoff (c) exists to eliminate on `doctor`, reintroduced
    here if this were silently swallowed instead.
    """
    attached, adb_diagnostics = list_attached()
    diagnostics = [d.model_copy(update={"severity": "warning"}) for d in adb_diagnostics]
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
            # The bound handset is gone. `match_candidates` is PURE (no
            # filesystem, no adb of its own, and it never writes a
            # binding), so asking it whether a DIFFERENT attached handset
            # now satisfies the selector keeps this function read-only
            # while still telling the truth: reporting `missing` alone
            # would render a swapped cable as "not attached" while a
            # matching phone sits on the bus. Only `apply --rebind` acts
            # on this; every reader merely reports it.
            rebind = [c.serial for c in match_candidates(device, attached)]
            statuses.append(
                DeviceStatus(
                    name=device.name,
                    state="missing",
                    serial=binding.serial,
                    rebind_candidates=rebind or None,
                )
            )
            continue
        entry = registry.get(binding.serial)
        facts = entry.facts if entry is not None else None
        statuses.append(
            DeviceStatus(
                name=device.name,
                state=candidate.state,
                serial=binding.serial,
                model=(facts.model if facts is not None else None) or candidate.model,
                manufacturer=facts.manufacturer if facts is not None else None,
                sdk=facts.sdk if facts is not None else None,
                abi=facts.abi if facts is not None else None,
                rooted=facts.rooted if facts is not None else None,
                last_verified=entry.last_verified if entry is not None else None,
            )
        )
    return statuses, diagnostics


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
        return cloud_digitalocean.estimate_cost_for_sizes(
            [plan.vm_sizes[name] for name in plan.vm_names]
        )
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
        Droplet size slug when every VM uses the same one; otherwise a
        per-size count such as ``"s-1vcpu-2gb x5, s-2vcpu-4gb x1"``.
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

    sizes = [plan.vm_sizes[name] for name in plan.vm_names]
    counts = {s: sizes.count(s) for s in dict.fromkeys(sizes)}
    if len(counts) > 1:
        size_summary = ", ".join(f"{s} x{n}" for s, n in counts.items())
    else:
        size_summary = next(iter(counts), plan.size)

    return {
        "region": plan.region,
        "size": size_summary,
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
    "execute_connect",
    "execute_destroy",
    "execute_isolate",
    "execute_reset",
    "execute_resume",
    "execute_suspend",
    "is_supported",
    "lab_isolated_diagnostic",
    "plan_provider_summary",
    "query_status",
    "unsupported_backend_diagnostic",
    "verb_not_supported_diagnostic",
]
