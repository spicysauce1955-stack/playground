"""Tests for the workload scheduler."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.planner.scheduling import (
    assign_swarm_membership,
    schedule_workloads,
    stage_workload_files,
    workload_to_ansible_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_generic_infra():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


def test_schedule_places_role_targeted_workload_on_matching_vm(
    resolved_generic_infra,
) -> None:
    schedule, diagnostics = schedule_workloads(resolved_generic_infra)

    assert diagnostics == []
    # generic-infra has one workload (demo-compose) targeting target_role
    # docker-host. The matching VM is docker1.
    assert [wl.name for wl in schedule["docker1"]] == ["demo-compose"]
    assert schedule["node1"] == []
    assert schedule["router1"] == []


def test_schedule_resolves_target_vm_directly(resolved_generic_infra) -> None:
    original = resolved_generic_infra.workloads[0]
    pinned = original.model_copy(
        update={
            "placement": original.placement.model_copy(
                update={"target_role": None, "target_vm": "router1"}
            ),
        }
    )
    lab = resolved_generic_infra.model_copy(update={"workloads": [pinned]})

    schedule, diagnostics = schedule_workloads(lab)

    assert diagnostics == []
    assert [wl.name for wl in schedule["router1"]] == [pinned.name]
    assert schedule["docker1"] == []


def test_schedule_resolves_target_tag(resolved_generic_infra) -> None:
    # Tag docker1, then point the workload at that tag.
    original_vms = resolved_generic_infra.vms
    tagged_docker = original_vms[1].model_copy(update={"tags": ["edge"]})
    new_vms = [original_vms[0], tagged_docker, original_vms[2]]
    original = resolved_generic_infra.workloads[0]
    by_tag = original.model_copy(
        update={
            "placement": original.placement.model_copy(
                update={"target_role": None, "target_tag": "edge"}
            ),
        }
    )
    lab = resolved_generic_infra.model_copy(
        update={"vms": new_vms, "workloads": [by_tag]}
    )

    schedule, diagnostics = schedule_workloads(lab)

    assert diagnostics == []
    assert [wl.name for wl in schedule["docker1"]] == [by_tag.name]


def test_schedule_auto_picks_docker_capable_vm(resolved_generic_infra) -> None:
    original = resolved_generic_infra.workloads[0]
    auto = original.model_copy(
        update={
            "placement": original.placement.model_copy(
                update={"target_role": None, "auto": True}
            ),
        }
    )
    lab = resolved_generic_infra.model_copy(update={"workloads": [auto]})

    schedule, diagnostics = schedule_workloads(lab)

    assert diagnostics == []
    # docker1 is the only docker-capable VM (capabilities.docker = true)
    assert [wl.name for wl in schedule["docker1"]] == [auto.name]


def test_schedule_matches_ancestor_role_not_just_leaf(
    resolved_generic_infra,
) -> None:
    # generic-infra's docker1 has role `docker-host` which extends
    # `generic-node`. A workload targeting `generic-node` must accept
    # docker1 because the validator does — otherwise the same config
    # passes validate and fails schedule.
    original = resolved_generic_infra.workloads[0]
    ancestor_target = original.model_copy(
        update={
            "placement": original.placement.model_copy(
                update={"target_role": "generic-node"}
            ),
        }
    )
    lab = resolved_generic_infra.model_copy(update={"workloads": [ancestor_target]})

    schedule, diagnostics = schedule_workloads(lab)

    assert diagnostics == []
    # First VM whose ancestry includes generic-node is node1 (declaration order).
    assert [wl.name for wl in schedule["node1"]] == [ancestor_target.name]


def test_schedule_auto_emits_no_target_when_no_docker_vm(
    resolved_generic_infra,
) -> None:
    # Strip docker capability from every VM, then schedule auto.
    plain_vms = [
        vm.model_copy(update={"capabilities": {}}) for vm in resolved_generic_infra.vms
    ]
    original = resolved_generic_infra.workloads[0]
    auto = original.model_copy(
        update={
            "placement": original.placement.model_copy(
                update={"target_role": None, "auto": True}
            ),
        }
    )
    lab = resolved_generic_infra.model_copy(
        update={"vms": plain_vms, "workloads": [auto]}
    )

    schedule, diagnostics = schedule_workloads(lab)

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.workload.no_target"


def test_schedule_handles_multiple_workloads_on_same_vm(
    resolved_generic_infra,
) -> None:
    original = resolved_generic_infra.workloads[0]
    twin = original.model_copy(update={"name": "demo-compose-2"})
    lab = resolved_generic_infra.model_copy(update={"workloads": [original, twin]})

    schedule, diagnostics = schedule_workloads(lab)

    assert diagnostics == []
    # Both target_role=docker-host → both land on docker1.
    assert [wl.name for wl in schedule["docker1"]] == [original.name, twin.name]


def test_stage_workload_files_copies_compose_source(
    resolved_generic_infra, tmp_path: Path
) -> None:
    # Build a fake compose source under a tmp source_base.
    source_base = tmp_path / "src"
    (source_base / "compose").mkdir(parents=True)
    src = source_base / "compose" / "demo.yaml"
    src.write_text("services:\n  web:\n    image: nginx:alpine\n")

    scheduled, _ = schedule_workloads(resolved_generic_infra)
    stage_dir = tmp_path / "stage"

    staged, diagnostics = stage_workload_files(
        scheduled, source_base=source_base, stage_dir=stage_dir
    )

    assert diagnostics == []
    # demo-compose scheduled on docker1 → staged under stage/docker1/
    docker_staged = staged["docker1"]["demo-compose"]
    assert docker_staged.exists()
    assert docker_staged.read_text() == src.read_text()
    # Non-compose VMs have empty maps.
    assert staged["node1"] == {}
    assert staged["router1"] == {}


def test_stage_workload_files_records_absolute_path(
    resolved_generic_infra, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-8: the recorded staged_source must be ABSOLUTE even when stage_dir
    is relative (the default state_dir is the relative `.playground`). Ansible's
    `copy` resolves a relative src against the role `files/` dirs, not the
    controller CWD, so a relative staged path is never found on the target."""
    source_base = tmp_path / "src"
    (source_base / "compose").mkdir(parents=True)
    (source_base / "compose" / "demo.yaml").write_text("services: {}\n")

    # Run with cwd=tmp_path and a *relative* stage_dir, mirroring the real
    # `state_dir=Path(".playground")` default.
    monkeypatch.chdir(tmp_path)
    scheduled, _ = schedule_workloads(resolved_generic_infra)

    staged, diagnostics = stage_workload_files(
        scheduled, source_base=source_base, stage_dir=Path(".playground/stage")
    )

    assert diagnostics == []
    docker_staged = staged["docker1"]["demo-compose"]
    assert docker_staged.is_absolute(), (
        f"staged_source must be absolute for Ansible copy; got {docker_staged}"
    )
    assert docker_staged.exists()


