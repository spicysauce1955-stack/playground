"""Service layer for the cloud-digitalocean apply / destroy / reset lifecycle.

Mirrors ``local_vbox/runner.py`` and returns the same finalized
:class:`OperationRun` so the CLI/TUI treat all backends identically.

Apply/resume steps:
  ``tofu-init`` → ``tofu-apply`` → ``fetch-vm-ips`` → ``render-inventory`` →
  ``wait-for-vms-ready`` → ``ansible-playbook`` → ``verify-lab`` (warning-only).

Destroy/suspend steps: ``tofu-destroy`` → ``tag-sweep``.

Reset steps: ``tofu-destroy`` → ``tag-sweep`` → ``clean-state-files``.

Droplets have routable public IPs so the configure half uses ``ssh_port=22``
(no NAT port-forward); same backend-neutral code the libvirt/vbox adapters use.

The API token is passed to ``tofu`` via the ``DIGITALOCEAN_TOKEN`` environment
variable inherited by the subprocess.  It never appears in any log event,
tfvars file, or Diagnostic message.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playground.backend.cloud_digitalocean.do import (
    CONSOLE_URL,
    delete_droplet,
    droplet_summary,
    list_droplets_by_tag,
    read_token,
    token_env_name,
)
from playground.backend.cloud_digitalocean.plan import DoPlan, build_do_plan
from playground.backend.cloud_digitalocean.settings import merge_provider_settings
from playground.backend.cloud_digitalocean.tfvars import render_do_tfvars
from playground.backend.local_libvirt.apply import (
    run_ansible_playbook,
    run_tofu_apply,
    run_tofu_destroy,
    run_tofu_init,
)
from playground.backend.local_libvirt.inventory import fetch_vm_ips, render_inventory
from playground.backend.local_libvirt.verify import verify_lab
from playground.backend.local_libvirt.wait import VmTarget, wait_for_vms_ready
from playground.events import EventBus, JsonlWriter
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.planner import schedule_workloads, stage_workload_files
from playground.runs import OperationRun, StepResult, finish_run, start_run

DEFAULT_SSH_KEY = "~/.ssh/id_rsa.pub"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def execute_apply(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    ansible_dir: Path,
    config_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    """Run the full cloud-digitalocean apply lifecycle. Never raises."""
    return _provision(
        operation="apply",
        resume=False,
        resolved=resolved,
        state_dir=state_dir,
        tofu_dir=tofu_dir,
        ansible_dir=ansible_dir,
        config_dir=config_dir,
        bus=bus,
    )


def execute_resume(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    ansible_dir: Path,
    config_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    """Rebuild Droplets and re-provision.  Never raises.

    Publishes a warning before mutating because resume rebuilds Droplets from
    config; VM disk changes are NOT preserved (no snapshot).
    """
    return _provision(
        operation="resume",
        resume=True,
        resolved=resolved,
        state_dir=state_dir,
        tofu_dir=tofu_dir,
        ansible_dir=ansible_dir,
        config_dir=config_dir,
        bus=bus,
    )


def execute_destroy(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
    config_dir: Path | None = None,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Destroy all Droplets. Never raises."""
    return _teardown(
        operation="destroy",
        resolved=resolved,
        state_dir=state_dir,
        tofu_dir=tofu_dir,
        bus=bus,
        config_dir=config_dir,
    )


