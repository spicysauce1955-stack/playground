"""Read-only CLI commands for inspecting playground configuration."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from playground.backend.local_libvirt import (
    fetch_vm_ips,
    query_status,
    render_inventory,
    render_tfvars,
    run_ansible_playbook,
    run_tofu_apply,
    run_tofu_destroy,
    tail_log,
)
from playground.config.loader import LoadedConfig, load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus, JsonlWriter
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus
from playground.planner import Plan, PlanAction, render_plan, schedule_workloads
from playground.runs import OperationRun, StepResult, finish_run, start_run
from playground.validation import validate as validate_loaded_config


class OutputFormat(StrEnum):
    human = "human"
    json = "json"


app = typer.Typer(no_args_is_help=True, help="Inspect playground lab configuration.")
lab_app = typer.Typer(no_args_is_help=True, help="Inspect configured labs.")
inventory_app = typer.Typer(
    no_args_is_help=True,
    help="Render Ansible inventory from resolved labs and tofu state.",
)
tofu_app = typer.Typer(
    no_args_is_help=True,
    help="Render OpenTofu input files from resolved labs.",
)
runs_app = typer.Typer(no_args_is_help=True, help="Inspect past operation runs.")
app.add_typer(lab_app, name="lab")
app.add_typer(inventory_app, name="inventory")
app.add_typer(tofu_app, name="tofu")
app.add_typer(runs_app, name="runs")


@app.command("validate")
def validate_command(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = OutputFormat.human,
    check_ansible_roles: Annotated[
        bool,
        typer.Option(
            "--check-ansible-roles/--no-check-ansible-roles",
            help="Check referenced Ansible roles on disk.",
        ),
    ] = False,
    ansible_roles_dir: Annotated[
        Path,
        typer.Option(
            "--ansible-roles-dir",
            help="Ansible roles directory used when role checks are enabled.",
        ),
    ] = Path("ansible/roles"),
) -> None:
    """Validate config syntax, schema, and cross-references."""
    loaded, diagnostics = _load_config_or_exit(config_dir, output)

    if not _has_errors(diagnostics):
        diagnostics.extend(
            validate_loaded_config(
                loaded,
                ansible_roles_dir=ansible_roles_dir if check_ansible_roles else None,
            )
        )

    if output is OutputFormat.json:
        _print_json(
            {
                "ok": not _has_errors(diagnostics),
                "diagnostics": [_diagnostic_to_dict(d) for d in diagnostics],
            }
        )
    else:
        if diagnostics:
            _print_diagnostics(diagnostics, err=False)
        errors, warnings = _count_diagnostics(diagnostics)
        typer.echo(f"{errors} errors, {warnings} warnings")

    if _has_errors(diagnostics):
        raise typer.Exit(code=1)


@lab_app.command("list")
def list_labs(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = OutputFormat.human,
) -> None:
    """List configured labs."""
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    labs = [
        {
            "name": lab.metadata.name,
            "description": lab.metadata.description,
            "tags": list(lab.metadata.tags),
        }
        for lab in sorted(loaded.labs.values(), key=lambda item: item.metadata.name)
    ]

    if output is OutputFormat.json:
        _print_json({"labs": labs})
        return

    if not labs:
        typer.echo("No labs configured.")
        return

    for lab in labs:
        typer.echo(lab["name"])


@lab_app.command("show")
def show_lab(
    name: Annotated[str, typer.Argument(help="Lab name to resolve.")],
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = OutputFormat.json,
) -> None:
    """Show a resolved lab definition."""
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    resolved = _resolve_lab_or_exit(loaded, name, config_dir, output)

    if output is OutputFormat.json:
        _print_json(resolved.model_dump(mode="json"))
        return

    typer.echo(f"{resolved.lab_name}")
    if resolved.description:
        typer.echo(f"  {resolved.description}")
    typer.echo(f"  backend: {resolved.backend}")
    typer.echo(f"  networks: {len(resolved.networks)}")
    typer.echo(f"  vms: {len(resolved.vms)}")
    typer.echo(f"  workloads: {len(resolved.workloads)}")
    typer.echo(f"  commands: {len(resolved.commands)}")


@inventory_app.command("render")
def render_inventory_command(
    lab: Annotated[str, typer.Argument(help="Lab name to render inventory for.")],
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    tofu_dir: Annotated[
        Path,
        typer.Option(
            "--tofu-dir",
            help="OpenTofu working directory to read `tofu output -json` from.",
        ),
    ] = Path("tofu"),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help=(
                "Write inventory to this path. Defaults to "
                "`.playground/state/inventory/<lab>.ini`."
            ),
        ),
    ] = None,
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format for status reporting."),
    ] = OutputFormat.human,
) -> None:
    """Render an Ansible inventory for ``lab`` from tofu state."""
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, output)

    vm_ips, fetch_diagnostics = fetch_vm_ips(tofu_dir)
    _exit_on_errors(fetch_diagnostics, output, json_errors=False)

    body, render_diagnostics = render_inventory(resolved, vm_ips)
    _exit_on_errors(render_diagnostics, output, json_errors=False)

    destination = out or (
        Path(".playground") / "state" / "inventory" / f"{lab}.ini"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(body)

    if output is OutputFormat.json:
        _print_json(
            {
                "ok": True,
                "lab": lab,
                "path": str(destination),
                "vm_count": len(resolved.vms),
            }
        )
        return

    typer.echo(f"wrote {destination}")
    typer.echo(f"  lab: {lab}")
    typer.echo(f"  vms: {len(resolved.vms)}")


@app.command("plan")
def plan_command(
    lab: Annotated[str, typer.Argument(help="Lab name to plan.")],
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = OutputFormat.human,
) -> None:
    """Render a backend-neutral plan for ``lab`` (read-only)."""
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    warnings = _warnings_in(diagnostics)
    _print_warnings(diagnostics)

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, output)
    plan = render_plan(resolved, warnings=warnings)

    if output is OutputFormat.json:
        _print_json(plan.model_dump(mode="json"))
        return

    _render_plan_human(plan)


def _render_plan_human(plan: Plan) -> None:
    typer.echo(f"Plan for lab {plan.lab_name!r} (backend: {plan.backend})")
    if plan.offline:
        typer.echo("  offline: true")
    typer.echo("")

    by_type: dict[str, list[PlanAction]] = {"network": [], "vm": [], "workload": []}
    for action in plan.actions:
        by_type[action.resource_type].append(action)

    for resource_type, label in [
        ("network", "Networks"),
        ("vm", "VMs"),
        ("workload", "Workloads"),
    ]:
        actions = by_type[resource_type]
        if not actions:
            continue
        typer.echo(f"{label}:")
        for action in actions:
            typer.echo(f"  + {action.name}  {action.summary}")
        typer.echo("")

    budget = plan.budget
    limits = budget.limits
    typer.echo("Budget:")
    typer.echo(
        f"  totals: {budget.vms} VMs, {budget.vcpu} vCPU, "
        f"{budget.memory_mb} MiB RAM, {budget.disk_gb} GiB disk, "
        f"{budget.containers} workloads"
    )
    typer.echo(
        f"  limits ({limits.mode}): "
        f"{limits.max_vms} VMs / {limits.max_vcpu} vCPU / "
        f"{limits.max_memory_mb} MiB / {limits.max_disk_gb} GiB / "
        f"{limits.max_containers} workloads"
    )
    typer.echo(f"  fits: {'yes' if budget.fits else 'NO'}")


@tofu_app.command("render")
def render_tfvars_command(
    lab: Annotated[str, typer.Argument(help="Lab name to render tofu vars for.")],
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help=(
                "Write tfvars JSON to this path. Defaults to "
                "`.playground/state/tofu/<lab>.tfvars.json`."
            ),
        ),
    ] = None,
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format for status reporting."),
    ] = OutputFormat.human,
) -> None:
    """Render a ``-var-file`` payload for ``lab`` so ``tofu apply`` is in sync."""
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, output)

    payload = render_tfvars(resolved)

    destination = out or (
        Path(".playground") / "state" / "tofu" / f"{lab}.tfvars.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    if output is OutputFormat.json:
        _print_json(
            {
                "ok": True,
                "lab": lab,
                "path": str(destination),
                "vars": sorted(payload),
            }
        )
        return

    typer.echo(f"wrote {destination}")
    typer.echo(f"  lab: {lab}")
    typer.echo(f"  vars: {', '.join(sorted(payload))}")
    typer.echo(
        f"  apply with: tofu -chdir=tofu apply -var-file={destination.resolve()}"
    )


@app.command("apply")
def apply_command(
    lab: Annotated[str, typer.Argument(help="Lab name to apply.")],
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    tofu_dir: Annotated[
        Path,
        typer.Option("--tofu-dir", help="OpenTofu working directory."),
    ] = Path("tofu"),
    ansible_dir: Annotated[
        Path,
        typer.Option("--ansible-dir", help="Ansible directory (containing site.yml)."),
    ] = Path("ansible"),
    state_dir: Annotated[
        Path,
        typer.Option(
            "--state-dir",
            help="Where generated state lives. Defaults to `.playground/`.",
        ),
    ] = Path(".playground"),
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format for status reporting."),
    ] = OutputFormat.human,
) -> None:
    """Apply ``lab``: render inputs, run tofu apply, render inventory, run Ansible."""
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, output)

    # Pre-flight: scheduling is a pure-config decision that must succeed
    # before tofu apply provisions anything. Failing here means no VMs
    # are touched and no run record is created.
    _, schedule_diagnostics = schedule_workloads(resolved)
    _exit_on_errors(schedule_diagnostics, output, json_errors=False)

    runs_dir = state_dir / "runs"
    run, run_dir = start_run(runs_dir, "apply", lab)
    logs_dir = run_dir / "logs"

    bus = EventBus()
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
        tofu_dir, tfvars_path.resolve(), logs_dir / "tofu-apply.log"
    )
    steps.append(tofu_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "tofu-apply", "exit_code": tofu_step.exit_code},
    )
    if tofu_diagnostics or tofu_step.exit_code != 0:
        _fail_apply(
            output, run, run_dir, steps,
            diagnostics=tofu_diagnostics,
            summary="tofu apply failed; no VMs provisioned",
            log_path=tofu_step.log_path,
            bus=bus,
        )

    # 3. Fetch vm_ips & render inventory.
    # If anything from here on fails, tofu has already provisioned VMs —
    # the summary tells the operator the state is partial.
    vm_ips, fetch_ip_diagnostics = fetch_vm_ips(tofu_dir)
    if fetch_ip_diagnostics:
        _fail_apply(
            output, run, run_dir, steps,
            diagnostics=fetch_ip_diagnostics,
            summary=(
                "tofu apply succeeded but reading state failed; VMs are alive. "
                "Investigate, then re-run `playground apply` or tear down via "
                "`cd tofu && tofu destroy`."
            ),
            log_path=None,
            bus=bus,
        )

    inventory_body, render_diagnostics = render_inventory(resolved, vm_ips)
    if render_diagnostics:
        _fail_apply(
            output, run, run_dir, steps,
            diagnostics=render_diagnostics,
            summary=(
                "tofu apply succeeded but inventory render failed; VMs are "
                "alive. Investigate, then re-run `playground apply` or "
                "tear down via `cd tofu && tofu destroy`."
            ),
            log_path=None,
            bus=bus,
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
    )
    steps.append(ansible_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "ansible-playbook", "exit_code": ansible_step.exit_code},
    )
    if ansible_diagnostics or ansible_step.exit_code != 0:
        _fail_apply(
            output, run, run_dir, steps,
            diagnostics=ansible_diagnostics,
            summary=(
                "VMs were provisioned but Ansible configuration failed. "
                "Ansible roles are idempotent: re-run `playground apply` "
                "after fixing the failure, or tear down via "
                "`cd tofu && tofu destroy`."
            ),
            log_path=ansible_step.log_path,
            bus=bus,
        )

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=f"applied lab {lab!r} ({len(resolved.vms)} VMs)",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})

    if output is OutputFormat.json:
        _print_json(finished.model_dump(mode="json", exclude_none=True))
        return

    typer.echo(f"applied lab {lab!r}")
    typer.echo(f"  run: {finished.run_id}")
    typer.echo(f"  record: {run_dir / 'run.json'}")
    for step in finished.steps:
        typer.echo(f"  {step.name}: exit {step.exit_code} (log {step.log_path})")


@runs_app.command("list")
def runs_list_command(
    state_dir: Annotated[
        Path,
        typer.Option(
            "--state-dir",
            help="Where generated state lives. Defaults to `.playground/`.",
        ),
    ] = Path(".playground"),
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = OutputFormat.human,
) -> None:
    """List past operation runs (newest first)."""
    runs_dir = state_dir / "runs"
    records: list[dict[str, str | None]] = []
    if runs_dir.is_dir():
        for entry in sorted(runs_dir.iterdir(), reverse=True):
            record_path = entry / "run.json"
            if not record_path.is_file():
                continue
            try:
                run = OperationRun.model_validate_json(record_path.read_text())
            except (ValueError, OSError):
                continue
            records.append(
                {
                    "run_id": run.run_id,
                    "operation": run.operation,
                    "lab": run.lab,
                    "status": run.status,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                }
            )

    if output is OutputFormat.json:
        _print_json({"runs": records})
        return

    if not records:
        typer.echo("No operation runs recorded yet.")
        return
    for r in records:
        finished = r["finished_at"] or "—"
        typer.echo(
            f"{r['run_id']}  {r['operation']:<7}  {r['status']:<9}  "
            f"start={r['started_at']}  end={finished}"
        )


@runs_app.command("show")
def runs_show_command(
    run_id: Annotated[str, typer.Argument(help="Run id to inspect.")],
    state_dir: Annotated[
        Path,
        typer.Option(
            "--state-dir",
            help="Where generated state lives. Defaults to `.playground/`.",
        ),
    ] = Path(".playground"),
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = OutputFormat.human,
) -> None:
    """Show one run's record, step results, and event log path."""
    run_dir = state_dir / "runs" / run_id
    record_path = run_dir / "run.json"
    if not record_path.is_file():
        _exit_with_diagnostic(
            Diagnostic(
                id="config.runs.unknown",
                severity="error",
                message=f"unknown run id {run_id!r}",
                source=SourceLocation(path=str(run_dir)),
                suggestion="run `playground runs list` to see recorded runs",
            ),
            output,
            json_errors=False,
        )

    run = OperationRun.model_validate_json(record_path.read_text())
    events_path = run_dir / "events.jsonl"

    if output is OutputFormat.json:
        _print_json(
            {
                "run": run.model_dump(mode="json", exclude_none=True),
                "events_path": str(events_path) if events_path.exists() else None,
                "logs_dir": str(run_dir / "logs"),
            }
        )
        return

    typer.echo(f"Run {run.run_id}")
    typer.echo(f"  operation: {run.operation}")
    typer.echo(f"  lab:       {run.lab}")
    typer.echo(f"  status:    {run.status}")
    typer.echo(f"  started:   {run.started_at}")
    if run.finished_at:
        typer.echo(f"  finished:  {run.finished_at}")
    if run.summary:
        typer.echo(f"  summary:   {run.summary}")
    if run.steps:
        typer.echo("  steps:")
        for step in run.steps:
            typer.echo(
                f"    - {step.name}: exit {step.exit_code} (log {step.log_path})"
            )
    if events_path.exists():
        typer.echo(f"  events:    {events_path}")
    typer.echo(f"  logs:      {run_dir / 'logs'}")


