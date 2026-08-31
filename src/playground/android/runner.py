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


def _run_one(target: AndroidTarget, command: str, timeout: float) -> TargetResult:
    argv = build_ssh_argv(
        target.ssh_host,
        user=target.ssh_user,
        port=target.ssh_port,
        command=command,
        batch=True,
    )
    try:
        done = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return TargetResult(target, 124, "", f"timeout after {timeout}s")
    except FileNotFoundError as exc:
        return TargetResult(target, 127, "", f"failed to launch ssh: {exc}")
    return TargetResult(target, done.returncode, done.stdout, done.stderr)


def run_on_targets(
    targets: list[AndroidTarget], command: str, *, timeout: float
) -> list[TargetResult]:
    """Run ``command`` on every target; results keep input order."""
    if not targets:
        return []
    results: dict[str, TargetResult] = {}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {
            pool.submit(_run_one, t, command, timeout): t for t in targets
        }
        for future in as_completed(futures):
            result = future.result()
            results[result.target.vm_name] = result
    return [results[t.vm_name] for t in targets]
