"""CLI behavior for read-only config inspection."""

from __future__ import annotations

import json
import os
import shlex
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


def _write_reset_shims(
    tmp_path: Path, *, tofu_destroy_exit: int = 0
) -> Path:
    """PATH shim with virsh + tofu for `playground reset` integration tests.

    virsh emits canned listings keyed on the subcommand so the scrub
    sees ``node1`` / ``edge`` etc. as present, then every other call
    (destroy / undefine / vol-delete) succeeds. tofu destroy is
    parametrizable to validate the best-effort tofu warning path.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    virsh = bin_dir / "virsh"
    virsh.write_text(
        "#!/usr/bin/env bash\n"
        '# strip global flags so positional logic is easier\n'
        "args=()\n"
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in --quiet|--connect) shift; [ "$1" != "" ] && [ "${1:0:1}" != "-" ] && shift ;;\n'
        '    *) args+=("$1"); shift ;;\n'
        "  esac\n"
        "done\n"
        '# The --connect URI follows --connect; the strip above already handled it.\n'
        'case "${args[0]}" in\n'
        '  list) echo "node1"; echo "docker1"; echo "router1" ;;\n'
        '  net-list) echo "edge"; echo "lab-private"; echo "routed-a" ;;\n'
        '  vol-list)\n'
        '    echo " Name                Path"\n'
        '    echo "-------------------------------------"\n'
        '    for vm in node1 docker1 router1; do\n'
        '      echo " ${vm}.qcow2          /var/lib/libvirt/images/${vm}.qcow2"\n'
        '      echo " commoninit-${vm}.iso /var/lib/libvirt/images/commoninit-${vm}.iso"\n'
        '    done\n'
        '    echo " ubuntu-noble.qcow2     /var/lib/libvirt/images/ubuntu-noble.qcow2"\n'
        '    ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    virsh.chmod(virsh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    tofu = bin_dir / "tofu"
    tofu.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f"  destroy) exit {tofu_destroy_exit} ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    tofu.chmod(tofu.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def test_reset_full_pipeline_scrubs_and_cleans_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: virsh + tofu shimmed, lab state files pre-populated,
    `playground reset` removes everything. Validates step ordering and
    cleanup contract together."""
    bin_dir = _write_reset_shims(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    state_dir = tmp_path / ".playground"
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    # Pre-populate the per-lab state files the cleanup step targets.
    tfvars = state_dir / "state" / "tofu" / "generic-infra.tfvars.json"
    inventory = state_dir / "state" / "inventory" / "generic-infra.ini"
    workloads = state_dir / "state" / "workloads" / "generic-infra"
    tfvars.parent.mkdir(parents=True, exist_ok=True)
    inventory.parent.mkdir(parents=True, exist_ok=True)
    workloads.mkdir(parents=True, exist_ok=True)
    tfvars.write_text("{}\n")
    inventory.write_text("[playground]\n")
    (workloads / "stale.txt").write_text("leftover\n")

    result = CliRunner().invoke(
        app,
        [
            "reset",
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

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["operation"] == "reset"
    assert payload["status"] == "succeeded"
    assert [s["name"] for s in payload["steps"]] == [
        "scrub-libvirt",
        "tofu-destroy",
        "clean-state-files",
    ]
    assert all(s["exit_code"] == 0 for s in payload["steps"])

    # Per-lab state files were removed; the inventory + workloads dir
    # are gone. tofu working dir is untouched.
    assert not inventory.exists()
    # tfvars gets re-rendered by execute_reset before scrub, then
    # cleaned up at the end — assert post-state, not in-flight state.
    assert not tfvars.exists()
    assert not workloads.exists()
    assert tofu_dir.exists()

    # The run record on disk matches the printed payload.
    run_dir = state_dir / "runs" / payload["run_id"]
    on_disk = json.loads((run_dir / "run.json").read_text())
    assert on_disk["operation"] == "reset"


def test_reset_continues_when_tofu_destroy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tofu destroy is best-effort: a non-zero exit becomes a warning,
    but the cleanup step still runs and the overall reset succeeds."""
    bin_dir = _write_reset_shims(tmp_path, tofu_destroy_exit=2)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    state_dir = tmp_path / ".playground"
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    tfvars = state_dir / "state" / "tofu" / "generic-infra.tfvars.json"
    tfvars.parent.mkdir(parents=True, exist_ok=True)
    tfvars.write_text("{}\n")

    result = CliRunner().invoke(
        app,
        [
            "reset",
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

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "succeeded"
    # The tofu warning is on the diagnostics list.
    diag_ids = [d["id"] for d in payload["diagnostics"]]
    assert "runtime.reset.tofu_destroy_warning" in diag_ids
    # State files still cleaned despite tofu failure.
    assert not tfvars.exists()


def test_reset_invokes_execute_reset_and_prints_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from playground.cli import main as cli_main
    from playground.runs import OperationRun

    captured: dict[str, object] = {}

    def _stub_execute_reset(**kwargs: object) -> tuple[OperationRun, list[object]]:
        captured.update(kwargs)
        run = OperationRun(
            run_id="20260519T000000Z-reset-generic-infra",
            operation="reset",
            lab="generic-infra",
            status="succeeded",
            started_at="2026-05-19T00:00:00+00:00",
            finished_at="2026-05-19T00:00:01+00:00",
            steps=[],
            summary="reset lab 'generic-infra' (scrubbed by name)",
        )
        return run, []

    monkeypatch.setattr(cli_main, "execute_reset", _stub_execute_reset)
    result = CliRunner().invoke(
        app,
        [
            "reset",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--state-dir",
            str(tmp_path / "state"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "reset lab 'generic-infra'" in result.stdout
    assert "20260519T000000Z-reset-generic-infra" in result.stdout
    # Sanity: the CLI passed the resolved lab + state dir through.
    assert captured["state_dir"] == tmp_path / "state"


def test_reset_surfaces_failure_with_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from playground.cli import main as cli_main
    from playground.models.diagnostic import Diagnostic
    from playground.runs import OperationRun, StepResult

    def _stub_execute_reset(**_kwargs: object) -> tuple[OperationRun, list[Diagnostic]]:
        run = OperationRun(
            run_id="failed-run",
            operation="reset",
            lab="generic-infra",
            status="failed",
            started_at="2026-05-19T00:00:00+00:00",
            finished_at="2026-05-19T00:00:01+00:00",
            steps=[
                StepResult(
                    name="scrub-libvirt",
                    command=["virsh"],
                    exit_code=127,
                    log_path=str(tmp_path / "scrub.log"),
                    started_at="2026-05-19T00:00:00+00:00",
                    finished_at="2026-05-19T00:00:01+00:00",
                )
            ],
            summary="reset aborted",
        )
        return run, [
            Diagnostic(
                id="runtime.reset.virsh_missing",
                severity="error",
                message="virsh missing",
            )
        ]

    monkeypatch.setattr(cli_main, "execute_reset", _stub_execute_reset)
    result = CliRunner().invoke(
        app,
        [
            "reset",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--state-dir",
            str(tmp_path / "state"),
        ],
    )
    assert result.exit_code == 1
    assert "runtime.reset.virsh_missing" in result.stderr


def test_reset_json_output_includes_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from playground.cli import main as cli_main
    from playground.models.diagnostic import Diagnostic
    from playground.runs import OperationRun

    def _stub_execute_reset(**_kwargs: object) -> tuple[OperationRun, list[Diagnostic]]:
        run = OperationRun(
            run_id="r1",
            operation="reset",
            lab="generic-infra",
            status="succeeded",
            started_at="2026-05-19T00:00:00+00:00",
            finished_at="2026-05-19T00:00:01+00:00",
            steps=[],
            summary="reset done",
        )
        return run, [
            Diagnostic(
                id="runtime.reset.tofu_destroy_warning",
                severity="warning",
                message="tofu destroy exited 1",
            )
        ]

    monkeypatch.setattr(cli_main, "execute_reset", _stub_execute_reset)
    result = CliRunner().invoke(
        app,
        [
            "reset",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--state-dir",
            str(tmp_path / "state"),
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["operation"] == "reset"
    assert payload["status"] == "succeeded"
    assert payload["diagnostics"][0]["id"] == "runtime.reset.tofu_destroy_warning"


def test_doctor_all_passing_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from playground.cli import main as cli_main

    monkeypatch.setattr(cli_main, "run_doctor_checks", lambda **_kw: [])
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "All checks passed." in result.stdout


def test_doctor_json_output_collects_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    from playground.cli import main as cli_main
    from playground.models.diagnostic import Diagnostic

    diagnostics = [
        Diagnostic(
            id="runtime.doctor.iso_tool_missing",
            severity="error",
            message="genisoimage missing",
        ),
        Diagnostic(
            id="runtime.doctor.apparmor_libvirt_unconfigured",
            severity="warning",
            message="apparmor warning",
        ),
    ]
    monkeypatch.setattr(cli_main, "run_doctor_checks", lambda **_kw: diagnostics)
    result = CliRunner().invoke(app, ["doctor", "--output", "json"])
    assert result.exit_code == 1  # any error -> exit 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    ids = [d["id"] for d in payload["diagnostics"]]
    assert ids == [
        "runtime.doctor.iso_tool_missing",
        "runtime.doctor.apparmor_libvirt_unconfigured",
    ]


def test_doctor_warnings_only_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from playground.cli import main as cli_main
    from playground.models.diagnostic import Diagnostic

    monkeypatch.setattr(
        cli_main,
        "run_doctor_checks",
        lambda **_kw: [
            Diagnostic(
                id="runtime.doctor.default_pool_no_autostart",
                severity="warning",
                message="autostart off",
            )
        ],
    )
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "0 errors, 1 warnings" in result.stdout


def test_doctor_passes_ssh_key_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from playground.cli import main as cli_main

    captured: dict[str, Path | None] = {}

    def _capture(**kwargs: object) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cli_main, "run_doctor_checks", _capture)
    result = CliRunner().invoke(app, ["doctor", "--ssh-key", "/tmp/key.pub"])
    assert result.exit_code == 0
    assert captured["ssh_key_path"] == Path("/tmp/key.pub")


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
    assert "generic-infra" in result.stdout.splitlines()
    assert "barak-deploy-cross-vm" in result.stdout.splitlines()
    assert "config.backend.per_vm_resources_unsupported" in result.stderr


def test_lab_list_json_shows_committed_lab() -> None:
    result = CliRunner().invoke(
        app,
        ["lab", "list", "--config-dir", str(CONFIG_DIR), "--output", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    names = {lab["name"] for lab in payload["labs"]}
    assert names == {"generic-infra", "barak-deploy-cross-vm"}
    generic = next(lab for lab in payload["labs"] if lab["name"] == "generic-infra")
    assert generic == {
        "name": "generic-infra",
        "description": "Generic VM, Docker, and network playground.",
        "tags": ["infra", "local"],
    }


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
    assert "tear down via destroy" in record["summary"]
    # Inventory + tfvars files both exist on disk (we got past render).
    assert (state_dir / "state" / "tofu" / "generic-infra.tfvars.json").exists()
    assert (state_dir / "state" / "inventory" / "generic-infra.ini").exists()


def test_apply_writes_events_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_apply_shims(tmp_path)
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
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    run_id = json.loads(result.stdout)["run_id"]
    events_path = state_dir / "runs" / run_id / "events.jsonl"
    assert events_path.exists()
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    # Filter out streaming log_line events to assert on the lifecycle skeleton.
    skeleton = [e["type"] for e in events if e["type"] != "log_line"]
    assert skeleton == [
        "operation_started",
        "step_started",   # tofu-apply
        "step_finished",
        "step_started",   # ansible-playbook
        "step_finished",
        "operation_finished",
    ]
    # And the streamed lines are present in the same log file.
    log_lines = [e["payload"]["line"] for e in events if e["type"] == "log_line"]
    assert any("tofu apply ok" in line for line in log_lines)
    assert any("ansible ran" in line for line in log_lines)
    assert events[-1]["payload"] == {"status": "succeeded"}


def test_apply_failure_still_emits_operation_finished_event(
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
            "apply", "generic-infra",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tofu_dir),
            "--ansible-dir", str(ansible_dir),
            "--state-dir", str(state_dir),
        ],
    )

    assert result.exit_code == 1
    [run_dir] = list((state_dir / "runs").iterdir())
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["type"] == "operation_finished"
    assert events[-1]["payload"] == {"status": "failed"}


def test_runs_list_shows_recent_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_apply_shims(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    state_dir = tmp_path / ".playground"
    ansible_dir = tmp_path / "ansible"
    ansible_dir.mkdir()
    (ansible_dir / "site.yml").write_text("")
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    # Run one apply so there's a run on disk.
    CliRunner().invoke(
        app,
        [
            "apply", "generic-infra",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tofu_dir),
            "--ansible-dir", str(ansible_dir),
            "--state-dir", str(state_dir),
        ],
    )

    result = CliRunner().invoke(
        app,
        ["runs", "list", "--state-dir", str(state_dir), "--output", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["operation"] == "apply"
    assert payload["runs"][0]["status"] == "succeeded"


def test_runs_list_empty_when_no_runs(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["runs", "list", "--state-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "No operation runs recorded yet" in result.stdout


def test_runs_show_unknown_run_fails(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["runs", "show", "20260101T000000Z-apply-ghost", "--state-dir", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "config.runs.unknown" in result.stderr


def test_runs_show_reports_recorded_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_apply_shims(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    state_dir = tmp_path / ".playground"
    ansible_dir = tmp_path / "ansible"
    ansible_dir.mkdir()
    (ansible_dir / "site.yml").write_text("")
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    apply_result = CliRunner().invoke(
        app,
        [
            "apply", "generic-infra",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tofu_dir),
            "--ansible-dir", str(ansible_dir),
            "--state-dir", str(state_dir),
            "--output", "json",
        ],
    )
    run_id = json.loads(apply_result.stdout)["run_id"]

    result = CliRunner().invoke(
        app,
        [
            "runs", "show", run_id,
            "--state-dir", str(state_dir),
            "--output", "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run"]["run_id"] == run_id
    assert payload["run"]["status"] == "succeeded"
    assert payload["events_path"] is not None
    assert "logs" in payload["logs_dir"]


def _write_ssh_shim(
    tmp_path: Path, *, exit_code: int = 0, stdout: str = ""
) -> Path:
    """PATH-shimmed `ssh` that records its argv to a log file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / "ssh.log"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > {shlex.quote(str(log_path))}\n'
        + (f'echo {shlex.quote(stdout)}\n' if stdout else "")
        + f"exit {exit_code}\n"
    )
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def test_exec_happy_path_invokes_ssh_with_resolved_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_apply_shims(tmp_path)  # provides `tofu output -json`
    ssh_bin = _write_ssh_shim(tmp_path, exit_code=0, stdout="up 12 days")
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )

    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "exec",
            "--lab", "generic-infra",
            "--on", "docker1",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tofu_dir),
            "uptime",
        ],
    )

    assert result.exit_code == 0, result.stderr
    # ssh got the resolved IP and the command at the tail.
    ssh_log = (tmp_path / "ssh.log").read_text().splitlines()
    assert "ubuntu@10.0.10.43" in ssh_log  # docker1's IP from the fake tofu shim
    assert "uptime" in ssh_log


