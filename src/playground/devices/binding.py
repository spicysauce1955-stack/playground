"""Which physical handset fills a lab's device slot.

Two separate questions, and the split matters:

*Matching* asks which attached devices satisfy the slot's selector. It is
pure and testable with no filesystem.

*Binding* is the persisted answer -- slot name to serial, under
`<state_dir>/state/devices/<lab>/<slot>.json`, the same per-lab directory
shape `capture/state.py` uses. It exists so a serial never has to be
written into a committed lab file, exactly as a VM's IP never is: a
teammate with a different handset of the same model uses the same YAML.

Rebinding is never automatic. A bound slot whose serial has been replaced
by a different matching handset is an error, not a silent retarget --
installing an APK onto the wrong phone because a cable was swapped is
exactly the failure this module exists to prevent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from playground.devices.adb import AttachedDevice
from playground.models.base import StrictModel
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.kinds import DeviceMatch
from playground.models.resolved import ResolvedDevice

__all__ = [
    "Binding",
    "binding_path",
    "clear_binding",
    "device_matches",
    "lab_devices_dir",
    "match_candidates",
    "read_binding",
    "resolve_binding",
    "write_binding",
]


class Binding(StrictModel):
    slot: str
    serial: str
    bound_at: str


def lab_devices_dir(state_dir: Path, lab: str) -> Path:
    """The per-lab directory holding one binding per slot.

    `binding_path` derives from here rather than spelling the path out a
    second time, so anything that ever needs to sweep a lab's bindings
    wholesale has one place to point at.
    """
    return state_dir / "state" / "devices" / lab


def binding_path(state_dir: Path, lab: str, slot: str) -> Path:
    return lab_devices_dir(state_dir, lab) / f"{slot}.json"


def read_binding(state_dir: Path, lab: str, slot: str) -> Binding | None:
    """The recorded binding, or None when there is none to read.

    A corrupt or truncated file reads as "not bound" rather than raising
    -- the same tolerance `devices.registry.read_registry` and
    `capture.state.read_session` apply.
    """
    path = binding_path(state_dir, lab, slot)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, NOT an OSError, so it needs
        # naming explicitly -- a file truncated mid multi-byte sequence is
        # exactly what a non-atomic write_text can produce.
        return None
    try:
        return Binding.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError):
        return None


def write_binding(state_dir: Path, lab: str, slot: str, serial: str) -> Binding:
    binding = Binding(
        slot=slot,
        serial=serial,
        bound_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    path = binding_path(state_dir, lab, slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(binding.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return binding


def clear_binding(state_dir: Path, lab: str, slot: str) -> None:
    """Remove the record. Absent is success -- the goal is "be gone"."""
    binding_path(state_dir, lab, slot).unlink(missing_ok=True)


def device_matches(match: DeviceMatch | None, candidate: AttachedDevice) -> bool:
    """Whether `candidate` satisfies every field a selector actually sets.

    An absent selector (`match is None`) matches everything -- that is what
    lets a single-handset lab need no serial in its YAML at all. Only
    `serial` and `model` are checked: `DeviceMatch` carries no
    `manufacturer` field (`adb devices -l` never reports one), and neither
    does `AttachedDevice`.

    The single shared predicate behind `match_candidates` below AND
    `playground.cli.device_commands`' "which profile does this listed
    device belong to" lookup -- two matchers that disagree about what
    "matches" means is exactly the kind of silent inconsistency this
    module exists to prevent for bindings, so it must not be re-implemented
    a second time for notes either.
    """
    if match is None:
        return True
    if match.serial is not None and candidate.serial != match.serial:
        return False
    if match.model is not None and candidate.model != match.model:
        return False
    return True


def match_candidates(
    device: ResolvedDevice, attached: list[AttachedDevice]
) -> list[AttachedDevice]:
    """Attached devices satisfying every field the selector actually sets."""
    return [candidate for candidate in attached if device_matches(device.match, candidate)]


def _source(lab: str) -> SourceLocation:
    return SourceLocation(path=f"config/labs/{lab}.yaml")


def resolve_binding(
    state_dir: Path,
    lab: str,
    device: ResolvedDevice,
    attached: list[AttachedDevice],
    *,
    rebind: bool,
) -> tuple[str | None, list[Diagnostic]]:
    """The serial this slot should act on, or the reason it cannot.

    Precedence, in order:

    1. An existing binding whose serial is still attached -- use it, no
       diagnostics, regardless of whether other devices also match.
    2. An existing binding whose serial is gone, and nothing else matches
       the selector -- `device.not_connected`.
    3. An existing binding whose serial is gone, but a DIFFERENT attached
       handset matches the selector -- `device.rebind_required` unless
       `rebind=True`, in which case the new serial is bound and persisted.
       This never happens silently.
    4. No existing binding, exactly one candidate -- bind and persist it.
    5. No existing binding, several candidates -- `config.device.ambiguous_match`,
       listing every candidate serial so the operator can narrow the
       selector.
    6. No existing binding, no candidates -- `config.device.no_match`.
    """
    candidates = match_candidates(device, attached)
    existing = read_binding(state_dir, lab, device.name)

    if existing is not None:
        if any(c.serial == existing.serial for c in candidates):
            return existing.serial, []
        if not candidates:
            return None, [
                Diagnostic(
                    id="device.not_connected",
                    severity="error",
                    message=(
                        f"device {device.name!r} is bound to serial "
                        f"{existing.serial!r}, which is not attached"
                    ),
                    source=_source(lab),
                    key_path="spec.devices",
                    suggestion="plug the handset in, or pass --rebind to bind another",
                )
            ]
        if not rebind:
            found = ", ".join(c.serial for c in candidates)
            return None, [
                Diagnostic(
                    id="device.rebind_required",
                    severity="error",
                    message=(
                        f"device {device.name!r} is bound to serial "
                        f"{existing.serial!r}, but a different handset "
                        f"matching its selector is attached ({found})"
                    ),
                    source=_source(lab),
                    key_path="spec.devices",
                    suggestion=(
                        "pass --rebind to point this slot at the attached "
                        "handset; playground never retargets a slot silently"
                    ),
                )
            ]
        # rebind=True: fall through to bind the (single, matching) candidate.

    if not candidates:
        return None, [
            Diagnostic(
                id="config.device.no_match",
                severity="error",
                message=f"no attached handset matches device {device.name!r}",
                source=_source(lab),
                key_path="spec.devices",
                suggestion="run `playground device list` to see what is attached",
            )
        ]
    if len(candidates) > 1:
        found = ", ".join(c.serial for c in candidates)
        return None, [
            Diagnostic(
                id="config.device.ambiguous_match",
                severity="error",
                message=(
                    f"device {device.name!r} matches {len(candidates)} attached "
                    f"handsets: {found}"
                ),
                source=_source(lab),
                key_path="spec.devices",
                suggestion="narrow spec.devices[].match, e.g. with a serial",
            )
        ]

    write_binding(state_dir, lab, device.name, candidates[0].serial)
    return candidates[0].serial, []
