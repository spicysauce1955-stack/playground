from __future__ import annotations

from playground.devices.adb import DeviceFacts
from playground.devices.registry import read_registry, record_seen


def _facts(serial="1A2B", fingerprint="goog/panther/1"):
    return DeviceFacts(serial=serial, model="Pixel 7", sdk=34, fingerprint=fingerprint)


def test_first_sighting_is_recorded(tmp_path):
    entry, diagnostics = record_seen(tmp_path, _facts())
    assert diagnostics == []
    assert entry.first_seen == entry.last_seen
    assert read_registry(tmp_path)["1A2B"].facts.model == "Pixel 7"


def test_second_sighting_updates_last_seen_not_first(tmp_path):
    first, _ = record_seen(tmp_path, _facts())
    second, _ = record_seen(tmp_path, _facts())
    assert second.first_seen == first.first_seen
    assert second.last_seen >= first.last_seen


def test_an_unplugged_device_stays_in_the_registry(tmp_path):
    record_seen(tmp_path, _facts())
    assert "1A2B" in read_registry(tmp_path)


def test_serial_collision_refuses_rather_than_merging(tmp_path):
    """Cheap and cloned handsets ship duplicate serials and adb cannot tell
    two such devices apart. Merging two devices' history into one record
    is worse than failing."""
    record_seen(tmp_path, _facts(fingerprint="goog/panther/1"))
    entry, diagnostics = record_seen(tmp_path, _facts(fingerprint="other/clone/9"))
    assert [d.id for d in diagnostics] == ["device.serial_collision"]
    assert read_registry(tmp_path)["1A2B"].facts.fingerprint == "goog/panther/1"


def test_no_false_collision_when_a_fingerprint_is_unknown(tmp_path):
    """The collision guard only fires when BOTH fingerprints are known --
    a None fingerprint on either side must not trigger a false collision."""
    record_seen(tmp_path, _facts(fingerprint=None))
    entry, diagnostics = record_seen(tmp_path, _facts(fingerprint="goog/panther/1"))
    assert diagnostics == []
    assert entry.facts.fingerprint == "goog/panther/1"

    entry2, diagnostics2 = record_seen(tmp_path, _facts(fingerprint=None))
    assert diagnostics2 == []
    assert entry2.facts.fingerprint is None


def test_last_verified_updates_on_every_sighting(tmp_path):
    first, _ = record_seen(tmp_path, _facts())
    second, _ = record_seen(tmp_path, _facts())
    assert second.last_verified >= first.last_verified


def test_ignored_flag_survives_re_registration(tmp_path):
    from playground.devices.registry import mark_ignored

    record_seen(tmp_path, _facts())
    mark_ignored(tmp_path, "1A2B")
    assert read_registry(tmp_path)["1A2B"].ignored is True

    entry, _ = record_seen(tmp_path, _facts())
    assert entry.ignored is True


def test_mark_ignored_on_unknown_serial_is_a_noop(tmp_path):
    from playground.devices.registry import mark_ignored

    mark_ignored(tmp_path, "nonexistent")
    assert read_registry(tmp_path) == {}


def test_a_corrupt_registry_reads_as_empty(tmp_path):
    from playground.devices.registry import registry_path

    path = registry_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert read_registry(tmp_path) == {}


def test_registry_path_layout(tmp_path):
    from playground.devices.registry import registry_path

    assert registry_path(tmp_path) == tmp_path / "state" / "devices" / "registry.json"
