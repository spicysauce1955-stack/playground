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


def _write_apply_shims(
    tmp_path: Path,
    *,
    tofu_apply_exit: int = 0,
    tofu_destroy_exit: int = 0,
    ansible_exit: int = 0,
    vm_ips_payload: str | None = None,
) -> Path:
    """Write tofu + ansible-playbook shims handling apply/destroy/output.

    Each `tofu <verb>` returns the corresponding exit code; `tofu output
    -json` returns ``vm_ips_payload``. ansible-playbook exits with
    ``ansible_exit``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    default_ips = (
        '{"vm_ips": {"sensitive": false, "type": ["map","string"], '
        '"value": {"node1":"10.0.10.42","docker1":"10.0.10.43","router1":"10.0.10.44"}}}'
    )
    payload = vm_ips_payload if vm_ips_payload is not None else default_ips
    tofu = bin_dir / "tofu"
    tofu.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f"  apply) echo 'tofu apply ok'; exit {tofu_apply_exit} ;;\n"
        f"  destroy) echo 'tofu destroy ok'; exit {tofu_destroy_exit} ;;\n"
        f"  output) cat <<'PAYLOAD'\n{payload}\nPAYLOAD\n   ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    tofu.chmod(tofu.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    ansible = bin_dir / "ansible-playbook"
    ansible.write_text(
        f"#!/usr/bin/env bash\necho ansible ran\nexit {ansible_exit}\n"
    )
    ansible.chmod(ansible.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def test_validate_committed_config_succeeds() -> None:
    result = CliRunner().invoke(app, ["validate", "--config-dir", str(CONFIG_DIR)])

    assert result.exit_code == 0
    # generic-infra has docker1 with explicit per-VM resources, so the
    # local-libvirt backend-capability warning fires. No errors.
    assert "0 errors, 1 warnings" in result.output
    assert "config.backend.per_vm_resources_unsupported" in result.output
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
    # Warnings land on stderr via _print_warnings; stdout stays clean.
    assert result.stdout.splitlines() == ["generic-infra"]
    assert "config.backend.per_vm_resources_unsupported" in result.stderr


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


def test_apply_happy_path_writes_run_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_apply_shims(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    # The CLI shells out to `tofu` and `ansible-playbook` — both are
    # PATH-shimmed above. State lives entirely under tmp_path/.playground/.
    state_dir = tmp_path / ".playground"
    ansible_dir = tmp_path / "ansible"
    ansible_dir.mkdir()
    (ansible_dir / "site.yml").write_text("- name: stub\n  hosts: playground\n")
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "apply",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--tofu-dir",
            str(tofu_dir),
            "--ansible-dir",
            str(ansible_dir),
            "--state-dir",
            str(state_dir),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "succeeded"
    assert payload["operation"] == "apply"
    assert payload["lab"] == "generic-infra"
    assert [s["name"] for s in payload["steps"]] == ["tofu-apply", "ansible-playbook"]
    assert all(s["exit_code"] == 0 for s in payload["steps"])
    # Run record on disk matches the printed payload.
    run_dir = state_dir / "runs" / payload["run_id"]
    on_disk = json.loads((run_dir / "run.json").read_text())
    assert on_disk == payload
    # Tofu vars + inventory written to standard locations.
    assert (state_dir / "state" / "tofu" / "generic-infra.tfvars.json").exists()
    assert (state_dir / "state" / "inventory" / "generic-infra.ini").exists()


def test_apply_tofu_failure_leaves_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_apply_shims(tmp_path, tofu_apply_exit=2)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    state_dir = tmp_path / ".playground"
    ansible_dir = tmp_path / "ansible"
    ansible_dir.mkdir()
    (ansible_dir / "site.yml").write_text("")
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "apply",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--tofu-dir",
            str(tofu_dir),
            "--ansible-dir",
            str(ansible_dir),
            "--state-dir",
            str(state_dir),
        ],
    )

    assert result.exit_code == 1
    assert "apply failed" in result.stderr
    assert "tofu apply ok" in result.stderr  # tail of failing log was dumped
    # Exactly one run record exists, marked failed, with only tofu in steps.
    runs = list((state_dir / "runs").iterdir())
    assert len(runs) == 1
    record = json.loads((runs[0] / "run.json").read_text())
    assert record["status"] == "failed"
    assert [s["name"] for s in record["steps"]] == ["tofu-apply"]
    assert record["steps"][0]["exit_code"] == 2


def test_apply_ansible_failure_after_tofu_success_records_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The operationally-dangerous path: tofu succeeded so VMs are alive,
    # but ansible failed so they're unconfigured. The run summary must
    # tell the operator what state they're in.
    bin_dir = _write_apply_shims(tmp_path, tofu_apply_exit=0, ansible_exit=2)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    state_dir = tmp_path / ".playground"
    ansible_dir = tmp_path / "ansible"
    ansible_dir.mkdir()
    (ansible_dir / "site.yml").write_text("")
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "apply",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--tofu-dir",
            str(tofu_dir),
            "--ansible-dir",
            str(ansible_dir),
            "--state-dir",
            str(state_dir),
        ],
    )

    assert result.exit_code == 1
    runs = list((state_dir / "runs").iterdir())
    assert len(runs) == 1
    record = json.loads((runs[0] / "run.json").read_text())
    assert record["status"] == "failed"
    # Both steps recorded; tofu succeeded, ansible failed.
    assert [s["name"] for s in record["steps"]] == ["tofu-apply", "ansible-playbook"]
    assert record["steps"][0]["exit_code"] == 0
    assert record["steps"][1]["exit_code"] == 2
    # Summary tells the operator the state is partial and what to do.
    assert "VMs were provisioned" in record["summary"]
    assert "tofu destroy" in record["summary"]
    # Inventory + tfvars files both exist on disk (we got past render).
    assert (state_dir / "state" / "tofu" / "generic-infra.tfvars.json").exists()
    assert (state_dir / "state" / "inventory" / "generic-infra.ini").exists()


def test_destroy_happy_path_writes_run_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_apply_shims(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    state_dir = tmp_path / ".playground"
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "destroy",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--tofu-dir",
            str(tofu_dir),
            "--state-dir",
            str(state_dir),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "succeeded"
    assert payload["operation"] == "destroy"
    assert [s["name"] for s in payload["steps"]] == ["tofu-destroy"]
    assert payload["steps"][0]["exit_code"] == 0
    # tfvars re-rendered for symmetry with apply
    assert (state_dir / "state" / "tofu" / "generic-infra.tfvars.json").exists()


def test_destroy_tofu_failure_leaves_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_apply_shims(tmp_path, tofu_destroy_exit=1)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    state_dir = tmp_path / ".playground"
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "destroy",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--tofu-dir",
            str(tofu_dir),
            "--state-dir",
            str(state_dir),
        ],
    )

    assert result.exit_code == 1
    runs = list((state_dir / "runs").iterdir())
    assert len(runs) == 1
    record = json.loads((runs[0] / "run.json").read_text())
    assert record["status"] == "failed"
    assert record["operation"] == "destroy"
    assert record["steps"][0]["exit_code"] == 1
    assert "tofu destroy failed" in record["summary"]


def test_apply_unknown_lab_fails_before_running_tofu(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "apply",
            "ghost-lab",
            "--config-dir",
            str(CONFIG_DIR),
            "--tofu-dir",
            str(tmp_path),
            "--ansible-dir",
            str(tmp_path),
            "--state-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "config.lab.unknown" in result.stderr
    # No run record should be created — we exit before start_run.
    assert not (tmp_path / "runs").exists()


def test_plan_renders_human_summary() -> None:
    result = CliRunner().invoke(
        app, ["plan", "generic-infra", "--config-dir", str(CONFIG_DIR)]
    )

    assert result.exit_code == 0
    assert "Plan for lab 'generic-infra' (backend: local-libvirt)" in result.stdout
    # Spot-checks across the three sections
    assert "+ edge  nat network on 10.20.10.0/24" in result.stdout
    assert "+ docker1  docker-host on ubuntu-noble" in result.stdout
    assert "+ demo-compose  compose -> role:docker-host" in result.stdout
    assert "fits: yes" in result.stdout
    # Warning from validation surfaces on stderr via _print_warnings.
    assert "config.backend.per_vm_resources_unsupported" in result.stderr


def test_plan_renders_json_payload() -> None:
    result = CliRunner().invoke(
        app,
        ["plan", "generic-infra", "--config-dir", str(CONFIG_DIR), "-o", "json"],
    )

    assert result.exit_code == 0
    plan = json.loads(result.stdout)
    assert plan["lab_name"] == "generic-infra"
    assert plan["backend"] == "local-libvirt"
    assert plan["offline"] is False
    verbs = {a["verb"] for a in plan["actions"]}
    assert verbs == {"create"}
    types = {a["resource_type"] for a in plan["actions"]}
    assert types == {"network", "vm", "workload"}
    assert plan["budget"]["fits"] is True
    # Warnings carried forward into the plan model for machine-readable
    # consumption.
    warning_ids = {w["id"] for w in plan["warnings"]}
    assert "config.backend.per_vm_resources_unsupported" in warning_ids


def test_plan_unknown_lab_fails() -> None:
    result = CliRunner().invoke(
        app, ["plan", "ghost-lab", "--config-dir", str(CONFIG_DIR)]
    )

    assert result.exit_code == 1
    assert "config.lab.unknown" in result.stderr


def test_tofu_render_unknown_lab(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "tofu",
            "render",
            "missing-lab",
            "--config-dir",
            str(CONFIG_DIR),
            "--out",
            str(tmp_path / "out.json"),
        ],
    )

    assert result.exit_code == 1
    assert "config.lab.unknown" in result.stderr
    assert not (tmp_path / "out.json").exists()


def test_tofu_render_writes_tfvars_and_json_payload(tmp_path: Path) -> None:
    out_path = tmp_path / "generic-infra.tfvars.json"
    result = CliRunner().invoke(
        app,
        [
            "tofu",
            "render",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--out",
            str(out_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "lab": "generic-infra",
        "path": str(out_path),
        "vars": ["vm_names"],
    }
    assert json.loads(out_path.read_text()) == {
        "vm_names": ["node1", "docker1", "router1"]
    }
    # Backend-capability warning surfaces during validate, on stderr
    assert "config.backend.per_vm_resources_unsupported" in result.stderr


def test_tofu_render_default_destination_and_apply_hint(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        result = runner.invoke(
            app,
            ["tofu", "render", "generic-infra", "--config-dir", str(CONFIG_DIR)],
        )

        assert result.exit_code == 0
        default_path = Path(cwd) / ".playground" / "state" / "tofu" / "generic-infra.tfvars.json"
        assert default_path.exists()
        assert json.loads(default_path.read_text()) == {
            "vm_names": ["node1", "docker1", "router1"]
        }
        # Apply hint must use the absolute resolved path so it works no
        # matter what cwd the operator runs `tofu` from.
        assert f"-var-file={default_path.resolve()}" in result.stdout
        assert "tofu -chdir=tofu apply" in result.stdout


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
