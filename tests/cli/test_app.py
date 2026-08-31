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
import stat
from pathlib import Path

from typer.testing import CliRunner

import playground.cli.app_commands as app_commands
from playground.cli.main import app
from playground.models.status import LabStatus, VmStatus

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _remote_command_line(ssh_log: str) -> str:
    """Extract the actual remote command from a `write_ssh_shim` log.

    `write_ssh_shim` records one ssh argv element per line; the remote
    command (this module's `-o ...` options, `user@host`, then the
    command string) is always the LAST element, and never itself
    contains a literal newline, so it is always the log's last line.
    """
    return ssh_log.rstrip("\n").splitlines()[-1]


def _simulate_device_argv(cmd: str) -> list[str]:
    """Approximate the two shell parses ``cmd`` survives before a real
    device sees it: the guest's login shell (which runs ``cmd`` via ssh)
    reduces the quoted device script to ONE token for `adb shell`, then
    `adb shell` hands that token to /system/bin/sh -c on the device for a
    second, independent parse. `shlex.split` follows the same POSIX
    quoting rules as `shlex.quote` on both hops, so it stands in for both
    without a real shell or device.

    Mirrors `tests/unit/android/test_commands.py::_simulate_device_argv`
    (duplicated here rather than imported — this test module owns the
    CLI-level round trip through `_capture_to_local`/`ssh.log`, not
    `playground.android.commands`, and importing across that boundary for
    one helper would blur which module's contract each test file pins).
    """
    guest_tokens = shlex.split(cmd)
    device_script = guest_tokens[-1]
    return shlex.split(device_script)


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


def test_launch_resolves_activity_before_falling_back_to_monkey(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """`launch` no longer goes straight to monkey (the stale name of this
    test claimed it did): it resolves the real launcher activity first via
    `cmd package resolve-activity` and only falls back to monkey — with
    `pidof` deciding success, since monkey's own exit status is not
    trustworthy — when that resolution comes up empty. See
    `launch_cmd`'s docstring in `playground.android.commands`.
    """
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "launch", "com.example.app")
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    assert "resolve-activity" in log
    assert "am start -n" in log
    assert "monkey -p" in log
    assert "com.example.app" in log
    assert "adb connect 127.0.0.1:5555" in log
    # resolution must be attempted BEFORE the monkey fallback runs.
    assert log.index("resolve-activity") < log.index("monkey -p")


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
    # `_run`'s shim always emits empty stdout, which `_capture_to_local`
    # now (correctly, see BUG A) treats as a failed capture rather than
    # silently writing a zero-byte "dump" — use the stdout-emitting shim
    # instead, same as the screenshot/pull tests below.
    out = tmp_path / "ui.xml"
    result = _run_with_ssh_stdout(
        tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "<hierarchy/>",
        "ui-dump", "--out", str(out),
    )
    assert result.exit_code == 0, result.output
    assert "uiautomator dump" in (tmp_path / "ssh.log").read_text()
    assert out.read_bytes() == b"<hierarchy/>\n"


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
    """`adb shell` re-joins whatever argv it is given with plain spaces
    before handing it to /system/bin/sh -c on the DEVICE for a second,
    independent parse — a second hop past the guest shell that ran this
    ssh command. `hello world` must survive both hops as ONE argument
    (LIVE BUG: it used to reach the device as two words, so `input text`
    only typed "hello")."""
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "input", "text", "hello world")
    assert result.exit_code == 0, result.output
    log = _remote_command_line((tmp_path / "ssh.log").read_text())
    argv = _simulate_device_argv(log)
    assert argv[-2:] == ["text", "hello world"]


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
    """The payload must stay inert on the DEVICE, not merely be quoted for
    the guest hop — quoting only the guest hop was itself a live bug:
    `adb shell` flattens its argv with spaces before the device's own
    `sh -c` re-parses it, so a `$(...)` that survived guest-side quoting
    could still be expanded a second time, on the device."""
    payload = "$(touch /tmp/pwned)"
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "input", "text", payload)
    assert result.exit_code == 0, result.output
    log = _remote_command_line((tmp_path / "ssh.log").read_text())
    argv = _simulate_device_argv(log)
    # the payload reaches the device as ONE literal argument ...
    assert argv[-2:] == ["text", payload]
    # ... never split or expanded into its own tokens by either hop.
    assert "touch" not in argv[:-1]
    assert "/tmp/pwned" not in argv


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
    log = _remote_command_line((tmp_path / "ssh.log").read_text())
    argv = _simulate_device_argv(log)
    assert argv == ["getprop", "ro.build.version.release"]


