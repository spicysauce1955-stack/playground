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
from playground.runs.operation import StepResult
from playground.state.isolation import IsolationRecord, read_isolation, write_isolation

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _run(
    op: Any, lab: str, status: Any = "succeeded", steps: list[str] | None = None,
) -> OperationRun:
    # Failed isolate runs default to having reached the mutating step.
    if steps is None:
        steps = ["attach-filter"] if (op == "isolate" and status == "failed") else []
    return OperationRun(
        steps=[
            StepResult(
                name=n, command=[n], exit_code=1, log_path="x",
                started_at="2026-01-01T00:00:00+00:00",
                finished_at="2026-01-01T00:00:01+00:00",
            )
            for n in steps
        ],
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
    # Fail closed: the record stays so apply is still guarded.
    rec = read_isolation(tmp_path / "state", "generic-infra")
    assert rec is not None and rec.state == "isolating"
    assert "partially isolated" in result.output


def test_isolate_help_does_not_claim_metadata_blocked() -> None:
    out = " ".join(CliRunner().invoke(app, ["isolate", "--help"]).output.split())
    assert "metadata" in out and "stays reachable" in out
    assert "cloud metadata," not in out


def test_isolate_human_output_does_not_mention_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_backend(monkeypatch)
    result = _invoke("isolate", "generic-infra", tmp_path)
    assert "metadata" not in result.output


def test_connect_noop_says_nothing_to_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    def fake(**kwargs: Any):
        run = _run("connect", kwargs["resolved"].lab_name).model_copy(
            update={"summary": "connected lab 'x': nothing to reopen (not isolated)"}
        )
        return run, []

    monkeypatch.setattr(local_libvirt, "execute_connect", fake)
    result = _invoke("connect", "generic-infra", tmp_path)
    assert result.exit_code == 0
    assert "nothing to restore" in result.output
    assert "egress restored" not in result.output


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
    write_isolation(tmp_path / "state", IsolationRecord(
        lab="generic-infra", backend="local-libvirt",
        isolated_at="2026-01-01T00:00:00+00:00", lab_cidrs=[], run_id="r0",
    ))
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


# ---------------------------------------------------------------------------
# Teardown must not be blocked by apply-only DigitalOcean validator errors
# ---------------------------------------------------------------------------


def _do_config_with_legacy_lab(tmp_path: Path) -> Path:
    """Copy config/ and give cloud-smoke a second network + a bad CIDR + a
    non-string size override: all three apply-only DO validator errors."""
    import shutil

    dest = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, dest)
    lab = dest / "labs" / "cloud-smoke.yaml"
    text = lab.read_text()
    text = text.replace(
        "      cidr: 10.60.0.0/24\n",
        "      cidr: 8.8.8.0/24\n    - name: extra-net\n      profile: nat\n"
        "      cidr: 10.61.0.0/24\n",
    )
    text = text.replace(
        "      networks: [lab-net]\n",
        "      networks: [lab-net]\n      provider_overrides:\n"
        "        cloud-digitalocean:\n          size: 4\n",
    )
    lab.write_text(text)
    return dest


@pytest.mark.parametrize("verb", ["destroy", "suspend", "reset"])
def test_teardown_proceeds_despite_do_validator_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verb: str,
) -> None:
    from playground.backend import cloud_digitalocean as do_backend
    from playground.config.loader import load_config
    from playground.validation import validate

    config_dir = _do_config_with_legacy_lab(tmp_path)

    # Sanity: the plain validator does flag all three as errors.
    loaded, _ = load_config(config_dir)
    ids = {d.id for d in validate(loaded, lab="cloud-smoke") if d.severity == "error"}
    assert {
        "config.network.do_single_network",
        "config.network.do_vpc_cidr_invalid",
        "config.backend.do_vm_size_invalid",
    } <= ids

    seen: list[str] = []

    def fake(**kwargs: Any):
        seen.append(verb)
        return _run(verb, kwargs["resolved"].lab_name), []

    monkeypatch.setattr(do_backend, f"execute_{verb}", fake)
    result = CliRunner().invoke(
        app,
        [verb, "cloud-smoke", "--config-dir", str(config_dir),
         "--state-dir", str(tmp_path / "state")],
    )
    assert result.exit_code == 0, result.output
    assert seen == [verb]
    assert "do_single_network" in result.output  # still reported, as a warning


def test_suspend_surfaces_diagnostics_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import json

    from playground.backend import cloud_digitalocean as do_backend

    warn = Diagnostic(
        id="runtime.suspend.orphaned_vpc", severity="warning",
        message="vpc left behind", source=SourceLocation(path="x"),
    )
    monkeypatch.setattr(
        do_backend, "execute_suspend",
        lambda **kw: (_run("suspend", kw["resolved"].lab_name), [warn]),
    )
    args = ["suspend", "cloud-smoke", "--config-dir", str(CONFIG_DIR),
            "--state-dir", str(tmp_path / "state")]
    human = CliRunner().invoke(app, args)
    assert human.exit_code == 0, human.output
    assert "vpc left behind" in human.output
    js = CliRunner().invoke(app, [*args, "-o", "json"])
    assert js.exit_code == 0, js.output
    payload = json.loads(js.stdout)
    assert [d["id"] for d in payload["diagnostics"]] == ["runtime.suspend.orphaned_vpc"]


def test_read_only_verbs_tolerate_do_validator_errors_but_apply_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    config_dir = _do_config_with_legacy_lab(tmp_path)
    base = ["--config-dir", str(config_dir), "--state-dir", str(tmp_path / "state")]
    for argv in (
        ["status", "cloud-smoke"],
        ["status"],
        ["exec", "cloud-smoke", "--on", "pg-node-1", "--", "true"],
        ["connect", "cloud-smoke"],
        ["isolate", "cloud-smoke"],
    ):
        result = CliRunner().invoke(app, [*argv[:2], *base, *argv[2:]]
                                    if len(argv) > 1 else [*argv, *base])
        assert "do_single_network" in result.output, (argv, result.output)
        assert "config.validation" not in result.output
        # a warning, not a blocking error
        assert "error: config.network.do_single_network" not in result.output, argv
    strict = CliRunner().invoke(app, ["apply", "cloud-smoke", *base])
    assert strict.exit_code == 1
    assert "do_single_network" in strict.output


def test_libvirt_isolate_failure_before_attach_keeps_prior_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Re-isolate of a live record failing early must not clear it (CLI path)."""
    monkeypatch.setattr(
        local_libvirt, "execute_isolate",
        lambda **kw: (_run("isolate", kw["resolved"].lab_name, "failed", steps=[]), []),
    )
    state = tmp_path / "state"
    write_isolation(state, IsolationRecord(
        lab="generic-infra", backend="local-libvirt",
        isolated_at="2026-01-01T00:00:00+00:00", lab_cidrs=[], run_id="r0",
    ))
    _invoke("isolate", "generic-infra", tmp_path)
    rec = read_isolation(state, "generic-infra")
    assert rec is not None and rec.run_id == "r0" and rec.state == "isolated"
