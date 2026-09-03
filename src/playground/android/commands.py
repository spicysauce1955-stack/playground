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
    """Run ``command`` on the device shell, immune to the guest/adb hop.

    `adb shell` joins whatever argv it is given into a single string with
    plain spaces and hands THAT to /system/bin/sh -c on the device for a
    second, independent parse. If the guest's login shell (which runs the
    string this function returns, via ssh) sees ``command`` as more than
    one token, that structure is destroyed by the join: a multi-word
    argument truncates, and metacharacters meant to be literal (a package
    name, free-form input text, ...) get reinterpreted by the device's
    shell -- both a correctness bug and a device-side injection bug.

    Quoting ``command`` as ONE token here defeats the guest-side split so
    exactly one string reaches the device. Callers are still responsible
    for `shlex.quote`-ing any individual dynamic values (package names,
    free text, ...) inside ``command`` -- that quoting is what makes the
    DEVICE's own sh -c treat them as a single argument once this outer
    layer is stripped away by the two hops in between.
    """
    return f"{adb_prefix()} shell {shlex.quote(command)}"


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
        return adb_shell("am start -a android.intent.action.VIEW -d " + shlex.quote(url))
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
    # resolve-activity can return a component that belongs to a DIFFERENT
    # package (e.g. `android/...ResolverActivity` or a Settings fallback)
    # when `package` has no launcher activity of its own -- both contain a
    # `/`, so checking for that alone is not enough; the component's own
    # package prefix must match `package` too, or fall through to monkey.
    #
    # monkey only dispatches an intent; process creation is async, so a
    # single `pidof` right after is a race that reports a false failure on
    # a slow cold start. Poll for a bounded number of attempts instead.
    #
    # adb_shell quotes this whole script as ONE argument, so `$(...)`,
    # `;` and the redirections below run on the DEVICE, never the guest.
    quoted = shlex.quote(package)
    script = (
        f"ACT=$(cmd package resolve-activity --brief {quoted} | tail -n 1); "
        'if [ -n "$ACT" ] && [ "${ACT#*/}" != "$ACT" ] '
        f'&& [ "${{ACT%%/*}}" = {quoted} ]; then '
        'am start -n "$ACT"; '
        f"else monkey -p {quoted} -c android.intent.category.LAUNCHER 1 "
        ">/dev/null 2>&1; "
        'n=0; while [ "$n" -lt 10 ]; do '
        f"pidof {quoted} >/dev/null 2>&1 && exit 0; "
        "n=$((n + 1)); sleep 1; done; exit 1; fi"
    )
    return adb_shell(script)


def stop_cmd(package: str) -> str:
    """Force-stop ``package``, verifying it actually happened.

    `am force-stop` exits 0 unconditionally, including for a package that
    was never installed, so its exit status alone cannot signal failure.
    Guard on both ends: `pm path` proves the package exists before we
    bother stopping it (this is what catches `playground app stop
    com.typo`), and `pidof` proves no process survived the stop.
    """
    quoted = shlex.quote(package)
    script = (
        f"pm path {quoted} >/dev/null 2>&1 || exit 1; "
        f"am force-stop {quoted} >/dev/null 2>&1; "
        f"pidof {quoted} >/dev/null 2>&1 && exit 1; exit 0"
    )
    return adb_shell(script)


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


def _logcat_filterspec(*, tag: str | None, priority: str | None) -> list[str]:
    """Build the trailing filterspec tokens shared by both logcat forms.

    `-s TAG:LEVEL` sets the default filter to silent (`-s`) and then adds
    one filterspec for `TAG` at `LEVEL` -- exactly the "show only this
    tag" idiom `--tag` promises. Priority defaults to V(erbose) so a bare
    `--tag` shows everything for that tag, matching the CLI help text
    ("`logcat -s TAG:V` style"). Without a tag, `*:LEVEL` sets every tag's
    minimum priority without silencing anything, which is the right
    default for a bare `--priority`.

    Uppercased here (not just at the CLI layer) because this builder is
    unit-tested directly and must produce the same shape regardless of
    the case the caller passed in.
    """
    if tag is not None:
        level = (priority or "V").upper()
        return ["-s", shlex.quote(f"{tag}:{level}")]
    if priority is not None:
        # Quote the `*`: for the plain `adb logcat` form this string is
        # interpreted by the GUEST shell, which would glob it against the
        # ssh working directory. A stray file matching `*:W` there would
        # silently rewrite the filter.
        return [shlex.quote(f"*:{priority.upper()}")]
    return []


