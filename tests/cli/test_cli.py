"""CLI behavior for read-only config inspection."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from playground.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _write_fake_tofu(tmp_path: Path, payload: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tofu = bin_dir / "tofu"
    tofu.write_text(f"#!/usr/bin/env bash\ncat <<'EOF'\n{payload}\nEOF\n")
    tofu.chmod(tofu.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


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


def test_inventory_render_unknown_lab(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "inventory",
            "render",
            "missing-lab",
            "--config-dir",
            str(CONFIG_DIR),
            "--tofu-dir",
            str(tmp_path),
            "--out",
            str(tmp_path / "out.ini"),
        ],
    )

    assert result.exit_code == 1
    assert "config.lab.unknown" in result.stderr
    assert not (tmp_path / "out.ini").exists()


def test_inventory_render_reports_missing_tofu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")

    result = CliRunner().invoke(
        app,
        [
            "inventory",
            "render",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--tofu-dir",
            str(tmp_path),
            "--out",
            str(tmp_path / "out.ini"),
        ],
    )

    assert result.exit_code == 1
    assert "config.inventory.tofu_binary_missing" in result.stderr
    assert not (tmp_path / "out.ini").exists()


def test_inventory_render_writes_inventory_and_json_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "vm_ips": {
                "sensitive": False,
                "type": ["map", "string"],
                "value": {
                    "node1": "10.0.10.42",
                    "docker1": "10.0.10.43",
                    "router1": "10.0.10.44",
                },
            }
        }
    )
    bin_dir = _write_fake_tofu(tmp_path, payload)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    out_path = tmp_path / "out.ini"
    result = CliRunner().invoke(
        app,
        [
            "inventory",
            "render",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--tofu-dir",
            str(tmp_path),
            "--out",
            str(out_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "lab": "generic-infra",
        "path": str(out_path),
        "vm_count": 3,
    }
    body = out_path.read_text()
    assert "[playground]" in body
    assert "node1 ansible_host=10.0.10.42" in body
    assert "router1 ansible_host=10.0.10.44" in body
    assert "pg_lab=generic-infra" in body
