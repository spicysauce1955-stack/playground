"""Service layer for the local-libvirt apply / destroy lifecycle.

The CLI (`playground apply` / `destroy`) and the TUI (`a` / `d`
bindings) both invoke these functions. They take a pre-resolved
:class:`ResolvedLab` plus an :class:`EventBus`, run the multi-step
operation, and **return** the finalized :class:`OperationRun` instead
of raising. Diagnostics that prevent a step from running (missing
binaries, scheduling failures, etc.) come back alongside the run; the
caller decides how to present them.

The bus is the single observation channel. Subscribers attached by
the caller see:

- ``operation_started`` / ``operation_finished``
- ``step_started`` / ``step_finished``
- ``log_line`` for every line of streamed subprocess output

JSONL persistence is attached as a bus subscriber by ``start_run``'s
caller (e.g. ``cli/main.py`` adds a :class:`JsonlWriter` per run); the
runner itself does not assume any particular subscriber.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from playground.backend.local_libvirt.apply import (
    run_ansible_playbook,
    run_tofu_apply,
    run_tofu_destroy,
)
from playground.backend.local_libvirt.inventory import (
    fetch_vm_ips,
    render_inventory,
)
from playground.backend.local_libvirt.tfvars import render_tfvars
from playground.events import EventBus, JsonlWriter
from playground.models.diagnostic import Diagnostic
from playground.models.resolved import ResolvedLab
from playground.planner import schedule_workloads, stage_workload_files
from playground.runs import OperationRun, StepResult, finish_run, start_run


def execute_apply(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    ansible_dir: Path,
    config_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    """Run the full apply lifecycle. Never raises.

    Returns ``(run, diagnostics)``. When ``run`` is ``None``, the
    pre-flight phase rejected the apply and no infrastructure was
    touched. When ``run`` is set, its ``status`` is ``"succeeded"`` or
    ``"failed"`` and the run record on disk has the final state.
    """
    lab = resolved.lab_name

    # Pre-flight: schedule + stage. These are pure-config decisions
    # that fail before any run record is created.
    scheduled, sched_diagnostics = schedule_workloads(resolved)
    if sched_diagnostics:
        return None, sched_diagnostics

    workload_stage_dir = state_dir / "state" / "workloads" / lab
    if workload_stage_dir.exists():
        shutil.rmtree(workload_stage_dir)
    staged_workloads, stage_diagnostics = stage_workload_files(
        scheduled,
        source_base=config_dir.parent.resolve(),
        stage_dir=workload_stage_dir,
    )
    if stage_diagnostics:
        return None, stage_diagnostics

    runs_dir = state_dir / "runs"
    run, run_dir = start_run(runs_dir, "apply", lab)
    logs_dir = run_dir / "logs"
    # Attach JSONL persistence as a subscriber as soon as we have a
    # run_dir — every event from here on lands in events.jsonl.
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": "apply", "lab": lab})

    tfvars_path = state_dir / "state" / "tofu" / f"{lab}.tfvars.json"
    inventory_path = state_dir / "state" / "inventory" / f"{lab}.ini"

    steps: list[StepResult] = []

    # 1. Render tofu vars
    tfvars_path.parent.mkdir(parents=True, exist_ok=True)
    tfvars = render_tfvars(resolved)
    tfvars_path.write_text(json.dumps(tfvars, indent=2, sort_keys=True) + "\n")

    # 2. tofu apply
    bus.publish(run.run_id, "step_started", {"step": "tofu-apply"})
    tofu_step, tofu_diagnostics = run_tofu_apply(
        tofu_dir, tfvars_path.resolve(), logs_dir / "tofu-apply.log",
        bus=bus, run_id=run.run_id,
    )
    steps.append(tofu_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "tofu-apply", "exit_code": tofu_step.exit_code},
    )
    if tofu_diagnostics or tofu_step.exit_code != 0:
        return _finalize_failure(
            run, run_dir, steps, bus, tofu_diagnostics,
            "tofu apply failed; no VMs provisioned",
        )

    # 3. fetch IPs + render inventory
    vm_ips, fetch_diagnostics = fetch_vm_ips(tofu_dir)
    if fetch_diagnostics:
        return _finalize_failure(
            run, run_dir, steps, bus, fetch_diagnostics,
            "tofu apply succeeded but reading state failed; VMs are alive. "
            "Investigate, then re-run apply or destroy via "
            "`cd tofu && tofu destroy`.",
        )

    inventory_body, render_diagnostics = render_inventory(
        resolved, vm_ips, staged_workloads=staged_workloads,
    )
    if render_diagnostics:
        return _finalize_failure(
            run, run_dir, steps, bus, render_diagnostics,
            "tofu apply succeeded but inventory render failed; VMs are alive. "
            "Investigate, then re-run apply or destroy via "
            "`cd tofu && tofu destroy`.",
        )
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(inventory_body)

    # 4. ansible-playbook
    bus.publish(run.run_id, "step_started", {"step": "ansible-playbook"})
    ansible_step, ansible_diagnostics = run_ansible_playbook(
        ansible_dir / "site.yml",
        inventory_path.resolve(),
        logs_dir / "ansible.log",
        cwd=ansible_dir.parent.resolve(),
        bus=bus, run_id=run.run_id,
    )
    steps.append(ansible_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "ansible-playbook", "exit_code": ansible_step.exit_code},
    )
    if ansible_diagnostics or ansible_step.exit_code != 0:
        return _finalize_failure(
            run, run_dir, steps, bus, ansible_diagnostics,
            "VMs were provisioned but Ansible configuration failed. "
            "Ansible roles are idempotent: re-run apply after fixing the "
            "failure, or tear down via destroy.",
        )

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=f"applied lab {lab!r} ({len(resolved.vms)} VMs)",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, []


def execute_destroy(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Run `tofu destroy`. Never raises. Always returns an OperationRun."""
    lab = resolved.lab_name
    runs_dir = state_dir / "runs"
    run, run_dir = start_run(runs_dir, "destroy", lab)
    logs_dir = run_dir / "logs"
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": "destroy", "lab": lab})

    tfvars_path = state_dir / "state" / "tofu" / f"{lab}.tfvars.json"
    tfvars_path.parent.mkdir(parents=True, exist_ok=True)
    tfvars = render_tfvars(resolved)
    tfvars_path.write_text(json.dumps(tfvars, indent=2, sort_keys=True) + "\n")

    steps: list[StepResult] = []
    bus.publish(run.run_id, "step_started", {"step": "tofu-destroy"})
    tofu_step, tofu_diagnostics = run_tofu_destroy(
        tofu_dir, tfvars_path.resolve(), logs_dir / "tofu-destroy.log",
        bus=bus, run_id=run.run_id,
    )
    steps.append(tofu_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "tofu-destroy", "exit_code": tofu_step.exit_code},
    )
    if tofu_diagnostics or tofu_step.exit_code != 0:
        finished, _ = _finalize_failure(
            run, run_dir, steps, bus, tofu_diagnostics,
            "tofu destroy failed; some resources may remain. Inspect "
            "tofu state with `cd tofu && tofu state list` and retry.",
        )
        return finished, tofu_diagnostics

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=f"destroyed lab {lab!r}",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, []


def _finalize_failure(
    run: OperationRun,
    run_dir: Path,
    steps: list[StepResult],
    bus: EventBus,
    diagnostics: list[Diagnostic],
    summary: str,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Single failure-finalize entry point — publishes the closing
    ``operation_finished`` event and persists the failed run."""
    finished = finish_run(
        run, run_dir, status="failed", steps=steps, summary=summary,
    )
    bus.publish(run.run_id, "operation_finished", {"status": "failed"})
    return finished, diagnostics


__all__ = ["execute_apply", "execute_destroy"]
