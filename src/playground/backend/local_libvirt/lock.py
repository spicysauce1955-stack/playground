"""Stale tofu state-lock detection and recovery for the local-libvirt backend.

OpenTofu's local backend guards ``terraform.tfstate`` with a POSIX
``fcntl`` record lock and writes a ``.terraform.tfstate.lock.info``
sidecar holding the lock metadata (``ID``, ``Who``, ``Operation`` …).
When an ``apply`` is killed mid-run (e.g. a sandbox reaps the process),
the kernel releases the ``fcntl`` lock automatically, but the sidecar
file and the operator's mental model of "it's still locked" can linger —
and a subsequent ``apply``/``destroy`` that races a *still-dying* holder
fails with ``Error acquiring the state lock … resource temporarily
unavailable``. Recovering meant dropping into raw ``tofu force-unlock``.

This module lets ``reset`` do that recovery automatically, but *only*
when it can prove the lock is stale. The proof is liveness, not a PID:
the sidecar carries no PID, and an ``fcntl`` lock is released the instant
its owner dies. So we probe the lock space directly — if we can acquire
the lock ourselves (non-blocking), no live process holds it and the lock
is safe to clear. If the probe is refused, a real operation is in flight
and we leave it alone rather than clobber a concurrent apply.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from playground.backend.local_libvirt.apply import run_tofu_force_unlock
from playground.events import EventBus
from playground.models.diagnostic import Diagnostic
from playground.runs.operation import StepResult

STATE_FILE = "terraform.tfstate"
LOCK_FILE = ".terraform.tfstate.lock.info"


class _ForceUnlock(Protocol):
    def __call__(
        self,
        tofu_dir: Path,
        lock_id: str,
        log_path: Path,
        *,
        bus: EventBus | None = ...,
        run_id: str | None = ...,
    ) -> tuple[StepResult, list[Diagnostic]]: ...


def read_lock_info(tofu_dir: Path) -> dict[str, Any] | None:
    """Return the parsed ``.terraform.tfstate.lock.info`` sidecar, or
    ``None`` when it is absent. A present-but-unparseable sidecar yields
    an empty dict (we still know a lock *file* exists, just not its ID)."""
    sidecar = tofu_dir / LOCK_FILE
    if not sidecar.exists():
        return None
    try:
        loaded = json.loads(sidecar.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def lock_is_held(state_path: Path) -> bool:
    """Return ``True`` when a live process holds tofu's ``fcntl`` lock on
    ``state_path``.

    Probes the same POSIX-record-lock space tofu uses by attempting a
    non-blocking exclusive ``lockf``. Acquiring it (then immediately
    releasing) proves no live owner — the lock is stale. ``EAGAIN``/
    ``EACCES`` means a real process holds it. A missing state file can't
    be locked, so it is reported as not held.
    """
    if not state_path.exists():
        return False
    try:
        fd = os.open(state_path, os.O_RDWR)
    except OSError:
        # Can't open to probe; be conservative and assume it's held so we
        # don't claim a stale lock we couldn't actually verify.
        return True
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.lockf(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def clear_stale_lock(
    tofu_dir: Path,
    log_path: Path,
    *,
    bus: EventBus | None = None,
    run_id: str | None = None,
    force_unlock: _ForceUnlock | Callable[..., tuple[StepResult, list[Diagnostic]]]
    = run_tofu_force_unlock,
) -> tuple[StepResult, list[Diagnostic]]:
    """Clear a stale tofu state lock if (and only if) it is provably stale.

    Returns the step record plus diagnostics. Never fails the step: reset
    treats lock recovery as best-effort, the same way it treats
    ``tofu destroy``. The outcomes:

    - no sidecar present → no-op (info, no diagnostic);
    - sidecar present but a live process holds the lock → leave it,
      ``runtime.reset.lock_held`` *warning*;
    - sidecar present and no live holder → ``tofu force-unlock -force``
      (or remove an ID-less orphan sidecar), ``runtime.reset.lock_cleared``
      *info* on success; ``runtime.reset.lock_clear_failed`` *warning* if
      force-unlock balks (commonly "already unlocked") — we then drop the
      orphan sidecar so the next apply starts clean.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = _now_iso()
    sidecar = tofu_dir / LOCK_FILE
    state = tofu_dir / STATE_FILE

    info = read_lock_info(tofu_dir)
    if info is None:
        return (
            _noop_step(log_path, started, "no tofu state lock present"),
            [],
        )

    lock_id = str(info.get("ID") or "").strip()
    who = str(info.get("Who") or "unknown")

    if lock_is_held(state):
        return (
            _noop_step(
                log_path, started,
                f"state lock {lock_id or '?'} (Who={who}) is held by a live "
                "process; not clearing",
            ),
            [
                Diagnostic(
                    id="runtime.reset.lock_held",
                    severity="warning",
                    message=(
                        f"tofu state lock (id {lock_id or 'unknown'}, "
                        f"Who {who}) is held by a running process; not "
                        "clearing it. Another apply/destroy may be in progress."
                    ),
                    suggestion=(
                        "wait for the other operation to finish; if you are "
                        "certain that process is dead, clear it by hand with "
                        f"`cd tofu && tofu force-unlock {lock_id or '<id>'}`"
                    ),
                )
            ],
        )

    # No live holder → the lock is stale. Prefer a real `force-unlock` (it
    # also reconciles tofu's view); fall back to unlinking an ID-less
    # orphan sidecar that force-unlock could not target.
    if not lock_id:
        removed = _unlink_quietly(sidecar)
        msg = (
            "removed an orphaned state-lock file with no recoverable lock ID"
            if removed
            else "stale state-lock file vanished before it could be removed"
        )
        return (
            _noop_step(log_path, started, msg),
            [
                Diagnostic(
                    id="runtime.reset.lock_cleared",
                    severity="info",
                    message=msg,
                )
            ],
        )

    step, binary_diags = force_unlock(
        tofu_dir, lock_id, log_path, bus=bus, run_id=run_id,
    )
    if binary_diags:
        # tofu binary missing — surface it; reset's caller decides severity.
        return step, binary_diags

    if step.exit_code == 0:
        return (
            step,
            [
                Diagnostic(
                    id="runtime.reset.lock_cleared",
                    severity="info",
                    message=(
                        f"cleared stale tofu state lock {lock_id} "
                        f"(Who {who}) left by a dead process"
                    ),
                )
            ],
        )

    # force-unlock balked (commonly "LocalState not locked" — the lock had
    # already cleared). Drop any lingering orphan sidecar so apply is clean.
    _unlink_quietly(sidecar)
    return (
        step,
        [
            Diagnostic(
                id="runtime.reset.lock_clear_failed",
                severity="warning",
                message=(
                    f"`tofu force-unlock` exited {step.exit_code} for lock "
                    f"{lock_id}; it had likely already cleared. Removed the "
                    "stale lock file so the next apply starts clean."
                ),
            )
        ],
    )


def _unlink_quietly(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _noop_step(log_path: Path, started: str, message: str) -> StepResult:
    log_path.write_text(f"# clear-stale-lock\n{message}\n")
    return StepResult(
        name="clear-stale-lock",
        command=["python", "-c", "clear-stale-lock"],
        exit_code=0,
        log_path=str(log_path),
        started_at=started,
        finished_at=_now_iso(),
    )


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = ["clear_stale_lock", "lock_is_held", "read_lock_info"]
