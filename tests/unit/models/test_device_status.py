from __future__ import annotations

from playground.models.status import DeviceStatus, LabStatus


def test_device_status_carries_the_probed_facts():
    d = DeviceStatus(
        name="pixel7", state="connected", serial="1A2B",
        model="Pixel 7", sdk=34, abi="arm64-v8a", rooted=False,
    )
    assert d.state == "connected"
    assert d.rooted is False


def test_unauthorized_is_its_own_state():
    """Never tapping the RSA prompt is the most common real failure and
    must not be reported as generic unreachability."""
    d = DeviceStatus(name="pixel7", state="unauthorized", serial="1A2B")
    assert d.state == "unauthorized"
    assert d.model is None


def test_lab_status_devices_defaults_empty():
    s = LabStatus(lab="l", backend="local-libvirt", expected_vms=0, provisioned_vms=0)
    assert s.devices == []