def test_stage_workload_files_reports_missing_source(
    resolved_generic_infra, tmp_path: Path
) -> None:
    scheduled, _ = schedule_workloads(resolved_generic_infra)
    # source_base exists but has no compose/demo.yaml
    staged, diagnostics = stage_workload_files(
        scheduled, source_base=tmp_path, stage_dir=tmp_path / "stage"
    )

    assert staged["docker1"] == {}
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.workload.source_missing"


def test_swarm_membership_assigns_first_docker_vm_as_manager(
    resolved_generic_infra,
) -> None:
    # Flip the committed compose workload to swarm so membership engages.
    original = resolved_generic_infra.workloads[0]
    swarm_wl = original.model_copy(update={"type": "swarm"})
    lab = resolved_generic_infra.model_copy(update={"workloads": [swarm_wl]})
    scheduled, _ = schedule_workloads(lab)

    membership, diagnostics = assign_swarm_membership(scheduled, lab.vms)

    assert diagnostics == []
    # generic-infra has one docker-capable VM (docker1) → it's the manager.
    assert membership["docker1"] == "manager"
    # node1 and router1 lack docker capability → no swarm membership.
    assert membership["node1"] == "none"
    assert membership["router1"] == "none"


def test_swarm_membership_promotes_additional_docker_vms_as_workers(
    resolved_generic_infra,
) -> None:
    # Add a second docker-capable VM ahead of docker1 in the list so we
    # can verify worker assignment for the non-first docker VMs.
    docker1 = resolved_generic_infra.vms[1]
    docker2 = docker1.model_copy(update={"name": "docker2"})
    new_vms = [docker1, docker2, *resolved_generic_infra.vms[2:]]
    original = resolved_generic_infra.workloads[0]
    swarm_wl = original.model_copy(update={"type": "swarm"})
    lab = resolved_generic_infra.model_copy(
        update={"vms": new_vms, "workloads": [swarm_wl]}
    )
    scheduled, _ = schedule_workloads(lab)

    membership, _ = assign_swarm_membership(scheduled, lab.vms)

    assert membership["docker1"] == "manager"
    assert membership["docker2"] == "worker"


