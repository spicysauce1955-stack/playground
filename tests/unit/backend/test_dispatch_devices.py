"""`backend.dispatch.query_status` populating `LabStatus.devices`.

This is Task 16's handoff (a): no backend provisions a physical handset,
so device status must be assembled in the backend-neutral dispatch layer,
not inside any one backend adapter -- and it must do so WITHOUT mutating
a binding (no `resolve_binding` auto-bind) and without invoking a real
`adb` (every test here monkeypatches `dispatch.list_attached`).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from textwrap import dedent

from playground.backend import dispatch
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.devices.adb import AttachedDevice, DeviceFacts
from playground.devices.binding import read_binding, write_binding
from playground.devices.registry import record_seen
from playground.models.resolved import ResolvedLab
from playground.models.status import DeviceStatus, LabStatus, VmStatus

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


def _device_lab_config(tmp_path: Path) -> Path:
    """`generic-infra`, plus a `pixel7` device slot -- synthesized rather
    than committed so `test_every_committed_yaml_parses` and friends
    don't have to account for a permanent device-bearing lab."""
    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config_dir)
    (config_dir / "labs" / "device-lab.yaml").write_text(
        dedent(
            """
            apiVersion: playground/v1
            kind: Lab
            metadata:
              name: device-lab
            spec:
              backend: local-libvirt
              networks:
                - name: lab-net
                  profile: nat
                  cidr: 10.88.0.0/24
              vms:
                - name: node1
                  role: generic-node
                  networks: [lab-net]
              devices:
                - name: pixel7
                  kind: android
            """
        ).lstrip("\n")
    )
    return config_dir


def _resolved_device_lab(tmp_path: Path) -> ResolvedLab:
    loaded, diagnostics = load_config(_device_lab_config(tmp_path))
    assert diagnostics == []
    return resolve_lab(loaded, "device-lab")


def _resolved_generic_infra() -> ResolvedLab:
    loaded, _diagnostics = load_config(CONFIG_DIR)
    return resolve_lab(loaded, "generic-infra")


def _stub_vm_status(resolved: ResolvedLab, tofu_dir: Path) -> tuple[LabStatus, list]:
    return (
        LabStatus(
            lab=resolved.lab_name,
            backend=resolved.backend,
            expected_vms=len(resolved.vms),
            provisioned_vms=0,
            vms=[
                VmStatus(name=vm.name, role=vm.role, state="missing")
                for vm in resolved.vms
            ],
        ),
        [],
    )


def test_a_device_less_lab_never_touches_adb(tmp_path, monkeypatch) -> None:
    """The core no-regression guarantee: a lab with no `spec.devices`
    must behave exactly as before this task -- `list_attached` (and so
    real `adb`) is never even called."""
    called = []
    monkeypatch.setattr(dispatch, "list_attached", lambda: called.append(1) or ([], []))
    monkeypatch.setattr(dispatch.local_libvirt, "query_status", _stub_vm_status)

    resolved = _resolved_generic_infra()
    status, diagnostics = dispatch.query_status(resolved, tmp_path / "tofu", tmp_path)

    assert status.devices == []
    assert diagnostics == []
    assert called == []


def test_unbound_slot_reports_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dispatch, "list_attached", lambda: ([], []))
    monkeypatch.setattr(dispatch.local_libvirt, "query_status", _stub_vm_status)

    resolved = _resolved_device_lab(tmp_path)
    status, _diagnostics = dispatch.query_status(resolved, tmp_path / "tofu", tmp_path)

    assert status.devices == [DeviceStatus(name="pixel7", state="missing")]


def test_bound_but_unplugged_slot_reports_missing_with_its_serial(
    tmp_path, monkeypatch
) -> None:
    write_binding(tmp_path, "device-lab", "pixel7", "S1")
    monkeypatch.setattr(dispatch, "list_attached", lambda: ([], []))
    monkeypatch.setattr(dispatch.local_libvirt, "query_status", _stub_vm_status)

    resolved = _resolved_device_lab(tmp_path)
    status, _diagnostics = dispatch.query_status(resolved, tmp_path / "tofu", tmp_path)

    assert status.devices == [DeviceStatus(name="pixel7", state="missing", serial="S1")]


