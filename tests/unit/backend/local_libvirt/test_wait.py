"""Unit tests for the apply tofu→ansible gate.

Real socket connects + ssh invocations are stubbed via monkeypatch
so the suite never touches a real VM. The integration smoke
(boot real Noble VM, race ansible) lives in the live-infra path
gated on ``PLAYGROUND_LIVE_INFRA=1``.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from playground.backend.local_libvirt import wait as wait_mod
from playground.backend.local_libvirt.wait import VmTarget, wait_for_vms_ready
from playground.events import EventBus


def _completed(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def test_no_targets_returns_clean_step(tmp_path: Path) -> None:
    step, diagnostics = wait_for_vms_ready(
        targets=[],
        log_path=tmp_path / "wait.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 0
    assert diagnostics == []
    assert (tmp_path / "wait.log").exists()


def test_missing_ssh_binary_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wait_mod.shutil, "which", lambda _name: None)
    step, diagnostics = wait_for_vms_ready(
        targets=[VmTarget(name="vm-a", ip="10.0.0.10", ssh_user="ubuntu")],
        log_path=tmp_path / "wait.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 127
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.apply.ssh_binary_missing"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_two_vms_in_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wait_mod.shutil, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(wait_mod, "_wait_tcp", lambda ip, port, timeout: (True, 0.1))
    monkeypatch.setattr(
        wait_mod, "_wait_ssh_auth", lambda *, target, timeout: (True, 0.1)
    )
    monkeypatch.setattr(
        wait_mod, "_wait_cloud_init",
        lambda *, ip, user, timeout, port=22: wait_mod._CloudInitResult(
            exit_code=0, stdout_summary="status: done", stderr_summary=""
        ),
    )
    step, diagnostics = wait_for_vms_ready(
        targets=[
            VmTarget(name="vm-a", ip="10.0.0.10", ssh_user="ubuntu"),
            VmTarget(name="vm-b", ip="10.0.0.11", ssh_user="ubuntu"),
        ],
        log_path=tmp_path / "wait.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 0
    assert diagnostics == []
    log = (tmp_path / "wait.log").read_text()
    assert "vm-a: sshd answering" in log
    assert "vm-b: sshd answering" in log
    assert "vm-a: cloud-init done" in log
    assert "vm-b: cloud-init done" in log


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_ssh_timeout_emits_per_vm_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wait_mod.shutil, "which", lambda _name: "/usr/bin/ssh")
    # vm-a reachable, vm-b never.
    def _tcp(ip: str, port: int, timeout: float) -> tuple[bool, float]:
        return (ip == "10.0.0.10", 0.1)

    monkeypatch.setattr(wait_mod, "_wait_tcp", _tcp)
    monkeypatch.setattr(
        wait_mod, "_wait_ssh_auth", lambda *, target, timeout: (True, 0.1)
    )
    monkeypatch.setattr(
        wait_mod, "_wait_cloud_init",
        lambda *, ip, user, timeout, port=22: wait_mod._CloudInitResult(
            exit_code=0, stdout_summary="", stderr_summary=""
        ),
    )
    step, diagnostics = wait_for_vms_ready(
        targets=[
            VmTarget(name="vm-a", ip="10.0.0.10", ssh_user="ubuntu"),
            VmTarget(name="vm-b", ip="10.0.0.11", ssh_user="ubuntu"),
        ],
        log_path=tmp_path / "wait.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 1
    timeouts = [d for d in diagnostics if d.id == "runtime.apply.wait_ssh_timeout"]
    assert len(timeouts) == 1
    assert "vm-b" in timeouts[0].message
    assert "virsh console vm-b" in (timeouts[0].suggestion or "")


def test_cloud_init_timeout_emits_specific_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wait_mod.shutil, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(wait_mod, "_wait_tcp", lambda *_a, **_k: (True, 0.1))
    monkeypatch.setattr(
        wait_mod, "_wait_ssh_auth", lambda *, target, timeout: (True, 0.1)
    )
    monkeypatch.setattr(
        wait_mod, "_wait_cloud_init",
        lambda *, ip, user, timeout, port=22: wait_mod._CloudInitResult(
            exit_code=-1, stdout_summary="", stderr_summary="", timed_out=True,
        ),
    )
    step, diagnostics = wait_for_vms_ready(
        targets=[VmTarget(name="vm-a", ip="10.0.0.10", ssh_user="ubuntu")],
        log_path=tmp_path / "wait.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 1
    assert any(d.id == "runtime.apply.wait_cloud_init_timeout" for d in diagnostics)


def test_cloud_init_error_emits_failed_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wait_mod.shutil, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(wait_mod, "_wait_tcp", lambda *_a, **_k: (True, 0.1))
    monkeypatch.setattr(
        wait_mod, "_wait_ssh_auth", lambda *, target, timeout: (True, 0.1)
    )
    monkeypatch.setattr(
        wait_mod, "_wait_cloud_init",
        lambda *, ip, user, timeout, port=22: wait_mod._CloudInitResult(
            exit_code=1, stdout_summary="status: error", stderr_summary="",
        ),
    )
    step, diagnostics = wait_for_vms_ready(
        targets=[VmTarget(name="vm-a", ip="10.0.0.10", ssh_user="ubuntu")],
        log_path=tmp_path / "wait.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 1
    failed = [d for d in diagnostics if d.id == "runtime.apply.wait_cloud_init_failed"]
    assert len(failed) == 1
    assert "status: error" in failed[0].message


def test_cloud_init_advisory_downgrades_failure_to_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``cloud_init_advisory=True`` a cloud-init failure becomes a
    warning and the step still succeeds (Ansible is the real gate). The
    SSH phases already proved reachability. This is the DigitalOcean
    vendor-script case; local backends leave the flag off and stay strict.
    """
    monkeypatch.setattr(wait_mod.shutil, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(wait_mod, "_wait_tcp", lambda *_a, **_k: (True, 0.1))
    monkeypatch.setattr(
        wait_mod, "_wait_ssh_auth", lambda *, target, timeout: (True, 0.1)
    )
    monkeypatch.setattr(
        wait_mod, "_wait_cloud_init",
        lambda *, ip, user, timeout, port=22: wait_mod._CloudInitResult(
            exit_code=1, stdout_summary="status: error", stderr_summary="",
        ),
    )
    step, diagnostics = wait_for_vms_ready(
        targets=[VmTarget(name="vm-a", ip="10.0.0.10", ssh_user="ubuntu")],
        log_path=tmp_path / "wait.log",
        bus=EventBus(),
        run_id="r1",
        cloud_init_advisory=True,
    )
    assert step.exit_code == 0  # advisory: step succeeds despite cloud-init
    failed = [d for d in diagnostics if d.id == "runtime.apply.wait_cloud_init_failed"]
    assert len(failed) == 1
    assert failed[0].severity == "warning"


def test_cloud_init_advisory_keeps_ssh_timeout_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advisory mode must NOT excuse SSH unreachability — only cloud-init
    status. A VM that never accepts SSH still fails the step.
    """
    monkeypatch.setattr(wait_mod.shutil, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(wait_mod, "_wait_tcp", lambda *_a, **_k: (True, 0.1))
    monkeypatch.setattr(
        wait_mod, "_wait_ssh_auth", lambda *, target, timeout: (False, 1.0)
    )
    step, diagnostics = wait_for_vms_ready(
        targets=[VmTarget(name="vm-a", ip="10.0.0.10", ssh_user="ubuntu")],
        log_path=tmp_path / "wait.log",
        bus=EventBus(),
        run_id="r1",
        cloud_init_advisory=True,
    )
    assert step.exit_code == 1
    assert any(
        d.id == "runtime.apply.wait_sshd_timeout" and d.severity == "error"
        for d in diagnostics
    )


def test_log_order_matches_target_declaration_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parallel completion order is non-deterministic, but the log
    must stay sorted by declaration order so it's readable."""
    monkeypatch.setattr(wait_mod.shutil, "which", lambda _name: "/usr/bin/ssh")

    # Make vm-b "finish" first by giving vm-a a slower TCP delay marker.
    call_count = {"a": 0}

    def _tcp(ip: str, port: int, timeout: float) -> tuple[bool, float]:
        # vm-b reports 0.05s elapsed; vm-a reports 2.0s. Either way both succeed.
        elapsed = 0.05 if ip == "10.0.0.11" else 2.0
        return True, elapsed

    monkeypatch.setattr(wait_mod, "_wait_tcp", _tcp)
    monkeypatch.setattr(
        wait_mod, "_wait_ssh_auth", lambda *, target, timeout: (True, 0.1)
    )
    monkeypatch.setattr(
        wait_mod, "_wait_cloud_init",
        lambda *, ip, user, timeout, port=22: wait_mod._CloudInitResult(
            exit_code=0, stdout_summary="", stderr_summary=""
        ),
    )
    wait_for_vms_ready(
        targets=[
            VmTarget(name="vm-a", ip="10.0.0.10", ssh_user="ubuntu"),
            VmTarget(name="vm-b", ip="10.0.0.11", ssh_user="ubuntu"),
        ],
        log_path=tmp_path / "wait.log",
        bus=EventBus(),
        run_id="r1",
    )
    log = (tmp_path / "wait.log").read_text()
    a_idx = log.find("vm-a: sshd answering")
    b_idx = log.find("vm-b: sshd answering")
    assert 0 < a_idx < b_idx, log


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def test_wait_tcp_returns_true_on_immediate_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSocket:
        def __enter__(self) -> "_FakeSocket":
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    monkeypatch.setattr(
        wait_mod.socket, "create_connection", lambda *_a, **_k: _FakeSocket()
    )
    ok, elapsed = wait_mod._wait_tcp("10.0.0.10", 22, timeout=5.0)
    assert ok is True
    assert elapsed < 1.0


def test_wait_tcp_returns_false_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse(*_a: Any, **_kw: Any) -> Any:
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(wait_mod.socket, "create_connection", _refuse)
    # Make the loop fail fast: monkeypatch time.sleep to no-op and
    # time.monotonic to advance past the deadline on the second call.
    monkeypatch.setattr(wait_mod.time, "sleep", lambda _s: None)
    # Two ticks: 0.0 at start, then 100.0 past the 1s deadline on every
    # subsequent call. Past-deadline shape forces the loop to exit.
    clock = {"calls": 0}

    def _monotonic() -> float:
        clock["calls"] += 1
        return 0.0 if clock["calls"] == 1 else 100.0

    monkeypatch.setattr(wait_mod.time, "monotonic", _monotonic)
    ok, _elapsed = wait_mod._wait_tcp("10.0.0.10", 22, timeout=1.0)
    assert ok is False


def test_wait_cloud_init_handles_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_timeout(*_a: Any, **_kw: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=60.0)

    monkeypatch.setattr(wait_mod.subprocess, "run", _raise_timeout)
    result = wait_mod._wait_cloud_init(ip="10.0.0.10", user="ubuntu", timeout=60.0)
    assert result.timed_out is True
    assert result.exit_code == -1


def test_wait_cloud_init_passes_ssh_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH must be invoked in batch mode with strict-host accept-new
    so it never prompts; otherwise the wait hangs forever on first
    boot."""
    captured: dict[str, list[str]] = {}

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        return _completed(stdout="status: done")

    monkeypatch.setattr(wait_mod.subprocess, "run", _stub)
    result = wait_mod._wait_cloud_init(
        ip="10.0.0.10", user="root", timeout=60.0
    )
    assert result.exit_code == 0
    assert "BatchMode=yes" in captured["args"]
    assert "StrictHostKeyChecking=accept-new" in captured["args"]
    assert "root@10.0.0.10" in captured["args"]
    assert "cloud-init status --wait" in captured["args"]


# ---------------------------------------------------------------------------
# SSH auth readiness gate (the vbox NAT-port-forward fix)
# ---------------------------------------------------------------------------


def test_wait_ssh_auth_retries_until_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP-open is a false positive for vbox NAT forwards, so the auth
    gate must keep retrying through transport failures until sshd
    actually answers."""
    target = VmTarget(name="vm", ip="127.0.0.1", ssh_user="ubuntu", ssh_port=2222)
    rcs = iter([255, 255, 0])  # not-up, not-up, then ready
    monkeypatch.setattr(wait_mod, "_ssh_probe", lambda *_a, **_k: next(rcs))
    monkeypatch.setattr(wait_mod.time, "sleep", lambda _s: None)
    ok, _elapsed = wait_mod._wait_ssh_auth(target=target, timeout=60.0)
    assert ok is True


def test_wait_ssh_auth_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    target = VmTarget(name="vm", ip="127.0.0.1", ssh_user="ubuntu", ssh_port=2222)
    monkeypatch.setattr(wait_mod, "_ssh_probe", lambda *_a, **_k: 255)
    monkeypatch.setattr(wait_mod.time, "sleep", lambda _s: None)
    clock = {"n": 0}

    def _monotonic() -> float:
        clock["n"] += 1
        return 0.0 if clock["n"] == 1 else 100.0  # past the 1s deadline

    monkeypatch.setattr(wait_mod.time, "monotonic", _monotonic)
    ok, _elapsed = wait_mod._wait_ssh_auth(target=target, timeout=1.0)
    assert ok is False


def test_ssh_probe_adds_port_for_nonstandard(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        return _completed(returncode=0)

    monkeypatch.setattr(wait_mod.subprocess, "run", _stub)
    target = VmTarget(name="vm", ip="127.0.0.1", ssh_user="ubuntu", ssh_port=2222)
    rc = wait_mod._ssh_probe(target)
    assert rc == 0
    assert "-p" in captured["args"]
    assert "2222" in captured["args"]
    assert "ubuntu@127.0.0.1" in captured["args"]
    assert "true" in captured["args"]


def test_ssh_probe_omits_port_for_default() -> None:
    # Port 22 must not add -p (keeps the libvirt command line unchanged).
    target = VmTarget(name="vm", ip="10.0.0.10", ssh_user="ubuntu")
    captured: dict[str, list[str]] = {}
    import playground.backend.local_libvirt.wait as w

    orig = w.subprocess.run
    try:
        w.subprocess.run = lambda args, **_k: (  # type: ignore[assignment]
            captured.__setitem__("args", args), _completed(returncode=0))[1]
        rc = w._ssh_probe(target)
    finally:
        w.subprocess.run = orig
    assert rc == 0
    assert "-p" not in captured["args"]
