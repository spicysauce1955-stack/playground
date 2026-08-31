"""`playground app` — install/launch/inspect apps on Redroid VMs.

``redroid-cloud`` is a ``cloud-digitalocean`` lab, so the real
``query_status`` would either report every VM ``missing`` (no
``$DIGITALOCEAN_TOKEN``) or, worse, make a live HTTP call to the
DigitalOcean API when a token happens to be present in the environment.
Either way that is not what these tests are exercising — they are
checking the adb command strings and targeting/exit-code behavior of
``playground app`` itself — so `_run` monkeypatches
``playground.cli.app_commands.query_status`` to return a canned
``LabStatus`` with ``droid1`` reachable, exactly as Task 3's target-
resolution tests build one.
"""

from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

import playground.cli.app_commands as app_commands
from playground.cli.main import app
from playground.models.status import LabStatus, VmStatus

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _stub_status(resolved, tofu_dir):
    return (
        LabStatus(
            lab=resolved.lab_name,
            backend=resolved.backend,
            expected_vms=len(resolved.vms),
            provisioned_vms=len(resolved.vms),
            vms=[
                VmStatus(
                    name=vm.name,
                    role=vm.role,
                    state="running",
                    ssh_host="127.0.0.1",
                    ssh_port=22,
                )
                for vm in resolved.vms
            ],
        ),
        [],
    )


def _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, *args):
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    monkeypatch.setattr(app_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    return CliRunner().invoke(
        app,
        ["app", *args, "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir)],
    )


def test_launch_uses_monkey_by_default(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "launch", "com.example.app")
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    assert "monkey -p com.example.app" in log
    assert "adb connect 127.0.0.1:5555" in log


def test_launch_with_url_fires_a_deep_link(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "launch", "com.example.app", "--url", "https://e.com/p")
    assert result.exit_code == 0
    log = (tmp_path / "ssh.log").read_text()
    assert "android.intent.action.VIEW" in log
    assert "monkey" not in log


def test_url_and_activity_are_mutually_exclusive(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "launch", "com.x", "--url", "https://e.com", "--activity", ".M")
    assert result.exit_code == 1
    assert "config.app.conflicting_launch" in result.output + str(result.stderr)


def test_clear_uses_pm_clear(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "clear", "com.example.app")
    assert result.exit_code == 0
    assert "pm clear com.example.app" in (tmp_path / "ssh.log").read_text()


def test_ui_dump_invokes_uiautomator(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "ui-dump", "--out", str(tmp_path / "ui.xml"))
    assert result.exit_code == 0, result.output
    assert "uiautomator dump" in (tmp_path / "ssh.log").read_text()


def test_logcat_follow_rejects_multi_device_targeting(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "logcat", "--follow", "--all")
    assert result.exit_code == 1
    assert "config.app.follow_needs_one_device" in result.output + str(result.stderr)


def test_failure_on_a_device_exits_nonzero(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=1)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    monkeypatch.setattr(app_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    result = CliRunner().invoke(
        app,
        ["app", "stop", "com.x", "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir)],
    )
    assert result.exit_code == 1


def test_install_rejects_a_missing_apk(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "install", str(tmp_path / "nope.apk"))
    assert result.exit_code == 1
    assert "config.app.apk_not_found" in result.output + str(result.stderr)