@app.command("status")
def status_command(
    lab: Annotated[str, typer.Argument(help="Lab name to inspect.")],
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    tofu_dir: Annotated[
        Path,
        typer.Option("--tofu-dir", help="OpenTofu working directory."),
    ] = Path("tofu"),
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = OutputFormat.human,
) -> None:
    """Show observed state of ``lab`` (read-only)."""
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, output)
    status, query_diagnostics = query_status(resolved, tofu_dir)
    _exit_on_errors(query_diagnostics, output, json_errors=False)

    if output is OutputFormat.json:
        _print_json(status.model_dump(mode="json", exclude_none=True))
        return

    _render_status_human(status)


def _render_status_human(status: LabStatus) -> None:
    typer.echo(f"Lab {status.lab!r} on {status.backend}")
    typer.echo(
        f"  {status.provisioned_vms} of {status.expected_vms} VMs provisioned"
    )
    typer.echo("")
    for vm in status.vms:
        marker = "+" if vm.state == "provisioned" else "-"
        ip = vm.ip or "—"
        typer.echo(f"  {marker} {vm.name}  role={vm.role}  ip={ip}")
    if status.unknown_vms:
        typer.echo("")
        typer.echo(
            f"  unknown VMs in observed state (not in lab): "
            f"{', '.join(status.unknown_vms)}"
        )


