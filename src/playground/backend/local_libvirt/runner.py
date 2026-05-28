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
from playground.backend.local_libvirt.domains import check_domains_running
from playground.backend.local_libvirt.inventory import (
    fetch_vm_ips,
    render_inventory,
)
from playground.backend.local_libvirt.scrub import scrub_lab
from playground.backend.local_libvirt.tfvars import render_tfvars
from playground.backend.local_libvirt.verify import verify_lab
from playground.backend.local_libvirt.wait import VmTarget, wait_for_vms_ready
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
    # Probe libvirt domain states whether tofu succeeded or failed.
    # When tofu fails, this gives a far more actionable diagnostic than
    # "tofu apply failed" — the canonical failure mode is QEMU pausing /
    # killing the guest at startup (VMX passthrough on nested-virt
    # hosts), which tofu sees as a `wait_for_lease` timeout but is
    # actually a guest-side crash. When tofu succeeds, the same check
    # catches a guest that booted, ran for a moment, then died — which
    # would otherwise burn the full ~5-min SSH timeout downstream.
    crash_diagnostics = check_domains_running(
        [vm.name for vm in resolved.vms], lab=lab,
    )

    if tofu_diagnostics or tofu_step.exit_code != 0:
        # Crash diagnostics, when present, replace the generic
        # "tofu apply failed" summary: they explain the real cause
        # and link to the cpu_mode workaround.
        if crash_diagnostics:
            return _finalize_failure(
                run, run_dir, steps, bus,
                list(tofu_diagnostics) + crash_diagnostics,
                (
                    f"libvirt domains are not running post-apply — "
                    f"see the libvirt_domain_crashed diagnostic for "
                    f"the cpu_mode workaround, then `playground reset "
                    f"{lab}` and re-apply."
                ),
            )
        return _finalize_failure(
            run, run_dir, steps, bus, tofu_diagnostics,
            "tofu apply failed; no VMs provisioned",
        )

    if crash_diagnostics:
        return _finalize_failure(
            run, run_dir, steps, bus, crash_diagnostics,
            (
                f"libvirt domains crashed at startup; recover with "
                f"`playground reset {lab}` after applying the cpu_mode "
                "workaround in the diagnostic above."
            ),
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

    # 4. wait-for-vms-ready: gate the handoff to ansible on SSH being
    # up AND cloud-init being done. Without this, ansible races
    # cloud-init's apt lock and hits "Connection refused" before sshd
    # is listening — both manifest as confusing "ansible failed"
    # errors that are actually timing races, not provisioning bugs.
    ssh_by_vm = {vm.name: vm.ssh.user for vm in resolved.vms}
    targets = [
        VmTarget(name=name, ip=ip, ssh_user=ssh_by_vm.get(name, "ubuntu"))
        for name, ip in vm_ips.items()
    ]
    bus.publish(run.run_id, "step_started", {"step": "wait-for-vms-ready"})
    wait_step, wait_diagnostics = wait_for_vms_ready(
        targets=targets,
        log_path=logs_dir / "wait-for-vms-ready.log",
        bus=bus,
        run_id=run.run_id,
    )
    steps.append(wait_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "wait-for-vms-ready", "exit_code": wait_step.exit_code},
    )
    if wait_step.exit_code != 0:
        return _finalize_failure(
            run, run_dir, steps, bus, wait_diagnostics,
            "VMs were provisioned but did not come up in time for ansible. "
            "Inspect cloud-init on each VM via `virsh console <vm>` then "
            "re-run apply.",
        )

    # 5. ansible-playbook
    # `ansible_cfg` is wired explicitly because cwd is the repo root,
    # not ansible_dir — so Ansible's auto-discovery of `./ansible.cfg`
    # would miss `ansible/ansible.cfg`. Passing None when the file is
    # absent matches Ansible's default behavior (use built-in defaults).
    ansible_cfg = ansible_dir / "ansible.cfg"
    bus.publish(run.run_id, "step_started", {"step": "ansible-playbook"})
    ansible_step, ansible_diagnostics = run_ansible_playbook(
        ansible_dir / "site.yml",
        inventory_path.resolve(),
        logs_dir / "ansible.log",
        cwd=ansible_dir.parent.resolve(),
        bus=bus, run_id=run.run_id,
        ansible_cfg=ansible_cfg if ansible_cfg.is_file() else None,
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

    # 6. verify-lab — post-apply sanity battery (WARNING-ONLY).
    # Failures attach diagnostics to the run but the run still
    # finishes status=succeeded. See docs/architecture/CONTRACTS.md
    # → verify-lab for the contract. Promoting to hard-fail can
    # come later if the warning signal proves too quiet.
    bus.publish(run.run_id, "step_started", {"step": "verify-lab"})
    verify_step, verify_diagnostics = verify_lab(
        resolved=resolved,
        vm_ips=vm_ips,
        log_path=logs_dir / "verify-lab.log",
        bus=bus,
        run_id=run.run_id,
    )
    steps.append(verify_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "verify-lab", "exit_code": verify_step.exit_code},
    )
    # Downgrade any error-severity verify diagnostics to warning so
    # the warning-only contract holds end-to-end. Step exit code
    # still reflects what really happened (visible in run.json).
    downgraded_verify: list[Diagnostic] = [
        d.model_copy(update={"severity": "warning"})
        if d.severity == "error"
        else d
        for d in verify_diagnostics
    ]

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=f"applied lab {lab!r} ({len(resolved.vms)} VMs)",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, downgraded_verify


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
            (
                f"tofu destroy failed; some resources may remain. "
                f"Recovery: `playground reset {lab}` scrubs the lab's "
                f"libvirt domains, networks, and per-VM volumes by name "
                f"(no tofu state dependency) and is safe to re-run. To "
                f"inspect first, `cd tofu && tofu state list`."
            ),
        )
        return finished, tofu_diagnostics

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=f"destroyed lab {lab!r}",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, []