def test_exec_propagates_remote_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_apply_shims(tmp_path)
    ssh_bin = _write_ssh_shim(tmp_path, exit_code=42)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "exec",
            "--lab", "generic-infra",
            "--on", "docker1",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tofu_dir),
            "false",
        ],
    )

    assert result.exit_code == 42


def test_exec_unknown_vm_fails(tmp_path: Path) -> None:
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()
    result = CliRunner().invoke(
        app,
        [
            "exec",
            "--lab", "generic-infra",
            "--on", "ghost-vm",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tofu_dir),
            "uptime",
        ],
    )

    assert result.exit_code == 1
    assert "config.exec.unknown_vm" in result.stderr


def test_exec_no_command_fails(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "exec",
            "--lab", "generic-infra",
            "--on", "docker1",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "config.exec.no_command" in result.stderr


def test_exec_defaults_lab_when_only_one_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolated config dir with just one lab — exec should pick it up
    # without an explicit --lab flag.
    config_dir = tmp_path / "config"
    import shutil as _shutil
    _shutil.copytree(CONFIG_DIR, config_dir)
    # Drop the second committed lab so the single-lab branch fires.
    (config_dir / "labs" / "barak-deploy-cross-vm.yaml").unlink()
    (config_dir / "roles" / "deployment-source.yaml").unlink()
    (config_dir / "roles" / "deployment-target.yaml").unlink()

    bin_dir = _write_apply_shims(tmp_path)
    ssh_bin = _write_ssh_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "exec",
            "--on", "docker1",
            "--config-dir", str(config_dir),
            "--tofu-dir", str(tofu_dir),
            "uptime",
        ],
    )

    assert result.exit_code == 0, result.stderr


