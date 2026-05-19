"""Map :class:`ResolvedWorkload` instances onto target VMs.

The planner already resolved placement *intent* (target_role / target_vm /
target_tag / auto). Scheduling turns intent into a concrete VM assignment
so backend adapters can render per-host inputs (Ansible vars, Docker
compose files staged on disk, etc.).

Today's scheduler is single-instance: one workload runs on exactly one
target VM. Replicated workloads (Compose/Swarm) will land in §8b/§8c
with richer placement semantics (manager/worker split, stack-wide
scheduling).
"""

from __future__ import annotations

from typing import Any

from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab, ResolvedVm, ResolvedWorkload


def schedule_workloads(
    resolved: ResolvedLab,
) -> tuple[dict[str, list[ResolvedWorkload]], list[Diagnostic]]:
    """Return ``{vm_name: [workloads scheduled on it]}`` plus diagnostics.

    Each workload picks **one** VM:
    - ``target_vm``: exact match
    - ``target_role``: first VM whose ``role`` matches (lab declaration order)
    - ``target_tag``: first VM whose ``tags`` include the tag
    - ``auto``: first VM whose ``capabilities['docker']`` is truthy

    Workloads with no matching VM produce
    ``config.workload.no_target`` diagnostics and are skipped (not
    placed). The lab's workloads section may be empty, in which case
    every VM gets an empty list.
    """
    schedule: dict[str, list[ResolvedWorkload]] = {vm.name: [] for vm in resolved.vms}
    diagnostics: list[Diagnostic] = []
    source = SourceLocation(path=f"config/labs/{resolved.lab_name}.yaml")

    for idx, workload in enumerate(resolved.workloads):
        target = _pick_target_vm(workload, resolved.vms)
        if target is None:
            diagnostics.append(
                Diagnostic(
                    id="config.workload.no_target",
                    severity="error",
                    message=(
                        f"workload {workload.name!r} in lab "
                        f"{resolved.lab_name!r} has no VM matching its "
                        "placement; nothing was scheduled"
                    ),
                    source=source,
                    key_path=f"spec.workloads[{idx}].placement",
                    suggestion=(
                        "adjust the workload placement or add a VM that "
                        "matches the role/tag/capability"
                    ),
                )
            )
            continue
        schedule[target.name].append(workload)

    return schedule, diagnostics


def _pick_target_vm(
    workload: ResolvedWorkload,
    vms: list[ResolvedVm],
) -> ResolvedVm | None:
    placement = workload.placement
    if placement.target_vm is not None:
        return next((vm for vm in vms if vm.name == placement.target_vm), None)
    if placement.target_role is not None:
        # Match against the full role ancestry so workloads targeting a
        # base role (e.g. `generic-node`) accept VMs whose leaf role
        # extends it (e.g. `docker-host`). Mirrors validator semantics.
        return next(
            (vm for vm in vms if placement.target_role in vm.roles), None
        )
    if placement.target_tag is not None:
        return next(
            (vm for vm in vms if placement.target_tag in vm.tags), None
        )
    # auto: any docker-capable VM
    return next(
        (vm for vm in vms if vm.capabilities.get("docker")), None
    )


def workload_to_ansible_payload(workload: ResolvedWorkload) -> dict[str, Any]:
    """Serialize a workload for the workload_* ansible roles.

    Backend-neutral projection — lives here so future
    ``workload_compose`` / ``workload_swarm`` slices can reuse it.
    The ``networks`` field is intentionally **omitted** today because
    today's `workload_container` role doesn't attach Docker networks
    (mapping lab-level network names to docker networks is a follow-up).
    """
    return {
        "name": workload.name,
        "type": workload.type,
        "source": workload.source,
        "ports": list(workload.ports),
        "volumes": list(workload.volumes),
        "environment": dict(workload.environment),
    }


__all__ = ["schedule_workloads", "workload_to_ansible_payload"]
