"""adb command-string builders.

These are pure string builders so the CLI layer stays thin and every
command shape is unit-testable without a device. Command syntax follows
docs/research/android-emulation/app-install-launch.md.
"""

from __future__ import annotations

import shlex

from playground.android.commands import (
    ADB_PORT,
    adb_prefix,
    adb_shell,
    clear_cmd,
    input_cmd,
    install_cmd,
    install_multiple_cmd,
    launch_cmd,
    list_packages_cmd,
    logcat_cmd,
    screenshot_cmd,
    stop_cmd,
    ui_dump_cmd,
    uninstall_cmd,
    wait_booted_cmd,
)


def _simulate_device_argv(cmd: str) -> list[str]:
    """Approximate the two shell parses `cmd` survives before a real
    device sees it: the guest's login shell (which runs `cmd` via ssh)
    reduces the quoted device script to ONE token for `adb shell`, then
    `adb shell` hands that token to /system/bin/sh -c on the device for a
    second, independent parse. `shlex.split` follows the same POSIX
    quoting rules as `shlex.quote`, so it is an adequate stand-in for both
    hops in a unit test that has no real shell or device."""
    guest_tokens = shlex.split(cmd)
    device_script = guest_tokens[-1]
    return shlex.split(device_script)


def test_every_command_connects_first() -> None:
    """adb must not depend on a pre-existing server on the guest."""
    assert f"adb connect 127.0.0.1:{ADB_PORT}" in adb_prefix()
    assert "|| true" in adb_prefix(), "a stale connect must not fail the command"


def test_adb_shell_targets_the_serial() -> None:
    cmd = adb_shell("getprop sys.boot_completed")
    assert f"-s 127.0.0.1:{ADB_PORT}" in cmd
    assert cmd.startswith(adb_prefix())


def test_install_uses_r_so_reinstall_is_allowed() -> None:
    assert "install -r" in install_cmd("/tmp/a.apk")


def test_install_multiple_passes_every_split() -> None:
    cmd = install_multiple_cmd(["/tmp/base.apk", "/tmp/split_config.en.apk"])
    assert "install-multiple -r" in cmd
    assert "/tmp/base.apk" in cmd
    assert "/tmp/split_config.en.apk" in cmd


def test_uninstall_removes_the_named_package() -> None:
    assert "uninstall com.example.app" in uninstall_cmd("com.example.app")


def test_launch_prefers_url_over_activity() -> None:
    cmd = launch_cmd("com.x", activity=".Main", url="https://e.com/p")
    assert "android.intent.action.VIEW" in cmd
    assert "https://e.com/p" in cmd
    assert ".Main" not in cmd


def test_launch_with_activity_uses_am_start_n() -> None:
    cmd = launch_cmd("com.x", activity=".Main", url=None)
    assert "am start -n com.x/.Main" in cmd


def test_launch_without_either_resolves_the_launcher_activity() -> None:
    """monkey is not a reliable launcher (see the live-bug test below), so
    the default path discovers the real component first."""
    cmd = launch_cmd("com.x", activity=None, url=None)
    assert "resolve-activity --brief" in cmd
    assert "am start -n" in cmd
    # monkey is kept only as the last-resort branch.
    assert "monkey -p com.x" in cmd
    assert "android.intent.category.LAUNCHER" in cmd


def test_stop_and_clear_are_distinct() -> None:
    assert "am force-stop com.x" in stop_cmd("com.x")
    assert "pm clear com.x" in clear_cmd("com.x")


def test_list_packages_third_party_flag() -> None:
    assert "pm list packages -3" in list_packages_cmd(pattern=None, third_party=True)
    cmd = list_packages_cmd(pattern="maps", third_party=False)
    assert "pm list packages" in cmd
    assert "maps" in cmd


def test_screenshot_and_ui_dump_write_to_the_given_remote_path() -> None:
    assert "/sdcard/s.png" in screenshot_cmd("/sdcard/s.png")
    assert "screencap -p" in screenshot_cmd("/sdcard/s.png")
    assert "uiautomator dump" in ui_dump_cmd("/sdcard/ui.xml")
    assert "/sdcard/ui.xml" in ui_dump_cmd("/sdcard/ui.xml")


def test_logcat_dump_vs_follow() -> None:
    assert "-d" in logcat_cmd(follow=False, lines=None)
    assert "-d" not in logcat_cmd(follow=True, lines=None)
    assert "-t 50" in logcat_cmd(follow=False, lines=50)


def test_input_builds_the_subcommand() -> None:
    assert "input text hello" in input_cmd("text", ["hello"])
    assert "input tap 10 20" in input_cmd("tap", ["10", "20"])
    assert "input keyevent KEYCODE_HOME" in input_cmd("key", ["KEYCODE_HOME"])


def test_shell_metacharacters_are_quoted() -> None:
    """A package or text argument must not be able to inject a shell command."""
    cmd = input_cmd("text", ["a; rm -rf /"])
    assert "; rm -rf /" not in cmd.replace("'a; rm -rf /'", "")


