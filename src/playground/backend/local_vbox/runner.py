"""Service layer for the local-vbox apply / destroy / reset lifecycle.

Mirrors ``local_libvirt/runner.py`` and returns the same finalized
:class:`OperationRun` so the CLI/TUI treat both backends identically.
Only the **front half** differs: instead of ``tofu apply`` creating
libvirt domains, ``vbox-create`` clones the base VDI and boots VirtualBox
VMs with NAT SSH port-forwards. The **back half** —
``wait-for-vms-ready``, ``ansible-playbook``, ``verify-lab`` — is the
exact same backend-neutral code the libvirt path uses (it lives under
``local_libvirt`` for historical reasons; see CONTRACTS.md).

Apply steps: ``vbox-create`` → ``wait-for-vms-ready`` → ``ansible-playbook``
→ ``verify-lab`` (warning-only). Reachability is ``127.0.0.1:<host_port>``
per VM, so the inventory/wait/verify all carry an ``ssh_port``.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from playground.backend.local_libvirt.apply import run_ansible_playbook
from playground.backend.local_libvirt.inventory import render_inventory
from playground.backend.local_libvirt.verify import verify_lab
from playground.backend.local_libvirt.wait import VmTarget, wait_for_vms_ready
from playground.backend.local_vbox.cloudinit import build_seed_iso
from playground.backend.local_vbox.image import ensure_base_vdi
from playground.backend.local_vbox.plan import VboxPlan, build_vbox_plan
from playground.backend.local_vbox.vbox import (
    create_vm,
    destroy_vm,
    list_vms,
    nat_ssh_port,
    pick_free_ports,
    run_vbox,
    vboxmanage_available,
    vm_exists,
    vm_running,
)
from playground.events import EventBus, JsonlWriter
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.planner import schedule_workloads, stage_workload_files
from playground.runs import OperationRun, StepResult, finish_run, start_run

DEFAULT_SSH_KEY = "~/.ssh/id_rsa.pub"


def execute_apply(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    ansible_dir: Path,
    config_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    """Run the full vbox apply lifecycle. Never raises."""
    lab = resolved.lab_name

    # Pre-flight: schedule + stage workloads (pure-config; same as libvirt).
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

    # SSH public key for cloud-init injection (read before the run so a
    # missing key fails pre-flight without creating a run record).
    ssh_key, key_diag = _read_ssh_public_key(resolved)
    if key_diag is not None:
        return None, [key_diag]

    plan = build_vbox_plan(resolved, ssh_public_key=ssh_key or "")

    runs_dir = state_dir / "runs"
    run, run_dir = start_run(runs_dir, "apply", lab)
    logs_dir = run_dir / "logs"
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": "apply", "lab": lab})

    vbox_state = state_dir / "state" / "vbox" / lab
    inventory_path = state_dir / "state" / "inventory" / f"{lab}.ini"

    steps: list[StepResult] = []

    # ---- Step 1: vbox-create ----
    bus.publish(run.run_id, "step_started", {"step": "vbox-create"})
    create_step, vm_ips, ssh_ports, create_diagnostics = _vbox_create(
        plan=plan,
        vbox_state=vbox_state,
        offline=resolved.offline,
        log_path=logs_dir / "vbox-create.log",
        bus=bus,
        run_id=run.run_id,
    )
    steps.append(create_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "vbox-create", "exit_code": create_step.exit_code},
    )
    if create_step.exit_code != 0:
        # Roll back any VMs we did manage to create so a failed apply
        # doesn't strand half a lab.
        _rollback(plan, logs_dir / "vbox-create.log", bus, run.run_id)
        return _finalize_failure(
            run, run_dir, steps, bus, create_diagnostics,
            "vbox-create failed; partially-created VMs were rolled back. "
            "Fix the cause and re-run apply.",
        )

    # ---- render inventory (127.0.0.1:<port> per VM) ----
    inventory_body, render_diagnostics = render_inventory(
        resolved, vm_ips, staged_workloads=staged_workloads, ssh_ports=ssh_ports,
    )
    if render_diagnostics:
        return _finalize_failure(
            run, run_dir, steps, bus, render_diagnostics,
            "VMs were created but inventory render failed; VMs are alive. "
            "Investigate, then re-run apply or `playground reset`.",
        )
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(inventory_body)

    # ---- Step 2: wait-for-vms-ready ----
    ssh_by_vm = {vm.name: vm.ssh.user for vm in resolved.vms}
    targets = [
        VmTarget(
            name=name,
            ip=ip,
            ssh_user=ssh_by_vm.get(name, "ubuntu"),
            ssh_port=ssh_ports.get(name, 22),
            console_hint=_console_hint(lab, name, ssh_ports.get(name, 22),
                                       ssh_by_vm.get(name, "ubuntu")),
        )
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
            "VMs were created but did not become reachable in time. "
            "Inspect a VM via `VBoxManage startvm <lab>-<vm> --type gui` "
            "then re-run apply.",
        )

    # ---- Step 3: ansible-playbook ----
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
            "VMs were created but Ansible configuration failed. Ansible "
            "roles are idempotent: re-run apply after fixing, or "
            "`playground reset` to tear down.",
        )

    # ---- Step 4: verify-lab (warning-only) ----
    bus.publish(run.run_id, "step_started", {"step": "verify-lab"})
    verify_step, verify_diagnostics = verify_lab(
        resolved=resolved,
        vm_ips=vm_ips,
        log_path=logs_dir / "verify-lab.log",
        bus=bus,
        run_id=run.run_id,
        ssh_ports=ssh_ports,
    )
    steps.append(verify_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "verify-lab", "exit_code": verify_step.exit_code},
    )
    downgraded_verify: list[Diagnostic] = [
        d.model_copy(update={"severity": "warning"})
        if d.severity == "error"
        else d
        for d in verify_diagnostics
    ]

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=f"applied lab {lab!r} on vbox ({len(resolved.vms)} VMs)",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, downgraded_verify


def execute_destroy(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Poweroff + delete every VM the lab declares. Never raises."""
    lab = resolved.lab_name
    plan = build_vbox_plan(resolved, ssh_public_key="")
    runs_dir = state_dir / "runs"
    run, run_dir = start_run(runs_dir, "destroy", lab)
    logs_dir = run_dir / "logs"
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": "destroy", "lab": lab})

    steps: list[StepResult] = []
    bus.publish(run.run_id, "step_started", {"step": "vbox-destroy"})
    step, diagnostics = _vbox_destroy(
        plan, log_path=logs_dir / "vbox-destroy.log", bus=bus, run_id=run.run_id,
    )
    steps.append(step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "vbox-destroy", "exit_code": step.exit_code},
    )
    if step.exit_code != 0:
        finished, _ = _finalize_failure(
            run, run_dir, steps, bus, diagnostics,
            (
                f"vbox-destroy failed; some VMs may remain. "
                f"Recovery: `playground reset {lab}` scrubs every "
                f"VirtualBox VM matching the lab name (no tofu state) "
                f"and is safe to re-run. To inspect first, "
                f"`VBoxManage list vms`."
            ),
        )
        return finished, diagnostics

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=f"destroyed lab {lab!r} on vbox",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, []


