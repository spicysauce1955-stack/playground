"""Pure builders for the adb command strings run on a Redroid guest.

Everything here returns a shell string to be handed to `ssh`. Keeping
them pure means every command shape is unit-testable without a device,
and the CLI layer stays a thin argument-parsing shell.

Every builder starts with `adb connect` so a command never depends on an
adb server already running on the guest; `|| true` because reconnecting
an existing device exits non-zero.

Syntax follows docs/research/android-emulation/app-install-launch.md.
"""

from __future__ import annotations

import shlex

ADB_PORT = 5555
"""Port the Redroid container publishes on the VM (ansible/roles/redroid)."""

_SERIAL = f"127.0.0.1:{ADB_PORT}"


def adb_prefix() -> str:
    return f"adb connect {_SERIAL} >/dev/null 2>&1 || true; adb -s {_SERIAL}"


def adb_shell(command: str) -> str:
    return f"{adb_prefix()} shell {command}"


def install_cmd(remote_apk: str) -> str:
    return f"{adb_prefix()} install -r {shlex.quote(remote_apk)}"


def install_multiple_cmd(remote_apks: list[str]) -> str:
    quoted = " ".join(shlex.quote(p) for p in remote_apks)
    return f"{adb_prefix()} install-multiple -r {quoted}"


def uninstall_cmd(package: str) -> str:
    return f"{adb_prefix()} uninstall {shlex.quote(package)}"


def launch_cmd(package: str, *, activity: str | None, url: str | None) -> str:
    """Three launch forms, resolved in priority order.

    A deep link wins over an explicit activity, which wins over monkey.
    monkey needs no activity name, which is why it is the fallback when
    the launcher activity is unknown.
    """
    if url is not None:
        return adb_shell(
            "am start -a android.intent.action.VIEW -d " + shlex.quote(url)
        )
    if activity is not None:
        return adb_shell(f"am start -n {shlex.quote(package + '/' + activity)}")
    # Launch discovery, per docs/research/android-emulation/app-install-launch.md.
    #
    # monkey is NOT a reliable launcher, verified live on Redroid 11
    # (DigitalOcean, 2026-08-31): it returned 251 on a successful launch,
    # and later stopped starting the app at all while reporting the same
    # output. `cmd package resolve-activity --brief` yields the real
    # launcher component and `am start -n` on it returns an honest 0/non-0.
    #
    # monkey survives only as a last resort for packages whose launcher
    # activity does not resolve, and there its exit status is discarded in
    # favour of `pidof` -- "did it launch" is a question about the process.
    #
    # The device-side script is quoted as ONE argument so `$(...)`, `;` and
    # the redirections run on the DEVICE, not on the guest invoking adb.
    quoted = shlex.quote(package)
    script = (
        f"ACT=$(cmd package resolve-activity --brief {quoted} | tail -n 1); "
        'if [ -n "$ACT" ] && [ "${ACT#*/}" != "$ACT" ]; then '
        'am start -n "$ACT"; '
        f"else monkey -p {quoted} -c android.intent.category.LAUNCHER 1 "
        f">/dev/null 2>&1; pidof {quoted} >/dev/null 2>&1; fi"
    )
    return f"{adb_prefix()} shell {shlex.quote(script)}"


def stop_cmd(package: str) -> str:
    return adb_shell(f"am force-stop {shlex.quote(package)}")


def clear_cmd(package: str) -> str:
    return adb_shell(f"pm clear {shlex.quote(package)}")


def list_packages_cmd(*, pattern: str | None, third_party: bool) -> str:
    parts = ["pm list packages"]
    if third_party:
        parts.append("-3")
    if pattern is not None:
        parts.append(shlex.quote(pattern))
    return adb_shell(" ".join(parts))


def screenshot_cmd(remote_path: str) -> str:
    return adb_shell(f"screencap -p {shlex.quote(remote_path)}")


def ui_dump_cmd(remote_path: str) -> str:
    return adb_shell(f"uiautomator dump {shlex.quote(remote_path)}")


def logcat_cmd(*, follow: bool, lines: int | None) -> str:
    parts = [adb_prefix(), "logcat"]
    if not follow:
        parts.append("-d")
    if lines is not None:
        parts.append(f"-t {int(lines)}")
    return " ".join(parts)


_INPUT_SUBCOMMANDS = {"key": "keyevent"}
"""adb's `input` subcommand names do not always match the short kind name
a caller passes in (`key` -> `keyevent`); everything else passes through
unchanged (`text`, `tap`, `swipe`, ...)."""


def input_cmd(kind: str, args: list[str]) -> str:
    subcommand = _INPUT_SUBCOMMANDS.get(kind, kind)
    quoted = " ".join(shlex.quote(a) for a in args)
    return adb_shell(f"input {subcommand} {quoted}")


def wait_booted_cmd(timeout_seconds: int) -> str:
    """Block until Android reports a completed boot.

    Immediately after apply the container is up but Android is still
    booting, so an install would fail on a device that is merely slow.
    """
    return (
        f"{adb_prefix()} wait-for-device >/dev/null 2>&1; "
        f"for i in $(seq 1 {int(timeout_seconds)}); do "
        f"[ \"$({adb_prefix()} shell getprop sys.boot_completed 2>/dev/null "
        "| tr -d '\\r')\" = 1 ] && exit 0; sleep 1; done; exit 1"
    )
