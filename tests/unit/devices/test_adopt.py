from __future__ import annotations

from playground.devices.adb import AttachedDevice, DeviceFacts
from playground.devices.adopt import adopt, check_requirements, release
from playground.devices.binding import read_binding
from playground.devices.notes import write_note
from playground.devices.registry import read_registry
from playground.models.kinds import DeviceRequire
from playground.models.resolved import ResolvedDevice


def _device(**kw):
    return ResolvedDevice(name="pixel7", kind="android", **kw)


def test_unauthorized_gets_its_own_diagnostic(tmp_path, monkeypatch):
    attached = [AttachedDevice(serial="S1", state="unauthorized")]
    statuses, diagnostics = adopt(
        tmp_path / "s", tmp_path / "c", "l", [_device()], attached
    )
    assert statuses[0].state == "unauthorized"
    assert [d.id for d in diagnostics] == ["device.unauthorized"]
    assert "Allow" in diagnostics[0].suggestion


def test_a_missing_optional_device_is_not_an_error(tmp_path):
    statuses, diagnostics = adopt(
        tmp_path / "s", tmp_path / "c", "l", [_device(required=False)], []
    )
    assert statuses[0].state == "missing"
    assert [d.severity for d in diagnostics] == ["warning"]


def test_a_missing_required_device_is_an_error(tmp_path):
    _, diagnostics = adopt(tmp_path / "s", tmp_path / "c", "l", [_device()], [])
    assert diagnostics and diagnostics[0].severity == "error"


def test_require_root_fails_on_an_unrooted_handset():
    device = _device(require=DeviceRequire(root=True))
    facts = DeviceFacts(serial="S1", rooted=False, sdk=34)
    diagnostics = check_requirements(device, facts, note=None)
    assert [d.id for d in diagnostics] == ["device.require_unmet"]


def test_require_failure_prints_the_operator_note():
    """The note earning its keep: the context you would otherwise hunt for,
    printed at the moment the assertion fails."""
    device = _device(require=DeviceRequire(root=True))
    facts = DeviceFacts(serial="S1", rooted=False)
    note = "root broken since the September OTA, needs Magisk re-flash"
    diagnostics = check_requirements(device, facts, note=note)
    assert note in diagnostics[0].message


def test_min_sdk_is_checked():
    device = _device(require=DeviceRequire(min_sdk=34))
    facts = DeviceFacts(serial="S1", sdk=30, rooted=True)
    assert [d.id for d in check_requirements(device, facts, note=None)] == [
        "device.require_unmet"
    ]


def test_satisfied_requirements_produce_nothing():
    device = _device(require=DeviceRequire(root=True, min_sdk=30))
    facts = DeviceFacts(serial="S1", sdk=34, rooted=True)
    assert check_requirements(device, facts, note=None) == []


# --- adoption: the "otherwise" (connected, healthy) path ------------------


def test_a_connected_device_is_probed_recorded_and_adopted(tmp_path, monkeypatch):
    attached = [AttachedDevice(serial="S1", state="connected", model="Pixel 7")]
    facts = DeviceFacts(serial="S1", model="Pixel 7", sdk=34, rooted=True)
    monkeypatch.setattr("playground.devices.adopt.probe_facts", lambda serial: facts)

    statuses, diagnostics = adopt(
        tmp_path / "s", tmp_path / "c", "l", [_device()], attached
    )

    assert diagnostics == []
    assert statuses[0].state == "connected"
    assert statuses[0].serial == "S1"
    assert statuses[0].sdk == 34
    assert statuses[0].rooted is True

    # The binding was persisted...
    assert read_binding(tmp_path / "s", "l", "pixel7").serial == "S1"
    # ...and so was the registry sighting.
    assert "S1" in read_registry(tmp_path / "s")


def test_an_offline_attached_device_reports_offline_state(tmp_path, monkeypatch):
    attached = [AttachedDevice(serial="S1", state="offline")]
    facts = DeviceFacts(serial="S1")
    monkeypatch.setattr("playground.devices.adopt.probe_facts", lambda serial: facts)

    statuses, _ = adopt(tmp_path / "s", tmp_path / "c", "l", [_device()], attached)

    assert statuses[0].state == "offline"


def test_serial_collision_is_propagated_not_swallowed(tmp_path, monkeypatch):
    from playground.devices.registry import record_seen

    other = DeviceFacts(serial="S1", fingerprint="fp-old")
    record_seen(tmp_path / "s", other)

    attached = [AttachedDevice(serial="S1", state="connected")]
    facts = DeviceFacts(serial="S1", fingerprint="fp-new")
    monkeypatch.setattr("playground.devices.adopt.probe_facts", lambda serial: facts)

    statuses, diagnostics = adopt(
        tmp_path / "s", tmp_path / "c", "l", [_device()], attached
    )

    assert [d.id for d in diagnostics] == ["device.serial_collision"]
    # The pre-existing, trusted fingerprint is what gets reported, not the
    # freshly probed one -- collision means the fresh probe is untrusted.
    assert statuses[0].serial == "S1"


