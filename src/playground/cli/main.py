"""Read-only CLI commands for inspecting playground configuration."""

from __future__ import annotations

import json
import re
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from playground.backend.dispatch import (
    SUPPORTED_BACKENDS,
    estimate_cost,
    execute_apply,
    execute_destroy,
    execute_reset,
    execute_resume,
    execute_suspend,
    plan_provider_summary,
    query_status,
    unsupported_backend_diagnostic,
)
from playground.backend.local_libvirt import (
    fetch_vm_ips,
    render_inventory,
    render_tfvars,
    tail_log,
)
from playground.config.loader import LoadedConfig, load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus
from playground.planner import Plan, PlanAction, render_plan
from playground.preflight import run_all_checks as run_doctor_checks
from playground.runs import OperationRun
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


@app.command("doctor")
def doctor_command(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = OutputFormat.human,
    ssh_key: Annotated[
        Path | None,
        typer.Option(
            "--ssh-key",
            help=(
                "SSH public key path the apply will inject via cloud-init. "
                "Defaults to ~/.ssh/id_rsa.pub (matching tofu's "
                "var.ssh_public_key_path default)."
            ),
        ),
    ] = None,
    backend: Annotated[
        str | None,
        typer.Option(
            "--backend",
            help=(
                "Run backend-specific checks, e.g. cloud-digitalocean. "
                "When set, only the checks relevant to that backend are run "
                "(libvirt/vbox host probes are skipped for cloud backends). "
                "Omit to run the full local-backend suite."
            ),
        ),
    ] = None,
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    state_dir: Annotated[
        Path,
        typer.Option("--state-dir", help="State directory for backend state."),
    ] = Path(".playground"),
) -> None:
    """Probe the local host for playground prerequisites.

    Without ``--backend``: runs the full local-backend suite (PATH
    binaries, libvirt group, storage pool, SSH key, AppArmor config,
    ansible collections).

    With ``--backend cloud-digitalocean``: runs a cloud-focused subset —
    token env-var present, no committed token in Git, tofu installed,
    SSH key, ansible collections, state dir writable, and provider config
    checks.  Libvirt/vbox/KVM host probes are skipped.

    Exits 0 when no errors fire; 1 if any check returned an error.
    Warnings never block the exit code.
    """
    diagnostics = run_doctor_checks(
        ssh_key_path=ssh_key,
        backend=backend,
        config_dir=config_dir,
        state_dir=state_dir,
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
        if errors == 0 and warnings == 0:
            typer.echo("All checks passed.")
        else:
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


@app.command("tui")
def tui_command(
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
        typer.Option("--state-dir", help="Generated state root."),
    ] = Path(".playground"),
) -> None:
    """Launch the operator TUI (requires the ``[tui]`` extra)."""
    try:
        from playground.tui import run_app
    except ImportError as exc:
        _exit_with_diagnostic(
            Diagnostic(
                id="runtime.tui.missing_dependency",
                severity="error",
                message=f"failed to import the TUI ({exc})",
                source=SourceLocation(path="pyproject.toml"),
                suggestion="install with `pip install -e .[tui]`",
            ),
            OutputFormat.human,
            json_errors=False,
        )
    run_app(
        config_dir=config_dir,
        tofu_dir=tofu_dir,
        ansible_dir=ansible_dir,
        state_dir=state_dir,
    )


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
    # Pass the already-loaded config to avoid parsing the config tree a second
    # and third time inside estimate_cost / plan_provider_summary.
    cost = estimate_cost(resolved, loaded=loaded)
    provider_summary = plan_provider_summary(resolved, loaded=loaded)
    plan = render_plan(resolved, warnings=warnings, cost_estimate=cost)

    if output is OutputFormat.json:
        payload = plan.model_dump(mode="json")
        payload["provider"] = provider_summary
        _print_json(payload)
        return

    _render_plan_human(plan, provider_summary=provider_summary)