@app.command("destroy")
def destroy_command(
    lab: Annotated[str, typer.Argument(help="Lab name to tear down.")],
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    tofu_dir: Annotated[
        Path,
        typer.Option("--tofu-dir", help="OpenTofu working directory."),
    ] = Path("tofu"),
    state_dir: Annotated[
        Path,
        typer.Option(
            "--state-dir",
            help="Where generated state lives. Defaults to `.playground/`.",
        ),
    ] = Path(".playground"),
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format for status reporting."),
    ] = OutputFormat.human,
) -> None:
    """Destroy ``lab``: render the same vars apply uses, then `tofu destroy`."""
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, output)

    runs_dir = state_dir / "runs"
    run, run_dir = start_run(runs_dir, "destroy", lab)
    logs_dir = run_dir / "logs"

    bus = EventBus()
    bus.subscribe(JsonlWriter(run_dir))
    bus.publish(run.run_id, "operation_started", {"operation": "destroy", "lab": lab})

    # Re-render the tfvars so tofu destroy sees the same var.vm_names
    # apply did — symmetric, deterministic, doesn't rely on stale state.
    tfvars_path = state_dir / "state" / "tofu" / f"{lab}.tfvars.json"
    tfvars_path.parent.mkdir(parents=True, exist_ok=True)
    tfvars = render_tfvars(resolved)
    tfvars_path.write_text(json.dumps(tfvars, indent=2, sort_keys=True) + "\n")

    steps: list[StepResult] = []
    bus.publish(run.run_id, "step_started", {"step": "tofu-destroy"})
    tofu_step, tofu_diagnostics = run_tofu_destroy(
        tofu_dir, tfvars_path.resolve(), logs_dir / "tofu-destroy.log"
    )
    steps.append(tofu_step)
    bus.publish(
        run.run_id, "step_finished",
        {"step": "tofu-destroy", "exit_code": tofu_step.exit_code},
    )
    if tofu_diagnostics or tofu_step.exit_code != 0:
        _fail_apply(
            output, run, run_dir, steps,
            diagnostics=tofu_diagnostics,
            summary=(
                "tofu destroy failed; some resources may remain. Inspect "
                "tofu state with `cd tofu && tofu state list` and retry."
            ),
            log_path=tofu_step.log_path,
            bus=bus,
        )

    finished = finish_run(
        run, run_dir, status="succeeded", steps=steps,
        summary=f"destroyed lab {lab!r}",
    )
    bus.publish(run.run_id, "operation_finished", {"status": "succeeded"})

    if output is OutputFormat.json:
        _print_json(finished.model_dump(mode="json", exclude_none=True))
        return

    typer.echo(f"destroyed lab {lab!r}")
    typer.echo(f"  run: {finished.run_id}")
    typer.echo(f"  record: {run_dir / 'run.json'}")