def execute_suspend(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
    config_dir: Path | None = None,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Destroy Droplets to stop billing.  Never raises.

    Publishes a warning before mutating: suspend destroys Droplets to stop
    billing (powered-off Droplets still bill); disk changes are NOT preserved.
    """
    return _teardown(
        operation="suspend",
        resolved=resolved,
        state_dir=state_dir,
        tofu_dir=tofu_dir,
        bus=bus,
        config_dir=config_dir,
    )


def execute_reset(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
    config_dir: Path | None = None,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Best-effort teardown + wipe per-lab state files. Never raises."""
    lab = resolved.lab_name
    runs_dir = state_dir / "runs"
    run, run_dir = start_run(runs_dir, "reset", lab)
    logs_dir = run_dir / "logs"
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": "reset", "lab": lab})

    per_lab_dir = state_dir / "state" / "cloud-digitalocean" / lab
    var_file = per_lab_dir / f"{lab}.tfvars.json"
    source_root = tofu_dir / "cloud_digitalocean"

    steps: list[StepResult] = []
    all_diagnostics: list[Diagnostic] = []

    # ---- Step 1: tofu-destroy (best-effort) ----
    bus.publish(run.run_id, "step_started", {"step": "tofu-destroy"})
    # Use merged provider settings (same as apply) so destroy tfvars match.
    plan = build_do_plan(
        resolved,
        provider_settings=_provider_settings(config_dir, resolved),
    )
    ssh_key, _ = _read_ssh_public_key(resolved)
    if per_lab_dir.exists():
        _prepare_tofu_dir(source_root, per_lab_dir)
        _write_tfvars(plan, ssh_key or "", per_lab_dir)
        # Run tofu init first so destroy works even if .terraform/ is absent.
        _init_step, _init_diags = run_tofu_init(
            per_lab_dir,
            logs_dir / "tofu-init-for-destroy.log",
            bus=bus,
            run_id=run.run_id,
        )
        all_diagnostics.extend(_init_diags)
        destroy_step, destroy_diags = run_tofu_destroy(
            per_lab_dir, var_file,
            logs_dir / "tofu-destroy.log",
            bus=bus, run_id=run.run_id,
        )
    else:
        destroy_step = _noop_step("tofu-destroy", logs_dir / "tofu-destroy.log")
        destroy_diags = []
    steps.append(destroy_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "tofu-destroy", "exit_code": destroy_step.exit_code},
    )
    all_diagnostics.extend(destroy_diags)

    # ---- Step 2: tag-sweep (always) ----
    bus.publish(run.run_id, "step_started", {"step": "tag-sweep"})
    sweep_step, sweep_diags = _tag_sweep(
        operation="reset",
        resolved=resolved,
        log_path=logs_dir / "tag-sweep.log",
        bus=bus,
        run_id=run.run_id,
    )
    steps.append(sweep_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "tag-sweep", "exit_code": sweep_step.exit_code},
    )
    all_diagnostics.extend(sweep_diags)

    # If the sweep found survivors or was inconclusive, do NOT delete state —
    # the per-lab dir/inventory are the operator's only handle to the live
    # resources.  Fail the reset so the operator is alerted.
    if _sweep_failed("reset", sweep_diags):
        finished = finish_run(
            run, run_dir, status="failed", steps=steps,
            summary=(
                f"reset lab {lab!r} on digitalocean: "
                "tag-sweep found survivors or was inconclusive — "
                "state files NOT deleted to preserve operator access to live resources"
            ),
        )
        bus.publish(run.run_id, "operation_finished", {"status": "failed"})
        return finished, all_diagnostics

    # ---- Step 3: clean-state-files (only when teardown is confirmed clean) ----
    inventory_path = state_dir / "state" / "inventory" / f"{lab}.ini"
    workload_dir = state_dir / "state" / "workloads" / lab
    bus.publish(run.run_id, "step_started", {"step": "clean-state-files"})
    cleanup_step, cleanup_diags = _clean_state_files(
        lab=lab,
        targets=[per_lab_dir, inventory_path, workload_dir],
        log_path=logs_dir / "clean-state-files.log",
    )
    steps.append(cleanup_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "clean-state-files", "exit_code": cleanup_step.exit_code},
    )
    all_diagnostics.extend(cleanup_diags)

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=f"reset lab {lab!r} on digitalocean",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, all_diagnostics


# ---------------------------------------------------------------------------
# Shared provisioning path
# ---------------------------------------------------------------------------


