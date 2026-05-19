"""Tests for the local-libvirt tfvars renderer."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.local_libvirt.tfvars import render_tfvars
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_generic_infra():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


def test_render_tfvars_emits_lab_vm_names_in_declaration_order(
    resolved_generic_infra,
) -> None:
    # Order must match lab.spec.vms — tofu's libvirt_domain count.index
    # depends on the list position for disk + cloud-init pairing.
    payload = render_tfvars(resolved_generic_infra)

    assert payload["vm_names"] == ["node1", "docker1", "router1"]


def test_render_tfvars_emits_networks_from_lab(resolved_generic_infra) -> None:
    payload = render_tfvars(resolved_generic_infra)

    assert payload["networks"] == [
        {"name": "edge", "cidr": "10.20.10.0/24"},
        {"name": "lab-private", "cidr": "10.20.20.0/24"},
        {"name": "routed-a", "cidr": "10.20.30.0/24"},
    ]


def test_render_tfvars_emits_vm_networks_per_vm(resolved_generic_infra) -> None:
    payload = render_tfvars(resolved_generic_infra)

    assert payload["vm_networks"] == {
        "node1": ["lab-private"],
        "docker1": ["edge", "lab-private"],
        "router1": ["edge", "lab-private", "routed-a"],
    }


def test_render_tfvars_omits_vm_network_ips_when_no_pins(
    resolved_generic_infra,
) -> None:
    # generic-infra uses the legacy networks: [name, ...] shape — no
    # per-VM IPs pinned anywhere. The renderer should omit the key so
    # tofu's default empty map applies.
    payload = render_tfvars(resolved_generic_infra)

    assert "vm_network_ips" not in payload


def test_render_tfvars_emits_vm_network_ips_when_lab_pins(
    resolved_generic_infra,
) -> None:
    # Mutate the resolved lab to pin IPs on docker1.
    docker = next(vm for vm in resolved_generic_infra.vms if vm.name == "docker1")
    pinned = docker.model_copy(
        update={"network_ips": {"edge": "10.20.10.42", "lab-private": "10.20.20.42"}}
    )
    others = [vm for vm in resolved_generic_infra.vms if vm.name != "docker1"]
    lab = resolved_generic_infra.model_copy(update={"vms": [*others, pinned]})

    payload = render_tfvars(lab)

    assert payload["vm_network_ips"] == {
        "docker1": {"edge": "10.20.10.42", "lab-private": "10.20.20.42"},
    }


def test_render_tfvars_handles_empty_lab(resolved_generic_infra) -> None:
    empty = resolved_generic_infra.model_copy(update={"vms": [], "networks": []})

    assert render_tfvars(empty) == {"vm_names": []}


def test_render_tfvars_is_pure_no_diagnostics_returned(
    resolved_generic_infra,
) -> None:
    # The backend-capability warning lives in validator.py now; render_tfvars
    # is a pure data transformer.
    payload = render_tfvars(resolved_generic_infra)

    assert isinstance(payload, dict)
    # vm_names is always emitted; the lab-derived networks/vm_networks
    # may or may not be present depending on the lab. Today's
    # generic-infra produces all three.
    assert "vm_names" in payload