def _fail_apply(
    output: OutputFormat,
    run: OperationRun,
    run_dir: Path,
    steps: list[StepResult],
    *,
    diagnostics: list[Diagnostic],
    summary: str,
    log_path: str | None,
    bus: EventBus | None = None,
) -> NoReturn:
    """Finalize ``run`` as failed, print diagnostics + log tail, exit nonzero.

    Single-call failure protocol: this is the only path that finalizes a
    failed run record, so the "running" state can't survive on disk.
    Also publishes ``operation_finished`` (status=failed) so any
    subscribed event log gets the final marker before we exit.
    """
    finish_run(run, run_dir, status="failed", steps=steps, summary=summary)
    if bus is not None:
        bus.publish(run.run_id, "operation_finished", {"status": "failed"})
    if diagnostics:
        _print_diagnostics(diagnostics, err=True)
    if log_path:
        tail = tail_log(Path(log_path))
        if tail:
            typer.echo(f"--- tail of {log_path} ---", err=True)
            typer.echo(tail, err=True)
    typer.echo(summary, err=True)
    typer.echo(f"{run.operation} failed; run record at {run_dir / 'run.json'}", err=True)
    raise typer.Exit(code=1)


def _resolve_lab_or_exit(
    loaded: LoadedConfig,
    name: str,
    config_dir: Path,
    output: OutputFormat,
) -> ResolvedLab:
    """Shared CLI helper: unknown-lab and resolver-error gate.

    Replicates the three byte-identical try/except blocks that grew across
    ``lab show``, ``inventory render``, and ``tofu render``. Returns the
    resolved lab on success; exits with the appropriate diagnostic
    otherwise.
    """
    if name not in loaded.labs:
        _exit_with_diagnostic(
            Diagnostic(
                id="config.lab.unknown",
                severity="error",
                message=f"unknown lab {name!r}",
                source=SourceLocation(path=str(config_dir / "labs")),
                suggestion="run `playground lab list` to see configured labs",
            ),
            output,
            json_errors=False,
        )

    try:
        return resolve_lab(loaded, name)
    except (KeyError, ValueError) as exc:
        _exit_with_diagnostic(
            Diagnostic(
                id="config.lab.resolve_failed",
                severity="error",
                message=str(exc),
                source=SourceLocation(path=str(config_dir / "labs" / f"{name}.yaml")),
            ),
            output,
            json_errors=False,
        )


