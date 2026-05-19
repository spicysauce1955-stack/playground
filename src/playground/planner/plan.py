"""Pure plan rendering from :class:`ResolvedLab`.

Today's planner is read-only and stateless — it answers "what would
``playground apply`` do on a fresh deploy?" without consulting any
backend or ``.playground/`` state. Every action is therefore ``create``.
When state observation lands in a follow-up slice, the same :class:`Plan`
shape will carry ``update`` / ``delete`` / ``no_op`` verbs and the
caller's choice of "current state" snapshot.

Public surface: :func:`render_plan`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from playground.models.base import StrictModel
from playground.models.diagnostic import Diagnostic
from playground.models.kinds import Budget
from playground.models.resolved import ResolvedLab

ActionVerb = Literal["create", "update", "delete", "no_op"]
"""Today only ``create`` is emitted; the others are reserved for the
state-observation slice."""

ResourceType = Literal["network", "vm", "workload"]


class PlanAction(StrictModel):
    """One unit of work the planner intends to do."""

    verb: ActionVerb
    resource_type: ResourceType
    name: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class PlanBudget(StrictModel):
    """Aggregate resource impact of the plan against the lab budget.

    ``limits`` is non-optional today because :class:`ResolvedLab` always
    carries a :class:`Budget` (lab-declared or inherited from Defaults).
    """

    vcpu: int
    memory_mb: int
    disk_gb: int
    vms: int
    containers: int
    fits: bool
    limits: Budget


class Plan(StrictModel):
    """A backend-neutral preview of what `apply` would do.

    Inherits ``extra="forbid"`` and ``frozen=True`` from :class:`StrictModel`.
    """

    lab_name: str
    backend: str
    offline: bool
    actions: list[PlanAction]
    budget: PlanBudget
    warnings: list[Diagnostic] = Field(default_factory=list)


def render_plan(
    resolved: ResolvedLab,
    *,
    warnings: list[Diagnostic] | None = None,
) -> Plan:
    """Produce a :class:`Plan` for ``resolved``.

    ``warnings`` carries forward any non-error diagnostics from validation
    (e.g. ``config.backend.per_vm_resources_unsupported``). The planner
    does not invent diagnostics — it only forwards what the caller passes.

    Pure function: no I/O, no subprocess.
    """
    actions: list[PlanAction] = [
        *_network_actions(resolved),
        *_vm_actions(resolved),
        *_workload_actions(resolved),
    ]
    return Plan(
        lab_name=resolved.lab_name,
        backend=resolved.backend,
        offline=resolved.offline,
        actions=actions,
        budget=_budget(resolved),
        warnings=list(warnings or []),
    )


# ---------------------------------------------------------------------------
# Action builders
# ---------------------------------------------------------------------------


def _network_actions(resolved: ResolvedLab) -> list[PlanAction]:
    return [
        PlanAction(
            verb="create",
            resource_type="network",
            name=net.name,
            summary=f"{net.intent} network on {net.cidr}",
            details={
                "intent": net.intent,
                "cidr": net.cidr,
                "internet_access": net.internet_access,
                "dns_enabled": net.dns_enabled,
            },
        )
        for net in resolved.networks
    ]


def _vm_actions(resolved: ResolvedLab) -> list[PlanAction]:
    return [
        PlanAction(
            verb="create",
            resource_type="vm",
            name=vm.name,
            summary=(
                f"{vm.role} on {vm.image} "
                f"({vm.vcpu} vCPU / {vm.memory_mb} MiB / {vm.disk_gb} GiB)"
            ),
            details={
                "role": vm.role,
                "image": vm.image,
                "vcpu": vm.vcpu,
                "memory_mb": vm.memory_mb,
                "disk_gb": vm.disk_gb,
                "networks": list(vm.networks),
                "ssh_user": vm.ssh.user,
                "tags": list(vm.tags),
                "routing": vm.routing.model_dump() if vm.routing else None,
            },
        )
        for vm in resolved.vms
    ]


def _workload_actions(resolved: ResolvedLab) -> list[PlanAction]:
    actions: list[PlanAction] = []
    for wl in resolved.workloads:
        placement = wl.placement
        if placement.target_vm is not None:
            target = f"vm:{placement.target_vm}"
        elif placement.target_role is not None:
            target = f"role:{placement.target_role}"
        elif placement.target_tag is not None:
            target = f"tag:{placement.target_tag}"
        else:
            target = "auto"
        actions.append(
            PlanAction(
                verb="create",
                resource_type="workload",
                name=wl.name,
                summary=f"{wl.type} -> {target}",
                details={
                    "type": wl.type,
                    "source": wl.source,
                    "placement": placement.model_dump(exclude_none=True),
                    "networks": list(wl.networks),
                },
            )
        )
    return actions


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def _budget(resolved: ResolvedLab) -> PlanBudget:
    totals = {
        "vcpu": sum(vm.vcpu for vm in resolved.vms),
        "memory_mb": sum(vm.memory_mb for vm in resolved.vms),
        "disk_gb": sum(vm.disk_gb for vm in resolved.vms),
        "vms": len(resolved.vms),
        "containers": len(resolved.workloads),
    }
    limits = resolved.budget
    fits = (
        totals["vcpu"] <= limits.max_vcpu
        and totals["memory_mb"] <= limits.max_memory_mb
        and totals["disk_gb"] <= limits.max_disk_gb
        and totals["vms"] <= limits.max_vms
        and totals["containers"] <= limits.max_containers
    )
    return PlanBudget(
        vcpu=totals["vcpu"],
        memory_mb=totals["memory_mb"],
        disk_gb=totals["disk_gb"],
        vms=totals["vms"],
        containers=totals["containers"],
        fits=fits,
        limits=limits,
    )


__all__ = ["Plan", "PlanAction", "PlanBudget", "render_plan"]