def _provision(
    *,
    operation: str,
    resume: bool,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    ansible_dir: Path,
    config_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    """Shared apply/resume lifecycle. Never raises."""
    lab = resolved.lab_name

    # Pre-flight: schedule + stage workloads (pure-config).
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

    # SSH public key required before the run starts.
    ssh_key, key_diag = _read_ssh_public_key(resolved)
    if key_diag is not None:
        return None, [key_diag]

    provider_settings = _provider_settings(config_dir, resolved)
    plan = build_do_plan(resolved, provider_settings=provider_settings)

    per_lab_dir = state_dir / "state" / "cloud-digitalocean" / lab
    source_root = tofu_dir / "cloud_digitalocean"
    var_file = per_lab_dir / f"{lab}.tfvars.json"
    inventory_path = state_dir / "state" / "inventory" / f"{lab}.ini"

    runs_dir = state_dir / "runs"
    run, run_dir = start_run(runs_dir, operation, lab)  # type: ignore[arg-type]
    logs_dir = run_dir / "logs"
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": operation, "lab": lab})

    if resume:
        bus.publish(
            run.run_id, "log_line",
            {
                "step": "resume",
                "line": (
                    "resume rebuilds Droplets from config; "
                    "VM disk changes are NOT preserved (no snapshot)"
                ),
            },
        )

    steps: list[StepResult] = []

    # ---- Step 1: tofu-init ----
    bus.publish(run.run_id, "step_started", {"step": "tofu-init"})
    _prepare_tofu_dir(source_root, per_lab_dir)
    _write_tfvars(plan, ssh_key or "", per_lab_dir)
    init_step, init_diags = run_tofu_init(
        per_lab_dir,
        logs_dir / "tofu-init.log",
        bus=bus,
        run_id=run.run_id,
    )
    steps.append(init_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "tofu-init", "exit_code": init_step.exit_code},
    )
    if init_step.exit_code != 0:
        return _finalize_failure(
            run, run_dir, steps, bus, init_diags,
            "tofu-init failed; check that $DIGITALOCEAN_TOKEN is set "
            "and the provider plugin can be fetched. "
            f"State is under {per_lab_dir}",
        )

    # ---- Step 2: tofu-apply ----
    bus.publish(run.run_id, "step_started", {"step": "tofu-apply"})
    apply_step, apply_diags = run_tofu_apply(
        per_lab_dir, var_file,
        logs_dir / "tofu-apply.log",
        bus=bus, run_id=run.run_id,
    )
    steps.append(apply_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "tofu-apply", "exit_code": apply_step.exit_code},
    )
    if apply_step.exit_code != 0:
        return _finalize_failure(
            run, run_dir, steps, bus, apply_diags,
            "tofu-apply failed. Droplets may have been created; run "
            f"`playground destroy {lab}` to clean up. "
            f"State is under {per_lab_dir}",
        )

    # ---- Step 3: fetch-vm-ips ----
    vm_ips, ip_diags = fetch_vm_ips(per_lab_dir)
    if ip_diags or not vm_ips:
        return _finalize_failure(
            run, run_dir, steps, bus, ip_diags or [
                Diagnostic(
                    id="runtime.apply.no_vm_ips",
                    severity="error",
                    message=(
                        "tofu apply succeeded but `tofu output -json` returned "
                        "no vm_ips; Droplets may exist but inventory cannot be built"
                    ),
                    source=SourceLocation(path=str(per_lab_dir)),
                    suggestion=(
                        f"inspect tofu state in {per_lab_dir} and re-run "
                        f"`playground apply {lab}`"
                    ),
                )
            ],
            "tofu apply succeeded but VM IPs could not be fetched",
        )

    # ---- render inventory ----
    inventory_body, render_diags = render_inventory(
        resolved, vm_ips,
        staged_workloads=staged_workloads,
        ssh_ports=None,  # public IPs → port 22
    )
    if render_diags:
        return _finalize_failure(
            run, run_dir, steps, bus, render_diags,
            "Droplets were created but inventory render failed. "
            "Investigate, then re-run apply or `playground reset`.",
        )
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(inventory_body)

    # ---- Step 4: wait-for-vms-ready ----
    ssh_by_vm = {vm.name: vm.ssh.user for vm in resolved.vms}
    targets = [
        VmTarget(
            name=name,
            ip=ip,
            ssh_user=ssh_by_vm.get(name, "ubuntu"),
            ssh_port=22,
            console_hint=(
                f"ssh {ssh_by_vm.get(name, 'ubuntu')}@{ip} "
                "'cloud-init status --long'"
            ),
        )
        for name, ip in vm_ips.items()
    ]
    bus.publish(run.run_id, "step_started", {"step": "wait-for-vms-ready"})
    wait_step, wait_diags = wait_for_vms_ready(
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
            run, run_dir, steps, bus, wait_diags,
            "Droplets were created but did not become reachable in time. "
            "Check the Droplet console in the DigitalOcean dashboard, then "
            "re-run apply.",
        )

    # ---- Step 5: ansible-playbook ----
    ansible_cfg = ansible_dir / "ansible.cfg"
    bus.publish(run.run_id, "step_started", {"step": "ansible-playbook"})
    ansible_step, ansible_diags = run_ansible_playbook(
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
    if ansible_diags or ansible_step.exit_code != 0:
        return _finalize_failure(
            run, run_dir, steps, bus, ansible_diags,
            "Droplets were created but Ansible configuration failed. Roles "
            "are idempotent: re-run apply after fixing, or "
            "`playground reset` to tear down.",
        )

    # ---- Step 6: verify-lab (warning-only) ----
    bus.publish(run.run_id, "step_started", {"step": "verify-lab"})
    verify_step, verify_diags = verify_lab(
        resolved=resolved,
        vm_ips=vm_ips,
        log_path=logs_dir / "verify-lab.log",
        bus=bus,
        run_id=run.run_id,
        ssh_ports=None,
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
        for d in verify_diags
    ]

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=(
            f"{operation} lab {lab!r} on digitalocean "
            f"({plan.vm_count} Droplets)"
        ),
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, downgraded_verify


# ---------------------------------------------------------------------------
# Shared teardown path
# ---------------------------------------------------------------------------


def _teardown(
    *,
    operation: str,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
    config_dir: Path | None = None,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Shared destroy/suspend lifecycle. Never raises."""
    lab = resolved.lab_name
    runs_dir = state_dir / "runs"
    run, run_dir = start_run(runs_dir, operation, lab)  # type: ignore[arg-type]
    logs_dir = run_dir / "logs"
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": operation, "lab": lab})

    if operation == "suspend":
        bus.publish(
            run.run_id, "log_line",
            {
                "step": "suspend",
                "line": (
                    "suspend destroys Droplets to stop billing "
                    "(powered-off Droplets still bill); "
                    "disk changes are NOT preserved"
                ),
            },
        )

    per_lab_dir = state_dir / "state" / "cloud-digitalocean" / lab
    source_root = tofu_dir / "cloud_digitalocean"
    var_file = per_lab_dir / f"{lab}.tfvars.json"

    # Build the plan using merged provider settings (same merge logic as
    # apply) so destroy-time tfvars match what apply used.
    plan = build_do_plan(
        resolved,
        provider_settings=_provider_settings(config_dir, resolved),
    )
    ssh_key, _ = _read_ssh_public_key(resolved)

    steps: list[StepResult] = []
    all_diagnostics: list[Diagnostic] = []

    # ---- Step 1: tofu-destroy ----
    bus.publish(run.run_id, "step_started", {"step": "tofu-destroy"})
    if per_lab_dir.exists():
        _prepare_tofu_dir(source_root, per_lab_dir)
        _write_tfvars(plan, ssh_key or "", per_lab_dir)
        # Run tofu init first so destroy works even if .terraform/ is absent
        # (e.g. a prior apply failed at init).  Init on an already-init'd
        # dir is idempotent.
        init_step, _init_diags = run_tofu_init(
            per_lab_dir,
            logs_dir / "tofu-init-for-destroy.log",
            bus=bus,
            run_id=run.run_id,
        )
        all_diagnostics.extend(_init_diags)
        destroy_step, destroy_diags = run_tofu_destroy(
            per_lab_dir, var_file,
            logs_dir / "tofu-destroy.log",
            bus=bus, run_id=run.run_id,
        )
    else:
        # Nothing was ever applied; record a no-op step.
        destroy_step = _noop_step("tofu-destroy", logs_dir / "tofu-destroy.log")
        destroy_diags = []
    steps.append(destroy_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "tofu-destroy", "exit_code": destroy_step.exit_code},
    )
    all_diagnostics.extend(destroy_diags)

    # ---- Step 2: tag-sweep (always) ----
    bus.publish(run.run_id, "step_started", {"step": "tag-sweep"})
    sweep_step, sweep_diags = _tag_sweep(
        operation=operation,
        resolved=resolved,
        log_path=logs_dir / "tag-sweep.log",
        bus=bus,
        run_id=run.run_id,
    )
    steps.append(sweep_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "tag-sweep", "exit_code": sweep_step.exit_code},
    )
    all_diagnostics.extend(sweep_diags)

    # If orphaned survivors remain OR the sweep is inconclusive (API error),
    # the operation must not report success — paid compute may still be running.
    if _sweep_failed(operation, sweep_diags):
        orphan_count = sum(
            1 for d in sweep_diags
            if d.id == f"runtime.{operation}.orphaned_resource"
        )
        finished = finish_run(
            run, run_dir, status="failed", steps=steps,
            summary=(
                f"{operation} lab {lab!r} on digitalocean: "
                f"{orphan_count} Droplet(s) still present or sweep inconclusive"
            ),
        )
        bus.publish(run.run_id, "operation_finished", {"status": "failed"})
        return finished, all_diagnostics

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=f"{operation} lab {lab!r} on digitalocean",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})
    return finished, all_diagnostics


# ---------------------------------------------------------------------------
# Helpers — pure/near-pure
# ---------------------------------------------------------------------------


def _provider_settings(
    config_dir: Path | None,
    resolved: ResolvedLab,
) -> dict[str, Any]:
    """Merge ProviderConfig spec defaults with lab provider overrides.

    Delegates to :func:`merge_provider_settings` so tests and dispatch
    share the same merge logic.
    """
    return merge_provider_settings(resolved, config_dir=config_dir)


def _prepare_tofu_dir(source_root: Path, per_lab_dir: Path) -> None:
    """Copy *.tf files and cloud_init.cfg from source_root into per_lab_dir.

    Creates per_lab_dir (and parents) if absent.  Removes any existing
    ``*.tf`` and ``cloud_init.cfg`` in per_lab_dir before copying so that
    files dropped from the source root do not linger.  Does NOT touch
    ``terraform.tfstate``, ``*.tfvars.json``, or ``.terraform/`` so
    re-apply is idempotent and state is preserved.
    """
    per_lab_dir.mkdir(parents=True, exist_ok=True)
    # Purge stale copies so a deleted source file does not persist.
    for pattern in ("*.tf", "cloud_init.cfg"):
        for stale in per_lab_dir.glob(pattern):
            stale.unlink(missing_ok=True)
    for pattern in ("*.tf", "cloud_init.cfg"):
        for src in source_root.glob(pattern):
            shutil.copy2(src, per_lab_dir / src.name)


def _write_tfvars(
    plan: DoPlan,
    ssh_public_key: str,
    per_lab_dir: Path,
) -> Path:
    """Render and write the tfvars JSON file. Returns the written path."""
    tfvars = render_do_tfvars(plan, ssh_public_key=ssh_public_key)
    out_path = per_lab_dir / f"{plan.lab_name}.tfvars.json"
    out_path.write_text(json.dumps(tfvars, indent=2) + "\n")
    return out_path


def _read_ssh_public_key(
    resolved: ResolvedLab,
) -> tuple[str | None, Diagnostic | None]:
    """Resolve the SSH public key path from provider override or default."""
    provider = resolved.providers.get(resolved.backend, {})
    path_str = provider.get("ssh_public_key_path") or DEFAULT_SSH_KEY
    path = Path(str(path_str)).expanduser()
    if not path.is_file():
        return None, Diagnostic(
            id="runtime.cloud.ssh_key_missing",
            severity="error",
            message=f"SSH public key not found at {path}",
            source=SourceLocation(path=str(path)),
            suggestion=(
                "generate one (`ssh-keygen`) or set spec.providers."
                f"{resolved.backend}.ssh_public_key_path in the lab"
            ),
        )
    return path.read_text().strip(), None


def _tag_sweep(
    *,
    operation: str,
    resolved: ResolvedLab,
    log_path: Path,
    bus: EventBus,
    run_id: str,
) -> tuple[StepResult, list[Diagnostic]]:
    """List Droplets by tag, delete each, re-list to find survivors.

    Returns a StepResult and a list of diagnostics.  If the token is
    absent, appends one warning diagnostic (env-var NAME only, never value)
    and returns exit_code=0 (the sweep is skipped, not failed).
    """
    lab = resolved.lab_name
    tag = f"lab:{lab}"
    started = _iso_now()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [f"# tag-sweep for lab {lab!r} (tag={tag!r})"]
    diagnostics: list[Diagnostic] = []

    token = read_token(resolved)
    if not token:
        env_name = token_env_name(resolved)
        lines.append(
            f"# token absent (${env_name} not set) — sweep skipped; "
            "survivors cannot be confirmed"
        )
        diagnostics.append(
            Diagnostic(
                id=f"runtime.{operation}.sweep_skipped",
                severity="warning",
                message=(
                    f"tag-sweep skipped: ${env_name} is not set; "
                    "Droplets tagged with "
                    f"'{tag}' may still be running and billing"
                ),
                source=SourceLocation(path="environment"),
                suggestion=f"set ${env_name} and run `playground reset {lab}`",
            )
        )
        log_path.write_text("\n".join(lines) + "\n")
        return (
            _step("tag-sweep", exit_code=0, log_path=log_path, started=started),
            diagnostics,
        )

    # First pass — list + delete.
    droplets, list_diags, list_ok = list_droplets_by_tag(token, tag)
    diagnostics.extend(list_diags)

    if not list_ok:
        # The initial listing itself failed; we cannot know what is running.
        lines.append("INCONCLUSIVE: initial listing failed — cannot enumerate Droplets to delete")
        diag = Diagnostic(
            id=f"runtime.{operation}.sweep_inconclusive",
            severity="error",
            message=(
                f"tag-sweep for lab {lab!r} is INCONCLUSIVE: the initial "
                f"Droplet listing failed (API/transport error). "
                "Droplets tagged with "
                f"'{tag}' may still be running and billing. "
                "Verify manually at "
                "https://cloud.digitalocean.com/droplets"
            ),
            source=SourceLocation(path="DigitalOcean API"),
            suggestion=(
                f"open https://cloud.digitalocean.com/droplets and check for "
                f"Droplets tagged '{tag}'; if any remain, delete them manually "
                f"then run `playground reset {lab}`"
            ),
        )
        diagnostics.append(diag)
        lines.append(str(diag.message))
        log_path.write_text("\n".join(lines) + "\n")
        # Non-zero exit so _teardown / execute_reset see a failing sweep.
        return (
            _step("tag-sweep", exit_code=1, log_path=log_path, started=started),
            diagnostics,
        )

    for d in droplets:
        summary = droplet_summary(d)
        did: int | str | None = summary.get("id")
        dname = summary.get("name", "<unknown>")
        lines.append(f"deleting Droplet {dname!r} (id={did})")
        if did is not None:
            del_diags = delete_droplet(token, did)
            diagnostics.extend(del_diags)
            if del_diags:
                bus.publish(
                    run_id, "log_line",
                    {
                        "step": "tag-sweep",
                        "line": f"WARNING: delete_droplet {dname!r} returned diagnostics",
                    },
                )

    # Second pass — re-list to find survivors.
    survivors, relist_diags, relist_ok = list_droplets_by_tag(token, tag)
    diagnostics.extend(relist_diags)

    if not relist_ok:
        # Re-list after attempted deletes failed; sweep is inconclusive.
        lines.append("INCONCLUSIVE: re-list after deletes failed — cannot confirm no survivors")
        diag = Diagnostic(
            id=f"runtime.{operation}.sweep_inconclusive",
            severity="error",
            message=(
                f"tag-sweep for lab {lab!r} is INCONCLUSIVE: Droplet deletes "
                "were attempted but the confirmation re-list failed "
                "(API/transport error). "
                "Droplets tagged with "
                f"'{tag}' may still be running and billing. "
                "Verify manually at "
                "https://cloud.digitalocean.com/droplets"
            ),
            source=SourceLocation(path="DigitalOcean API"),
            suggestion=(
                f"open https://cloud.digitalocean.com/droplets and check for "
                f"Droplets tagged '{tag}'; if any remain, delete them manually "
                f"then run `playground reset {lab}`"
            ),
        )
        diagnostics.append(diag)
        lines.append(str(diag.message))
        log_path.write_text("\n".join(lines) + "\n")
        return (
            _step("tag-sweep", exit_code=1, log_path=log_path, started=started),
            diagnostics,
        )

    for d in survivors:
        summary = droplet_summary(d)
        did = summary.get("id")
        dname = summary.get("name", "<unknown>")
        lines.append(f"SURVIVOR: Droplet {dname!r} (id={did}) still present")
        diagnostics.append(
            Diagnostic(
                id=f"runtime.{operation}.orphaned_resource",
                severity="warning",
                message=(
                    f"Droplet {dname!r} (id={did}) still present; "
                    "remove manually"
                ),
                source=SourceLocation(path=str(did)),
                suggestion=CONSOLE_URL.format(id=did),
            )
        )

    if not survivors:
        lines.append("tag-sweep complete: no survivors")

    log_path.write_text("\n".join(lines) + "\n")
    return (
        _step("tag-sweep", exit_code=0, log_path=log_path, started=started),
        diagnostics,
    )


def _sweep_failed(operation: str, sweep_diags: list[Diagnostic]) -> bool:
    """Return True if a tag-sweep result means the teardown must be failed.

    A sweep must fail the containing operation when:
    - Orphaned Droplets survived deletion (``orphaned_resource`` diagnostic), or
    - The sweep is inconclusive due to an API/transport error that prevented
      confirming cleanup (``sweep_inconclusive`` diagnostic).

    Both represent a state where paid compute may still be running.
    """
    for d in sweep_diags:
        if d.id in (
            f"runtime.{operation}.orphaned_resource",
            f"runtime.{operation}.sweep_inconclusive",
        ):
            return True
    return False


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
        _step(
            "clean-state-files",
            exit_code=1 if diagnostics else 0,
            log_path=log_path,
            started=started,
        ),
        diagnostics,
    )


def _noop_step(name: str, log_path: Path) -> StepResult:
    """A no-op step for when a directory/resource never existed."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = _iso_now()
    log_path.write_text(f"# {name}: no-op (nothing to act on)\n")
    return StepResult(
        name=name,
        command=[name],
        exit_code=0,
        log_path=str(log_path),
        started_at=now,
        finished_at=now,
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


__all__ = [
    "execute_apply",
    "execute_destroy",
    "execute_reset",
    "execute_resume",
    "execute_suspend",
]
