"""Tests for the VBoxManage wrapper layer (subprocess mocked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from playground.backend.local_vbox import vbox
from playground.backend.local_vbox.plan import VboxNic, VboxVmPlan


def _vm() -> VboxVmPlan:
    return VboxVmPlan(
        vbox_name="lab-node1",
        lab_vm_name="node1",
        role="docker-host",
        vcpu=2,
        memory_mb=4096,
        disk_gb=20,
        ssh_user="ubuntu",
        ssh_public_key="KEY",
        hostname="node1",
        fqdn="node1.lab.lab",
        nics=[
            VboxNic(index=1, kind="nat", mac="080027aabbcc"),
            VboxNic(index=2, kind="intnet", mac="080027ddeeff",
                    intnet_name="lab-net", static_ip_cidr="10.50.0.10/24"),
        ],
    )


def test_list_vms_parses_quoted_names(monkeypatch) -> None:
    monkeypatch.setattr(vbox, "vboxmanage_available", lambda: True)

    class _R:
        stdout = '"alpha" {uuid-1}\n"beta-vm" {uuid-2}\n'

    monkeypatch.setattr(vbox.subprocess, "run", lambda *a, **k: _R())
    assert vbox.list_vms() == ["alpha", "beta-vm"]


def test_pick_free_ports_returns_distinct_free_ports() -> None:
    ports = vbox.pick_free_ports(3, start=27000)
    assert len(ports) == 3
    assert len(set(ports)) == 3
    assert all(27000 <= p for p in ports)


def test_create_vm_runs_full_sequence_and_sets_natpf(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **_kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vbox, "run_vbox", fake_run)
    logs: list[str] = []
    diags = vbox.create_vm(
        _vm(),
        base_vdi=tmp_path / "base.vdi",
        seed_iso=tmp_path / "seed.iso",
        disk_path=tmp_path / "node1.vdi",
        ssh_host_port=2222,
        log=logs.append,
    )
    assert diags == []
    verbs = [c[0] for c in calls]
    assert verbs[0] == "createvm"
    assert "clonemedium" in verbs
    assert "startvm" == verbs[-1]
    # NAT port-forward was configured.
    flat = [tok for c in calls for tok in c]
    assert "--natpf1" in flat
    assert any("ssh,tcp,127.0.0.1,2222,,22" == tok for tok in flat)
    # intnet NIC2 wired to the lab network.
    assert "--intnet2" in flat


def test_create_vm_stops_at_first_failure(monkeypatch, tmp_path: Path) -> None:
    def fake_run(args, **_kw):
        rc = 1 if args[0] == "modifyvm" else 0
        return subprocess.CompletedProcess(args, returncode=rc, stdout="", stderr="boom")

    monkeypatch.setattr(vbox, "run_vbox", fake_run)
    diags = vbox.create_vm(
        _vm(),
        base_vdi=tmp_path / "base.vdi",
        seed_iso=tmp_path / "seed.iso",
        disk_path=tmp_path / "node1.vdi",
        ssh_host_port=2222,
        log=lambda _s: None,
    )
    assert len(diags) == 1
    assert diags[0].id == "runtime.vbox.create_failed"
    assert "modifyvm" in diags[0].message


def test_nat_ssh_port_parses_forwarding(monkeypatch) -> None:
    monkeypatch.setattr(vbox, "vboxmanage_available", lambda: True)

    class _R:
        stdout = (
            'name="lab-node1"\n'
            'Forwarding(0)="ssh,tcp,127.0.0.1,2247,,22"\n'
            'memory=4096\n'
        )

    monkeypatch.setattr(vbox.subprocess, "run", lambda *a, **k: _R())
    assert vbox.nat_ssh_port("lab-node1") == 2247


def test_nat_ssh_port_none_when_no_rule(monkeypatch) -> None:
    monkeypatch.setattr(vbox, "vboxmanage_available", lambda: True)

    class _R:
        stdout = 'name="lab-node1"\nmemory=4096\n'

    monkeypatch.setattr(vbox.subprocess, "run", lambda *a, **k: _R())
    assert vbox.nat_ssh_port("lab-node1") is None


def test_vm_running_uses_runningvms(monkeypatch) -> None:
    monkeypatch.setattr(vbox, "list_running_vms", lambda: ["lab-node1"])
    assert vbox.vm_running("lab-node1") is True
    assert vbox.vm_running("other") is False


def test_destroy_vm_skips_absent(monkeypatch) -> None:
    monkeypatch.setattr(vbox, "vboxmanage_available", lambda: True)
    monkeypatch.setattr(vbox, "list_vms", lambda: [])  # not registered
    called: list[list[str]] = []
    monkeypatch.setattr(vbox, "run_vbox", lambda args, **k: called.append(args))
    logs: list[str] = []
    vbox.destroy_vm("lab-node1", log=logs.append)
    assert called == []  # nothing run for an absent VM
    assert any("not registered" in line for line in logs)
