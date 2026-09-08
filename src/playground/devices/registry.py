"""Every handset this machine has ever seen, keyed by serial.

Machine-owned and never hand-edited: this is the *facts* half of the
intent/facts split, the mirror of `config/devices/*.yaml`. It is what
lets `device list` show a phone that is currently unplugged, and what
makes a note survive a disconnect.

Layout follows `capture/state.py`: `<state_dir>/state/devices/`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from playground.devices.adb import DeviceFacts
from playground.models.base import StrictModel
from playground.models.diagnostic import Diagnostic, SourceLocation

__all__ = [
    "RegistryEntry",
    "devices_dir",
    "mark_ignored",
    "read_registry",
    "record_seen",
    "registry_path",
]


class RegistryEntry(StrictModel):
    facts: DeviceFacts
    first_seen: str
    last_seen: str
    last_verified: str
    ignored: bool = False
    """Set when the operator answered "no" to the discovery prompt, so the
    same device is not offered again on every poll."""


def devices_dir(state_dir: Path) -> Path:
    return state_dir / "state" / "devices"


def registry_path(state_dir: Path) -> Path:
    return devices_dir(state_dir) / "registry.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read_registry(state_dir: Path) -> dict[str, RegistryEntry]:
    """The registry, or an empty mapping when there is none to read.

    A corrupt or truncated file reads as empty rather than raising, the
    same tolerance `capture.state.read_session` applies: a damaged cache
    of observations must never block a command -- the operator's next
    probe simply rewrites it.
    """
    path = registry_path(state_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, NOT an OSError, so it needs
        # naming explicitly -- a file truncated mid multi-byte sequence is
        # exactly what a non-atomic write_text can produce.
        return {}
    try:
        parsed = json.loads(raw)
        devices = parsed["devices"]
        return {k: RegistryEntry.model_validate(v) for k, v in devices.items()}
    except (json.JSONDecodeError, ValidationError, KeyError, TypeError, AttributeError):
        return {}


def _write(state_dir: Path, entries: dict[str, RegistryEntry]) -> None:
    path = registry_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"devices": {k: v.model_dump() for k, v in entries.items()}}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def record_seen(state_dir: Path, facts: DeviceFacts) -> tuple[RegistryEntry, list[Diagnostic]]:
    """Record a sighting, refusing to merge two handsets that share a serial.

    Cheap and cloned handsets ship duplicate USB serials, and `adb` itself
    cannot tell two such devices apart. When the serial is already
    registered against a DIFFERENT fingerprint, the existing record is
    returned untouched alongside a `device.serial_collision` diagnostic --
    silently merging two phones' probe history and notes into one record
    is worse than failing loudly. The guard only fires when BOTH
    fingerprints are known: a `None` fingerprint on either side must not
    trigger a false collision.
    """
    entries = read_registry(state_dir)
    now = _now()
    existing = entries.get(facts.serial)

    if (
        existing is not None
        and existing.facts.fingerprint is not None
        and facts.fingerprint is not None
        and existing.facts.fingerprint != facts.fingerprint
    ):
        return existing, [
            Diagnostic(
                id="device.serial_collision",
                severity="error",
                message=(
                    f"serial {facts.serial!r} is already registered to a "
                    f"different handset (fingerprint "
                    f"{existing.facts.fingerprint!r} vs {facts.fingerprint!r})"
                ),
                source=SourceLocation(path=str(registry_path(state_dir))),
                suggestion=(
                    "duplicate USB serials are common on cloned devices; "
                    "attach only one of them, or clear its registry entry"
                ),
            )
        ]

    entry = RegistryEntry(
        facts=facts,
        first_seen=existing.first_seen if existing else now,
        last_seen=now,
        last_verified=now,
        ignored=existing.ignored if existing else False,
    )
    entries[facts.serial] = entry
    _write(state_dir, entries)
    return entry, []


def mark_ignored(state_dir: Path, serial: str) -> None:
    """Record that the operator declined this device, so the discovery
    prompt does not offer it again on every poll.

    Called by ``devices.discovery.offer_unknown`` for every device the
    operator declines (and for every unknown device under ``silent``). A
    serial not yet in the registry is a no-op: there is nothing to mark.
    """
    entries = read_registry(state_dir)
    entry = entries.get(serial)
    if entry is None:
        return
    entries[serial] = entry.model_copy(update={"ignored": True})
    _write(state_dir, entries)