def test_exec_requires_lab_when_multiple_configured(tmp_path: Path) -> None:
    # Committed config has TWO labs (generic-infra + barak-deploy-cross-vm).
    # Without --lab, exec should refuse rather than guess.
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()
    result = CliRunner().invoke(
        app,
        [
            "exec",
            "--on", "docker1",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tofu_dir),
            "uptime",
        ],
    )

    assert result.exit_code == 1
    assert "config.exec.lab_required" in result.stderr


def test_status_reports_all_vms_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_apply_shims(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "status",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--tofu-dir",
            str(tofu_dir),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["lab"] == "generic-infra"
    assert payload["expected_vms"] == 3
    assert payload["provisioned_vms"] == 3
    assert {v["state"] for v in payload["vms"]} == {"provisioned"}


def test_status_human_output_lists_each_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_apply_shims(
        tmp_path,
        vm_ips_payload='{"vm_ips": {"value": {"docker1": "10.0.10.43"}}}',
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "status",
            "generic-infra",
            "--config-dir",
            str(CONFIG_DIR),
            "--tofu-dir",
            str(tofu_dir),
        ],
    )

    assert result.exit_code == 0
    assert "1 of 3 VMs provisioned" in result.stdout
    assert "+ docker1" in result.stdout  # provisioned VM
    assert "- node1" in result.stdout    # missing
    assert "- router1" in result.stdout  # missing