def _load_config_or_exit(
    config_dir: Path,
    output: OutputFormat,
) -> tuple[LoadedConfig, list[Diagnostic]]:
    try:
        return load_config(config_dir)
    except NotADirectoryError as exc:
        _exit_with_diagnostic(
            Diagnostic(
                id="config.discovery.not_directory",
                severity="error",
                message=str(exc),
                source=SourceLocation(path=str(config_dir)),
            ),
            output,
        )


def _exit_on_errors(
    diagnostics: list[Diagnostic],
    output: OutputFormat,
    *,
    json_errors: bool = True,
) -> None:
    if not _has_errors(diagnostics):
        return

    if output is OutputFormat.json and json_errors:
        _print_json(
            {
                "ok": False,
                "diagnostics": [_diagnostic_to_dict(d) for d in diagnostics],
            }
        )
    else:
        _print_diagnostics(diagnostics, err=True)
    raise typer.Exit(code=1)


def _exit_with_diagnostic(
    diagnostic: Diagnostic,
    output: OutputFormat,
    *,
    json_errors: bool = True,
) -> NoReturn:
    _exit_on_errors([diagnostic], output, json_errors=json_errors)
    raise typer.Exit(code=1)


def _has_errors(diagnostics: list[Diagnostic]) -> bool:
    return any(d.severity == "error" for d in diagnostics)


