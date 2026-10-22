"""CLI tests for ``playground isolate`` / ``playground connect`` and the apply guard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from playground.backend import local_libvirt
from playground.cli import main as cli_main
from playground.cli.main import app
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.runs import OperationRun
from playground.state.isolation import IsolationRecord, read_isolation, write_isolation

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _run(op: Any, lab: str, status: Any = "succeeded") -> OperationRun:
    return OperationRun(
        run_id=f"20260101T000000Z-{op}-{lab}", operation=op, lab=lab,
        status=status, started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
    )


def _invoke(verb: str, lab: str, tmp_path: Path, *extra: str):
    return CliRunner().invoke(
        app,
        [verb, lab, "--config-dir", str(CONFIG_DIR),
         "--state-dir", str(tmp_path / "state"), *extra],
    )


def _patch_backend(monkeypatch: pytest.MonkeyPatch, status: str = "succeeded") -> list[str]:
    seen: list[str] = []

    def make(verb: str):
        def fake(**kwargs: Any):
            seen.append(verb)
            return _run(verb, kwargs["resolved"].lab_name, status), []
        return fake

    monkeypatch.setattr(local_libvirt, "execute_isolate", make("isolate"))
    monkeypatch.setattr(local_libvirt, "execute_connect", make("connect"))
    return seen


def test_isolate_human_output_and_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    seen = _patch_backend(monkeypatch)
    result = _invoke("isolate", "generic-infra", tmp_path)
    assert result.exit_code == 0, result.output
    assert seen == ["isolate"]
    assert "isolated lab 'generic-infra'" in result.output
    assert "dropped, not rejected" in result.output
    assert "playground connect generic-infra" in result.output
    assert "run.json" in result.output
    rec = read_isolation(tmp_path / "state", "generic-infra")
    assert rec is not None and rec.backend == "local-libvirt"


def test_isolate_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_backend(monkeypatch)
    result = _invoke("isolate", "generic-infra", tmp_path, "--output", "json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["operation"] == "isolate"
    assert payload["status"] == "succeeded"


def test_isolate_failed_run_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_backend(monkeypatch, status="failed")
    result = _invoke("isolate", "generic-infra", tmp_path)
    assert result.exit_code == 1
    assert "isolate failed" in result.output
    assert read_isolation(tmp_path / "state", "generic-infra") is None


def test_connect_clears_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _patch_backend(monkeypatch)
    state = tmp_path / "state"
    write_isolation(state, IsolationRecord(
        lab="generic-infra", backend="local-libvirt",
        isolated_at="2026-01-01T00:00:00+00:00", lab_cidrs=[], run_id="r0",
    ))
    result = _invoke("connect", "generic-infra", tmp_path)
    assert result.exit_code == 0, result.output
    assert seen == ["connect"]
    assert "connected lab 'generic-infra'" in result.output
    assert read_isolation(state, "generic-infra") is None


@pytest.mark.parametrize("verb", ["isolate", "connect"])
def test_vbox_exits_nonzero_with_diagnostic(verb: str, tmp_path: Path) -> None:
    result = _invoke(verb, "vbox-smoke", tmp_path)
    assert result.exit_code == 1
    assert "verb_not_supported" in result.output
    assert "only cloud backends" not in result.output


def test_unknown_lab_exits_nonzero(tmp_path: Path) -> None:
    assert _invoke("isolate", "no-such-lab", tmp_path).exit_code == 1


def test_apply_refused_on_isolated_lab(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    write_isolation(state, IsolationRecord(
        lab="generic-infra", backend="local-libvirt",
        isolated_at="2026-01-01T00:00:00+00:00", lab_cidrs=[], run_id="r0",
    ))

    def must_not_run(**kwargs: Any) -> None:
        raise AssertionError("backend apply must not run on an isolated lab")

    monkeypatch.setattr(local_libvirt, "execute_apply", must_not_run)
    result = _invoke("apply", "generic-infra", tmp_path)
    assert result.exit_code == 1
    assert "runtime.apply.lab_isolated" in result.output
    assert "playground connect generic-infra" in result.output


def test_destroy_surfaces_filter_cleanup_warnings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    def fake_destroy(**kwargs: Any):
        return _run("destroy", kwargs["resolved"].lab_name), []

    monkeypatch.setattr(local_libvirt, "execute_destroy", fake_destroy)
    monkeypatch.setattr(
        local_libvirt, "remove_isolation_filter",
        lambda lab: [Diagnostic(
            id="runtime.destroy.filter_warn", severity="warning",
            message="filter cleanup warning", source=SourceLocation(path="x"),
        )],
    )
    result = _invoke("destroy", "generic-infra", tmp_path)
    assert result.exit_code == 0, result.output
    assert "filter cleanup warning" in result.output
    assert cli_main.app is app
