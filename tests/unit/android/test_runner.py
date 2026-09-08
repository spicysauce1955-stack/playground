"""android/runner.py: which argv gets built for each transport.

`_run_one` is the seam the whole transport refactor exists for -- a
`GuestAdb` target must render through `render_guest_command` and run
under ssh, a `LocalAdb` target must render through `local_argv` and run
`adb` directly, with no shell in between. Nothing exercised the LocalAdb
branch before this file: `local_argv` itself was only unit-tested in
isolation (tests/unit/android/test_transport.py), never through the
runner that actually dispatches to it.

`run_on_targets` is now typed to `AdbInvocation` only -- guest-shell
strings that are not adb at all (systemctl wrappers, `mkdir -p` staging,
...) go through the separate `run_guest_shell` entry point, which is
typed to `str` and requires a `GuestAdb` transport structurally via
`_run_shell_one`/`require_guest` rather than an isinstance check buried
in a shared dispatcher. That split is what makes pairing a `LocalAdb`
target with a guest-only command a type error at the call site instead
of a runtime raise discovered inside `_run_one`.
"""

from __future__ import annotations

import subprocess

import pytest

from playground.android.runner import TargetResult, _run_one, run_guest_shell, run_on_targets
from playground.android.transport import AdbInvocation, AndroidTarget, GuestAdb, LocalAdb


@pytest.fixture
def fake_run(monkeypatch):
    """Replace `subprocess.run` inside `android/runner.py` and record
    every argv it was called with."""
    calls: list[list[str]] = []

    def _fake(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr("playground.android.runner.subprocess.run", _fake)
    return calls


def test_local_target_dispatches_through_local_argv(fake_run) -> None:
    """A `LocalAdb` target must run `adb -s <serial> ...` directly -- no
    ssh anywhere in the chain, since there is no guest to ssh into."""
    target = AndroidTarget(name="phone1", transport=LocalAdb("1A2B3C"))
    inv = AdbInvocation(("shell", "pm clear com.example"))

    result = _run_one(target, inv, timeout=5.0)

    assert result.ok
    assert fake_run == [["adb", "-s", "1A2B3C", "shell", "pm clear com.example"]]


def test_local_target_never_builds_an_ssh_argv(fake_run) -> None:
    target = AndroidTarget(name="phone1", transport=LocalAdb("1A2B3C"))
    inv = AdbInvocation(("install", "-r", "/tmp/a.apk"))

    _run_one(target, inv, timeout=5.0)

    assert fake_run[0][0] == "adb"
    assert "ssh" not in fake_run[0]


def test_guest_target_still_dispatches_through_ssh(fake_run) -> None:
    """The GuestAdb branch is unchanged by this file's addition -- pinned
    here so a future edit cannot silently route a guest target through
    `local_argv` instead."""
    target = AndroidTarget(name="vm1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    inv = AdbInvocation(("uninstall", "com.example"))

    result = _run_one(target, inv, timeout=5.0)

    assert result.ok
    assert fake_run[0][0] == "ssh"
    assert fake_run[0][-1] == (
        "adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true; "
        "adb -s 127.0.0.1:5555 uninstall com.example"
    )


def test_run_on_targets_mixes_guest_and_local_transports(fake_run) -> None:
    """`run_on_targets` fans one invocation out to every target regardless
    of transport, and keeps results in input order."""
    guest = AndroidTarget(name="vm1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    local = AndroidTarget(name="phone1", transport=LocalAdb("SERIAL1"))
    inv = AdbInvocation(("install", "-r", "/tmp/a.apk"))

    results = run_on_targets([guest, local], inv, timeout=5.0)

    assert [r.target.name for r in results] == ["vm1", "phone1"]
    assert all(r.ok for r in results)
    local_argvs = [argv for argv in fake_run if argv[0] == "adb"]
    ssh_argvs = [argv for argv in fake_run if argv[0] == "ssh"]
    assert local_argvs == [["adb", "-s", "SERIAL1", "install", "-r", "/tmp/a.apk"]]
    assert len(ssh_argvs) == 1


def test_guest_shell_command_dispatches_verbatim_over_ssh(fake_run) -> None:
    """`run_guest_shell` hands its ``command`` straight to ssh -- it is
    already a full guest shell string (a systemctl wrapper, `mkdir -p`
    staging, ...), not an `AdbInvocation` to render."""
    target = AndroidTarget(name="vm1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))

    results = run_guest_shell([target], "mkdir -p /tmp/x", timeout=5.0)

    assert results[0].ok
    assert fake_run[0][0] == "ssh"
    assert fake_run[0][-1] == "mkdir -p /tmp/x"


def test_local_target_with_a_guest_shell_command_raises() -> None:
    """A bare `str` command (a guest-shell script, not an adb invocation)
    has no meaning on a USB-attached device: there is no shell to run it
    in. This is a programming error (a caller building a guest command
    for a target it never checked was a guest), not an operator error, so
    it raises rather than reporting a device failure -- mirrors
    `transport.require_guest`, which is exactly what `run_guest_shell`
    routes through."""
    target = AndroidTarget(name="phone1", transport=LocalAdb("1A2B3C"))

    with pytest.raises(TypeError):
        run_guest_shell([target], "mkdir -p /tmp/x", timeout=5.0)


def test_target_result_type_still_matches() -> None:
    """Cheap sanity check that TargetResult's shape did not drift."""
    result = TargetResult(
        target=AndroidTarget(name="x", transport=LocalAdb("s")),
        returncode=0, stdout="", stderr="",
    )
    assert result.ok