def test_status_does_not_create_a_run_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # status is read-only — per requirements §5.10 it must not leave a
    # run record on disk.
    bin_dir = _write_apply_shims(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()
    state_dir = tmp_path / ".playground"

    # Even though `status` doesn't have a --state-dir flag, we point
    # cwd elsewhere so .playground/ wouldn't be in our repo. Invoke
    # via isolated filesystem to be safe.
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        result = runner.invoke(
            app,
            [
                "status",
                "generic-infra",
                "--config-dir",
                str(CONFIG_DIR),
                "--tofu-dir",
                str(tofu_dir),
            ],
        )
        assert result.exit_code == 0
        assert not (Path(cwd) / ".playground" / "runs").exists()
    # Belt-and-braces: explicit state_dir we passed also has no runs.
    assert not (state_dir / "runs").exists()


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
    assert payload["ok"] is True
    assert payload["lab"] == "generic-infra"
    assert payload["path"] == str(out_path)
    # generic-infra has 3 networks + per-VM network attachments, so the
    # renderer emits more than just vm_names. No IPs pinned in this lab,
    # so vm_dns_hosts is absent; dns_domain defaults to <lab>.lab.
    assert set(payload["vars"]) == {
        "vm_names",
        "networks",
        "vm_networks",
        "dns_domain",
    }
    on_disk = json.loads(out_path.read_text())
    assert on_disk["vm_names"] == ["node1", "docker1", "router1"]
    assert {"name": "edge", "cidr": "10.20.10.0/24"} in on_disk["networks"]
    assert on_disk["vm_networks"]["docker1"] == ["edge", "lab-private"]
    assert on_disk["dns_domain"] == "generic-infra.lab"
    assert "vm_network_ips" not in on_disk
    assert "vm_dns_hosts" not in on_disk
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
        on_disk = json.loads(default_path.read_text())
        assert on_disk["vm_names"] == ["node1", "docker1", "router1"]
        assert "networks" in on_disk
        assert "vm_networks" in on_disk
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
