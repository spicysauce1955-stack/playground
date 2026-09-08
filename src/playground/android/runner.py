"""Run one adb command across many devices in parallel.

Mirrors the fan-out shape already used by
`backend/local_libvirt/verify.py`: one worker per target, results
re-ordered back into declaration order so logs are stable regardless of
completion order.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from playground.android.targets import AndroidTarget
from playground.android.transport import AdbInvocation, GuestAdb, local_argv, render_guest_command
from playground.ssh.argv import build_ssh_argv


@dataclass(frozen=True)
class TargetResult:
    target: AndroidTarget
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _run_one(target: AndroidTarget, inv: AdbInvocation | str, timeout: float) -> TargetResult:
    """Render ``inv`` for ``target``'s transport, then run it.

    ``inv`` is normally an `AdbInvocation` from an `android/commands.py`
    (or `capture/commands.py`) builder, but `capture/commands.py`'s
    builders -- and `wait_booted_cmd` -- still return a plain, already
    guest-rendered ``str`` (see their own modules for why). Those are
    guest-shell commands, not adb invocations, and only ever make sense
    on `GuestAdb` -- there is no shell to run them in on a USB-attached
    `LocalAdb` handset.

    A `LocalAdb` target paired with a raw `str` command RAISES rather
    than returning a failing `TargetResult`, consistent with
    `transport.require_guest`: it is a caller bug (something built a
    guest-only command for a target it never checked was a guest), not a
    device failure, and reporting it as an ordinary non-zero exit code
    would mislead the operator into thinking the DEVICE failed.
    """
    transport = target.transport
    if isinstance(transport, GuestAdb):
        command = render_guest_command(inv) if isinstance(inv, AdbInvocation) else inv
        argv = build_ssh_argv(
            transport.ssh_host,
            user=transport.ssh_user,
            port=transport.ssh_port,
            command=command,
            batch=True,
        )
    else:
        if not isinstance(inv, AdbInvocation):
            raise TypeError(
                f"{target.name!r} is a LocalAdb target; raw guest-shell commands "
                "have no meaning on a USB-attached device (there is no shell to "
                "run them in)"
            )
        argv = local_argv(transport, inv)
    try:
        done = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return TargetResult(target, 124, "", f"timeout after {timeout}s")
    except FileNotFoundError as exc:
        return TargetResult(target, 127, "", f"failed to launch {argv[0]}: {exc}")
    return TargetResult(target, done.returncode, done.stdout, done.stderr)


def run_on_targets(
    targets: list[AndroidTarget], inv: AdbInvocation | str, *, timeout: float
) -> list[TargetResult]:
    """Run ``inv`` on every target; results keep input order."""
    if not targets:
        return []
    results: dict[str, TargetResult] = {}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {
            pool.submit(_run_one, t, inv, timeout): t for t in targets
        }
        for future in as_completed(futures):
            result = future.result()
            results[result.target.name] = result
    return [results[t.name] for t in targets]
