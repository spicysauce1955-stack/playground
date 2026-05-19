"""Tests for the on-disk YAML kind models.

The most valuable test in this file is ``test_every_committed_yaml_parses``
— it walks ``config/`` and parses every committed file with
``parse_resource``. If a YAML field diverges from the contract, this
test fails before the resolver ever sees the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from playground.models.kinds import (
    KNOWN_KINDS,
    Budget,
    Lab,
    Resources,
    TargetSelector,
    VmRole,
    WorkloadPlacement,
    parse_resource,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

_yaml = YAML(typ="safe")


def _load_yaml(path: Path) -> dict[str, Any]:
    return _yaml.load(path.read_text())


# ---------------------------------------------------------------------------
# Live integration: parse every committed YAML
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("yaml_path", sorted(CONFIG_DIR.rglob("*.yaml")), ids=str)
def test_every_committed_yaml_parses(yaml_path: Path) -> None:
    raw = _load_yaml(yaml_path)
    resource = parse_resource(raw)
    assert resource.metadata.name
    assert resource.kind in KNOWN_KINDS


def test_lab_generic_infra_has_expected_shape() -> None:
    raw = _load_yaml(CONFIG_DIR / "labs" / "generic-infra.yaml")
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    assert lab.metadata.name == "generic-infra"
    assert {v.name for v in lab.spec.vms} == {"node1", "docker1", "router1"}
    assert {n.name for n in lab.spec.networks} == {"edge", "lab-private", "routed-a"}
    assert lab.spec.commands.enabled == ["check-docker", "ping-network"]


def test_role_inheritance_field_present() -> None:
    raw = _load_yaml(CONFIG_DIR / "roles" / "docker-host.yaml")
    role = parse_resource(raw)
    assert isinstance(role, VmRole)
    assert role.spec.extends == "generic-node"
    assert role.spec.capabilities == {"docker": True, "compose": True, "swarm": True}


# ---------------------------------------------------------------------------
# Negative tests
# ---------------------------------------------------------------------------


def test_resources_rejects_negative_vcpu() -> None:
    with pytest.raises(ValidationError):
        Resources(vcpu=0, memory_mb=512, disk_gb=10)


def test_budget_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        Budget(
            mode="lax",  # type: ignore[arg-type]
            max_vcpu=1,
            max_memory_mb=1024,
            max_disk_gb=10,
            max_vms=1,
            max_containers=0,
        )


def test_target_selector_requires_exactly_one() -> None:
    with pytest.raises(ValidationError):
        TargetSelector()
    with pytest.raises(ValidationError):
        TargetSelector(role="docker-host", vm="docker1")


def test_target_selector_any_must_be_true_when_set() -> None:
    with pytest.raises(ValidationError):
        TargetSelector(any=False)


def test_target_selector_accepts_each_form() -> None:
    assert TargetSelector(role="x").role == "x"
    assert TargetSelector(vm="x").vm == "x"
    assert TargetSelector(tag="x").tag == "x"
    assert TargetSelector(any=True).any is True


def test_workload_placement_requires_exactly_one() -> None:
    with pytest.raises(ValidationError):
        WorkloadPlacement()
    with pytest.raises(ValidationError):
        WorkloadPlacement(target_role="x", target_vm="y")


def test_lab_rejects_duplicate_vm_names() -> None:
    raw = _load_yaml(CONFIG_DIR / "labs" / "generic-infra.yaml")
    raw["spec"]["vms"].append(
        {"name": "node1", "role": "generic-node", "networks": ["lab-private"]}
    )
    with pytest.raises(ValidationError) as exc:
        parse_resource(raw)
    assert "duplicate names" in str(exc.value)


def test_lab_vm_networks_accepts_legacy_string_shape() -> None:
    """The committed `generic-infra` lab uses the legacy `networks:
    [name, name]` shape — it must keep parsing unchanged."""
    raw = _load_yaml(CONFIG_DIR / "labs" / "generic-infra.yaml")
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    # Every VM normalizes to LabVmNetwork objects with no IP pinned.
    for vm in lab.spec.vms:
        assert all(net.ip is None for net in vm.networks)
    docker_nets = next(vm for vm in lab.spec.vms if vm.name == "docker1").networks
    assert [n.name for n in docker_nets] == ["edge", "lab-private"]


def test_lab_vm_networks_accepts_object_shape_with_ip() -> None:
    raw = _load_yaml(CONFIG_DIR / "labs" / "generic-infra.yaml")
    raw["spec"]["networks"].append(
        {"name": "deploy-net", "profile": "isolated", "cidr": "10.20.40.0/24"}
    )
    raw["spec"]["vms"][0]["networks"] = [
        {"name": "lab-private"},  # no ip
        {"name": "deploy-net", "ip": "10.20.40.42"},  # pinned
    ]
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    nets = lab.spec.vms[0].networks
    assert nets[0].name == "lab-private" and nets[0].ip is None
    assert nets[1].name == "deploy-net" and nets[1].ip == "10.20.40.42"


def test_lab_vm_extra_hosts_defaults_to_empty_list() -> None:
    raw = _load_yaml(CONFIG_DIR / "labs" / "generic-infra.yaml")
    lab = parse_resource(raw)
    assert all(vm.extra_hosts == [] for vm in lab.spec.vms)


def test_lab_vm_extra_hosts_parses_when_set() -> None:
    raw = _load_yaml(CONFIG_DIR / "labs" / "generic-infra.yaml")
    raw["spec"]["vms"][0]["extra_hosts"] = ["10.20.40.21 target", "10.20.40.22 other"]
    lab = parse_resource(raw)
    assert lab.spec.vms[0].extra_hosts == ["10.20.40.21 target", "10.20.40.22 other"]


def test_lab_rejects_duplicate_network_names() -> None:
    raw = _load_yaml(CONFIG_DIR / "labs" / "generic-infra.yaml")
    raw["spec"]["networks"].append({"name": "edge", "profile": "nat", "cidr": "10.99.0.0/24"})
    with pytest.raises(ValidationError) as exc:
        parse_resource(raw)
    assert "duplicate names" in str(exc.value)


def test_provider_config_driver_must_match_name() -> None:
    raw = _load_yaml(CONFIG_DIR / "providers" / "local-libvirt.yaml")
    raw["spec"]["driver"] = "vmware"
    with pytest.raises(ValidationError) as exc:
        parse_resource(raw)
    assert "metadata.name" in str(exc.value)


def test_parse_resource_rejects_unknown_kind() -> None:
    raw = {
        "apiVersion": "playground/v1",
        "kind": "DefinitelyNotAKind",
        "metadata": {"name": "x"},
        "spec": {},
    }
    with pytest.raises(ValueError) as exc:
        parse_resource(raw)
    assert "unknown kind" in str(exc.value)


def test_network_profile_rejects_unknown_intent() -> None:
    raw = _load_yaml(CONFIG_DIR / "networks" / "nat.yaml")
    raw["spec"]["intent"] = "mesh"
    with pytest.raises(ValidationError):
        parse_resource(raw)


def test_command_preset_rejects_zero_timeout() -> None:
    raw = _load_yaml(CONFIG_DIR / "commands" / "check-docker.yaml")
    raw["spec"]["timeout_seconds"] = 0
    with pytest.raises(ValidationError):
        parse_resource(raw)
