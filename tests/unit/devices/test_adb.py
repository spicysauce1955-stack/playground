from __future__ import annotations

import subprocess

from playground.devices import adb
from playground.devices.adb import (
    AttachedDevice,
    DeviceFacts,
    list_attached,
    parse_devices_l,
    probe_facts,
)

SAMPLE = """List of devices attached
1A2B3C4D5E             device product:panther model:Pixel_7 device:panther transport_id:1
9Z8Y7X                 unauthorized usb:1-2
0Q0Q0Q                 offline
emulator-5554          device product:sdk_gphone model:sdk_gphone64 device:emu transport_id:3

"""


def test_parses_an_authorized_device():
    devices = parse_devices_l(SAMPLE)
    first = devices[0]
    assert first == AttachedDevice(serial="1A2B3C4D5E", state="connected", model="Pixel 7")


def test_underscores_in_the_model_become_spaces():
    assert parse_devices_l(SAMPLE)[0].model == "Pixel 7"


def test_unauthorized_is_reported_not_dropped():
    unauth = [d for d in parse_devices_l(SAMPLE) if d.serial == "9Z8Y7X"]
    assert unauth[0].state == "unauthorized"
    assert unauth[0].model is None


def test_offline_is_its_own_state():
    off = [d for d in parse_devices_l(SAMPLE) if d.serial == "0Q0Q0Q"]
    assert off[0].state == "offline"
    assert off[0].model is None


def test_header_and_blank_lines_are_ignored():
    assert len(parse_devices_l(SAMPLE)) == 4


def test_empty_output_is_no_devices():
    assert parse_devices_l("List of devices attached\n\n") == []


def test_trailing_whitespace_is_tolerated():
    noisy = "List of devices attached   \n1A2B3C4D5E    device model:Pixel_7   \n\n   \n"
    devices = parse_devices_l(noisy)
    assert devices == [AttachedDevice(serial="1A2B3C4D5E", state="connected", model="Pixel 7")]


def test_attached_device_has_no_manufacturer_field():
    # AttachedDevice is the parse result of `adb devices -l`, which never
    # emits a manufacturer -- that field belongs to DeviceFacts (from
    # getprop), not here.
    assert not hasattr(AttachedDevice(serial="X", state="connected"), "manufacturer")


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_list_attached_returns_devices_and_no_diagnostics_on_success(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv == ["adb", "devices", "-l"]
        return _FakeCompleted(0, stdout=SAMPLE)

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    devices, diagnostics = list_attached()
    assert len(devices) == 4
    assert diagnostics == []


def test_list_attached_reports_diagnostic_when_adb_missing(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("no such file: adb")

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    devices, diagnostics = list_attached()
    assert devices == []
    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "error"
    assert diagnostics[0].id == "device.adb_unavailable"


def test_list_attached_reports_diagnostic_on_timeout(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=20.0)

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    devices, diagnostics = list_attached()
    assert devices == []
    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "error"


def test_list_attached_reports_diagnostic_on_nonzero_exit(monkeypatch):
    def fake_run(argv, **kwargs):
        return _FakeCompleted(1, stdout="", stderr="adb server error")

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    devices, diagnostics = list_attached()
    assert devices == []
    assert len(diagnostics) == 1
    assert "adb server error" in diagnostics[0].message


def test_probe_facts_reads_getprop_and_probes_root(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv[:3] == ["adb", "-s", "SERIAL1"]
        if argv[-1] == "ro.product.model":
            return _FakeCompleted(0, stdout="Pixel 7\n")
        if argv[-1] == "ro.product.manufacturer":
            return _FakeCompleted(0, stdout="Google\n")
        if argv[-1] == "ro.build.version.sdk":
            return _FakeCompleted(0, stdout="34\n")
        if argv[-1] == "ro.product.cpu.abi":
            return _FakeCompleted(0, stdout="arm64-v8a\n")
        if argv[-1] == "ro.build.fingerprint":
            return _FakeCompleted(0, stdout="google/panther/panther:14/x/y:user/release-keys\n")
        if argv[-4:] == ["shell", "su", "-c", "id"]:
            return _FakeCompleted(0, stdout="uid=0(root) gid=0(root)\n")
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    facts = probe_facts("SERIAL1")
    assert facts == DeviceFacts(
        serial="SERIAL1",
        model="Pixel 7",
        manufacturer="Google",
        sdk=34,
        abi="arm64-v8a",
        rooted=True,
        fingerprint="google/panther/panther:14/x/y:user/release-keys",
    )


def test_probe_facts_root_is_false_when_su_is_absent(monkeypatch):
    def fake_run(argv, **kwargs):
        if argv[-4:] == ["shell", "su", "-c", "id"]:
            return _FakeCompleted(127, stdout="", stderr="su: not found")
        return _FakeCompleted(0, stdout="")

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    facts = probe_facts("SERIAL2")
    assert facts.rooted is False


def test_probe_facts_handles_adb_missing(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("no adb")

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    facts = probe_facts("SERIAL3")
    assert facts.model is None
    assert facts.rooted is False


def test_probe_facts_handles_timeout(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=20.0)

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    facts = probe_facts("SERIAL4")
    assert facts.model is None
    assert facts.rooted is False
