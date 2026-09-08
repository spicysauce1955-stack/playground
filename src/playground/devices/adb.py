"""Talk to the local adb server about attached handsets.

Everything here runs `adb` on the CONTROLLER -- the operator's own
machine. Nothing in this module touches ssh or a lab guest: a Redroid
device running inside a VM is reached through
`playground.android.transport.GuestAdb` instead. That is a different
adb server on a different host, and the two are never mixed here.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from playground.models.base import StrictModel
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.status import DeviceState

__all__ = [
    "AttachedDevice",
    "DeviceFacts",
    "list_attached",
    "parse_devices_l",
    "probe_facts",
]

_ADB_TIMEOUT = 20.0
"""Per-invocation timeout. A hung adb server (stuck USB device, dead
`adb server`) must not hang the caller forever."""

_STATE_MAP: dict[str, DeviceState] = {
    "device": "connected",
    "unauthorized": "unauthorized",
    "offline": "offline",
}
"""adb's own vocabulary on the left, this codebase's `DeviceState` on the
right. adb's word `device` means state `connected`."""

_KV = re.compile(r"(\w+):(\S+)")


@dataclass(frozen=True)
class AttachedDevice:
    """One line of `adb devices -l`.

    No `manufacturer` field: that key never appears in this listing (only
    `model:` does), so it would be permanently `None`. `DeviceFacts` --
    populated from `getprop`, which can actually answer -- carries
    manufacturer instead.
    """

    serial: str
    state: DeviceState
    model: str | None = None


class DeviceFacts(StrictModel):
    """A handset's own description of itself, read via `getprop` (and a
    root probe). Distinct from `AttachedDevice`: that type is what the
    listing gives you, this is what a probe gives you."""

    serial: str
    model: str | None = None
    manufacturer: str | None = None
    sdk: int | None = None
    abi: str | None = None
    rooted: bool = False
    fingerprint: str | None = None


def parse_devices_l(stdout: str) -> list[AttachedDevice]:
    """Parse `adb devices -l` output into `AttachedDevice`s.

    Survives the `List of devices attached` header, blank lines, and
    trailing whitespace. An `unauthorized` or `offline` line carries no
    `model:` key, so those devices come back with `model=None` -- they
    are still reported, never dropped, because "attached but untrusted"
    is the state an operator most needs to see. Underscores in a
    `model:` value become spaces (`model:Pixel_7` -> `Pixel 7`).
    """
    devices: list[AttachedDevice] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split(maxsplit=1)
        serial = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        state_word = rest.split(maxsplit=1)[0] if rest else ""
        state = _STATE_MAP.get(state_word)
        if state is None:
            continue
        fields = dict(_KV.findall(rest))
        model = fields.get("model")
        devices.append(
            AttachedDevice(
                serial=serial,
                state=state,
                model=model.replace("_", " ") if model else None,
            )
        )
    return devices


def _adb(args: list[str], *, timeout: float = _ADB_TIMEOUT) -> tuple[int, str, str]:
    """Run `adb <args>` on the controller. Never raises: `adb` missing from
    PATH or hanging turns into a synthetic non-zero exit + stderr message
    rather than a traceback, so callers can turn it into a `Diagnostic`."""
    try:
        done = subprocess.run(  # noqa: S603
            ["adb", *args], capture_output=True, text=True, check=False, timeout=timeout
        )
    except FileNotFoundError:
        return 127, "", "adb not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"adb timed out after {timeout}s"
    return done.returncode, done.stdout, done.stderr


def list_attached() -> tuple[list[AttachedDevice], list[Diagnostic]]:
    """List handsets attached to the controller. Never raises: a missing or
    hung `adb` comes back as an empty device list plus a `Diagnostic`
    explaining why, not an exception."""
    code, out, err = _adb(["devices", "-l"])
    if code != 0:
        return [], [
            Diagnostic(
                id="device.adb_unavailable",
                severity="error",
                message=f"`adb devices -l` failed: {err.strip() or code}",
                source=SourceLocation(path="adb"),
                suggestion="install android-platform-tools and re-run `playground doctor`",
            )
        ]
    return parse_devices_l(out), []


def _getprop(serial: str, prop: str) -> str | None:
    code, out, _ = _adb(["-s", serial, "shell", "getprop", prop])
    value = out.strip()
    return value if code == 0 and value else None


def probe_facts(serial: str) -> DeviceFacts:
    """Read a handset's own description of itself via `getprop`.

    Root is probed, not declared: `su -c id` printing `uid=0` is the only
    answer that means anything -- a device with no `su` at all simply
    fails the command, and that failure (any non-zero exit, or a missing
    `adb`) reads as `rooted=False`, never as an exception.
    """
    sdk_raw = _getprop(serial, "ro.build.version.sdk")
    root_code, root_out, _ = _adb(["-s", serial, "shell", "su", "-c", "id"])
    return DeviceFacts(
        serial=serial,
        model=_getprop(serial, "ro.product.model"),
        manufacturer=_getprop(serial, "ro.product.manufacturer"),
        sdk=int(sdk_raw) if sdk_raw and sdk_raw.isdigit() else None,
        abi=_getprop(serial, "ro.product.cpu.abi"),
        rooted=root_code == 0 and "uid=0" in root_out,
        fingerprint=_getprop(serial, "ro.build.fingerprint"),
    )
