"""Tests for the vbox cloud-init renderer + seed ISO builder."""

from __future__ import annotations

from pathlib import Path

from playground.backend.local_vbox import cloudinit
from playground.backend.local_vbox.cloudinit import (
    _colonize,
    build_seed_iso,
    needs_network_config,
    render_meta_data,
    render_network_config,
    render_user_data,
)
from playground.backend.local_vbox.plan import VboxNic, VboxVmPlan


def _vm(nics: list[VboxNic]) -> VboxVmPlan:
    return VboxVmPlan(
        vbox_name="lab-node1",
        lab_vm_name="node1",
        role="docker-host",
        vcpu=2,
        memory_mb=4096,
        disk_gb=20,
        ssh_user="ubuntu",
        ssh_public_key="ssh-ed25519 KEYBODY user@host",
        hostname="node1",
        fqdn="node1.lab.lab",
        nics=nics,
    )


NAT = VboxNic(index=1, kind="nat", mac="080027aabbcc")
INTNET = VboxNic(
    index=2, kind="intnet", mac="080027ddeeff",
    intnet_name="lab-net", static_ip_cidr="10.50.0.10/24",
)


def test_user_data_has_cloud_config_header_and_key() -> None:
    body = render_user_data(_vm([NAT]))
    assert body.startswith("#cloud-config\n")
    assert "ssh-ed25519 KEYBODY user@host" in body
    assert "node1" in body
    assert "node1.lab.lab" in body
    assert "ssh_pwauth" in body


def test_meta_data_has_instance_id_and_hostname() -> None:
    body = render_meta_data(_vm([NAT]))
    assert "instance-id: lab-node1" in body
    assert "local-hostname: node1" in body


def test_network_config_dhcp_nat_static_intnet() -> None:
    body = render_network_config(_vm([NAT, INTNET]))
    assert "version: 2" in body
    # NAT NIC -> dhcp4 true; intnet NIC -> static address.
    assert "08:00:27:aa:bb:cc" in body
    assert "08:00:27:dd:ee:ff" in body
    assert "10.50.0.10/24" in body
    assert "dhcp4: true" in body
    # no set-name (we match by MAC only)
    assert "set-name" not in body


def test_needs_network_config() -> None:
    assert needs_network_config(_vm([NAT, INTNET])) is True
    assert needs_network_config(_vm([NAT])) is False


def test_colonize() -> None:
    assert _colonize("080027AABBCC") == "08:00:27:aa:bb:cc"


def test_build_seed_iso_missing_tool(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cloudinit.shutil, "which", lambda _name: None)
    iso, diags = build_seed_iso(_vm([NAT]), out_dir=tmp_path)
    assert iso is None
    assert [d.id for d in diags] == ["runtime.vbox.iso_tool_missing"]


def test_build_seed_iso_nat_only_omits_network_config(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, list[str]] = {}

    def fake_which(name: str):
        return "/usr/bin/genisoimage" if name == "genisoimage" else None

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        return _OK()

    monkeypatch.setattr(cloudinit.shutil, "which", fake_which)
    monkeypatch.setattr(cloudinit.subprocess, "run", fake_run)

    iso, diags = build_seed_iso(_vm([NAT]), out_dir=tmp_path)
    assert diags == []
    assert iso is not None
    work = tmp_path / "lab-node1"
    assert (work / "user-data").is_file()
    assert (work / "meta-data").is_file()
    # NAT-only: no network-config written or fed to the ISO tool.
    assert not (work / "network-config").exists()
    assert not any("network-config" in str(a) for a in captured["cmd"])
    assert "cidata" in captured["cmd"]


def test_build_seed_iso_includes_network_config_for_intnet(monkeypatch, tmp_path: Path) -> None:
    def fake_which(name: str):
        return "/usr/bin/genisoimage" if name == "genisoimage" else None

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        return _OK()

    monkeypatch.setattr(cloudinit.shutil, "which", fake_which)
    monkeypatch.setattr(cloudinit.subprocess, "run", fake_run)

    iso, diags = build_seed_iso(_vm([NAT, INTNET]), out_dir=tmp_path)
    assert diags == []
    assert iso is not None
    assert (tmp_path / "lab-node1" / "network-config").is_file()
    assert any("network-config" in str(a) for a in captured["cmd"])