def test_shell_multiword_argument_survives_the_double_shell_hop(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """`playground app shell -- input text "hello world"` must reach the
    device with `hello world` as ONE argument, not two — the same
    double-hop hazard as `playground app input text`, just reached via
    the raw escape hatch instead."""
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
         "--", "input", "text", "hello world"],
    )
    assert result.exit_code == 0, result.output
    log = _remote_command_line((tmp_path / "ssh.log").read_text())
    argv = _simulate_device_argv(log)
    assert argv == ["input", "text", "hello world"]


# --------------------------------------------------------------------------- #
# Audit fixes below: BUG A (stale capture), BUG B (unquoted scp operand),
# BUG C (install races a still-booting Android), BUG D (guest /tmp never
# cleaned up), BUG E (pull/ui-dump crash instead of a clean diagnostic).
# --------------------------------------------------------------------------- #


def _write_capture_bug_shims(tmp_path: Path) -> Path:
    """A `ssh` shim that actually EXECUTES the remote command via bash,
    plus a fake `adb` for it to call — needed for BUG A specifically.

    `write_ssh_shim` (used everywhere else in this file) never executes
    what `_capture_to_local` built; it just returns a canned exit code and
    canned stdout regardless of the command's actual shape. That is
    exactly why the original `&&`/`;` chain bug shipped with a full green
    suite: no test ever ran the real shell semantics of the built string.
    This shim does, against a fake `adb` that fails the capture step
    (`shell screencap`) but would happily "pull" back a STALE file if the
    caller's chaining does not stop it from running after that failure.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\n"
        'cmd="${@: -1}"\n'
        'exec bash -c "$cmd"\n'
    )
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    adb_log = tmp_path / "adb.log"
    adb = bin_dir / "adb"
    adb.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(adb_log))}\n'
        'case "$1" in\n'
        "  connect) exit 0 ;;\n"
        "  -s)\n"
        "    shift 2\n"
        '    case "$1" in\n'
        "      shell)\n"
        # `adb_shell` quotes the whole device-side command as ONE token
        # (see its docstring in playground.android.commands), so `$2`
        # here is the entire "verb args..." string, not just the verb --
        # match on a glob prefix, not an exact word.
        '        case "$2" in\n'
        "          'rm '*) exit 0 ;;\n"
        "          'screencap '*) exit 1 ;;\n"  # simulated capture failure
        "          *) exit 0 ;;\n"
        "        esac\n"
        "        ;;\n"
        '      pull) printf "STALE_CONTENT" > "$3"; exit 0 ;;\n'
        "      *) exit 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    adb.chmod(adb.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def test_failed_capture_does_not_return_a_stale_screenshot(
    tmp_path, monkeypatch, write_apply_shims
) -> None:
    """BUG A regression test.

    Reproduces the real bug: `screencap` fails on the device, but a STALE
    screenshot from a previous run is still sitting at the fixed device
    path. The old `&&`/`;` chain in `_capture_to_local` interpolated
    `adb_prefix()` (`adb connect ... || true; adb -s ...`) right after a
    bare `&&`; bash treats the embedded `;` as ending that statement, so
    `capture_cmd`'s exit status stopped gating anything and `adb pull` +
    `cat` ran unconditionally — the CLI reported success and handed back
    the stale bytes.

    A canned-exit-code shim cannot see this class of bug (it never
    executes the built command), so this test drives the actual bash
    chain via a real `bash -c` and a fake `adb`. Expected/fixed behavior:
    capture failure aborts before `pull` ever runs, no local file is
    written, and the CLI exits non-zero.
    """
    apply_bin_dir = write_apply_shims(tmp_path)
    _write_capture_bug_shims(tmp_path)  # writes ssh + adb into the same bin dir
    monkeypatch.setenv("PATH", f"{apply_bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(app_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    out_dir = tmp_path / "shots"

    result = CliRunner().invoke(
        app,
        ["app", "screenshot", "--out", str(out_dir), "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir)],
    )

    assert result.exit_code != 0
    assert list(out_dir.glob("*.png")) == []
    adb_log = (tmp_path / "adb.log").read_text()
    # the capture failed, so `pull` must never have been invoked -- if it
    # was, the fake stale file would have been fetched and `cat`'d back to
    # the controller as though it were fresh.
    assert " pull " not in adb_log


def test_successful_capture_still_pulls_and_writes_the_file(
    tmp_path, monkeypatch, write_apply_shims
) -> None:
    """Same real-exec harness as the BUG A regression test above, but with
    a capture that succeeds, proving the fix's `{ ...; }` grouping did not
    also break the happy path it has to keep working."""
    bin_dir = write_apply_shims(tmp_path)
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\n"
        'cmd="${@: -1}"\n'
        'exec bash -c "$cmd"\n'
    )
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    adb = bin_dir / "adb"
    adb.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  connect) exit 0 ;;\n"
        "  -s)\n"
        "    shift 2\n"
        '    case "$1" in\n'
        "      shell) exit 0 ;;\n"  # rm -f and screencap both succeed
        '      pull) printf "FRESH_CONTENT" > "$3"; exit 0 ;;\n'
        "      *) exit 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    adb.chmod(adb.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(app_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    out_dir = tmp_path / "shots"

    result = CliRunner().invoke(
        app,
        ["app", "screenshot", "--out", str(out_dir), "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir)],
    )

    assert result.exit_code == 0, result.output
    written = list(out_dir.glob("droid1-*.png"))
    assert len(written) == 1, written
    assert written[0].read_bytes() == b"FRESH_CONTENT"


def test_scp_quotes_a_remote_path_containing_a_space(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, write_scp_shim
) -> None:
    """BUG B regression test.

    `dst = f"{user}@{host}:{remote_path}"` built the scp `host:path`
    operand by unquoted concatenation. That operand IS shell-interpreted
    on the remote side by scp, so a filename with a space produced
    `scp: ambiguous target`, and one with a backtick or `$(...)` would run
    on the guest. `install` stages the APK's own filename verbatim under
    `remote_path`, so a `.apk` with a space in its name exercises this
    directly.
    """
    apk_dir = tmp_path / "apks"
    apk_dir.mkdir()
    apk = apk_dir / "My App.apk"
    apk.write_bytes(b"fake apk bytes")

    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)
    write_scp_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    monkeypatch.setattr(app_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)

    result = CliRunner().invoke(
        app,
        ["app", "install", str(apk), "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir)],
    )
    assert result.exit_code == 0, result.output

    scp_log = (tmp_path / "scp.log").read_text()
    # the destination operand's PATH half must be quoted as one shell
    # token (the user@host half must stay bare, unquoted).
    assert "ubuntu@127.0.0.1:'/tmp/playground-apk-" in scp_log
    assert "My App.apk'" in scp_log
    # never emit the bare, unquoted operand -- that is what "ambiguous
    # target" / device-side injection came from.
    assert "ubuntu@127.0.0.1:/tmp/playground-apk-" not in scp_log


def test_install_waits_for_android_to_finish_booting(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, write_scp_shim
) -> None:
    """BUG C regression test: `install` must gate on `wait_booted_cmd`
    before staging/installing anything. `write_ssh_shim` overwrites its
    log on every call, so this drives the failure path (the very first
    ssh call -- the boot gate -- fails) and asserts install never reaches
    scp, rather than trying to observe an intermediate successful call
    that a later call would overwrite in the log."""
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"fake apk bytes")

    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=1)  # boot-wait never succeeds
    write_scp_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    monkeypatch.setattr(app_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)

    result = CliRunner().invoke(
        app,
        ["app", "install", str(apk), "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir)],
    )

    assert result.exit_code == 1
    assert not (tmp_path / "scp.log").exists(), "install must not stage before boot completes"
    assert "did not finish boot" in result.output
    # the boot-gate failure must be reported plainly, not as a raw adb error.
    assert "adb: error" not in result.output


def test_install_command_includes_stage_dir_cleanup(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, write_scp_shim
) -> None:
    """BUG D regression test: the staged APK directory on the guest is
    never removed anywhere else in this verb -- a screenshot loop or
    repeated large-APK installs fill the guest disk. The boot-wait and
    mkdir calls each overwrite `ssh.log` before the install call, so the
    install call itself (the last one made) is what is inspected."""
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"fake apk bytes")

    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)
    write_scp_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    monkeypatch.setattr(app_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)

    result = CliRunner().invoke(
        app,
        ["app", "install", str(apk), "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir)],
    )
    assert result.exit_code == 0, result.output
    log = _remote_command_line((tmp_path / "ssh.log").read_text())
    assert "install -r" in log
    assert "rm -rf /tmp/playground-apk-" in log


def test_pull_creates_a_missing_parent_directory(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """BUG E regression test: `local_path.write_bytes(...)` used to raise
    a bare traceback (FileNotFoundError) when the destination's parent
    directory did not exist yet."""
    dest = tmp_path / "nested" / "does" / "not" / "exist" / "pulled.bin"
    result = _run_with_ssh_stdout(
        tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "PULLED_CONTENT",
        "pull", "/sdcard/source.bin", str(dest),
    )
    assert result.exit_code == 0, result.output
    assert dest.read_bytes() == b"PULLED_CONTENT\n"


def test_pull_onto_an_existing_directory_is_a_clean_failure(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """BUG E regression test: a directory at the destination used to
    raise a bare IsADirectoryError traceback instead of a clean failure."""
    dest_dir = tmp_path / "already-a-directory"
    dest_dir.mkdir()
    result = _run_with_ssh_stdout(
        tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "PULLED_CONTENT",
        "pull", "/sdcard/source.bin", str(dest_dir),
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "is a directory" in result.output