def execute_reset(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Scrub-by-name → best-effort tofu destroy → wipe per-lab state files.

    The lab-state cleanup path of last resort. Used when ``playground
    destroy`` can't proceed (tofu state corrupt, libvirt out of sync,
    lab YAML changed without a prior destroy). The lab YAML is the only
    source of truth for which resources to remove — tofu state is
    treated as best-effort.

    Lifecycle is the same as ``execute_destroy`` (OperationRun +
    EventBus + step records) so the resulting run shows up in
    ``playground runs list`` and the TUI alongside apply/destroy.

    Order matters:

    1. **scrub-libvirt**: enumerate live libvirt resources, force-stop
       and undefine the ones whose names match this lab's VMs and
       networks; remove per-VM disk volumes and cloud-init ISOs in the
       default pool. Skipped silently on "already gone". Fatal on
       missing virsh or unreachable libvirtd.
    2. **tofu-destroy**: best-effort. tofu state may already match
       reality (everything destroyed) or be irrecoverably out of sync.
       Non-zero exit attaches a warning diagnostic but does not fail
       the reset — the operator chose reset precisely because tofu
       was already unreliable.
    3. **clean-state-files**: remove per-lab artifacts under
       ``.playground/state/{tofu,inventory,workloads}/`` so the next
       ``playground apply`` starts from a clean slate. Shared
       artifacts (tofu/terraform.tfstate, ubuntu-noble.qcow2 base
       image) are never touched.

    Returns ``(OperationRun, diagnostics)``. The run's ``status`` is
    ``succeeded`` when step 1 succeeded; the tofu warning and any
    state-file warnings live on the diagnostics list regardless.
    """
    lab = resolved.lab_name
    runs_dir = state_dir / "runs"
    run, run_dir = start_run(runs_dir, "reset", lab)
    logs_dir = run_dir / "logs"
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": "reset", "lab": lab})

    tfvars_path = state_dir / "state" / "tofu" / f"{lab}.tfvars.json"
    inventory_path = state_dir / "state" / "inventory" / f"{lab}.ini"
    workload_dir = state_dir / "state" / "workloads" / lab

    tfvars_path.parent.mkdir(parents=True, exist_ok=True)
    tfvars = render_tfvars(resolved)
    tfvars_path.write_text(json.dumps(tfvars, indent=2, sort_keys=True) + "\n")

    steps: list[StepResult] = []
    all_diagnostics: list[Diagnostic] = []

    # ---- Step 1: scrub-libvirt (fatal on virsh missing/unreachable) ----
    bus.publish(run.run_id, "step_started", {"step": "scrub-libvirt"})
    scrub_step, scrub_diagnostics = scrub_lab(
        resolved=resolved,
        log_path=logs_dir / "scrub-libvirt.log",
        bus=bus,
        run_id=run.run_id,
    )
    steps.append(scrub_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "scrub-libvirt", "exit_code": scrub_step.exit_code},
    )
    fatal_scrub = [
        d for d in scrub_diagnostics
        if d.id in ("runtime.reset.virsh_missing", "runtime.reset.virsh_unreachable")
    ]
    all_diagnostics.extend(scrub_diagnostics)
    if fatal_scrub:
        return _finalize_failure(
            run, run_dir, steps, bus, all_diagnostics,
            "reset aborted: virsh missing or libvirtd unreachable",
        )

    # ---- Step 2: tofu-destroy (best-effort) ----
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
    if tofu_step.exit_code != 0:
        # Downgrade severity: scrub already cleaned reality, so a tofu
        # failure here is informational. Surface it as a warning so the
        # operator knows tofu state may still have stale entries.
        all_diagnostics.append(
            Diagnostic(
                id="runtime.reset.tofu_destroy_warning",
                severity="warning",
                message=(
                    f"`tofu destroy` exited {tofu_step.exit_code} during reset; "
                    "scrub-libvirt already removed lab resources, but tofu "
                    "state may still hold stale entries"
                ),
                suggestion=(
                    "`cd tofu && tofu state list` to inspect; "
                    "`tofu state rm` to drop stale entries manually"
                ),
            )
        )

    # ---- Step 3: clean-state-files (warnings only on per-file failure) ----
    bus.publish(run.run_id, "step_started", {"step": "clean-state-files"})
    cleanup_step, cleanup_diagnostics = _clean_state_files(
        lab=lab,
        targets=[tfvars_path, inventory_path, workload_dir],
        log_path=logs_dir / "clean-state-files.log",
    )
    steps.append(cleanup_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "clean-state-files", "exit_code": cleanup_step.exit_code},
    )
    all_diagnostics.extend(cleanup_diagnostics)

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=f"reset lab {lab!r} (scrubbed by name)",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, all_diagnostics


def _clean_state_files(
    *,
    lab: str,
    targets: list[Path],
    log_path: Path,
) -> tuple[StepResult, list[Diagnostic]]:
    """Remove per-lab artifacts under ``.playground/state/``.

    Each target is either a file or a directory. Missing paths are
    silently OK (the goal is "be gone"). Any other OSError emits a
    warning diagnostic so the operator can clean up manually.
    """
    from datetime import UTC, datetime

    log_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    lines = [f"# clean-state-files for lab {lab!r}"]
    diagnostics: list[Diagnostic] = []
    removed: list[str] = []

    for target in targets:
        try:
            if target.is_dir():
                shutil.rmtree(target)
                removed.append(str(target))
                lines.append(f"removed dir {target}")
            elif target.exists():
                target.unlink()
                removed.append(str(target))
                lines.append(f"removed file {target}")
            else:
                lines.append(f"skipped (absent) {target}")
        except OSError as exc:
            lines.append(f"FAILED to remove {target}: {exc}")
            diagnostics.append(
                Diagnostic(
                    id="runtime.reset.state_cleanup_failed",
                    severity="warning",
                    message=f"could not remove per-lab state {target}: {exc}",
                    suggestion=f"remove manually: `rm -rf {target}`",
                )
            )

    if not removed and not diagnostics:
        lines.append("nothing to remove (no per-lab state files present)")
    log_path.write_text("\n".join(lines) + "\n")

    return (
        StepResult(
            name="clean-state-files",
            command=["python", "-c", "clean-state-files"],
            exit_code=1 if diagnostics else 0,
            log_path=str(log_path),
            started_at=started_at,
            finished_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        ),
        diagnostics,
    )


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


__all__ = ["execute_apply", "execute_destroy", "execute_reset"]
