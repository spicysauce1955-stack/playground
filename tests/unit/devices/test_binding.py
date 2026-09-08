from __future__ import annotations

from playground.devices.adb import AttachedDevice
from playground.devices.binding import (
    match_candidates,
    resolve_binding,
    write_binding,
)
from playground.models.kinds import DeviceMatch
from playground.models.resolved import ResolvedDevice


def _device(**kw) -> ResolvedDevice:
    return ResolvedDevice(name="pixel7", kind="android", **kw)


def _attached(serial, model="Pixel 7") -> AttachedDevice:
    return AttachedDevice(serial=serial, state="connected", model=model)


def test_no_selector_and_one_device_binds_it(tmp_path):
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [_attached("S1")], rebind=False
    )
    assert diagnostics == []
    assert serial == "S1"


def test_no_selector_and_several_devices_is_ambiguous(tmp_path):
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [_attached("S1"), _attached("S2")], rebind=False
    )
    assert serial is None
    assert [d.id for d in diagnostics] == ["config.device.ambiguous_match"]
    assert "S1" in diagnostics[0].message and "S2" in diagnostics[0].message


def test_serial_selector_narrows(tmp_path):
    device = _device(match=DeviceMatch(serial="S2"))
    serial, diagnostics = resolve_binding(
        tmp_path, "l", device, [_attached("S1"), _attached("S2")], rebind=False
    )
    assert serial == "S2"


def test_model_selector_narrows():
    device = _device(match=DeviceMatch(model="Pixel 7"))
    attached = [_attached("S1", "Pixel 7"), _attached("S2", "Galaxy S22")]
    assert [d.serial for d in match_candidates(device, attached)] == ["S1"]


def test_no_selector_matches_everything():
    device = _device()
    attached = [_attached("S1", "Pixel 7"), _attached("S2", "Galaxy S22")]
    assert [d.serial for d in match_candidates(device, attached)] == ["S1", "S2"]


def test_selector_matching_nothing_is_an_error(tmp_path):
    device = _device(match=DeviceMatch(serial="NOPE"))
    serial, diagnostics = resolve_binding(
        tmp_path, "l", device, [_attached("S1")], rebind=False
    )
    assert serial is None
    assert [d.id for d in diagnostics] == ["config.device.no_match"]


def test_a_bound_serial_that_is_gone_reports_not_connected(tmp_path):
    write_binding(tmp_path, "l", "pixel7", "S1")
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [], rebind=False
    )
    assert serial is None
    assert [d.id for d in diagnostics] == ["device.not_connected"]


def test_a_bound_serial_that_is_still_attached_wins_over_other_matches(tmp_path):
    """Rule 1 in the precedence: an attached bound serial is used with no
    diagnostics, even when other attached handsets also match the
    selector -- the existing binding is never second-guessed."""
    write_binding(tmp_path, "l", "pixel7", "S1")
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [_attached("S1"), _attached("S2")], rebind=False
    )
    assert diagnostics == []
    assert serial == "S1"


def test_a_different_device_matching_the_slot_refuses_to_rebind(tmp_path):
    """Installing an APK onto the wrong phone because a cable was swapped
    is exactly the failure that must not be silent."""
    write_binding(tmp_path, "l", "pixel7", "S1")
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [_attached("S2")], rebind=False
    )
    assert serial is None
    assert [d.id for d in diagnostics] == ["device.rebind_required"]
    assert "--rebind" in diagnostics[0].suggestion


def test_rebind_flag_allows_the_swap(tmp_path):
    write_binding(tmp_path, "l", "pixel7", "S1")
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [_attached("S2")], rebind=True
    )
    assert diagnostics == []
    assert serial == "S2"


def test_rebind_persists_the_new_binding(tmp_path):
    write_binding(tmp_path, "l", "pixel7", "S1")
    resolve_binding(tmp_path, "l", _device(), [_attached("S2")], rebind=True)
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [_attached("S2")], rebind=False
    )
    assert diagnostics == []
    assert serial == "S2"


def test_binding_survives_a_corrupt_file(tmp_path):
    """A damaged binding reads as 'not bound', the same tolerance the
    registry and capture session readers apply."""
    path = tmp_path / "state" / "devices" / "l" / "pixel7.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [_attached("S1")], rebind=False
    )
    assert diagnostics == []
    assert serial == "S1"


def test_clear_binding_is_a_noop_when_absent(tmp_path):
    from playground.devices.binding import clear_binding

    clear_binding(tmp_path, "l", "pixel7")  # must not raise
