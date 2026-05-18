"""Read-only CLI commands for inspecting playground configuration."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from playground.config.loader import LoadedConfig, load_config
from playground.config.resolver import resolve_lab
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.validation import validate as validate_loaded_config


class OutputFormat(StrEnum):
    human = "human"
    json = "json"


app = typer.Typer(no_args_is_help=True, help="Inspect playground lab configuration.")
lab_app = typer.Typer(no_args_is_help=True, help="Inspect configured labs.")
app.add_typer(lab_app, name="lab")


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
        resolved = resolve_lab(loaded, name)
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


def _print_warnings(diagnostics: list[Diagnostic]) -> None:
    warnings = [d for d in diagnostics if d.severity == "warning"]
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