def test_bound_and_attached_slot_reports_connected_with_registry_facts(
    tmp_path, monkeypatch
) -> None:
    write_binding(tmp_path, "device-lab", "pixel7", "S1")
    entry, seen_diagnostics = record_seen(
        tmp_path,
        DeviceFacts(
            serial="S1", model="Pixel 7", manufacturer="Google",
            sdk=34, abi="arm64-v8a", rooted=True,
        ),
    )
    assert seen_diagnostics == []
    monkeypatch.setattr(
        dispatch,
        "list_attached",
        lambda: ([AttachedDevice(serial="S1", state="connected", model="Pixel 7")], []),
    )
    monkeypatch.setattr(dispatch.local_libvirt, "query_status", _stub_vm_status)

    resolved = _resolved_device_lab(tmp_path)
    status, _diagnostics = dispatch.query_status(resolved, tmp_path / "tofu", tmp_path)

    assert status.devices == [
        DeviceStatus(
            name="pixel7", state="connected", serial="S1", model="Pixel 7",
            manufacturer="Google", sdk=34, abi="arm64-v8a", rooted=True,
            last_verified=entry.last_verified,
        )
    ]


def test_bound_and_attached_but_unauthorized(tmp_path, monkeypatch) -> None:
    write_binding(tmp_path, "device-lab", "pixel7", "S1")
    monkeypatch.setattr(
        dispatch,
        "list_attached",
        lambda: ([AttachedDevice(serial="S1", state="unauthorized")], []),
    )
    monkeypatch.setattr(dispatch.local_libvirt, "query_status", _stub_vm_status)

    resolved = _resolved_device_lab(tmp_path)
    status, _diagnostics = dispatch.query_status(resolved, tmp_path / "tofu", tmp_path)

    assert status.devices[0].state == "unauthorized"
    assert status.devices[0].serial == "S1"


def test_query_status_never_auto_binds_an_unbound_slot(tmp_path, monkeypatch) -> None:
    """A live handset that matches an unbound slot's selector must NOT be
    bound as a side effect of a read-only `status` query -- that is
    exactly what `resolve_binding` would do (bind-on-first-sight when
    there's no existing binding and exactly one candidate), so this
    function reads the persisted binding instead of resolving one."""
    monkeypatch.setattr(
        dispatch,
        "list_attached",
        lambda: ([AttachedDevice(serial="S1", state="connected", model="Pixel 7")], []),
    )
    monkeypatch.setattr(dispatch.local_libvirt, "query_status", _stub_vm_status)

    resolved = _resolved_device_lab(tmp_path)
    status, _diagnostics = dispatch.query_status(resolved, tmp_path / "tofu", tmp_path)

    assert status.devices == [DeviceStatus(name="pixel7", state="missing")]
    assert read_binding(tmp_path, "device-lab", "pixel7") is None


def test_query_status_default_state_dir_matches_the_rest_of_the_cli(
    tmp_path, monkeypatch
) -> None:
    """Callers that pass no `state_dir` still see live device state for
    the common case: the default matches every other command's default
    (`.playground`), not an arbitrary path only this function knows."""
    monkeypatch.chdir(tmp_path)
    write_binding(Path(".playground"), "device-lab", "pixel7", "S1")
    monkeypatch.setattr(
        dispatch,
        "list_attached",
        lambda: ([AttachedDevice(serial="S1", state="connected")], []),
    )
    monkeypatch.setattr(dispatch.local_libvirt, "query_status", _stub_vm_status)

    resolved = _resolved_device_lab(tmp_path)
    status, _diagnostics = dispatch.query_status(resolved, tmp_path / "tofu")

    assert status.devices[0].state == "connected"
