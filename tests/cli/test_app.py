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
import shlex
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


# --------------------------------------------------------------------------- #
# Coverage added below for verbs the original 8 tests above did not exercise:
# uninstall, list, screenshot, input, push, pull, shell.
# --------------------------------------------------------------------------- #


def _run_with_ssh_stdout(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, ssh_stdout, *args
):
    """Same wiring as `_run`, but lets the ssh shim emit stdout.

    Needed for `screenshot`/`ui-dump`/`pull`, which pipe the ssh session's
    stdout to a local file via `_capture_to_local` — `_run`'s shim always
    emits empty stdout, which cannot prove bytes actually reach disk.
    """
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0, stdout=ssh_stdout)
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


def test_uninstall_runs_adb_uninstall(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "uninstall", "com.example.app")
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    assert "uninstall com.example.app" in log
    # uninstall runs `adb uninstall`, not `adb shell uninstall`.
    assert "shell uninstall" not in log


def test_uninstall_quotes_shell_metacharacters_in_package_name(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    package = "com.example; rm -rf /tmp/pwned"
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "uninstall", package)
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    assert f"uninstall {shlex.quote(package)}" in log
    # The raw, unquoted metacharacters must never appear bare in the
    # command string (that would mean shell interpolation, not an argv
    # value handed to `adb uninstall`).
    assert "uninstall com.example; rm -rf /tmp/pwned" not in log


def test_list_default_lists_all_packages(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "list")
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    assert "pm list packages" in log
    assert "pm list packages -3" not in log


def test_list_third_party_adds_dash_3(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "list", "--third-party")
    assert result.exit_code == 0, result.output
    assert "pm list packages -3" in (tmp_path / "ssh.log").read_text()


def test_list_pattern_reaches_the_command(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "list", "--pattern", "com.example")
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    assert "pm list packages" in log
    assert "com.example" in log


def test_screenshot_captures_and_writes_a_file(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    out_dir = tmp_path / "shots"
    result = _run_with_ssh_stdout(
        tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "FAKEPNGBYTES",
        "screenshot", "--out", str(out_dir),
    )
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    assert "screencap -p" in log
    written = list(out_dir.glob("droid1-*.png"))
    assert len(written) == 1, written
    assert written[0].read_bytes() == b"FAKEPNGBYTES\n"


def test_input_text_sends_input_text(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "input", "text", "hello world")
    assert result.exit_code == 0, result.output
    assert "input text 'hello world'" in (tmp_path / "ssh.log").read_text()


def test_input_tap_sends_input_tap_with_coordinates(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "input", "tap", "100", "200")
    assert result.exit_code == 0, result.output
    assert "input tap 100 200" in (tmp_path / "ssh.log").read_text()


def test_input_key_maps_to_adbs_keyevent_subcommand(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "input", "key", "66")
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    # adb has no `input key`; the real subcommand is `keyevent`.
    assert "input keyevent 66" in log
    assert "input key 66" not in log


def test_input_text_quotes_shell_metacharacters(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    payload = "$(touch /tmp/pwned)"
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "input", "text", payload)
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    assert f"input text {shlex.quote(payload)}" in log
    assert "input text $(touch /tmp/pwned)" not in log


def test_push_uploads_via_scp_then_adb_push(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, write_scp_shim
) -> None:
    src = tmp_path / "payload.bin"
    src.write_bytes(b"local file contents")
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)
    write_scp_shim(tmp_path, exit_code=0)  # shares tmp_path/bin with the ssh shim
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    monkeypatch.setattr(app_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)

    result = CliRunner().invoke(
        app,
        ["app", "push", str(src), "/sdcard/dest.bin", "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir)],
    )
    assert result.exit_code == 0, result.output

    scp_log = (tmp_path / "scp.log").read_text()
    assert str(src) in scp_log
    assert "ubuntu@127.0.0.1:/tmp/playground-push-" in scp_log
    # scp operand order: local source first, then the remote destination.
    assert scp_log.index(str(src)) < scp_log.index("ubuntu@127.0.0.1:/tmp/playground-push-")

    ssh_log = (tmp_path / "ssh.log").read_text()
    # shlex.quote only adds quotes when a value needs them; these paths
    # have no shell metacharacters, so they appear bare in the command.
    assert "push /tmp/playground-push-" in ssh_log
    assert "/sdcard/dest.bin" in ssh_log
    # adb push operand order: guest-local tmp file first, device dest second.
    assert ssh_log.index("/tmp/playground-push-") < ssh_log.index("/sdcard/dest.bin")


def test_pull_downloads_file_and_writes_it_locally(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    dest = tmp_path / "pulled.bin"
    result = _run_with_ssh_stdout(
        tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "PULLED_CONTENT",
        "pull", "/sdcard/source.bin", str(dest),
    )
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    # shlex.quote leaves this path bare — no shell metacharacters in it.
    assert "pull /sdcard/source.bin" in log
    assert dest.read_bytes() == b"PULLED_CONTENT\n"


def test_shell_runs_the_raw_command_after_the_double_dash(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    monkeypatch.setattr(app_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)

    result = CliRunner().invoke(
        app,
        ["app", "shell", "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir),
         "--", "getprop", "ro.build.version.release"],
    )
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    assert "shell getprop ro.build.version.release" in log
