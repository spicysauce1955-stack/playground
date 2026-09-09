"""Run one adb command across many devices in parallel.

Mirrors the fan-out shape already used by
`backend/local_libvirt/verify.py`: one worker per target, results
re-ordered back into declaration order so logs are stable regardless of
completion order.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from playground.android.commands import adb_shell
from playground.android.targets import AndroidTarget
from playground.android.transport import (
    AdbInvocation,
    GuestAdb,
    local_argv,
    render_guest_command,
    require_guest,
)
from playground.ssh.argv import build_ssh_argv

BOOT_PROP_TIMEOUT = 15.0
"""Per-attempt timeout for one getprop round trip."""

__all__ = [
    "BOOT_PROP_TIMEOUT",
    "TargetResult",
    "run_guest_shell",
    "run_on_targets",
    "wait_booted",
]


@dataclass(frozen=True)
class TargetResult:
    target: AndroidTarget
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _execute(target: AndroidTarget, argv: list[str], timeout: float) -> TargetResult:
    try:
        done = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return TargetResult(target, 124, "", f"timeout after {timeout}s")
    except FileNotFoundError as exc:
        return TargetResult(target, 127, "", f"failed to launch {argv[0]}: {exc}")
    return TargetResult(target, done.returncode, done.stdout, done.stderr)


def _run_one(target: AndroidTarget, inv: AdbInvocation, timeout: float) -> TargetResult:
    """Render ``inv`` for ``target``'s transport, then run it.

    ``inv`` is an `AdbInvocation` from an `android/commands.py` (or
    `capture/commands.py`) builder -- a real adb command, meaningful on
    both transports. Guest-shell commands that are not adb at all
    (systemctl wrappers, `mkdir -p`/`rm -rf` staging, ...) go through
    `run_guest_shell` instead: keeping this function's parameter typed to
    `AdbInvocation` (not `AdbInvocation | str`) makes pairing a `LocalAdb`
    target with a guest-only string a structural impossibility rather
    than a runtime raise buried in here.
    """
    transport = target.transport
    if isinstance(transport, GuestAdb):
        argv = build_ssh_argv(
            transport.ssh_host,
            user=transport.ssh_user,
            port=transport.ssh_port,
            command=render_guest_command(inv),
            batch=True,
        )
    else:
        argv = local_argv(transport, inv)
    return _execute(target, argv, timeout)


def _run_shell_one(target: AndroidTarget, command: str, timeout: float) -> TargetResult:
    """Run a guest-shell ``command`` (not an adb invocation) over ssh.

    `require_guest` RAISES `TypeError` for a `LocalAdb` target rather
    than returning a failing `TargetResult`, consistent with
    `transport.require_guest`'s own docstring: a `LocalAdb` target
    reaching here is not an operator mistake to explain -- there is no
    flag an operator could have passed differently -- it is a caller
    that built a guest-only command for a target it never checked was a
    guest. That is a programming error, and it must never render to the
    operator as an ordinary device failure.
    """
    guest = require_guest(target)
    argv = build_ssh_argv(
        guest.ssh_host,
        user=guest.ssh_user,
        port=guest.ssh_port,
        command=command,
        batch=True,
    )
    return _execute(target, argv, timeout)


def _fan_out(
    targets: list[AndroidTarget], run_one: Callable[[AndroidTarget], TargetResult]
) -> list[TargetResult]:
    if not targets:
        return []
    results: dict[str, TargetResult] = {}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {pool.submit(run_one, t): t for t in targets}
        for future in as_completed(futures):
            result = future.result()
            results[result.target.name] = result
    return [results[t.name] for t in targets]


def run_on_targets(
    targets: list[AndroidTarget], inv: AdbInvocation, *, timeout: float
) -> list[TargetResult]:
    """Run adb invocation ``inv`` on every target; results keep input order."""
    return _fan_out(targets, lambda t: _run_one(t, inv, timeout))


def run_guest_shell(
    targets: list[AndroidTarget], command: str, *, timeout: float
) -> list[TargetResult]:
    """Run guest-shell ``command`` on every target; results keep input order.

    For commands that are not adb invocations at all and therefore only
    make sense on a `GuestAdb` transport -- see `_run_shell_one`. Real
    call sites: `capture/commands.py`'s systemctl builders, and
    `app_commands.py`'s `mkdir -p`/`rm -rf` staging and `adb push` chain.
    """
    return _fan_out(targets, lambda t: _run_shell_one(t, command, timeout))


def wait_booted(
    targets: list[AndroidTarget], *, attempts: int, interval: float
) -> list[str]:
    """Poll until Android reports a completed boot; return the stragglers.

    This used to be a shell `for` loop built by `commands.wait_booted_cmd`
    and executed on the guest. A handset transport has no shell, so the
    loop moved here -- which also means one poll implementation for both
    transports instead of a string that only one of them can run.
    """
    invocation = adb_shell("getprop sys.boot_completed")
    pending = list(targets)
    for attempt in range(attempts):
        if not pending:
            break
        results = run_on_targets(pending, invocation, timeout=BOOT_PROP_TIMEOUT)
        pending = [r.target for r in results if r.stdout.strip() != "1"]
        if pending and attempt < attempts - 1 and interval:
            time.sleep(interval)
    return [t.name for t in pending]
