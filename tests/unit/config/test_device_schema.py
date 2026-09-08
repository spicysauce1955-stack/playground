from __future__ import annotations

import pytest
from pydantic import ValidationError

from playground.models.kinds import DeviceMatch, DeviceRequire, LabDevice
from playground.models.resolved import ResolvedDevice


def test_device_defaults_to_android_and_required():
    d = LabDevice(name="pixel7")
    assert d.kind == "android"
    assert d.required is True
    assert d.match is None
    assert d.require is None


def test_match_is_an_optional_selector():
    d = LabDevice(name="pixel7", match=DeviceMatch(serial="1A2B"))
    assert d.match.serial == "1A2B"


def test_require_carries_assertions_not_facts():
    d = LabDevice(name="pixel7", require=DeviceRequire(root=True, min_sdk=30))
    assert d.require.root is True
    assert d.require.min_sdk == 30


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        LabDevice(name="pixel7", rooted=True)


def test_resolved_device_has_no_capabilities_field():
    """PROBED FACTS must not reach the resolved model -- the resolver runs
    with no handset attached and cannot know them. `profile` is not a
    probed fact; it is config-declared intent like `match`/`require`, so
    it does belong here."""
    assert "capabilities" not in ResolvedDevice.model_fields
    assert set(ResolvedDevice.model_fields) == {
        "name", "kind", "profile", "match", "require", "tags", "required",
    }


def test_device_match_has_no_manufacturer_field():
    """`adb devices -l` never emits a manufacturer -- a manufacturer
    selector could never match anything, so the field does not exist."""
    assert "manufacturer" not in DeviceMatch.model_fields
    assert set(DeviceMatch.model_fields) == {"serial", "model"}