def test_wait_booted_polls_sys_boot_completed() -> None:
    cmd = wait_booted_cmd(60)
    assert "sys.boot_completed" in cmd
    assert "60" in cmd


def test_monkey_launch_uses_pidof_as_the_success_signal() -> None:
    """LIVE BUG 2026-08-31: monkey returns 251 on Redroid 11 even when the
    app launches fine, while `am start` returns 0. Relying on monkey's exit
    status made `playground app launch` report failure on every successful
    launch, and would have failed the whole apply for any lab declaring
    `launch: true` without an activity."""
    cmd = launch_cmd("com.example.app", activity=None, url=None)
    assert "resolve-activity --brief" in cmd, "prefer the real launcher component"
    assert "monkey -p" in cmd, "monkey remains the last-resort branch"
    assert "pidof" in cmd, "monkey's exit code must not decide success"
    # The device-side script must be ONE quoted argument so `;` runs on the
    # device, not on the guest that invokes adb.
    assert cmd.count("shell ") == 1
    tail = cmd.split("shell ", 1)[1]
    assert tail.startswith("'"), "device script must be quoted as one arg"


def test_activity_launch_needs_no_pidof_because_am_start_is_honest() -> None:
    cmd = launch_cmd("com.example.app", activity=".Main", url=None)
    assert "am start -n" in cmd
    assert "pidof" not in cmd


# --- BUG A: adb_shell quotes for the guest hop but not the adb-shell hop ---
# `adb shell` flattens whatever argv it receives by joining with spaces
# before handing that string to the device's own sh -c. Anything that
# reaches `adb shell` as more than one token loses its structure there,
# so multi-word arguments truncate and shell metacharacters get a second,
# unwanted parse on the device.


def test_adb_shell_quotes_the_whole_command_as_one_token() -> None:
    cmd = adb_shell("echo hi there")
    assert cmd.count("shell ") == 1
    tail = cmd.split("shell ", 1)[1]
    assert tail == shlex.quote("echo hi there")


def test_input_text_multiword_survives_the_double_shell_hop() -> None:
    """LIVE BUG: `input text 'hello world'` must arrive on the device as
    ONE argument, or `input` only types the first word."""
    cmd = input_cmd("text", ["hello world"])
    argv = _simulate_device_argv(cmd)
    assert argv[-2:] == ["text", "hello world"]


def test_command_substitution_payload_is_inert_on_the_device() -> None:
    """A `$(...)` payload in a package name or input text must reach the
    device as a literal argument, never something the device's sh
    expands (device-side injection, not just guest-side)."""
    cmd = input_cmd("text", ["$(reboot)"])
    argv = _simulate_device_argv(cmd)
    assert argv[-1] == "$(reboot)"

    cmd = uninstall_cmd("`id`; rm -rf /")
    # uninstall_cmd does not cross the adb-shell hop (it is a direct adb
    # protocol command, not `adb shell ...`), so device-side sh never
    # sees it at all; guest-side quoting alone is the correct protection.
    assert "`id`; rm -rf /" not in cmd.replace(shlex.quote("`id`; rm -rf /"), "")


def test_stop_cmd_payload_is_inert_on_the_device() -> None:
    cmd = stop_cmd("$(reboot)")
    argv = _simulate_device_argv(cmd)
    assert "$(reboot)" in argv
    assert "reboot" not in [a for a in argv if a != "$(reboot)"]


# --- BUG B: resolve-activity guard accepts another package's component ---


def test_launch_resolve_guard_requires_component_to_match_the_package() -> None:
    """`cmd package resolve-activity` can return e.g.
    `android/com.android.internal.app.ResolverActivity` or
    `com.android.settings/.FallbackHome` when the requested package has no
    launcher activity -- both contain a `/` but belong to the WRONG
    package. The guard must also check the component's package prefix."""
    cmd = launch_cmd("com.x", activity=None, url=None)
    assert '[ "${ACT%%/*}" = com.x ]' in cmd


# --- BUG C: pidof right after monkey races the async process start ---


def test_monkey_fallback_polls_pidof_instead_of_a_single_check() -> None:
    """`monkey` returns once the intent is dispatched, before the process
    necessarily exists; a single `pidof` check right after is a race that
    reports a false failure on a slow cold start."""
    cmd = launch_cmd("com.example.app", activity=None, url=None)
    assert "while" in cmd
    assert "sleep" in cmd
    assert "pidof com.example.app >/dev/null 2>&1 && exit 0" in cmd


# --- BUG D: stop_cmd cannot fail ---


def test_stop_fails_for_a_package_that_does_not_exist() -> None:
    """`am force-stop` exits 0 unconditionally, even for a package that
    was never installed; stop must verify."""
    cmd = stop_cmd("com.typo")
    assert "pm path com.typo" in cmd
    assert "exit 1" in cmd


def test_stop_verifies_the_process_is_actually_gone() -> None:
    cmd = stop_cmd("com.x")
    assert "am force-stop com.x" in cmd
    assert "pidof com.x" in cmd
    assert "exit 0" in cmd
