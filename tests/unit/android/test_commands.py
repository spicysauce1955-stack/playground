"""adb command-string builders.

These are pure string builders so the CLI layer stays thin and every
command shape is unit-testable without a device. Command syntax follows
docs/research/android-emulation/app-install-launch.md.
"""

from __future__ import annotations

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