def _render_plan_human(
    plan: Plan,
    *,
    provider_summary: dict[str, str] | None = None,
) -> None:
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

    if provider_summary is not None:
        typer.echo(f"Provider ({plan.backend}):")
        for key, value in provider_summary.items():
            display_key = key.replace("_", " ")
            typer.echo(f"  {display_key}: {value}")
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

    if plan.cost_estimate is not None:
        ce = plan.cost_estimate
        typer.echo("")
        typer.echo("Cost (estimated):")
        typer.echo(f"  ~${ce.hourly_usd:.4f}/hr  ~${ce.monthly_usd:.2f}/mo")
        if ce.note:
            typer.echo(f"  {ce.note}")


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
    check_idempotent: Annotated[
        bool,
        typer.Option(
            "--check-idempotent",
            help=(
                "Run apply twice; fail if any role reports `changed` on "
                "the second pass. Catches roles that mutate state on every "
                "run. Default off — only opt in for CI / pre-release."
            ),
        ),
    ] = False,
) -> None:
    """Apply ``lab``: render inputs, run tofu apply, render inventory, run Ansible."""
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, output)
    _exit_if_unsupported_backend(resolved, output)

    # JsonlWriter is attached inside execute_apply as soon as start_run
    # creates the run directory. The CLI only needs an empty bus.
    bus = EventBus()

    finished, diagnostics = execute_apply(
        resolved=resolved,
        state_dir=state_dir,
        tofu_dir=tofu_dir,
        ansible_dir=ansible_dir,
        config_dir=config_dir,
        bus=bus,
    )

    if finished is None:
        # Pre-flight rejected the apply; no run record created.
        _exit_on_errors(diagnostics, output, json_errors=False)
        return

    if finished.status == "failed":
        _present_apply_failure(output, finished, diagnostics, state_dir)
        raise typer.Exit(code=1)

    # `--check-idempotent`: run apply again and parse the second
    # ansible.log for the PLAY RECAP. Every host must report
    # `changed=0` — otherwise some role is mutating state on every
    # run. This is the regression signal the fresh-state E2E test
    # uses internally; the flag makes it available to operators.
    second_run = None
    not_idempotent_diagnostic = None
    if check_idempotent:
        bus2 = EventBus()
        second_run, second_diagnostics = execute_apply(
            resolved=resolved,
            state_dir=state_dir,
            tofu_dir=tofu_dir,
            ansible_dir=ansible_dir,
            config_dir=config_dir,
            bus=bus2,
        )
        if second_run is None or second_run.status == "failed":
            # Second-pass failure is an apply failure; surface and exit 1.
            if second_run is not None:
                _present_apply_failure(output, second_run, second_diagnostics, state_dir)
            else:
                _print_diagnostics(second_diagnostics, err=True)
            raise typer.Exit(code=1)
        not_idempotent_diagnostic = _check_apply_idempotence(
            second_run, state_dir,
        )
        if not_idempotent_diagnostic is not None:
            diagnostics.append(not_idempotent_diagnostic)

    if output is OutputFormat.json:
        payload = finished.model_dump(mode="json", exclude_none=True)
        if check_idempotent and second_run is not None:
            payload["second_apply"] = second_run.model_dump(
                mode="json", exclude_none=True
            )
        if diagnostics:
            payload["diagnostics"] = [_diagnostic_to_dict(d) for d in diagnostics]
        _print_json(payload)
        if not_idempotent_diagnostic is not None:
            raise typer.Exit(code=1)
        return

    if diagnostics:
        _print_diagnostics(diagnostics, err=True)
    typer.echo(f"applied lab {lab!r}")
    typer.echo(f"  run: {finished.run_id}")
    typer.echo(f"  record: {state_dir / 'runs' / finished.run_id / 'run.json'}")
    for step in finished.steps:
        typer.echo(f"  {step.name}: exit {step.exit_code} (log {step.log_path})")
    if check_idempotent and second_run is not None:
        typer.echo(f"  idempotence check: run {second_run.run_id}")
        if not_idempotent_diagnostic is None:
            typer.echo("  second apply: changed=0 on every host (idempotent)")
        else:
            raise typer.Exit(code=1)


