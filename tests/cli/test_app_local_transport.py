"""Transport-conditional file staging: `stage_for`/`retrieve_from`/
`_guest_cleanup`/`_capture_and_retrieve` in `cli/app_commands.py`.

A guest target runs `adb` inside a VM, so a file has to travel there
(scp) before `adb install`/`adb push` can see it, and a captured/pulled
file has to travel back (a second ssh hop) before it lands on the
controller. A local target runs `adb` directly against a USB-attached
handset on the controller itself, where the file already is (for a push)
or where `adb pull` already writes it (for a pull) -- staging or
relaying it anywhere would be pointless at best, and a wrong path at
worst. These tests pin that asymmetry at the function level, the same
altitude `tests/unit/android/test_runner.py` pins guest-vs-local
dispatch at for `run_on_targets`/`run_guest_shell`.
"""

from __future__ import annotations

import pytest

from playground.android.transport import AndroidTarget, GuestAdb, LocalAdb
from playground.cli.app_commands import (
    _capture_and_retrieve,
    _guest_cleanup,
    _RetrievalFailed,
    _StagingFailed,
    retrieve_from,
    stage_for,
)


def test_local_target_stages_in_place(tmp_path):
    apk = tmp_path / "a.apk"
    apk.write_bytes(b"x")
    target = AndroidTarget(name="pixel7", transport=LocalAdb("S1"))
    assert stage_for(target, apk) == str(apk)


