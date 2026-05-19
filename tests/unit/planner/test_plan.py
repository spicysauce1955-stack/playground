"""Tests for the read-only planner."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.planner import render_plan

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_generic_infra():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


def test_render_plan_metadata_matches_resolved(resolved_generic_infra) -> None:
    plan = render_plan(resolved_generic_infra)

    assert plan.lab_name == "generic-infra"
    assert plan.backend == "local-libvirt"
    assert plan.offline is False


def test_render_plan_emits_create_actions_per_resource(
    resolved_generic_infra,
) -> None:
    plan = render_plan(resolved_generic_infra)

    by_type = {(a.resource_type, a.name) for a in plan.actions}
    assert ("network", "edge") in by_type
    assert ("network", "lab-private") in by_type
    assert ("network", "routed-a") in by_type
    assert ("vm", "node1") in by_type
    assert ("vm", "docker1") in by_type
    assert ("vm", "router1") in by_type
    assert ("workload", "demo-compose") in by_type
    # Today, every action is `create`.
    assert {a.verb for a in plan.actions} == {"create"}


def test_render_plan_vm_details_carry_resolved_values(resolved_generic_infra) -> None:
    plan = render_plan(resolved_generic_infra)
    docker = next(a for a in plan.actions if a.resource_type == "vm" and a.name == "docker1")

    # docker1 has explicit per-VM resources in the committed lab.
    assert docker.details["vcpu"] == 2
    assert docker.details["memory_mb"] == 4096
    assert docker.details["disk_gb"] == 40
    assert docker.details["role"] == "docker-host"
    assert docker.details["image"] == "ubuntu-noble"
    assert sorted(docker.details["networks"]) == ["edge", "lab-private"]


def test_render_plan_workload_target_serialized_in_summary(
    resolved_generic_infra,
) -> None:
    plan = render_plan(resolved_generic_infra)
    workload = next(a for a in plan.actions if a.resource_type == "workload")

    # The committed lab targets a role.
    assert workload.summary == "compose -> role:docker-host"
    assert workload.details["placement"] == {"target_role": "docker-host"}


def test_render_plan_budget_totals_match_lab(resolved_generic_infra) -> None:
    plan = render_plan(resolved_generic_infra)

    assert plan.budget.vms == 3
    assert plan.budget.vcpu == 1 + 2 + 1  # node1 + docker1 + router1
    assert plan.budget.memory_mb == 2048 + 4096 + 2048
    assert plan.budget.disk_gb == 20 + 40 + 20
    assert plan.budget.containers == 1
    assert plan.budget.fits is True
    assert plan.budget.limits is not None
    assert plan.budget.limits.mode == "permissive"


def test_render_plan_budget_fits_false_when_totals_exceed_limits(
    resolved_generic_infra,
) -> None:
    # Replace the lab budget with one nobody can fit into.
    tight = resolved_generic_infra.budget.model_copy(
        update={"max_vcpu": 1, "max_vms": 1, "max_memory_mb": 128, "max_disk_gb": 1}
    )
    tightened = resolved_generic_infra.model_copy(update={"budget": tight})

    plan = render_plan(tightened)

    assert plan.budget.fits is False


def test_render_plan_carries_warnings_forward(resolved_generic_infra) -> None:
    fake = Diagnostic(
        id="config.backend.per_vm_resources_unsupported",
        severity="warning",
        message="warning from validation",
        source=SourceLocation(path="config/labs/generic-infra.yaml"),
    )

    plan = render_plan(resolved_generic_infra, warnings=[fake])

    assert plan.warnings == [fake]


def test_render_plan_invents_no_diagnostics_on_its_own(resolved_generic_infra) -> None:
    # No warnings passed in -> no warnings in plan.
    plan = render_plan(resolved_generic_infra)

    assert plan.warnings == []


def test_render_plan_empty_lab(resolved_generic_infra) -> None:
    empty = resolved_generic_infra.model_copy(
        update={"vms": [], "networks": [], "workloads": []}
    )

    plan = render_plan(empty)

    assert plan.actions == []
    assert plan.budget.vms == 0
    assert plan.budget.vcpu == 0
    assert plan.budget.fits is True


def test_render_plan_orders_actions_networks_then_vms_then_workloads(
    resolved_generic_infra,
) -> None:
    # The order is load-bearing for the human format and any JSON
    # consumer that doesn't re-sort. Pin it so a refactor can't silently
    # shuffle.
    plan = render_plan(resolved_generic_infra)
    types = [a.resource_type for a in plan.actions]
    n = len(resolved_generic_infra.networks)
    v = len(resolved_generic_infra.vms)
    w = len(resolved_generic_infra.workloads)
    assert types[:n] == ["network"] * n
    assert types[n : n + v] == ["vm"] * v
    assert types[n + v : n + v + w] == ["workload"] * w


def test_render_plan_only_emits_create_today(resolved_generic_infra) -> None:
    # §5 ships with create-only verbs. ActionVerb reserves
    # update/delete/no_op for §5b (state observation). When that lands,
    # this assertion needs to relax — until then, treat any other verb
    # as a regression.
    plan = render_plan(resolved_generic_infra)
    assert {a.verb for a in plan.actions} == {"create"}


def test_plan_is_frozen(resolved_generic_infra) -> None:
    # Plan instances are immutable — handed to operators / future
    # adapters as a stable snapshot.
    plan = render_plan(resolved_generic_infra)
    with pytest.raises(ValidationError):
        plan.lab_name = "tampered"  # type: ignore[misc]


def test_render_plan_handles_offline_lab(resolved_generic_infra) -> None:
    offline = resolved_generic_infra.model_copy(update={"offline": True})

    plan = render_plan(offline)

    assert plan.offline is True


def test_render_plan_workload_auto_placement(resolved_generic_infra) -> None:
    # Replace the committed workload (target_role) with one using auto
    # placement — the planner should emit a summary that reads "<type>
    # -> auto" rather than role:/vm:/tag:.
    original = resolved_generic_infra.workloads[0]
    auto = original.model_copy(
        update={
            "placement": original.placement.model_copy(
                update={"target_role": None, "auto": True}
            ),
        }
    )
    lab = resolved_generic_infra.model_copy(update={"workloads": [auto]})

    plan = render_plan(lab)

    workload = next(a for a in plan.actions if a.resource_type == "workload")
    assert workload.summary == "compose -> auto"


def test_render_plan_workload_target_vm(resolved_generic_infra) -> None:
    original = resolved_generic_infra.workloads[0]
    pinned = original.model_copy(
        update={
            "placement": original.placement.model_copy(
                update={"target_role": None, "target_vm": "docker1"}
            ),
        }
    )
    lab = resolved_generic_infra.model_copy(update={"workloads": [pinned]})

    plan = render_plan(lab)

    workload = next(a for a in plan.actions if a.resource_type == "workload")
    assert workload.summary == "compose -> vm:docker1"
