"""Frozen snapshots of every adb builder's output, taken BEFORE the
transport refactor.

Task 2 moves outer adb-argv quoting out of these builders and into the
GuestAdb renderer. That refactor is correct only if the guest-rendered
string is byte-identical to what shipped. This file is the proof, and it
must not be edited to accommodate the refactor -- if a value here has to
change, the refactor changed guest behaviour and is wrong.
"""

from __future__ import annotations

import pytest

from playground.android import commands as c

PREFIX = "adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true; adb -s 127.0.0.1:5555"


def test_adb_prefix_is_frozen():
    assert c.adb_prefix() == PREFIX


@pytest.mark.parametrize(
    ("built", "expected"),
    [
        (c.install_cmd("/tmp/a.apk"), f"{PREFIX} install -r /tmp/a.apk"),
        (c.install_cmd("/tmp/my app.apk"), f"{PREFIX} install -r '/tmp/my app.apk'"),
        (
            c.install_multiple_cmd(["/t/a.apk", "/t/b.apk"]),
            f"{PREFIX} install-multiple -r /t/a.apk /t/b.apk",
        ),
        (c.uninstall_cmd("com.example"), f"{PREFIX} uninstall com.example"),
        (c.adb_shell("getprop ro.build.version.sdk"),
         f"{PREFIX} shell 'getprop ro.build.version.sdk'"),
        (c.clear_cmd("com.example"), f"{PREFIX} shell 'pm clear com.example'"),
        (c.screenshot_cmd("/sdcard/s.png"), f"{PREFIX} shell 'screencap -p /sdcard/s.png'"),
        (c.ui_dump_cmd("/sdcard/u.xml"), f"{PREFIX} shell 'uiautomator dump /sdcard/u.xml'"),
        (c.logcat_cmd(follow=False, lines=None, clear=True), f"{PREFIX} logcat -c"),
        (c.logcat_cmd(follow=False, lines=None), f"{PREFIX} logcat -d"),
        (c.logcat_cmd(follow=True, lines=None), f"{PREFIX} logcat"),
        (c.logcat_cmd(follow=False, lines=5), f"{PREFIX} logcat -d -t 5"),
        (c.logcat_cmd(follow=True, lines=None, priority="w"), f"{PREFIX} logcat '*:W'"),
        (c.logcat_cmd(follow=True, lines=None, tag="Zygote"), f"{PREFIX} logcat -s Zygote:V"),
        (c.list_packages_cmd(pattern=None, third_party=False),
         f"{PREFIX} shell 'pm list packages'"),
        (c.list_packages_cmd(pattern="exam", third_party=True),
         f"{PREFIX} shell 'pm list packages -3 exam'"),
        (c.input_cmd("key", ["KEYCODE_HOME"]), f"{PREFIX} shell 'input keyevent KEYCODE_HOME'"),
        (c.input_cmd("text", ["hello world"]),
         f'{PREFIX} shell \'input text \'"\'"\'hello world\'"\'"\'\''),
    ],
)
def test_builder_output_is_frozen(built, expected):
    assert built == expected


def test_launch_deep_link_is_frozen():
    assert c.launch_cmd("com.example", activity=None, url="https://x.test") == (
        f"{PREFIX} shell 'am start -a android.intent.action.VIEW -d https://x.test'"
    )


def test_launch_activity_is_frozen():
    assert c.launch_cmd("com.example", activity=".Main", url=None) == (
        f"{PREFIX} shell 'am start -n com.example/.Main'"
    )


def test_launch_discovery_script_is_frozen():
    built = c.launch_cmd("com.example", activity=None, url=None)
    assert built.startswith(f"{PREFIX} shell '")
    assert "cmd package resolve-activity --brief com.example" in built
    assert "monkey -p com.example" in built


def test_stop_script_is_frozen():
    built = c.stop_cmd("com.example")
    assert built.startswith(f"{PREFIX} shell '")
    assert "pm path com.example >/dev/null 2>&1 || exit 1" in built
    assert "pidof com.example >/dev/null 2>&1 && exit 1" in built


def test_logcat_package_script_is_frozen():
    built = c.logcat_cmd(follow=False, lines=3, package="com.example")
    assert built.startswith(f"{PREFIX} shell '")
    assert "PID=$(pidof com.example 2>/dev/null)" in built
    assert '--pid="$PID"' in built
    assert "| tail -n 3" in built


def test_wait_booted_cmd_is_frozen():
    built = c.wait_booted_cmd(30)
    # Full string equality: every character must match, including the double
    # prefix (once bare at the start, once nested in the command substitution)
    # and the timeout argument reflected in the seq range.
    assert built == (
        f"{PREFIX} wait-for-device >/dev/null 2>&1; "
        f"for i in $(seq 1 30); do "
        f'[ "$(adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true; adb -s '
        f"127.0.0.1:5555 shell getprop sys.boot_completed 2>/dev/null "
        f"| tr -d '\\r')\" = 1 ] && exit 0; sleep 1; done; exit 1"
    )