def _logcat_package_cmd(
    package: str, *, follow: bool, lines: int | None, tag: str | None, priority: str | None
) -> str:
    """Filter logcat to one package by resolving its pid ON THE DEVICE.

    `adb logcat` (the plain protocol form `logcat_cmd` otherwise uses)
    takes its arguments directly -- it never reaches a device shell, so
    it has no way to run `pidof` for us. Package filtering therefore has
    to go through `adb_shell`'s single-quoted device script instead, the
    same double-hop-safe path `launch_cmd`/`stop_cmd` use, so `$(...)`
    and `$PID` are expanded by the DEVICE's sh, never the guest's.

    `pidof` can print more than one pid (isolated/multi-process apps
    sharing the same base name); `${PID%% *}` keeps only the first,
    since `--pid=` takes exactly one. An empty result (app not running)
    exits with a clear stderr message instead of ever building a
    malformed `--pid=` with nothing after the `=`.
    """
    quoted = shlex.quote(package)
    not_running = shlex.quote(f"{package} is not running (no matching process)")
    prefix = (
        f"PID=$(pidof {quoted} 2>/dev/null); "
        "PID=${PID%% *}; "
        f'if [ -z "$PID" ]; then echo {not_running} >&2; exit 1; fi; '
    )
    args = ["logcat"]
    if not follow:
        args.append("-d")
    args.append('--pid="$PID"')
    args.extend(_logcat_filterspec(tag=tag, priority=priority))
    # NOT `-t N`. Verified live on Redroid 11: `-t N` seeks to the last N
    # RAW lines and only then applies the filter, so `-t 5 -s Zygote:V`
    # returns nothing whenever the newest 5 lines are not Zygote's --
    # silently defeating the filter. `--lines` has to mean "the last N
    # MATCHING lines", so filter first and tail afterwards.
    tail = f" | tail -n {int(lines)}" if lines is not None and not follow else ""
    return adb_shell(prefix + " ".join(args) + tail)


def logcat_cmd(
    *,
    follow: bool,
    lines: int | None,
    package: str | None = None,
    tag: str | None = None,
    priority: str | None = None,
    clear: bool = False,
) -> str:
    """Dump/follow/filter/clear the device log.

    `clear` short-circuits everything else and maps straight to `adb
    logcat -c`: the CLI layer (`app_commands.py`) rejects combining
    `--clear` with any other logcat flag before this is ever called, so
    the other parameters are simply ignored here rather than validated
    twice.

    `package` routes through `_logcat_package_cmd` (a device shell
    script, see its docstring); without it this stays the plain `adb
    logcat` protocol form, which accepts `-s`/`*:LEVEL` filterspecs
    directly as adb arguments with no device-shell re-parsing hazard.
    """
    if clear:
        return f"{adb_prefix()} logcat -c"
    if package is not None:
        return _logcat_package_cmd(
            package, follow=follow, lines=lines, tag=tag, priority=priority
        )
    parts = [adb_prefix(), "logcat"]
    if not follow:
        parts.append("-d")
    filterspec = _logcat_filterspec(tag=tag, priority=priority)
    if lines is not None and filterspec and not follow:
        # `-t N` applies the filter to the last N RAW lines, so it silently
        # returns nothing when the newest lines do not match -- verified
        # live on Redroid 11 (`-t 5 -s Zygote:V` gave only the header while
        # the unfiltered buffer was full of Zygote lines). When a filter is
        # in play, `--lines` must mean the last N MATCHING lines, which
        # needs a device-side pipe rather than the bare protocol form.
        device = " ".join(["logcat", "-d", *filterspec])
        return adb_shell(f"{device} | tail -n {int(lines)}")
    if lines is not None:
        parts.append(f"-t {int(lines)}")
    parts.extend(filterspec)
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
        f'[ "$({adb_prefix()} shell getprop sys.boot_completed 2>/dev/null '
        "| tr -d '\\r')\" = 1 ] && exit 0; sleep 1; done; exit 1"
    )