_ANSIBLE_RECAP_RE = re.compile(
    r"^(?P<host>[A-Za-z0-9._-]+)\s*:\s*ok=\d+\s+changed=(?P<changed>\d+)",
    re.MULTILINE,
)


def _check_apply_idempotence(
    second_run: OperationRun, state_dir: Path,
) -> Diagnostic | None:
    """Parse the second apply's ansible.log PLAY RECAP and check
    `changed=0` per host. Return a diagnostic if any host reports
    `changed > 0`.
    """
    ansible_log_path = state_dir / "runs" / second_run.run_id / "logs" / "ansible.log"
    if not ansible_log_path.is_file():
        # No ansible.log = no ansible step ran (e.g. fully stubbed
        # tests). Treat as idempotent — there's nothing to check.
        return None
    try:
        text = ansible_log_path.read_text()
    except OSError as exc:
        return Diagnostic(
            id="runtime.apply.not_idempotent",
            severity="error",
            message=f"could not read second-apply ansible.log: {exc}",
            source=SourceLocation(path=str(ansible_log_path)),
        )

    changed_by_host: dict[str, int] = {}
    for match in _ANSIBLE_RECAP_RE.finditer(text):
        changed_by_host[match.group("host")] = int(match.group("changed"))
    non_idempotent = {h: c for h, c in changed_by_host.items() if c > 0}
    if not non_idempotent:
        return None

    summary = ", ".join(f"{h}=changed:{c}" for h, c in non_idempotent.items())
    return Diagnostic(
        id="runtime.apply.not_idempotent",
        severity="error",
        message=(
            f"second-pass apply reported changed>0 on {len(non_idempotent)} "
            f"host(s): {summary}. A role is mutating state on every run"
        ),
        source=SourceLocation(path=str(ansible_log_path)),
        suggestion=(
            f"inspect tasks reporting `changed` in {ansible_log_path}; "
            "fix the role to be no-op when state already matches "
            "(see Ansible idempotency conventions)"
        ),
    )


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


