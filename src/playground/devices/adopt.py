"""Adopt and release physical device slots.

Adoption is STRICTLY ADDITIVE. It resolves a selector, asserts the
handset is trusted, probes what it is, checks the lab's assertions and
records the binding. It installs nothing the lab did not declare, changes
no setting, and never wipes anything.

Release forgets the binding. It uninstalls only packages playground
itself installed, and never factory-resets or reverts a setting
playground did not set -- but nothing in the tasks this module consumes
(adb, binding, registry, notes) tracks which packages playground
installed, so today `release` does the one thing it can do honestly:
forget the binding. A later task that adds install bookkeeping is where
the uninstall half belongs.
"""

from __future__ import annotations

from pathlib import Path

from playground.devices.adb import AttachedDevice, DeviceFacts, probe_facts
from playground.devices.binding import clear_binding, resolve_binding
from playground.devices.notes import read_note
from playground.devices.registry import record_seen
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedDevice
from playground.models.status import DeviceStatus

__all__ = ["adopt", "check_requirements", "release"]

_ABSENCE_IDS = {"config.device.no_match", "device.not_connected"}
"""The two `resolve_binding` diagnostic ids that mean "nothing attached
satisfies this slot", as opposed to `config.device.ambiguous_match` /
`device.rebind_required`, which mean a handset IS attached but resolution
still can't proceed. Only the first kind downgrades to a warning for an
optional device -- an unplugged optional phone is fine; two phones both
matching one slot's selector is a real problem whether or not the slot
is required."""


def check_requirements(
    device: ResolvedDevice, facts: DeviceFacts, *, note: str | None = None
) -> list[Diagnostic]:
    """Verify the lab's `require` assertions against the live handset.

    On failure the operator's own note is appended to the message. That is
    the whole reason notes exist: "root broken since the September OTA" is
    exactly the context otherwise gone hunting for.
    """
    require = device.require
    if require is None:
        return []
    unmet: list[str] = []
    if require.root is True and not facts.rooted:
        unmet.append("root: true (handset is not rooted)")
    if require.min_sdk is not None and (facts.sdk is None or facts.sdk < require.min_sdk):
        unmet.append(f"min_sdk: {require.min_sdk} (handset reports {facts.sdk})")
    if not unmet:
        return []
    message = f"device {device.name!r} does not satisfy: {'; '.join(unmet)}"
    if note:
        message = f"{message}\nnote: {note.strip()}"
    return [
        Diagnostic(
            id="device.require_unmet",
            severity="error",
            message=message,
            source=SourceLocation(path="device"),
            key_path="spec.devices[].require",
            suggestion="relax spec.devices[].require, or attach a handset that satisfies it",
        )
    ]


def _source(lab: str) -> SourceLocation:
    return SourceLocation(path=f"config/labs/{lab}.yaml")


def _lookup_note(
    config_dir: Path, state_dir: Path, device: ResolvedDevice, serial: str
) -> str | None:
    """The operator's note for this handset, shared surface first.

    Retrieval order: SHARED (keyed by `device.profile`, falling back to
    `device.name` for an inline slot with no profile) first, then PRIVATE
    (keyed by the bound serial). The profile name matters: shared notes
    live at `config/devices/<profile-name>.md`, so a slot named `pixel7`
    that references profile `my-pixel` must resolve to `my-pixel.md`, not
    `pixel7.md` -- using the slot's own name unconditionally would silently
    miss the note in the common case where several labs share one
    handset's profile. Both scopes raise a plain `ValueError` when their
    required key is missing; a missing note is normal, not an error, so
    that is caught rather than left to escape as a traceback.
    """
    shared_name = device.profile or device.name
    try:
        shared = read_note(
            "shared", config_dir=config_dir, state_dir=state_dir, name=shared_name, serial=None
        )
    except ValueError:
        shared = None
    if shared is not None:
        return shared
    try:
        return read_note(
            "private", config_dir=config_dir, state_dir=state_dir, name=None, serial=serial
        )
    except ValueError:
        return None