def _count_diagnostics(diagnostics: list[Diagnostic]) -> tuple[int, int]:
    errors = sum(1 for d in diagnostics if d.severity == "error")
    warnings = sum(1 for d in diagnostics if d.severity == "warning")
    return errors, warnings


def _diagnostic_to_dict(diagnostic: Diagnostic) -> dict[str, object]:
    return diagnostic.model_dump(mode="json", exclude_none=True)


def _warnings_in(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """The single source of "what counts as a warning" used by the CLI."""
    return [d for d in diagnostics if d.severity == "warning"]


def _print_warnings(diagnostics: list[Diagnostic]) -> None:
    warnings = _warnings_in(diagnostics)
    if warnings:
        _print_diagnostics(warnings, err=True)


def _print_diagnostics(diagnostics: list[Diagnostic], *, err: bool | None = None) -> None:
    for diagnostic in diagnostics:
        use_stderr = diagnostic.severity == "error" if err is None else err
        location = ""
        if diagnostic.source is not None:
            location = diagnostic.source.path
            if diagnostic.key_path:
                location = f"{location}:{diagnostic.key_path}"
        prefix = f"{diagnostic.severity.upper()} {diagnostic.id}"
        typer.echo(f"{prefix}: {diagnostic.message}", err=use_stderr)
        if location:
            typer.echo(f"  at {location}", err=use_stderr)
        if diagnostic.suggestion:
            typer.echo(
                f"  suggestion: {diagnostic.suggestion}",
                err=use_stderr,
            )


def _print_json(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
