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

import shutil
from pathlib import Path
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


def workload_to_ansible_payload(
    workload: ResolvedWorkload,
    *,
    staged_source: Path | None = None,
) -> dict[str, Any]:
    """Serialize a workload for the workload_* ansible roles.

    Backend-neutral projection — lives here so the
    ``workload_container`` / ``workload_compose`` / ``workload_swarm``
    roles each consume the same shape. For compose/swarm workloads,
    ``staged_source`` (the absolute path of the file
    :func:`stage_workload_files` wrote on the controller) is included so
    the role can ``ansible.builtin.copy`` it to the target VM.

    The lab-level ``networks`` field is intentionally **omitted** today
    — mapping lab network names to docker networks is a follow-up; the
    field is preserved on :class:`ResolvedWorkload` so the future slice
    can wire it in.
    """
    payload: dict[str, Any] = {
        "name": workload.name,
        "type": workload.type,
        "source": workload.source,
        "ports": list(workload.ports),
        "volumes": list(workload.volumes),
        "environment": dict(workload.environment),
    }
    if staged_source is not None:
        payload["staged_source"] = str(staged_source)
    return payload


def stage_workload_files(
    scheduled: dict[str, list[ResolvedWorkload]],
    *,
    source_base: Path,
    stage_dir: Path,
) -> tuple[dict[str, dict[str, Path]], list[Diagnostic]]:
    """Copy compose/swarm source files into ``stage_dir/<vm>/<workload>.yml``.

    Convention: ``workload.source`` is a path interpreted relative to
    ``source_base`` (typically the repo root — the directory above
    ``config/``). ``container`` workloads don't have a file to stage
    and are skipped silently. Missing source files emit
    ``config.workload.source_missing``.

    Returns a mapping ``{vm_name: {workload_name: staged_path}}`` so the
    caller (inventory renderer) can thread staged paths into the
    Ansible payload for each VM.
    """
    diagnostics: list[Diagnostic] = []
    staged: dict[str, dict[str, Path]] = {vm: {} for vm in scheduled}

    for vm_name, workloads in scheduled.items():
        for workload in workloads:
            if workload.type == "container":
                continue
            src = (source_base / workload.source).resolve()
            if not src.is_file():
                diagnostics.append(
                    Diagnostic(
                        id="config.workload.source_missing",
                        severity="error",
                        message=(
                            f"workload {workload.name!r} declares "
                            f"source {workload.source!r} but the file "
                            f"does not exist at {src}"
                        ),
                        source=SourceLocation(path=str(src)),
                        suggestion=(
                            f"create {src} (relative paths are resolved "
                            f"against {source_base})"
                        ),
                    )
                )
                continue
            # Resolve to an ABSOLUTE path: this string is handed to the
            # workload_compose/swarm roles as the `copy` task's `src`, and
            # Ansible resolves a *relative* src against the role/play
            # `files/` search dirs — not the controller CWD — so a relative
            # `.playground/state/...` (the default state_dir is relative)
            # is never found and the stage step fails (BUG-8).
            dest = (stage_dir / vm_name / f"{workload.name}{src.suffix}").resolve()
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            staged[vm_name][workload.name] = dest

    return staged, diagnostics


def assign_swarm_membership(
    scheduled: dict[str, list[ResolvedWorkload]],
    vms: list[ResolvedVm],
) -> tuple[dict[str, str], list[Diagnostic]]:
    """Decide each VM's role in the lab's Swarm cluster.

    Today's model: there is at most one Swarm per lab. When the lab
    has any ``type: swarm`` workload:

    - The first docker-capable VM (lab declaration order) becomes the
      Swarm **manager**. Stack workloads land on it.
    - Every other docker-capable VM becomes a **worker**. Workers
      contribute capacity but don't host stack-deploy directives.
    - VMs without ``capabilities['docker']`` are tagged ``"none"``.

    Per ``docs/product/requirements.md`` §5.7 the model promises
    "hybrid automatic/explicit manager-worker assignment". Today only
    automatic is implemented; explicit overrides (a future
    ``LabVm.swarm_role`` field or workload-level pin) layer on top of
    this function without changing its signature.

    Emits ``config.workload.swarm_needs_docker_host`` if a swarm
    workload is present but no VM is docker-capable.
    """
    has_swarm = any(
        wl.type == "swarm" for workloads in scheduled.values() for wl in workloads
    )
    membership: dict[str, str] = {vm.name: "none" for vm in vms}
    diagnostics: list[Diagnostic] = []
    if not has_swarm:
        return membership, diagnostics

    docker_vms = [vm for vm in vms if vm.capabilities.get("docker")]
    if not docker_vms:
        diagnostics.append(
            Diagnostic(
                id="config.workload.swarm_needs_docker_host",
                severity="error",
                message=(
                    "lab declares swarm workloads but no VM advertises "
                    "capabilities.docker=true; cannot init a swarm"
                ),
                source=SourceLocation(path="config/labs/"),
                suggestion=(
                    "give one of the lab VMs the docker-host role, or "
                    "remove the swarm workload"
                ),
            )
        )
        return membership, diagnostics

    membership[docker_vms[0].name] = "manager"
    for vm in docker_vms[1:]:
        membership[vm.name] = "worker"
    return membership, diagnostics


__all__ = [
    "assign_swarm_membership",
    "schedule_workloads",
    "stage_workload_files",
    "workload_to_ansible_payload",
]
