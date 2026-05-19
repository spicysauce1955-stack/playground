"""Tests for the workload scheduler."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.planner.scheduling import schedule_workloads

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
