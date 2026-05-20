"""Unit tests for the verify-lab post-apply phase."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from playground.backend.local_libvirt import verify as verify_mod
from playground.backend.local_libvirt.verify import verify_lab
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_generic_infra():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


def _completed(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def test_no_vms_returns_clean_step(resolved_generic_infra, tmp_path: Path) -> None:
    step, diagnostics = verify_lab(
        resolved=resolved_generic_infra,
        vm_ips={},
        log_path=tmp_path / "v.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 0
    assert diagnostics == []


def test_missing_ssh_fails_fatal(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(verify_mod.shutil, "which", lambda _name: None)
    step, diagnostics = verify_lab(
        resolved=resolved_generic_infra,
        vm_ips={"node1": "10.0.0.10"},
        log_path=tmp_path / "v.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 127
    assert any(d.id == "runtime.apply.verify_ssh_missing" for d in diagnostics)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_runs_systemd_docker_and_commands(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(verify_mod.shutil, "which", lambda _name: "/usr/bin/ssh")
    invocations: list[str] = []

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        # args ends with the SSH host followed by the remote command.
        invocations.append(args[-1])
        if "is-system-running" in args[-1]:
            return _completed(returncode=0, stdout="running\n")
        if "docker ps" in args[-1]:
            return _completed(returncode=0, stdout="CONTAINER ID\n")
        return _completed(returncode=0, stdout="ok\n")

    monkeypatch.setattr(verify_mod.subprocess, "run", _stub)
    vm_ips = {"node1": "10.0.0.10", "docker1": "10.0.0.11", "router1": "10.0.0.12"}
    step, diagnostics = verify_lab(
        resolved=resolved_generic_infra,
        vm_ips=vm_ips,
        log_path=tmp_path / "v.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 0
    assert diagnostics == []
    # systemctl ran on all three; docker ps only on docker1 (the
    # only VM whose VmRole provisions docker).
    systemctl_calls = [c for c in invocations if "is-system-running" in c]
    docker_calls = [c for c in invocations if "docker ps" in c]
    assert len(systemctl_calls) == 3
    assert len(docker_calls) == 1


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_systemd_failed_state_emits_diagnostic(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(verify_mod.shutil, "which", lambda _name: "/usr/bin/ssh")

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        if "is-system-running" in args[-1]:
            return _completed(returncode=1, stdout="failed\n")
        return _completed(returncode=0)

    monkeypatch.setattr(verify_mod.subprocess, "run", _stub)
    step, diagnostics = verify_lab(
        resolved=resolved_generic_infra,
        vm_ips={"node1": "10.0.0.10"},
        log_path=tmp_path / "v.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 1
    failed = [d for d in diagnostics if d.id == "runtime.apply.verify_failed"]
    assert any("'failed'" in d.message for d in failed)


def test_systemd_degraded_is_accepted(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`degraded` is normal for some libvirt + ssh setups (e.g.,
    one user unit fails). Doctor's contract is to accept it."""
    monkeypatch.setattr(verify_mod.shutil, "which", lambda _name: "/usr/bin/ssh")

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        if "is-system-running" in args[-1]:
            # is-system-running returns exit 1 even for 'degraded'
            # (some distros) — but the state value is the signal we
            # care about, not the exit code.
            return _completed(returncode=1, stdout="degraded\n")
        return _completed(returncode=0)

    monkeypatch.setattr(verify_mod.subprocess, "run", _stub)
    step, diagnostics = verify_lab(
        resolved=resolved_generic_infra,
        vm_ips={"node1": "10.0.0.10"},
        log_path=tmp_path / "v.log",
        bus=EventBus(),
        run_id="r1",
    )
    # degraded passes; no systemd diagnostic.
    sysd_diag = [
        d for d in diagnostics
        if d.id == "runtime.apply.verify_failed"
        and "is-system-running" in (d.message or "")
    ]
    assert sysd_diag == []


def test_docker_ps_failure_emits_diagnostic(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(verify_mod.shutil, "which", lambda _name: "/usr/bin/ssh")

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        if "is-system-running" in args[-1]:
            return _completed(returncode=0, stdout="running\n")
        if "docker ps" in args[-1]:
            return _completed(returncode=1, stderr="Cannot connect to docker socket")
        return _completed(returncode=0)

    monkeypatch.setattr(verify_mod.subprocess, "run", _stub)
    step, diagnostics = verify_lab(
        resolved=resolved_generic_infra,
        vm_ips={"docker1": "10.0.0.11"},
        log_path=tmp_path / "v.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 1
    docker_diag = [
        d for d in diagnostics
        if d.id == "runtime.apply.verify_failed"
        and "docker ps" in (d.message or "")
    ]
    assert len(docker_diag) == 1


def test_enabled_command_failure_emits_diagnostic(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The generic-infra lab enables ping-network with target: any."""
    monkeypatch.setattr(verify_mod.shutil, "which", lambda _name: "/usr/bin/ssh")

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        remote = args[-1]
        if "is-system-running" in remote or "docker ps" in remote:
            return _completed(returncode=0, stdout="ok\n")
        # Pretend the ping-network preset failed.
        return _completed(returncode=1, stderr="ping: connect: Network is unreachable")

    monkeypatch.setattr(verify_mod.subprocess, "run", _stub)
    step, diagnostics = verify_lab(
        resolved=resolved_generic_infra,
        vm_ips={"node1": "10.0.0.10"},
        log_path=tmp_path / "v.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 1
    command_diag = [
        d for d in diagnostics
        if d.id == "runtime.apply.verify_failed"
        and "ping-network" in (d.message or "")
    ]
    assert command_diag


def test_subprocess_timeout_surfaces_as_diagnostic(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(verify_mod.shutil, "which", lambda _name: "/usr/bin/ssh")

    def _raise(*_a: Any, **_kw: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=30.0)

    monkeypatch.setattr(verify_mod.subprocess, "run", _raise)
    step, diagnostics = verify_lab(
        resolved=resolved_generic_infra,
        vm_ips={"node1": "10.0.0.10"},
        log_path=tmp_path / "v.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 1
    assert any(d.id == "runtime.apply.verify_failed" for d in diagnostics)


# ---------------------------------------------------------------------------
# VM without IP is skipped silently
# ---------------------------------------------------------------------------


def test_vm_without_ip_is_skipped(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wait-for-vms-ready phase already surfaced the
    no-IP-for-VM case; verify shouldn't double-report."""
    monkeypatch.setattr(verify_mod.shutil, "which", lambda _name: "/usr/bin/ssh")
    invocations: list[str] = []

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        invocations.append(args[-1])
        return _completed(returncode=0, stdout="running\n")

    monkeypatch.setattr(verify_mod.subprocess, "run", _stub)
    # Only docker1 has an IP.
    step, diagnostics = verify_lab(
        resolved=resolved_generic_infra,
        vm_ips={"docker1": "10.0.0.11"},
        log_path=tmp_path / "v.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 0
    # Only docker1's SSH commands ran — node1 / router1 were skipped.
    targets_referenced = {
        "node1" in c or "router1" in c for c in invocations
    }
    assert not any(targets_referenced)