def execute_reset(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Scrub-by-name → wipe per-lab state. The last-resort cleanup path.

    Mirrors the libvirt reset contract: enumerate live VirtualBox VMs,
    delete the ones whose names start with ``<lab>-`` (catches VMs from a
    prior apply even if the lab spec since changed), then remove per-lab
    state files. Fatal only if VBoxManage is missing.
    """
    lab = resolved.lab_name
    runs_dir = state_dir / "runs"
    run, run_dir = start_run(runs_dir, "reset", lab)
    logs_dir = run_dir / "logs"
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": "reset", "lab": lab})

    steps: list[StepResult] = []
    all_diagnostics: list[Diagnostic] = []

    # ---- Step 1: scrub-vbox ----
    bus.publish(run.run_id, "step_started", {"step": "scrub-vbox"})
    scrub_step, scrub_diagnostics = _vbox_scrub(
        lab=lab, log_path=logs_dir / "scrub-vbox.log", bus=bus, run_id=run.run_id,
    )
    steps.append(scrub_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "scrub-vbox", "exit_code": scrub_step.exit_code},
    )
    all_diagnostics.extend(scrub_diagnostics)
    fatal = [d for d in scrub_diagnostics if d.id == "runtime.reset.vboxmanage_missing"]
    if fatal:
        return _finalize_failure(
            run, run_dir, steps, bus, all_diagnostics,
            "reset aborted: VBoxManage missing",
        )

    # ---- Step 2: clean-state-files ----
    vbox_state = state_dir / "state" / "vbox" / lab
    inventory_path = state_dir / "state" / "inventory" / f"{lab}.ini"
    workload_dir = state_dir / "state" / "workloads" / lab
    bus.publish(run.run_id, "step_started", {"step": "clean-state-files"})
    cleanup_step, cleanup_diagnostics = _clean_state_files(
        lab=lab,
        targets=[vbox_state, inventory_path, workload_dir],
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
        summary=f"reset lab {lab!r} on vbox (scrubbed by name)",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, all_diagnostics


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _vbox_create(
    *,
    plan: VboxPlan,
    vbox_state: Path,
    offline: bool,
    log_path: Path,
    bus: EventBus,
    run_id: str,
) -> tuple[StepResult, dict[str, str], dict[str, int], list[Diagnostic]]:
    """Create + boot every VM. Returns (step, vm_ips, ssh_ports, diags).

    ``vm_ips`` maps lab VM name -> ``127.0.0.1`` and ``ssh_ports`` maps it
    to the per-VM NAT host port; both are keyed by the lab VM name so the
    shared inventory/wait/verify steps line up.
    """
    started = _iso_now()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w")
    log: Callable[[str], None] = lambda s: (handle.write(s), handle.flush(), None)[2]  # noqa: E731

    vm_ips: dict[str, str] = {}
    ssh_ports: dict[str, int] = {}
    diagnostics: list[Diagnostic] = []

    try:
        if not vboxmanage_available():
            diagnostics.append(
                Diagnostic(
                    id="runtime.vbox.vboxmanage_missing",
                    severity="error",
                    message="`VBoxManage` not found on PATH",
                    source=SourceLocation(path="host"),
                    suggestion="install VirtualBox (apt install virtualbox)",
                )
            )
            return _step("vbox-create", exit_code=127, log_path=log_path,
                         started=started), vm_ips, ssh_ports, diagnostics

        base_vdi, image_diags = ensure_base_vdi(
            image_source=plan.image_source,
            qcow2_cache=Path(plan.image_cache_qcow2),
            offline=offline,
            log=log,
        )
        if base_vdi is None:
            diagnostics.extend(image_diags)
            return _step("vbox-create", exit_code=1, log_path=log_path,
                         started=started), vm_ips, ssh_ports, diagnostics

        # Idempotent re-apply: VMs that already exist are reused on their
        # current SSH port (started if stopped). Only genuinely-new VMs
        # need a fresh free port. pick_free_ports binds to test, so it
        # naturally avoids ports already held by existing forwards.
        new_vms = [vm for vm in plan.vms if not vm_exists(vm.vbox_name)]
        fresh_ports = iter(pick_free_ports(len(new_vms)))

        seeds_dir = vbox_state / "seeds"
        disks_dir = vbox_state / "disks"
        for vm in plan.vms:
            if vm_exists(vm.vbox_name):
                port = nat_ssh_port(vm.vbox_name)
                if port is None:
                    diagnostics.append(
                        Diagnostic(
                            id="runtime.vbox.reuse_no_port",
                            severity="error",
                            message=(
                                f"VM {vm.vbox_name!r} already exists but has no "
                                "ssh NAT port-forward; can't reuse it"
                            ),
                            source=SourceLocation(path=vm.vbox_name),
                            suggestion=f"`playground reset {plan.lab_name}` then re-apply",
                        )
                    )
                    return _step("vbox-create", exit_code=1, log_path=log_path,
                                 started=started), vm_ips, ssh_ports, diagnostics
                if not vm_running(vm.vbox_name):
                    log(f"# {vm.vbox_name}: exists, powered off — starting\n")
                    run_vbox(["startvm", vm.vbox_name, "--type", "headless"],
                             log=log, bus=bus, run_id=run_id)
                else:
                    log(f"# {vm.vbox_name}: already running on 127.0.0.1:{port} — reusing\n")
                vm_ips[vm.lab_vm_name] = "127.0.0.1"
                ssh_ports[vm.lab_vm_name] = port
                continue

            try:
                port = next(fresh_ports)
            except StopIteration:
                diagnostics.append(
                    Diagnostic(
                        id="runtime.vbox.no_free_ports",
                        severity="error",
                        message="could not find enough free host ports for SSH forwards",
                        source=SourceLocation(path="host"),
                    )
                )
                return _step("vbox-create", exit_code=1, log_path=log_path,
                             started=started), vm_ips, ssh_ports, diagnostics

            seed_iso, seed_diags = build_seed_iso(vm, out_dir=seeds_dir)
            if seed_iso is None:
                diagnostics.extend(seed_diags)
                return _step("vbox-create", exit_code=1, log_path=log_path,
                             started=started), vm_ips, ssh_ports, diagnostics
            create_diags = create_vm(
                vm,
                base_vdi=base_vdi,
                seed_iso=seed_iso,
                disk_path=disks_dir / f"{vm.vbox_name}.vdi",
                ssh_host_port=port,
                log=log,
                bus=bus,
                run_id=run_id,
            )
            if create_diags:
                diagnostics.extend(create_diags)
                return _step("vbox-create", exit_code=1, log_path=log_path,
                             started=started), vm_ips, ssh_ports, diagnostics
            vm_ips[vm.lab_vm_name] = "127.0.0.1"
            ssh_ports[vm.lab_vm_name] = port

        return _step("vbox-create", exit_code=0, log_path=log_path,
                     started=started), vm_ips, ssh_ports, diagnostics
    finally:
        handle.close()


def _vbox_destroy(
    plan: VboxPlan, *, log_path: Path, bus: EventBus, run_id: str,
) -> tuple[StepResult, list[Diagnostic]]:
    started = _iso_now()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w")
    log: Callable[[str], None] = lambda s: (handle.write(s), handle.flush(), None)[2]  # noqa: E731
    try:
        if not vboxmanage_available():
            return (
                _step("vbox-destroy", exit_code=127, log_path=log_path, started=started),
                [
                    Diagnostic(
                        id="runtime.vbox.vboxmanage_missing",
                        severity="error",
                        message="`VBoxManage` not found on PATH",
                        source=SourceLocation(path="host"),
                    )
                ],
            )
        for vm in plan.vms:
            destroy_vm(vm.vbox_name, log=log, bus=bus, run_id=run_id)
        return _step("vbox-destroy", exit_code=0, log_path=log_path, started=started), []
    finally:
        handle.close()


def _vbox_scrub(
    *, lab: str, log_path: Path, bus: EventBus, run_id: str,
) -> tuple[StepResult, list[Diagnostic]]:
    """Delete every registered VM whose name starts with ``<lab>-``."""
    started = _iso_now()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w")
    log: Callable[[str], None] = lambda s: (handle.write(s), handle.flush(), None)[2]  # noqa: E731
    try:
        if not vboxmanage_available():
            return (
                _step("scrub-vbox", exit_code=127, log_path=log_path, started=started),
                [
                    Diagnostic(
                        id="runtime.reset.vboxmanage_missing",
                        severity="error",
                        message="`VBoxManage` not found on PATH; cannot scrub",
                        source=SourceLocation(path="host"),
                        suggestion="install VirtualBox and retry",
                    )
                ],
            )
        prefix = f"{lab}-"
        matches = [name for name in list_vms() if name.startswith(prefix)]
        if not matches:
            log(f"# no VMs matching {prefix!r}\n")
        for name in matches:
            destroy_vm(name, log=log, bus=bus, run_id=run_id, step_name="scrub-vbox")
        return _step("scrub-vbox", exit_code=0, log_path=log_path, started=started), []
    finally:
        handle.close()


def _rollback(plan: VboxPlan, log_path: Path, bus: EventBus, run_id: str) -> None:
    """Best-effort teardown of any VMs created before a failure."""
    with log_path.open("a") as handle:
        log: Callable[[str], None] = lambda s: (handle.write(s), handle.flush(), None)[2]  # noqa: E731
        log("# rollback: removing any partially-created VMs\n")
        for vm in plan.vms:
            destroy_vm(vm.vbox_name, log=log, bus=bus, run_id=run_id, step_name="vbox-create")


# ---------------------------------------------------------------------------
# Shared plumbing (kept local to avoid importing libvirt-runner privates)
# ---------------------------------------------------------------------------


def _read_ssh_public_key(
    resolved: ResolvedLab,
) -> tuple[str | None, Diagnostic | None]:
    """Resolve the public key to inject: provider override or default."""
    provider = resolved.providers.get(resolved.backend, {})
    path_str = provider.get("ssh_public_key_path") or DEFAULT_SSH_KEY
    path = Path(str(path_str)).expanduser()
    if not path.is_file():
        return None, Diagnostic(
            id="runtime.vbox.ssh_key_missing",
            severity="error",
            message=f"SSH public key not found at {path}",
            source=SourceLocation(path=str(path)),
            suggestion=(
                "generate one (`ssh-keygen`) or set spec.providers."
                f"{resolved.backend}.ssh_public_key_path in the lab"
            ),
        )
    return path.read_text().strip(), None


def _console_hint(lab: str, vm_name: str, port: int, user: str) -> str:
    return (
        f"inspect the VM: `VBoxManage startvm {lab}-{vm_name} --type gui` "
        f"or check cloud-init: `ssh -p {port} {user}@127.0.0.1 "
        "cloud-init status --long`"
    )


def _clean_state_files(
    *, lab: str, targets: list[Path], log_path: Path,
) -> tuple[StepResult, list[Diagnostic]]:
    """Remove per-lab artifacts. Missing paths are OK; other OSErrors warn."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = _iso_now()
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
        _step("clean-state-files", exit_code=1 if diagnostics else 0,
              log_path=log_path, started=started),
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
    finished = finish_run(run, run_dir, status="failed", steps=steps, summary=summary)
    bus.publish(run.run_id, "operation_finished", {"status": "failed"})
    return finished, diagnostics


def _step(
    name: str, *, exit_code: int, log_path: Path, started: str,
) -> StepResult:
    return StepResult(
        name=name,
        command=[name],
        exit_code=exit_code,
        log_path=str(log_path),
        started_at=started,
        finished_at=_iso_now(),
    )


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = ["execute_apply", "execute_destroy", "execute_reset"]