@app.command(
    "exec",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    help=(
        "Run a command on a lab VM over SSH. The remote exit code is "
        "the playground's exit code. Stdout / stderr stream through."
    ),
)
def exec_command(
    ctx: typer.Context,
    on: Annotated[str, typer.Option("--on", help="VM name within the lab.")],
    lab: Annotated[
        str | None,
        typer.Option(
            "--lab",
            help=(
                "Lab name. Defaults to the only configured lab; required "
                "when multiple labs are configured."
            ),
        ),
    ] = None,
    user: Annotated[
        str,
        typer.Option("--user", help="SSH user (default: ubuntu)."),
    ] = "ubuntu",
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    tofu_dir: Annotated[
        Path,
        typer.Option("--tofu-dir", help="OpenTofu working directory."),
    ] = Path("tofu"),
) -> None:
    command = list(ctx.args)
    if not command:
        _exit_with_diagnostic(
            Diagnostic(
                id="config.exec.no_command",
                severity="error",
                message="no command given after --on; e.g. `playground exec --on central uptime`",
                source=SourceLocation(path="<argv>"),
            ),
            OutputFormat.human,
            json_errors=False,
        )

    loaded, diagnostics = _load_config_or_exit(config_dir, OutputFormat.human)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, OutputFormat.human, json_errors=False)
    _print_warnings(diagnostics)

    # Lab defaulting: when --lab isn't given, accept the only configured
    # lab. Multi-lab projects must be explicit.
    if lab is None:
        if len(loaded.labs) == 1:
            lab = next(iter(loaded.labs))
        else:
            _exit_with_diagnostic(
                Diagnostic(
                    id="config.exec.lab_required",
                    severity="error",
                    message=(
                        f"--lab required when {len(loaded.labs)} labs are "
                        "configured; pass --lab <name>"
                    ),
                    source=SourceLocation(path=str(config_dir / "labs")),
                    suggestion=(
                        "run `playground lab list` and pass --lab <name>"
                    ),
                ),
                OutputFormat.human,
                json_errors=False,
            )

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, OutputFormat.human)

    vm_names = {vm.name for vm in resolved.vms}
    if on not in vm_names:
        _exit_with_diagnostic(
            Diagnostic(
                id="config.exec.unknown_vm",
                severity="error",
                message=(
                    f"VM {on!r} is not declared in lab {lab!r} "
                    f"(known VMs: {sorted(vm_names) or '<none>'})"
                ),
                source=SourceLocation(path=f"config/labs/{lab}.yaml"),
                key_path="spec.vms",
            ),
            OutputFormat.human,
            json_errors=False,
        )

    vm_ips, fetch_diagnostics = fetch_vm_ips(tofu_dir)
    _exit_on_errors(fetch_diagnostics, OutputFormat.human, json_errors=False)

    ip = vm_ips.get(on)
    if ip is None:
        _exit_with_diagnostic(
            Diagnostic(
                id="config.exec.vm_ip_not_found",
                severity="error",
                message=(
                    f"VM {on!r} has no IP in tofu state — "
                    "has the lab been applied?"
                ),
                source=SourceLocation(path=str(tofu_dir)),
                suggestion=f"run `playground apply {lab}` first",
            ),
            OutputFormat.human,
            json_errors=False,
        )

    ssh_argv = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
        f"{user}@{ip}",
        *command,
    ]
    try:
        completed = subprocess.run(ssh_argv, check=False)  # noqa: S603
    except FileNotFoundError as exc:
        _exit_with_diagnostic(
            Diagnostic(
                id="runtime.exec.ssh_binary_missing",
                severity="error",
                message=f"failed to launch ssh: {exc}",
                source=SourceLocation(path="ssh"),
                suggestion="install openssh-client",
            ),
            OutputFormat.human,
            json_errors=False,
        )
    raise typer.Exit(code=completed.returncode)


@app.command("status")
def status_command(
    lab: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Lab name to inspect. Omit to list every configured lab's "
                "status (useful for downstream tooling that needs to "
                "discover labs and IPs in one call)."
            ),
        ),
    ] = None,
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
    """Show observed state of ``lab`` — or every lab when ``lab`` is omitted."""
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    # Multi-lab listing path. We invoke query_status per lab and emit a
    # single envelope, so callers (the barak-deploy cross-VM test among
    # others) can discover labs + their VMs/IPs in one round trip.
    if lab is None:
        lab_names = sorted(loaded.labs)
        entries: list[LabStatus] = []
        any_diags: list[Diagnostic] = []
        for name in lab_names:
            resolved = _resolve_lab_or_exit(loaded, name, config_dir, output)
            if resolved.backend not in SUPPORTED_BACKENDS:
                # Skip labs whose backend has no adapter — surface as a
                # warning rather than blocking the whole listing.
                any_diags.append(unsupported_backend_diagnostic(resolved.backend))
                continue
            lab_status, lab_diags = query_status(resolved, tofu_dir)
            entries.append(lab_status)
            any_diags.extend(lab_diags)
        if output is OutputFormat.json:
            payload = {"labs": [s.model_dump(mode="json", exclude_none=True)
                                for s in entries]}
            if any_diags:
                payload["diagnostics"] = [_diagnostic_to_dict(d) for d in any_diags]
            _print_json(payload)
            return
        if any_diags:
            _print_diagnostics(any_diags, err=True)
        for s in entries:
            _render_status_human(s)
            typer.echo("")
        return

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, output)
    _exit_if_unsupported_backend(resolved, output)
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
    _exit_if_unsupported_backend(resolved, output)

    bus = EventBus()
    finished, diagnostics = execute_destroy(
        resolved=resolved,
        state_dir=state_dir,
        tofu_dir=tofu_dir,
        bus=bus,
        config_dir=config_dir,
    )

    if finished.status == "failed":
        _present_apply_failure(output, finished, diagnostics, state_dir)
        raise typer.Exit(code=1)

    if output is OutputFormat.json:
        _print_json(finished.model_dump(mode="json", exclude_none=True))
        return

    typer.echo(f"destroyed lab {lab!r}")
    typer.echo(f"  run: {finished.run_id}")
    typer.echo(f"  record: {state_dir / 'runs' / finished.run_id / 'run.json'}")


