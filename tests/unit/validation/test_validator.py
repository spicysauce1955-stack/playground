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


def test_missing_defaults_is_validation_error(committed_load: LoadedConfig) -> None:
    committed_load.defaults = None

    diagnostics = validate(committed_load)

    matching = [d for d in diagnostics if d.id == "config.required.defaults_missing"]
    assert len(matching) == 1
    assert matching[0].severity == "error"


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


def test_workload_target_role_must_match_lab_vm(committed_load: LoadedConfig) -> None:
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: bad-placement-role
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
          workloads:
            - name: misplaced
              type: compose
              source: ./compose/demo.yaml
              placement:
                target_role: docker-host
              networks: [net]
        """
    )
    committed_load.labs[bad.metadata.name] = bad

    diagnostics = validate(committed_load)

    matching = [
        d for d in diagnostics if d.id == "config.reference.unknown_workload_target"
    ]
    assert len(matching) == 1
    assert matching[0].key_path == "spec.workloads[0].placement.target_role"


def test_workload_target_role_accepts_inherited_role(
    committed_load: LoadedConfig,
) -> None:
    # generic-infra contains a docker-host VM, which extends generic-node.
    # A workload targeting `generic-node` must match it via the extends chain.
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: inherited-target
        spec:
          backend: local-libvirt
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: dh
              role: docker-host
              networks: [net]
          workloads:
            - name: parented
              type: compose
              source: ./compose/demo.yaml
              placement:
                target_role: generic-node
              networks: [net]
        """
    )
    committed_load.labs[bad.metadata.name] = bad

    diagnostics = validate(committed_load)

    assert not [
        d for d in diagnostics if d.id == "config.reference.unknown_workload_target"
    ]


def test_workload_target_tag_must_match_some_vm(
    committed_load: LoadedConfig,
) -> None:
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: bad-placement-tag
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
              tags: [keep]
          workloads:
            - name: misplaced
              type: compose
              source: ./compose/demo.yaml
              placement:
                target_tag: missing
              networks: [net]
        """
    )
    committed_load.labs[bad.metadata.name] = bad

    diagnostics = validate(committed_load)

    matching = [
        d for d in diagnostics if d.id == "config.reference.unknown_workload_target"
    ]
    assert len(matching) == 1
    assert matching[0].key_path == "spec.workloads[0].placement.target_tag"


def test_workload_placement_auto_emits_no_diagnostic(
    committed_load: LoadedConfig,
) -> None:
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: placement-auto
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
          workloads:
            - name: scheduled
              type: compose
              source: ./compose/demo.yaml
              placement:
                auto: true
              networks: [net]
        """
    )
    committed_load.labs[bad.metadata.name] = bad

    diagnostics = validate(committed_load)

    assert not [
        d for d in diagnostics if d.id == "config.reference.unknown_workload_target"
    ]


def test_workload_target_vm_must_exist_in_lab(committed_load: LoadedConfig) -> None:
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: bad-placement-vm
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
          workloads:
            - name: misplaced
              type: compose
              source: ./compose/demo.yaml
              placement:
                target_vm: vm-missing
              networks: [net]
        """
    )
    committed_load.labs[bad.metadata.name] = bad

    diagnostics = validate(committed_load)

    matching = [
        d for d in diagnostics if d.id == "config.reference.unknown_workload_target"
    ]
    assert len(matching) == 1
    assert matching[0].key_path == "spec.workloads[0].placement.target_vm"


def test_budget_exceeded_is_error_in_strict_mode(committed_load: LoadedConfig) -> None:
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: strict-budget
        spec:
          backend: local-libvirt
          budget:
            mode: strict
            max_vcpu: 1
            max_memory_mb: 512
            max_disk_gb: 10
            max_vms: 1
            max_containers: 0
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: vm1
              role: generic-node
              networks: [net]
            - name: vm2
              role: generic-node
              networks: [net]
        """
    )
    committed_load.labs[bad.metadata.name] = bad

    diagnostics = validate(committed_load)

    matching = [d for d in diagnostics if d.id == "config.budget.exceeded"]
    assert len(matching) == 1
    assert matching[0].severity == "error"
    assert "vms 2 > 1" in matching[0].message