def test_swarm_membership_silent_when_no_swarm_workloads(
    resolved_generic_infra,
) -> None:
    scheduled, _ = schedule_workloads(resolved_generic_infra)

    membership, diagnostics = assign_swarm_membership(scheduled, resolved_generic_infra.vms)

    assert diagnostics == []
    assert set(membership.values()) == {"none"}


def test_swarm_membership_errors_when_no_docker_capable_vm(
    resolved_generic_infra,
) -> None:
    # Strip docker capability from every VM, then add a swarm workload.
    plain_vms = [
        vm.model_copy(update={"capabilities": {}}) for vm in resolved_generic_infra.vms
    ]
    original = resolved_generic_infra.workloads[0]
    # Target docker-host so schedule_workloads' role match still succeeds —
    # we want to isolate the swarm-membership error, not the no-target one.
    # But none of the VMs have docker-host role after the strip... so use
    # auto placement which falls back to first docker-capable (also fails).
    # We need scheduling to *succeed* so the swarm-check fires. Easiest:
    # give one VM a target_vm pin and strip docker on it.
    swarm_wl = original.model_copy(
        update={
            "type": "swarm",
            "placement": original.placement.model_copy(
                update={"target_role": None, "target_vm": "node1"}
            ),
        }
    )
    lab = resolved_generic_infra.model_copy(
        update={"vms": plain_vms, "workloads": [swarm_wl]}
    )
    scheduled, _ = schedule_workloads(lab)

    _, diagnostics = assign_swarm_membership(scheduled, lab.vms)

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.workload.swarm_needs_docker_host"


def test_workload_payload_includes_staged_source_when_provided() -> None:
    # workload_to_ansible_payload is straightforward; test the
    # staged_source pass-through.
    class _Wl:  # minimal duck-type
        name = "x"
        type = "compose"
        source = "./compose/x.yaml"
        ports: list[str] = []
        volumes: list[str] = []
        environment: dict[str, str] = {}

    payload = workload_to_ansible_payload(_Wl(), staged_source=Path("/abs/x.yaml"))
    assert payload["staged_source"] == "/abs/x.yaml"

    bare = workload_to_ansible_payload(_Wl())
    assert "staged_source" not in bare


def test_schedule_emits_no_target_diagnostic_when_no_vm_matches(
    resolved_generic_infra,
) -> None:
    # target_role nobody has
    original = resolved_generic_infra.workloads[0]
    orphan = original.model_copy(
        update={
            "placement": original.placement.model_copy(
                update={"target_role": "phantom-role"}
            ),
        }
    )
    lab = resolved_generic_infra.model_copy(update={"workloads": [orphan]})

    schedule, diagnostics = schedule_workloads(lab)

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.workload.no_target"
    assert diagnostics[0].severity == "error"
    # No VM got the orphaned workload.
    assert all(not wls for wls in schedule.values())