@app.command("reset")
def reset_command(
    lab: Annotated[str, typer.Argument(help="Lab name to scrub.")],
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
    """Scrub ``lab`` by name when tofu destroy isn't enough.

    Cleanup path of last resort: enumerates lab YAML, force-removes
    every matching libvirt domain / network / per-VM volume via virsh,
    runs ``tofu destroy`` best-effort, then deletes the lab's per-lab
    state files under ``.playground/state/``. Doesn't touch the shared
    ``ubuntu-noble.qcow2`` base image or other labs' state.

    Use this when ``playground destroy`` fails because tofu state got
    out of sync with reality (corrupt state, manual virsh undefine, lab
    YAML renamed). A second ``reset`` on a clean lab is a no-op.
    """
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, output)
    _exit_if_unsupported_backend(resolved, output)

    bus = EventBus()
    finished, reset_diagnostics = execute_reset(
        resolved=resolved,
        state_dir=state_dir,
        tofu_dir=tofu_dir,
        bus=bus,
        config_dir=config_dir,
    )

    if finished.status == "failed":
        _present_apply_failure(output, finished, reset_diagnostics, state_dir)
        raise typer.Exit(code=1)

    if output is OutputFormat.json:
        payload = finished.model_dump(mode="json", exclude_none=True)
        payload["diagnostics"] = [_diagnostic_to_dict(d) for d in reset_diagnostics]
        _print_json(payload)
        return

    if reset_diagnostics:
        _print_diagnostics(reset_diagnostics, err=True)
    typer.echo(f"reset lab {lab!r}")
    typer.echo(f"  run: {finished.run_id}")
    typer.echo(f"  record: {state_dir / 'runs' / finished.run_id / 'run.json'}")


@app.command("suspend")
def suspend_command(
    lab: Annotated[str, typer.Argument(help="Lab name to suspend.")],
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
    """Suspend ``lab`` by destroying its Droplets to stop billing.

    DigitalOcean charges for powered-off Droplets; this command destroys
    them (``tofu destroy``) then sweeps for any orphaned tagged resources.
    Local state and run history are preserved so ``playground resume`` can
    rebuild from the same config.

    NOTE: Droplets are destroyed, not snapshotted. Any in-VM disk changes
    since the last apply are not preserved.

    ``suspend`` is only meaningful for cloud backends. Local backend labs
    will receive a ``runtime.backend.verb_not_supported`` error.
    """
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, output)
    _exit_if_unsupported_backend(resolved, output)

    bus = EventBus()
    finished, diagnostics = execute_suspend(
        resolved=resolved,
        state_dir=state_dir,
        tofu_dir=tofu_dir,
        bus=bus,
        config_dir=config_dir,
    )

    if finished is None:
        # Backend does not support suspend (local backends).
        _exit_on_errors(diagnostics, output, json_errors=False)
        return

    if finished.status == "failed":
        _present_apply_failure(output, finished, diagnostics, state_dir)
        raise typer.Exit(code=1)

    if output is OutputFormat.json:
        _print_json(finished.model_dump(mode="json", exclude_none=True))
        return

    typer.echo(f"suspended lab {lab!r}")
    typer.echo("  note: Droplets were destroyed to stop billing; disk state is not preserved")
    typer.echo(f"  run: {finished.run_id}")
    typer.echo(f"  record: {state_dir / 'runs' / finished.run_id / 'run.json'}")


