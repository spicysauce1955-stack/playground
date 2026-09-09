"""Where an adb invocation is rendered for one transport or another.

`android/commands.py` builds WHAT to run as an argument list; this module
decides HOW it reaches a device. Two transports:

`GuestAdb` -- today's path. The invocation is rendered into a shell
string, prefixed with `adb connect`, and handed to ssh. Because a shell
is involved, this renderer applies the outer `shlex.quote` that used to
live in the builders. Output is byte-identical to the pre-refactor
strings; `tests/unit/android/test_commands_characterization.py` is the
proof -- every expected string literal in that file is unchanged from
before this refactor, even though the calls producing them now route
through `render_guest_command`.

`LocalAdb` -- a handset on the operator's own USB bus. The invocation
becomes an argv executed with no shell anywhere in the chain, so the
guest/device double-parse hazard documented in `commands.adb_shell`
simply does not exist on this path. That is why the builders must return
UNQUOTED outer arguments: a `shlex.quote` baked into a builder would ship
literal quote characters to adb here.

Inner quoting is a different thing and stays where it is. A device shell
script (`am start …`, `pm clear …`) is one argument to `adb shell`, and
the quoting of package names and free text INSIDE that script defends
against the DEVICE's own `sh -c` on both transports.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

ADB_PORT = 5555
"""Port the Redroid container publishes on the VM (ansible/roles/redroid)."""

GUEST_SERIAL = f"127.0.0.1:{ADB_PORT}"


@dataclass(frozen=True)
class AdbInvocation:
    """The adb arguments that follow `-s <serial>`."""

    args: tuple[str, ...]


@dataclass(frozen=True)
class GuestAdb:
    """adb runs inside a lab guest, reached over ssh."""

    ssh_host: str
    ssh_port: int
    ssh_user: str


@dataclass(frozen=True)
class LocalAdb:
    """adb runs on the controller against a USB-attached handset."""

    serial: str


Transport = GuestAdb | LocalAdb


@dataclass(frozen=True)
class AndroidTarget:
    """One reachable Android device, however it is reached."""

    name: str
    transport: Transport


def guest_prefix() -> str:
    """`adb connect` then `adb -s`, so no command depends on a running server.

    `|| true` because reconnecting an already-connected device exits
    non-zero.
    """
    return (
        f"adb connect {GUEST_SERIAL} >/dev/null 2>&1 || true; "
        f"adb -s {GUEST_SERIAL}"
    )


def render_guest_command(inv: AdbInvocation) -> str:
    """Render for a guest login shell. Byte-identical to the legacy strings."""
    return " ".join([guest_prefix(), *(shlex.quote(a) for a in inv.args)])


def local_argv(transport: LocalAdb, inv: AdbInvocation) -> list[str]:
    """Render for direct execution. No shell, therefore no quoting."""
    return ["adb", "-s", transport.serial, *inv.args]


def require_guest(target: AndroidTarget) -> GuestAdb:
    """Narrow ``target.transport`` to `GuestAdb`, for callers that reach a
    device by ssh-ing into a guest VM (scp, a hand-built guest-shell
    string, ...) rather than through `AdbInvocation`/`render_guest_command`.

    Raises `TypeError` rather than returning a `Diagnostic`, unlike this
    codebase's usual `_fail(Diagnostic(...))` CLI-error style: a
    `LocalAdb` target reaching one of these call sites is not an operator
    mistake to explain -- there is no flag an operator could have passed
    differently -- it is a caller that built a guest-only command for a
    target it never checked was a guest. That is a programming error, and
    the asymmetry with the surrounding Diagnostic-based style is
    deliberate.
    """
    transport = target.transport
    if not isinstance(transport, GuestAdb):
        raise TypeError(
            f"{target.name!r} has no guest ssh transport (got {transport!r})"
        )
    return transport


__all__ = [
    "ADB_PORT",
    "AdbInvocation",
    "AndroidTarget",
    "GUEST_SERIAL",
    "GuestAdb",
    "LocalAdb",
    "Transport",
    "guest_prefix",
    "local_argv",
    "render_guest_command",
    "require_guest",
]