def test_require_failure_on_a_connected_device_includes_the_shared_note(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "c"
    write_note(
        "root broken since the September OTA, needs Magisk re-flash",
        scope="shared",
        config_dir=config_dir,
        state_dir=tmp_path / "s",
        name="pixel7",
        serial=None,
    )
    attached = [AttachedDevice(serial="S1", state="connected")]
    facts = DeviceFacts(serial="S1", rooted=False, sdk=34)
    monkeypatch.setattr("playground.devices.adopt.probe_facts", lambda serial: facts)

    device = _device(require=DeviceRequire(root=True))
    _, diagnostics = adopt(tmp_path / "s", config_dir, "l", [device], attached)

    ids = [d.id for d in diagnostics]
    assert "device.require_unmet" in ids
    require_diag = next(d for d in diagnostics if d.id == "device.require_unmet")
    assert "Magisk" in require_diag.message


def test_require_failure_uses_the_profiles_note_not_the_slots_own_name(
    tmp_path, monkeypatch
):
    """A slot named `pixel7` referencing profile `my-pixel` must find the
    note at `my-pixel.md`, not `pixel7.md` -- the case that was silently
    broken before `ResolvedDevice.profile` existed."""
    config_dir = tmp_path / "c"
    write_note(
        "root broken since the September OTA, needs Magisk re-flash",
        scope="shared",
        config_dir=config_dir,
        state_dir=tmp_path / "s",
        name="my-pixel",
        serial=None,
    )
    # A decoy note under the slot's own name -- proves the profile name
    # wins, not merely that *a* note was found.
    write_note(
        "unrelated note that must not be picked up",
        scope="shared",
        config_dir=config_dir,
        state_dir=tmp_path / "s",
        name="pixel7",
        serial=None,
    )
    attached = [AttachedDevice(serial="S1", state="connected")]
    facts = DeviceFacts(serial="S1", rooted=False, sdk=34)
    monkeypatch.setattr("playground.devices.adopt.probe_facts", lambda serial: facts)

    device = _device(profile="my-pixel", require=DeviceRequire(root=True))
    _, diagnostics = adopt(tmp_path / "s", config_dir, "l", [device], attached)

    require_diag = next(d for d in diagnostics if d.id == "device.require_unmet")
    assert "Magisk" in require_diag.message
    assert "unrelated" not in require_diag.message


def test_require_failure_falls_back_to_the_private_note(tmp_path, monkeypatch):
    state_dir = tmp_path / "s"
    write_note(
        "private note: this unit's fingerprint sensor is flaky",
        scope="private",
        config_dir=tmp_path / "c",
        state_dir=state_dir,
        name=None,
        serial="S1",
    )
    attached = [AttachedDevice(serial="S1", state="connected")]
    facts = DeviceFacts(serial="S1", rooted=False, sdk=34)
    monkeypatch.setattr("playground.devices.adopt.probe_facts", lambda serial: facts)

    device = _device(require=DeviceRequire(root=True))
    _, diagnostics = adopt(state_dir, tmp_path / "c", "l", [device], attached)

    require_diag = next(d for d in diagnostics if d.id == "device.require_unmet")
    assert "flaky" in require_diag.message


def test_no_note_anywhere_does_not_error(tmp_path, monkeypatch):
    attached = [AttachedDevice(serial="S1", state="connected")]
    facts = DeviceFacts(serial="S1", rooted=False, sdk=34)
    monkeypatch.setattr("playground.devices.adopt.probe_facts", lambda serial: facts)

    device = _device(require=DeviceRequire(root=True))
    _, diagnostics = adopt(tmp_path / "s", tmp_path / "c", "l", [device], attached)

    require_diag = next(d for d in diagnostics if d.id == "device.require_unmet")
    assert "note:" not in require_diag.message


def test_an_ambiguous_match_stays_an_error_even_when_optional(tmp_path):
    attached = [
        AttachedDevice(serial="S1", state="connected"),
        AttachedDevice(serial="S2", state="connected"),
    ]
    _, diagnostics = adopt(
        tmp_path / "s", tmp_path / "c", "l", [_device(required=False)], attached
    )
    assert [d.id for d in diagnostics] == ["config.device.ambiguous_match"]
    assert diagnostics[0].severity == "error"


# --- release ----------------------------------------------------------


def test_release_clears_the_binding(tmp_path, monkeypatch):
    attached = [AttachedDevice(serial="S1", state="connected")]
    facts = DeviceFacts(serial="S1", sdk=34, rooted=False)
    monkeypatch.setattr("playground.devices.adopt.probe_facts", lambda serial: facts)
    adopt(tmp_path / "s", tmp_path / "c", "l", [_device()], attached)
    assert read_binding(tmp_path / "s", "l", "pixel7") is not None

    diagnostics = release(tmp_path / "s", "l", [_device()])

    assert diagnostics == []
    assert read_binding(tmp_path / "s", "l", "pixel7") is None


def test_release_on_a_never_adopted_slot_is_a_quiet_no_op(tmp_path):
    assert release(tmp_path / "s", "l", [_device()]) == []
