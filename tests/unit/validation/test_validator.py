"""Tests for the cross-reference validator."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from ruamel.yaml import YAML

from playground.config.loader import LoadedConfig, load_config
from playground.models.kinds import Lab, VmRole, parse_resource
from playground.validation.validator import validate

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"
ANSIBLE_ROLES_DIR = REPO_ROOT / "ansible" / "roles"

_yaml = YAML(typ="safe")


@pytest.fixture
def committed_load() -> LoadedConfig:
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return loaded


def test_committed_config_validates_with_no_errors(committed_load: LoadedConfig) -> None:
    diagnostics = validate(committed_load, ansible_roles_dir=None)
    errors = [d for d in diagnostics if d.severity == "error"]
    assert errors == [], f"expected no errors, got: {errors}"


def test_committed_config_warns_about_missing_router_ansible_role(
    committed_load: LoadedConfig,
) -> None:
    # The missing router ansible role surfaces as a warning, not an error.
    diagnostics = validate(committed_load, ansible_roles_dir=ANSIBLE_ROLES_DIR)
    warnings = [d for d in diagnostics if d.id == "config.reference.ansible_role_missing"]
    assert any("router" in d.message for d in warnings)
    errors = [d for d in diagnostics if d.severity == "error"]
    assert errors == []


def _yaml_to_lab(text: str) -> Lab:
    raw = _yaml.load(dedent(text).lstrip("\n"))
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    return lab


def test_unknown_role_reference(committed_load: LoadedConfig) -> None:
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: bad-role
        spec:
          backend: local-libvirt
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: lonely
              role: does-not-exist
              networks: [net]
        """
    )
    committed_load.labs[bad.metadata.name] = bad
    diagnostics = validate(committed_load)
    matching = [d for d in diagnostics if d.id == "config.reference.unknown_role"]
    assert len(matching) == 1
    assert matching[0].key_path == "spec.vms[0].role"


def test_unknown_network_reference(committed_load: LoadedConfig) -> None:
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: bad-network
        spec:
          backend: local-libvirt
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: vm1
              role: generic-node
              networks: [phantom]
        """
    )
    committed_load.labs[bad.metadata.name] = bad
    diagnostics = validate(committed_load)
    assert any(d.id == "config.reference.unknown_network" for d in diagnostics)


def test_unknown_command_reference(committed_load: LoadedConfig) -> None:
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: bad-command
        spec:
          backend: local-libvirt
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: vm1
              role: generic-node
              networks: [net]
          commands:
            enabled: [check-docker, does-not-exist]
        """
    )
    committed_load.labs[bad.metadata.name] = bad
    diagnostics = validate(committed_load)
    matching = [d for d in diagnostics if d.id == "config.reference.unknown_command"]
    assert len(matching) == 1
    assert "does-not-exist" in matching[0].message


def test_unknown_provider(committed_load: LoadedConfig) -> None:
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: bad-provider
        spec:
          backend: cloud-vmware
          networks: []
          vms: []
        """
    )
    committed_load.labs[bad.metadata.name] = bad
    diagnostics = validate(committed_load)
    matching = [d for d in diagnostics if d.id == "config.reference.unknown_provider"]
    assert len(matching) == 1


def test_unknown_network_profile(committed_load: LoadedConfig) -> None:
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: bad-profile
        spec:
          backend: local-libvirt
          networks:
            - name: net
              profile: phantom
              cidr: 10.99.0.0/24
          vms: []
        """
    )
    committed_load.labs[bad.metadata.name] = bad
    diagnostics = validate(committed_load)
    assert any(d.id == "config.reference.unknown_network_profile" for d in diagnostics)


def test_role_inheritance_cycle(committed_load: LoadedConfig) -> None:
    def _role(name: str, extends: str | None) -> VmRole:
        return parse_resource(
            {
                "apiVersion": "playground/v1",
                "kind": "VmRole",
                "metadata": {"name": name},
                "spec": {"extends": extends, "provisioners": []},
            }
        )  # type: ignore[return-value]

    committed_load.roles["a-cycle"] = _role("a-cycle", "b-cycle")  # type: ignore[assignment]
    committed_load.roles["b-cycle"] = _role("b-cycle", "a-cycle")  # type: ignore[assignment]
    diagnostics = validate(committed_load)
    cycles = [d for d in diagnostics if d.id == "config.role.inheritance_cycle"]
    assert cycles
    assert "a-cycle" in cycles[0].message and "b-cycle" in cycles[0].message


def test_role_unknown_extends(committed_load: LoadedConfig) -> None:
    role = parse_resource(
        {
            "apiVersion": "playground/v1",
            "kind": "VmRole",
            "metadata": {"name": "orphan"},
            "spec": {"extends": "nonexistent", "provisioners": []},
        }
    )
    committed_load.roles["orphan"] = role  # type: ignore[assignment]
    diagnostics = validate(committed_load)
    matching = [d for d in diagnostics if d.id == "config.role.unknown_extends"]
    assert len(matching) == 1
    assert "orphan" in matching[0].message


def test_unknown_image_reference_against_artifact_sources(
    committed_load: LoadedConfig,
) -> None:
    rogue_role = parse_resource(
        {
            "apiVersion": "playground/v1",
            "kind": "VmRole",
            "metadata": {"name": "exotic"},
            "spec": {"image": "alpine-mystery", "provisioners": []},
        }
    )
    committed_load.roles["exotic"] = rogue_role  # type: ignore[assignment]
    diagnostics = validate(committed_load)
    matching = [d for d in diagnostics if d.id == "config.reference.unknown_image"]
    assert len(matching) == 1
    assert "alpine-mystery" in matching[0].message