def _unauthorized_diagnostic(device: ResolvedDevice, lab: str, serial: str) -> Diagnostic:
    return Diagnostic(
        id="device.unauthorized",
        severity="error",
        message=(
            f"device {device.name!r} (serial {serial!r}) is attached but its "
            "RSA trust prompt has not been accepted"
        ),
        source=_source(lab),
        key_path="spec.devices",
        suggestion="tap Allow on the handset's RSA prompt, then re-run",
    )


def adopt(
    state_dir: Path,
    config_dir: Path,
    lab: str,
    resolved: list[ResolvedDevice],
    attached: list[AttachedDevice],
    *,
    rebind: bool = False,
) -> tuple[list[DeviceStatus], list[Diagnostic]]:
    """Bind each device slot to an attached handset and check it out.

    Strictly additive: nothing here installs anything the lab did not
    declare, changes a setting, or wipes anything. Per slot:

    - `resolve_binding` picks (and persists) the serial, or explains why
      it cannot.
    - Unauthorized: `device.unauthorized`, never reported as generic
      unreachability -- it is the most common real-world failure.
    - Absent (no candidate at all) and `required: true`: error. Absent
      and `required: false`: warning, `state="missing"` -- an unplugged
      optional handset must not fail an apply. A genuine conflict
      (ambiguous match, rebind required) stays an error either way.
    - Otherwise: `probe_facts`, `record_seen` (propagating a
      `device.serial_collision` rather than swallowing it -- on
      collision the registry's existing, trusted facts are used instead
      of the freshly probed ones), `check_requirements` with the
      operator's own note, and a `DeviceStatus`.
    """
    statuses: list[DeviceStatus] = []
    diagnostics: list[Diagnostic] = []

    for device in resolved:
        serial, resolve_diagnostics = resolve_binding(
            state_dir, lab, device, attached, rebind=rebind
        )

        if serial is None:
            is_absence = bool(resolve_diagnostics) and resolve_diagnostics[0].id in _ABSENCE_IDS
            if is_absence and not device.required:
                resolve_diagnostics = [
                    d.model_copy(update={"severity": "warning"}) for d in resolve_diagnostics
                ]
            diagnostics.extend(resolve_diagnostics)
            statuses.append(DeviceStatus(name=device.name, state="missing"))
            continue

        matched = next((a for a in attached if a.serial == serial), None)
        # resolve_binding only ever returns a serial that is a member of
        # `attached` (either the existing binding confirmed still
        # attached, or a freshly matched candidate), so `matched` is
        # never None here; the `next(..., None)` is defensive, not load
        # bearing.
        if matched is not None and matched.state == "unauthorized":
            diagnostics.append(_unauthorized_diagnostic(device, lab, serial))
            statuses.append(DeviceStatus(name=device.name, state="unauthorized", serial=serial))
            continue

        facts = probe_facts(serial)
        entry, seen_diagnostics = record_seen(state_dir, facts)
        diagnostics.extend(seen_diagnostics)

        note = _lookup_note(config_dir, state_dir, device, entry.facts.serial)
        diagnostics.extend(check_requirements(device, entry.facts, note=note))

        state = matched.state if matched is not None else "connected"
        statuses.append(
            DeviceStatus(
                name=device.name,
                state=state,
                serial=entry.facts.serial,
                model=entry.facts.model,
                manufacturer=entry.facts.manufacturer,
                sdk=entry.facts.sdk,
                abi=entry.facts.abi,
                rooted=entry.facts.rooted,
                last_verified=entry.last_verified,
            )
        )

    return statuses, diagnostics


def release(state_dir: Path, lab: str, resolved: list[ResolvedDevice]) -> list[Diagnostic]:
    """Forget every slot's binding. Never a factory reset, never a revert
    of a setting playground did not set -- see the module docstring for
    why this does not (yet) uninstall anything."""
    for device in resolved:
        clear_binding(state_dir, lab, device.name)
    return []
