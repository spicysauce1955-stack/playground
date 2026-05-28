"""Tests for the pure vbox provisioning planner."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.local_vbox.plan import (
    VBOX_OUI,
    _mac_for,
    build_vbox_plan,
)
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"

KEY = "ssh-ed25519 AAAATESTKEY user@host"


@pytest.fixture
def resolved_vbox_smoke():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "vbox-smoke")


@pytest.fixture
def resolved_generic_infra():
    loaded, _ = load_config(CONFIG_DIR)
    return resolve_lab(loaded, "generic-infra")


def test_smoke_lab_single_vm_nat_plus_intnet(resolved_vbox_smoke) -> None:
    plan = build_vbox_plan(resolved_vbox_smoke, ssh_public_key=KEY)
    assert plan.lab_name == "vbox-smoke"
    assert plan.image_key == "ubuntu-noble"
    assert plan.image_source.startswith("https://cloud-images.ubuntu.com/")
    assert len(plan.vms) == 1

    vm = plan.vms[0]
    assert vm.vbox_name == "vbox-smoke-node1"
    assert vm.lab_vm_name == "node1"
    assert vm.role == "docker-host"
    assert vm.ssh_user == "ubuntu"
    assert vm.ssh_public_key == KEY
    assert vm.hostname == "node1"
    assert vm.fqdn == "node1.vbox-smoke.lab"

    # NIC1 NAT, NIC2 intnet with a static IP from the lab CIDR.
    assert [n.kind for n in vm.nics] == ["nat", "intnet"]
    nat, intnet = vm.nics
    assert nat.index == 1 and nat.intnet_name is None and nat.static_ip_cidr is None
    assert intnet.index == 2
    assert intnet.intnet_name == "vbox-smoke-lab-net"
    assert intnet.static_ip_cidr == "10.50.0.10/24"


def test_vbox_names_are_lab_namespaced(resolved_generic_infra) -> None:
    plan = build_vbox_plan(resolved_generic_infra, ssh_public_key=KEY)
    names = {vm.vbox_name for vm in plan.vms}
    assert names == {
        "generic-infra-node1",
        "generic-infra-docker1",
        "generic-infra-router1",
    }


def test_auto_ip_assignment_is_collision_free(resolved_generic_infra) -> None:
    plan = build_vbox_plan(resolved_generic_infra, ssh_public_key=KEY)
    # Collect every static IP assigned across all intnet NICs.
    ips = [
        nic.static_ip_cidr
        for vm in plan.vms
        for nic in vm.nics
        if nic.kind == "intnet" and nic.static_ip_cidr
    ]
    assert len(ips) == len(set(ips)), f"duplicate static IPs: {ips}"
    # Auto-assignment starts at .10 within each network CIDR.
    assert any(ip.endswith(".10/24") for ip in ips)


def test_macs_are_deterministic_and_vbox_oui() -> None:
    a = _mac_for("node1", 1)
    b = _mac_for("node1", 1)
    c = _mac_for("node1", 2)
    assert a == b  # stable
    assert a != c  # per-NIC
    assert a.startswith(VBOX_OUI)
    assert len(a) == 12
    int(a, 16)  # valid hex


def test_macs_unique_across_nics(resolved_generic_infra) -> None:
    plan = build_vbox_plan(resolved_generic_infra, ssh_public_key=KEY)
    macs = [nic.mac for vm in plan.vms for nic in vm.nics]
    assert len(macs) == len(set(macs))