def test_budget_inherits_from_defaults_when_lab_omits_it(
    committed_load: LoadedConfig,
) -> None:
    # Lab.spec.budget is None, so Defaults.spec.budget applies.
    # Defaults has max_vms=8; we exceed it.
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: inherits-budget
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
            - name: vm2
              role: generic-node
              networks: [net]
            - name: vm3
              role: generic-node
              networks: [net]
            - name: vm4
              role: generic-node
              networks: [net]
            - name: vm5
              role: generic-node
              networks: [net]
            - name: vm6
              role: generic-node
              networks: [net]
            - name: vm7
              role: generic-node
              networks: [net]
            - name: vm8
              role: generic-node
              networks: [net]
            - name: vm9
              role: generic-node
              networks: [net]
        """
    )
    committed_load.labs[bad.metadata.name] = bad

    diagnostics = validate(committed_load)

    matching = [d for d in diagnostics if d.id == "config.budget.exceeded"]
    assert len(matching) == 1
    # Defaults.budget.mode is permissive → warning.
    assert matching[0].severity == "warning"
    assert "vms 9 > 8" in matching[0].message


def test_budget_exceeded_warns_in_permissive_mode(committed_load: LoadedConfig) -> None:
    bad = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: permissive-budget
        spec:
          backend: local-libvirt
          budget:
            mode: permissive
            max_vcpu: 1
            max_memory_mb: 512
            max_disk_gb: 10
            max_vms: 1
            max_containers: 0
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: vm1
              role: generic-node
              networks: [net]
            - name: vm2
              role: generic-node
              networks: [net]
        """
    )
    committed_load.labs[bad.metadata.name] = bad

    diagnostics = validate(committed_load)

    matching = [d for d in diagnostics if d.id == "config.budget.exceeded"]
    assert len(matching) == 1
    assert matching[0].severity == "warning"


def test_offline_lab_errors_when_image_local_path_missing(
    committed_load: LoadedConfig,
) -> None:
    # Drop local_path on the only image the committed labs use.
    assert committed_load.artifacts is not None
    image = committed_load.artifacts.spec.vm_images["ubuntu-noble"]
    committed_load.artifacts.spec.vm_images["ubuntu-noble"] = image.model_copy(
        update={"local_path": None}
    )
    # Flip the committed lab into offline mode.
    lab = committed_load.labs["generic-infra"]
    committed_load.labs["generic-infra"] = lab.model_copy(
        update={"spec": lab.spec.model_copy(update={"offline": True})}
    )

    diagnostics = validate(committed_load)

    matching = [d for d in diagnostics if d.id == "config.artifact.offline_missing"]
    # generic-infra has three VMs that all resolve to ubuntu-noble — one
    # diagnostic per VM.
    assert len(matching) == 3
    assert all(d.severity == "error" for d in matching)
    assert all("ubuntu-noble" in d.message for d in matching)


def test_offline_check_is_silent_when_local_path_is_set(
    committed_load: LoadedConfig,
) -> None:
    lab = committed_load.labs["generic-infra"]
    committed_load.labs["generic-infra"] = lab.model_copy(
        update={"spec": lab.spec.model_copy(update={"offline": True})}
    )

    diagnostics = validate(committed_load)

    assert not [d for d in diagnostics if d.id == "config.artifact.offline_missing"]


def test_offline_defaults_cascade_into_labs(committed_load: LoadedConfig) -> None:
    # Drop local_path so an offline lab would fail, then enable offline on
    # Defaults rather than the Lab — the diagnostic must still surface.
    assert committed_load.artifacts is not None
    image = committed_load.artifacts.spec.vm_images["ubuntu-noble"]
    committed_load.artifacts.spec.vm_images["ubuntu-noble"] = image.model_copy(
        update={"local_path": None}
    )
    defaults = committed_load.defaults
    assert defaults is not None
    committed_load.defaults = defaults.model_copy(
        update={"spec": defaults.spec.model_copy(update={"offline": True})}
    )

    diagnostics = validate(committed_load)

    matching = [d for d in diagnostics if d.id == "config.artifact.offline_missing"]
    assert matching, "offline=true on Defaults should still trigger the check"


def test_offline_missing_fires_alongside_unknown_image(
    committed_load: LoadedConfig,
) -> None:
    # An image not declared in ArtifactSources surfaces both
    # unknown_image (role-level) and offline_missing (lab-level): one
    # diagnostic per VM for the latter, plus the role-level
    # unknown_image. They report different things and shouldn't be
    # collapsed.
    assert committed_load.artifacts is not None
    committed_load.artifacts.spec.vm_images.pop("ubuntu-noble")
    lab = committed_load.labs["generic-infra"]
    committed_load.labs["generic-infra"] = lab.model_copy(
        update={"spec": lab.spec.model_copy(update={"offline": True})}
    )

    diagnostics = validate(committed_load)

    offline = [d for d in diagnostics if d.id == "config.artifact.offline_missing"]
    assert len(offline) == 3
    assert all("not declared in ArtifactSources" in d.message for d in offline)


def test_validator_uses_loader_source_path_when_filename_differs(
    tmp_path: Path,
) -> None:
    lab_path = tmp_path / "labs" / "actual-file.yaml"
    lab_path.parent.mkdir(parents=True)
    lab_path.write_text(
        dedent(
            """
            apiVersion: playground/v1
            kind: Lab
            metadata:
              name: semantic-name
            spec:
              backend: missing-provider
              networks: []
              vms: []
            """
        ).lstrip("\n")
    )
    loaded, load_diagnostics = load_config(tmp_path)
    assert load_diagnostics == []

    diagnostics = validate(loaded)

    matching = [d for d in diagnostics if d.id == "config.reference.unknown_provider"]
    assert len(matching) == 1
    assert matching[0].source is not None
    assert matching[0].source.path.endswith("labs/actual-file.yaml")


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