@app.command("resume")
def resume_command(
    lab: Annotated[str, typer.Argument(help="Lab name to resume.")],
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
    """Resume a previously suspended ``lab`` by rebuilding its Droplets.

    Rebuilds Droplets from the current config (``tofu apply``) and re-runs
    readiness checks and Ansible provisioning. This is equivalent to
    ``apply`` on a lab that was suspended.

    NOTE: Droplets are created from the base image; VM disk changes from
    before the suspend are not preserved (no snapshot).

    ``resume`` is only meaningful for cloud backends. Local backend labs
    will receive a ``runtime.backend.verb_not_supported`` error.
    """
    loaded, diagnostics = _load_config_or_exit(config_dir, output)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded))
    _exit_on_errors(diagnostics, output, json_errors=False)
    _print_warnings(diagnostics)

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, output)
    _exit_if_unsupported_backend(resolved, output)

    bus = EventBus()
    finished, diagnostics = execute_resume(
        resolved=resolved,
        state_dir=state_dir,
        tofu_dir=tofu_dir,
        ansible_dir=ansible_dir,
        config_dir=config_dir,
        bus=bus,
    )

    if finished is None:
        # Backend does not support resume (local backends).
        _exit_on_errors(diagnostics, output, json_errors=False)
        return

    if finished.status == "failed":
        _present_apply_failure(output, finished, diagnostics, state_dir)
        raise typer.Exit(code=1)

    if output is OutputFormat.json:
        payload = finished.model_dump(mode="json", exclude_none=True)
        if diagnostics:
            payload["diagnostics"] = [_diagnostic_to_dict(d) for d in diagnostics]
        _print_json(payload)
        return

    if diagnostics:
        _print_diagnostics(diagnostics, err=True)
    typer.echo(f"resumed lab {lab!r}")
    typer.echo(f"  run: {finished.run_id}")
    typer.echo(f"  record: {state_dir / 'runs' / finished.run_id / 'run.json'}")
    for step in finished.steps:
        typer.echo(f"  {step.name}: exit {step.exit_code} (log {step.log_path})")


def _present_apply_failure(
    output: OutputFormat,
    run: OperationRun,
    diagnostics: list[Diagnostic],
    state_dir: Path,
) -> None:
    """Print diagnostics + tail of the failing step's log to stderr.

    The runner already persisted the failed run record and published
    ``operation_finished`` (status=failed). This helper only handles
    CLI presentation — the TUI uses a different presentation path.
    """
    run_dir = state_dir / "runs" / run.run_id
    if diagnostics:
        _print_diagnostics(diagnostics, err=True)
    # Find the last failed step and dump the tail of its log.
    failing = next(
        (s for s in reversed(run.steps) if s.exit_code != 0), None
    )
    if failing is not None:
        tail = tail_log(Path(failing.log_path))
        if tail:
            typer.echo(f"--- tail of {failing.log_path} ---", err=True)
            typer.echo(tail, err=True)
    if run.summary:
        typer.echo(run.summary, err=True)
    typer.echo(
        f"{run.operation} failed; run record at {run_dir / 'run.json'}",
        err=True,
    )


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


def _exit_if_unsupported_backend(
    resolved: ResolvedLab, output: OutputFormat
) -> None:
    """Reject a lab whose backend has no implemented adapter.

    The validator only guarantees the backend has a ProviderConfig; this
    gate guarantees there's actually a backend adapter wired in dispatch
    before any lifecycle command tries to run it."""
    if resolved.backend not in SUPPORTED_BACKENDS:
        _exit_on_errors(
            [unsupported_backend_diagnostic(resolved.backend)],
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
