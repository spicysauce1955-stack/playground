"""Transport rendering: the guest path preserves today's shell string,
the local path never builds a shell string at all."""

from __future__ import annotations

from playground.android import commands as c
from playground.android.transport import (
    AdbInvocation,
    AndroidTarget,
    GuestAdb,
    LocalAdb,
    local_argv,
    render_guest_command,
)

PREFIX = "adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true; adb -s 127.0.0.1:5555"


def test_guest_render_reproduces_the_legacy_prefix():
    inv = AdbInvocation(("install", "-r", "/tmp/a.apk"))
    assert render_guest_command(inv) == f"{PREFIX} install -r /tmp/a.apk"


def test_guest_render_quotes_only_what_needs_it():
    inv = AdbInvocation(("install", "-r", "/tmp/my app.apk"))
    assert render_guest_command(inv) == f"{PREFIX} install -r '/tmp/my app.apk'"


def test_guest_render_quotes_a_device_script_as_one_token():
    inv = AdbInvocation(("shell", "pm clear com.example"))
    assert render_guest_command(inv) == f"{PREFIX} shell 'pm clear com.example'"


def test_local_argv_is_shell_free_and_unquoted():
    inv = AdbInvocation(("shell", "pm clear com.example"))
    assert local_argv(LocalAdb("1A2B3C"), inv) == [
        "adb", "-s", "1A2B3C", "shell", "pm clear com.example",
    ]


def test_local_argv_keeps_a_glob_intact():
    """The guest renderer quotes `*:W` for the guest shell; the local path
    must hand adb the raw two-character filterspec, not `'*:W'`."""
    inv = c.logcat_cmd(follow=True, lines=None, priority="w")
    assert local_argv(LocalAdb("S"), inv) == ["adb", "-s", "S", "logcat", "*:W"]
    assert render_guest_command(inv) == f"{PREFIX} logcat '*:W'"


def test_builders_return_invocations():
    assert isinstance(c.install_cmd("/tmp/a.apk"), AdbInvocation)
    assert c.install_cmd("/tmp/a.apk").args == ("install", "-r", "/tmp/a.apk")


def test_android_target_carries_a_transport():
    t = AndroidTarget(name="droid1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    assert t.name == "droid1"
    assert isinstance(t.transport, GuestAdb)
