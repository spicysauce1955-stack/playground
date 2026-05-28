"""Tests for the libvirt domain-state crash detector (Issue 1).

Without this gate, ``wait-for-vms-ready`` silently burns its full SSH
timeout when QEMU killed the guest at startup (the canonical
nested-virt-VMX-passthrough failure). The detector turns that into a
fast, actionable diagnostic.
"""

from __future__ import annotations

import subprocess

import pytest

from playground.backend.local_libvirt import domains


def _completed(
    returncode: int = 0, stdout: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["virsh"], returncode=returncode, stdout=stdout, stderr="",
    )


def test_check_returns_empty_when_virsh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(domains.shutil, "which", lambda _name: None)
    assert domains.check_domains_running(["central"], lab="L") == []


def test_check_returns_empty_for_running_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(domains.shutil, "which", lambda _name: "/usr/bin/virsh")
    monkeypatch.setattr(
        domains.subprocess, "run",
        lambda *a, **k: _completed(returncode=0, stdout="running"),
    )
    assert domains.check_domains_running(["central"], lab="L") == []


def test_check_flags_paused_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    """One symptom: VM goes into `paused (crashed)` because QEMU
    couldn't survive VMX passthrough on a nested host."""
    monkeypatch.setattr(domains.shutil, "which", lambda _name: "/usr/bin/virsh")
    monkeypatch.setattr(
        domains.subprocess, "run",
        lambda *a, **k: _completed(returncode=0, stdout="paused (crashed)"),
    )
    diagnostics = domains.check_domains_running(["central"], lab="barak-lab")
    assert [d.id for d in diagnostics] == ["runtime.apply.libvirt_domain_crashed"]
    msg = diagnostics[0].message
    assert "central" in msg
    assert "paused (crashed)" in msg
    suggestion = diagnostics[0].suggestion or ""
    # The suggestion must teach the operator both the diagnosis and the
    # full escalation ladder. Each rung is named.
    assert "kvm_intel" in suggestion
    assert "cpu_mode" in suggestion
    assert "host-model" in suggestion
    # Rung 1: cpu_features_disable strips the offending flag.
    assert "cpu_features_disable" in suggestion
    assert "vmx" in suggestion
    # Rung 2: TCG fallback for hosts where masking doesn't help.
    assert "domain_type" in suggestion
    assert "qemu" in suggestion
    # Rung 3: the bare-metal escape hatch.
    assert "kvm_intel.nested" in suggestion
    assert "playground reset barak-lab" in suggestion


def test_check_flags_shut_off_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(domains.shutil, "which", lambda _name: "/usr/bin/virsh")
    monkeypatch.setattr(
        domains.subprocess, "run",
        lambda *a, **k: _completed(returncode=0, stdout="shut off (crashed)"),
    )
    diagnostics = domains.check_domains_running(["target"], lab="L")
    assert len(diagnostics) == 1
    assert "shut off (crashed)" in diagnostics[0].message


def test_check_flags_paused_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The actual symptom on bob-lnx during the barak-deploy
    qualification on 2026-05-28: libvirt reports `paused (unknown)`
    rather than a specific crashed reason, and the user's pipeline
    silently timed out on `wait_for_lease`. Post-tofu-apply, any
    non-running state must surface a fast diagnostic with the
    cpu_mode workaround."""
    monkeypatch.setattr(domains.shutil, "which", lambda _name: "/usr/bin/virsh")
    monkeypatch.setattr(
        domains.subprocess, "run",
        lambda *a, **k: _completed(returncode=0, stdout="paused (unknown)"),
    )
    diagnostics = domains.check_domains_running(["central"], lab="L")
    assert [d.id for d in diagnostics] == ["runtime.apply.libvirt_domain_crashed"]
    assert "paused (unknown)" in diagnostics[0].message


def test_check_ignores_unknown_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A VM that virsh doesn't know about (rc != 0) is silently
    ignored — the next step downstream will surface a clearer
    diagnostic about the missing tofu/libvirt state."""
    monkeypatch.setattr(domains.shutil, "which", lambda _name: "/usr/bin/virsh")
    monkeypatch.setattr(
        domains.subprocess, "run",
        lambda *a, **k: _completed(returncode=1, stdout=""),
    )
    assert domains.check_domains_running(["central"], lab="L") == []


def test_check_handles_timeout_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd=["virsh"], timeout=10.0)

    monkeypatch.setattr(domains.shutil, "which", lambda _name: "/usr/bin/virsh")
    monkeypatch.setattr(domains.subprocess, "run", _timeout)
    # A virsh hang must not crash apply — silently skip; the
    # downstream wait-for-vms-ready will time out with its own
    # actionable diagnostic if the VM really is unreachable.
    assert domains.check_domains_running(["central"], lab="L") == []
