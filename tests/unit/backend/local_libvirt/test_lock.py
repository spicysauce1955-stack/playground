"""Unit tests for stale tofu state-lock recovery (PAPERCUT-5).

The recovery hinges on a liveness proof: we clear a lock only when no
live process holds the underlying ``fcntl`` lock. These tests exercise
both the probe (`lock_is_held`) and the decision logic (`clear_stale_lock`),
injecting a fake ``force_unlock`` so no real ``tofu`` binary is needed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from playground.backend.local_libvirt import lock as lockmod
from playground.backend.local_libvirt.lock import (
    LOCK_FILE,
    STATE_FILE,
    clear_stale_lock,
    lock_is_held,
    read_lock_info,
)
from playground.models.diagnostic import Diagnostic
from playground.runs.operation import StepResult


def _write_lock(tofu_dir: Path, *, lock_id: str | None = "abc-123",
                who: str = "user@host", state: bool = True) -> Path:
    """Materialise a sidecar (and optionally a state file) under tofu_dir."""
    tofu_dir.mkdir(parents=True, exist_ok=True)
    if state:
        (tofu_dir / STATE_FILE).write_text("{}\n")
    payload: dict[str, object] = {"Who": who, "Operation": "OperationTypeApply"}
    if lock_id is not None:
        payload["ID"] = lock_id
    sidecar = tofu_dir / LOCK_FILE
    sidecar.write_text(json.dumps(payload))
    return sidecar


class _FakeUnlock:
    """Records calls and returns a configurable StepResult."""

    def __init__(self, exit_code: int = 0,
                 diags: list[Diagnostic] | None = None) -> None:
        self.exit_code = exit_code
        self.diags = diags or []
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, tofu_dir: Path, lock_id: str, log_path: Path,
                 *, bus=None, run_id=None) -> tuple[StepResult, list[Diagnostic]]:
        self.calls.append((tofu_dir, lock_id))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake force-unlock\n")
        step = StepResult(
            name="clear-stale-lock",
            command=["tofu", "force-unlock", "-force", lock_id],
            exit_code=self.exit_code,
            log_path=str(log_path),
            started_at="2026-06-02T00:00:00+00:00",
            finished_at="2026-06-02T00:00:01+00:00",
        )
        return step, self.diags


# --------------------------------------------------------------------------- #
# read_lock_info
# --------------------------------------------------------------------------- #


def test_read_lock_info_absent(tmp_path: Path) -> None:
    assert read_lock_info(tmp_path) is None


def test_read_lock_info_parses(tmp_path: Path) -> None:
    _write_lock(tmp_path, lock_id="zz", who="alice@box")
    info = read_lock_info(tmp_path)
    assert info == {"ID": "zz", "Who": "alice@box",
                    "Operation": "OperationTypeApply"}


def test_read_lock_info_garbage_returns_empty(tmp_path: Path) -> None:
    (tmp_path / LOCK_FILE).write_text("not json {{{")
    assert read_lock_info(tmp_path) == {}


# --------------------------------------------------------------------------- #
# lock_is_held
# --------------------------------------------------------------------------- #


def test_lock_not_held_when_state_absent(tmp_path: Path) -> None:
    assert lock_is_held(tmp_path / STATE_FILE) is False


def test_lock_not_held_when_unlocked(tmp_path: Path) -> None:
    state = tmp_path / STATE_FILE
    state.write_text("{}\n")
    assert lock_is_held(state) is False


def test_lock_held_by_live_process(tmp_path: Path) -> None:
    """A separate process holding the fcntl lock must read as held."""
    state = tmp_path / STATE_FILE
    state.write_text("{}\n")
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            sys.executable, "-c",
            (
                "import fcntl,os,sys,time;"
                "fd=os.open(sys.argv[1], os.O_RDWR);"
                "fcntl.lockf(fd, fcntl.LOCK_EX);"
                "open(sys.argv[2],'w').close();"
                "time.sleep(10)"
            ),
            str(state), str(ready),
        ],
    )
    try:
        for _ in range(100):  # wait up to ~5s for the child to grab the lock
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "holder subprocess never acquired the lock"
        assert lock_is_held(state) is True
    finally:
        holder.terminate()
        holder.wait(timeout=5)
    # Once the holder is gone the lock is released.
    assert lock_is_held(state) is False


# --------------------------------------------------------------------------- #
# clear_stale_lock
# --------------------------------------------------------------------------- #


def test_clear_noop_when_no_lock(tmp_path: Path) -> None:
    unlock = _FakeUnlock()
    step, diags = clear_stale_lock(
        tmp_path, tmp_path / "log.txt", force_unlock=unlock,
    )
    assert step.exit_code == 0
    assert diags == []
    assert unlock.calls == []


def test_clear_skips_when_lock_held(tmp_path: Path, monkeypatch) -> None:
    _write_lock(tmp_path, lock_id="held-1", who="bob@box")
    monkeypatch.setattr(lockmod, "lock_is_held", lambda _state: True)
    unlock = _FakeUnlock()
    step, diags = clear_stale_lock(
        tmp_path, tmp_path / "log.txt", force_unlock=unlock,
    )
    assert step.exit_code == 0
    assert unlock.calls == []  # never force-unlock a live lock
    assert [d.id for d in diags] == ["runtime.reset.lock_held"]
    assert diags[0].severity == "warning"
    assert "held-1" in diags[0].message
    assert (tmp_path / LOCK_FILE).exists()  # left intact


def test_clear_force_unlocks_stale_lock(tmp_path: Path) -> None:
    _write_lock(tmp_path, lock_id="stale-9", who="dead@box", state=False)
    unlock = _FakeUnlock(exit_code=0)
    step, diags = clear_stale_lock(
        tmp_path, tmp_path / "log.txt", force_unlock=unlock,
    )
    assert unlock.calls == [(tmp_path, "stale-9")]
    assert [d.id for d in diags] == ["runtime.reset.lock_cleared"]
    assert diags[0].severity == "info"
    assert "stale-9" in diags[0].message


def test_clear_force_unlock_failure_removes_sidecar(tmp_path: Path) -> None:
    sidecar = _write_lock(tmp_path, lock_id="gone-5", state=False)
    unlock = _FakeUnlock(exit_code=1)
    step, diags = clear_stale_lock(
        tmp_path, tmp_path / "log.txt", force_unlock=unlock,
    )
    assert unlock.calls == [(tmp_path, "gone-5")]
    assert [d.id for d in diags] == ["runtime.reset.lock_clear_failed"]
    assert diags[0].severity == "warning"
    assert not sidecar.exists()  # orphan dropped so apply starts clean


def test_clear_idless_orphan_is_unlinked(tmp_path: Path) -> None:
    sidecar = _write_lock(tmp_path, lock_id=None, state=False)
    unlock = _FakeUnlock()
    step, diags = clear_stale_lock(
        tmp_path, tmp_path / "log.txt", force_unlock=unlock,
    )
    assert unlock.calls == []  # nothing to force-unlock without an ID
    assert [d.id for d in diags] == ["runtime.reset.lock_cleared"]
    assert not sidecar.exists()
