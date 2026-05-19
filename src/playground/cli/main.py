"""Read-only CLI commands for inspecting playground configuration."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from playground.backend.local_libvirt import (
    fetch_vm_ips,
    render_inventory,
    render_tfvars,
)
from playground.config.loader import LoadedConfig, load_config
from playground.config.resolver import resolve_lab
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.planner import Plan, PlanAction, render_plan
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
app.add_typer(lab_app, name="lab")
app.add_typer(inventory_app, name="inventory")
app.add_typer(tofu_app, name="tofu")


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
