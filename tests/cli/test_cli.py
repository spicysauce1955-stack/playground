"""CLI behavior for read-only config inspection."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from playground.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def test_validate_committed_config_succeeds() -> None:
    result = CliRunner().invoke(app, ["validate", "--config-dir", str(CONFIG_DIR)])

    assert result.exit_code == 0
    assert "0 errors, 0 warnings" in result.output
    assert "ERROR" not in result.output


def test_validate_json_reports_schema_errors(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "bad.yaml").write_text("apiVersion: playground/v1\nmetadata:\n  name: bad\n")

    result = CliRunner().invoke(
        app,
        ["validate", "--config-dir", str(config_dir), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["diagnostics"][0]["id"] == "config.schema.kind_missing"


def test_lab_list_shows_committed_lab() -> None:
    result = CliRunner().invoke(app, ["lab", "list", "--config-dir", str(CONFIG_DIR)])

    assert result.exit_code == 0
    assert result.output.splitlines() == ["generic-infra"]


def test_lab_list_json_shows_committed_lab() -> None:
    result = CliRunner().invoke(
        app,
        ["lab", "list", "--config-dir", str(CONFIG_DIR), "--output", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["labs"] == [
        {
            "name": "generic-infra",
            "description": "Generic VM, Docker, and network playground.",
            "tags": ["infra", "local"],
        }
    ]


def test_lab_show_defaults_to_resolved_json() -> None:
    result = CliRunner().invoke(
        app,
        ["lab", "show", "generic-infra", "--config-dir", str(CONFIG_DIR)],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["lab_name"] == "generic-infra"
    assert payload["backend"] == "local-libvirt"
    assert [vm["name"] for vm in payload["vms"]] == ["node1", "docker1", "router1"]


def test_lab_show_unknown_lab_fails() -> None:
    result = CliRunner().invoke(
        app,
        ["lab", "show", "missing", "--config-dir", str(CONFIG_DIR)],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "config.lab.unknown" in result.stderr