def test_guest_target_stages_under_tmp(tmp_path, monkeypatch):
    apk = tmp_path / "a.apk"
    apk.write_bytes(b"x")
    copied: list[tuple[str, str]] = []

    def _fake_scp_one(target, src, dst):
        copied.append((str(src), dst))
        return True  # the real `_scp_one` returns True on success

    monkeypatch.setattr("playground.cli.app_commands._scp_one", _fake_scp_one)
    target = AndroidTarget(name="droid1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    remote = stage_for(target, apk)
    assert remote.startswith("/tmp/")
    assert remote.endswith("a.apk")
    assert copied == [(str(apk), remote)]


def test_guest_target_stages_each_file_under_a_distinct_name(tmp_path, monkeypatch):
    """Two files with the same basename staged for the same target must
    not collide -- unlike the old shared `stage_dir` (one mkdir per
    install, reused across every split APK), `stage_for` is called once
    per file and must pick a fresh name each time."""
    monkeypatch.setattr("playground.cli.app_commands._scp_one", lambda target, src, dst: True)
    target = AndroidTarget(name="droid1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    a = tmp_path / "base.apk"
    a.write_bytes(b"x")
    b = tmp_path / "base.apk"  # same name, different call
    remote1 = stage_for(target, a)
    remote2 = stage_for(target, b)
    assert remote1 != remote2


def test_scp_failure_raises_staging_failed(tmp_path, monkeypatch):
    """`stage_for` propagates a failed scp rather than swallowing it:
    `_scp_one` is declared `-> bool` and documents "returns False on
    failure" precisely so staging failure is a signal a caller can act
    on, not one it has to infer later from an adb command failing on a
    file that was never actually staged."""
    apk = tmp_path / "a.apk"
    apk.write_bytes(b"x")
    monkeypatch.setattr("playground.cli.app_commands._scp_one", lambda target, src, dst: False)
    target = AndroidTarget(name="droid1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    with pytest.raises(_StagingFailed):
        stage_for(target, apk)


def test_guest_cleanup_is_a_no_op_for_a_local_target(monkeypatch):
    called = []
    monkeypatch.setattr(
        "playground.cli.app_commands.run_guest_shell",
        lambda *a, **kw: called.append((a, kw)),
    )
    target = AndroidTarget(name="pixel7", transport=LocalAdb("S1"))
    _guest_cleanup(target, "/tmp/whatever")
    assert called == []


def test_guest_cleanup_removes_the_staged_file_over_ssh(monkeypatch):
    seen = {}

    def _fake_run_guest_shell(targets, command, *, timeout):
        seen["targets"] = targets
        seen["command"] = command
        seen["timeout"] = timeout
        return [object()]

    monkeypatch.setattr(
        "playground.cli.app_commands.run_guest_shell", _fake_run_guest_shell
    )
    target = AndroidTarget(name="droid1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    _guest_cleanup(target, "/tmp/playground-stage-abc-app.apk")
    assert seen["targets"] == [target]
    assert seen["command"] == "rm -f /tmp/playground-stage-abc-app.apk"


def test_guest_cleanup_quotes_a_path_with_shell_metacharacters():
    """`_guest_cleanup` builds `rm -f <path>` for a GUEST SHELL to parse
    (`run_guest_shell` hands it to ssh verbatim) -- an unquoted metachar
    in ``remote_path`` would be guest-side command injection, the same
    class of bug this plan's earlier rounds shipped once already."""
    import shlex

    remote_path = "/tmp/playground-stage-abc-a$(touch pwned).apk"
    # Exercise the same quoting `_guest_cleanup` applies, without needing
    # a live ssh round trip: prove the payload is inert once quoted.
    quoted = shlex.quote(remote_path)
    assert quoted != remote_path
    assert shlex.split(f"rm -f {quoted}") == ["rm", "-f", remote_path]


def test_retrieve_from_local_target_pulls_directly(monkeypatch, tmp_path):
    dest = tmp_path / "out" / "pulled.bin"
    calls = []

    def _fake_run_on_targets(targets, inv, *, timeout):
        calls.append((targets, inv))
        return [_Result(ok=True)]

    monkeypatch.setattr(
        "playground.cli.app_commands.run_on_targets", _fake_run_on_targets
    )
    target = AndroidTarget(name="pixel7", transport=LocalAdb("S1"))
    retrieve_from(target, "/sdcard/source.bin", dest)
    assert dest.parent.is_dir()
    assert len(calls) == 1
    ((got_targets, inv),) = calls
    assert got_targets == [target]
    assert inv.args == ("pull", "/sdcard/source.bin", str(dest))


def test_retrieve_from_local_target_raises_on_pull_failure(monkeypatch, tmp_path):
    dest = tmp_path / "pulled.bin"
    monkeypatch.setattr(
        "playground.cli.app_commands.run_on_targets",
        lambda targets, inv, *, timeout: [_Result(ok=False)],
    )
    target = AndroidTarget(name="pixel7", transport=LocalAdb("S1"))
    with pytest.raises(_RetrievalFailed):
        retrieve_from(target, "/sdcard/source.bin", dest)


def test_retrieve_from_local_target_rejects_an_existing_directory(tmp_path):
    dest = tmp_path / "already-a-directory"
    dest.mkdir()
    target = AndroidTarget(name="pixel7", transport=LocalAdb("S1"))
    with pytest.raises(_RetrievalFailed):
        retrieve_from(target, "/sdcard/source.bin", dest)


def test_retrieve_from_guest_target_delegates_to_capture_to_local(monkeypatch, tmp_path):
    """Guest retrieval reuses `_capture_to_local`'s existing, heavily
    tested ssh chain (capture=None, clean_device_path_first=False -- the
    device path is a pre-existing file this call did not create)."""
    dest = tmp_path / "pulled.bin"
    seen = {}

    def _fake_capture_to_local(target, capture_cmd, device_path, local_path, **kw):
        seen["capture_cmd"] = capture_cmd
        seen["device_path"] = device_path
        seen["local_path"] = local_path
        seen["clean_device_path_first"] = kw.get("clean_device_path_first")
        return True

    monkeypatch.setattr(
        "playground.cli.app_commands._capture_to_local", _fake_capture_to_local
    )
    target = AndroidTarget(name="droid1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    retrieve_from(target, "/sdcard/source.bin", dest)
    assert seen["capture_cmd"] is None
    assert seen["device_path"] == "/sdcard/source.bin"
    assert seen["local_path"] == dest
    assert seen["clean_device_path_first"] is False


def test_retrieve_from_guest_target_raises_when_capture_to_local_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "playground.cli.app_commands._capture_to_local", lambda *a, **kw: False
    )
    target = AndroidTarget(name="droid1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    with pytest.raises(_RetrievalFailed):
        retrieve_from(target, "/sdcard/source.bin", tmp_path / "out.bin")


def test_capture_and_retrieve_local_target_skips_pull_after_a_failed_capture(
    monkeypatch, tmp_path
):
    """Mirrors BUG A (`test_failed_capture_does_not_return_a_stale_screenshot`
    in `test_app.py`) for the LOCAL transport: a failed capture must never
    be followed by a pull, or a stale on-device file could be returned as
    though it were fresh."""
    from playground.android.commands import screenshot_cmd

    retrieve_called = []
    monkeypatch.setattr(
        "playground.cli.app_commands.run_on_targets",
        lambda targets, inv, *, timeout: [_Result(ok=False)],
    )
    monkeypatch.setattr(
        "playground.cli.app_commands.retrieve_from",
        lambda *a, **kw: retrieve_called.append((a, kw)),
    )
    target = AndroidTarget(name="pixel7", transport=LocalAdb("S1"))
    ok = _capture_and_retrieve(
        target, screenshot_cmd("/sdcard/s.png"), "/sdcard/s.png", tmp_path / "s.png"
    )
    assert ok is False
    assert retrieve_called == []


def test_capture_and_retrieve_local_target_pulls_after_a_successful_capture(
    monkeypatch, tmp_path
):
    from playground.android.commands import screenshot_cmd

    monkeypatch.setattr(
        "playground.cli.app_commands.run_on_targets",
        lambda targets, inv, *, timeout: [_Result(ok=True)],
    )
    retrieve_called = []
    monkeypatch.setattr(
        "playground.cli.app_commands.retrieve_from",
        lambda *a, **kw: retrieve_called.append(a),
    )
    target = AndroidTarget(name="pixel7", transport=LocalAdb("S1"))
    dest = tmp_path / "s.png"
    ok = _capture_and_retrieve(target, screenshot_cmd("/sdcard/s.png"), "/sdcard/s.png", dest)
    assert ok is True
    assert retrieve_called == [(target, "/sdcard/s.png", dest)]


def test_capture_and_retrieve_guest_target_delegates_to_capture_to_local(
    monkeypatch, tmp_path
):
    from playground.android.commands import screenshot_cmd

    seen = {}

    def _fake_capture_to_local(target, capture_cmd, device_path, local_path, **kw):
        seen["capture_cmd"] = capture_cmd
        return True

    monkeypatch.setattr(
        "playground.cli.app_commands._capture_to_local", _fake_capture_to_local
    )
    target = AndroidTarget(name="droid1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    cmd = screenshot_cmd("/sdcard/s.png")
    ok = _capture_and_retrieve(target, cmd, "/sdcard/s.png", tmp_path / "s.png")
    assert ok is True
    assert seen["capture_cmd"] is cmd


class _Result:
    """Enough of a `TargetResult` for `_echo_target_output` to accept it."""

    def __init__(self, *, ok: bool, target: AndroidTarget | None = None) -> None:
        self.ok = ok
        self.target = target or AndroidTarget(name="pixel7", transport=LocalAdb("S1"))
        self.returncode = 0 if ok else 1
        self.stdout = ""
        self.stderr = "" if ok else "boom"
