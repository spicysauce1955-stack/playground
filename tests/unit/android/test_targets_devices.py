"""Physical handsets as `resolve_targets` candidates, alongside Redroid VMs.

Mirrors `test_targets.py`'s VM-only coverage, but for the device half of
`resolve_targets` added to make `playground app`/`adb`/`shell` treat a
handset exactly like a VM. Fixtures come from `conftest.py`.
"""

from __future__ import annotations

from playground.android.targets import resolve_targets
from playground.android.transport import GuestAdb, LocalAdb
from playground.models.status import DeviceStatus, LabStatus, VmStatus


def test_a_connected_device_becomes_a_local_target(resolved_lab_with_device) -> None:
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=0, provisioned_vms=0,
        devices=[DeviceStatus(name="pixel7", state="connected", serial="S1")],
    )
    targets, diagnostics = resolve_targets(
        resolved_lab_with_device, status, on="pixel7", role=None,
        all_devices=False, user="ubuntu",
    )
    assert diagnostics == []
    assert targets[0].transport == LocalAdb("S1")


def test_an_unauthorized_device_is_not_a_target(resolved_lab_with_device) -> None:
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=0, provisioned_vms=0,
        devices=[DeviceStatus(name="pixel7", state="unauthorized", serial="S1")],
    )
    targets, diagnostics = resolve_targets(
        resolved_lab_with_device, status, on="pixel7", role=None,
        all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["device.unauthorized"]
    assert "Allow" in diagnostics[0].suggestion


def test_an_offline_device_gets_not_connected(resolved_lab_with_device) -> None:
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=0, provisioned_vms=0,
        devices=[DeviceStatus(name="pixel7", state="offline", serial="S1")],
    )
    targets, diagnostics = resolve_targets(
        resolved_lab_with_device, status, on="pixel7", role=None,
        all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["device.not_connected"]


def test_a_missing_device_is_never_silently_skipped(resolved_lab_with_device) -> None:
    """No status entry at all for a declared device slot must still be an
    explicit diagnostic, not an empty (silently-skipped) target list."""
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=0, provisioned_vms=0,
        devices=[],
    )
    targets, diagnostics = resolve_targets(
        resolved_lab_with_device, status, on="pixel7", role=None,
        all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["device.not_connected"]


def test_all_spans_emulators_and_handsets(resolved_mixed_lab) -> None:
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=1, provisioned_vms=1,
        vms=[
            VmStatus(
                name="droid1", role="redroid-host", state="provisioned",
                ssh_host="10.0.0.5", ssh_port=22,
            )
        ],
        devices=[DeviceStatus(name="pixel7", state="connected", serial="S1")],
    )
    targets, diagnostics = resolve_targets(
        resolved_mixed_lab, status, on=None, role=None, all_devices=True, user="ubuntu",
    )
    assert diagnostics == []
    kinds = {type(t.transport) for t in targets}
    assert kinds == {GuestAdb, LocalAdb}
    assert {t.name for t in targets} == {"droid1", "pixel7"}


def test_single_default_target_spans_a_device_only_lab(resolved_lab_with_device) -> None:
    """A lab with exactly one Android target (here, a lone device slot)
    needs no `--on`/`--all` -- same rule VMs already get."""
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=0, provisioned_vms=0,
        devices=[DeviceStatus(name="pixel7", state="connected", serial="S1")],
    )
    targets, diagnostics = resolve_targets(
        resolved_lab_with_device, status, on=None, role=None,
        all_devices=False, user="ubuntu",
    )
    assert diagnostics == []
    assert [t.name for t in targets] == ["pixel7"]


def test_role_flag_never_matches_a_device(resolved_mixed_lab) -> None:
    """Device slots have `tags` but no `role` -- `--role` must not
    silently drop the handset from consideration; it simply never
    selects one, and an unmatched `--role` still reports a clean error."""
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=1, provisioned_vms=1,
        vms=[
            VmStatus(
                name="droid1", role="redroid-host", state="provisioned",
                ssh_host="10.0.0.5", ssh_port=22,
            )
        ],
        devices=[DeviceStatus(name="pixel7", state="connected", serial="S1")],
    )
    targets, diagnostics = resolve_targets(
        resolved_mixed_lab, status, on=None, role="nonexistent-role",
        all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.app.unknown_vm"]


def test_unknown_on_names_devices_when_the_lab_has_one(resolved_mixed_lab) -> None:
    """The unknown-name error must not tell an operator with a handset to
    go looking for a VM -- it names both populations and uses the
    device-aware diagnostic id."""
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=1, provisioned_vms=1,
        vms=[
            VmStatus(
                name="droid1", role="redroid-host", state="provisioned",
                ssh_host="10.0.0.5", ssh_port=22,
            )
        ],
        devices=[DeviceStatus(name="pixel7", state="connected", serial="S1")],
    )
    targets, diagnostics = resolve_targets(
        resolved_mixed_lab, status, on="nope", role=None,
        all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.device.unknown_device"]
    assert "droid1" in diagnostics[0].message
    assert "pixel7" in diagnostics[0].message


def test_unknown_on_stays_the_old_id_without_any_device(resolved_mixed_lab) -> None:
    """A lab with no device slots at all keeps the pre-existing
    `config.app.unknown_vm` id -- unaffected by this change."""
    no_devices = resolved_mixed_lab.model_copy(update={"devices": []})
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=1, provisioned_vms=1,
        vms=[
            VmStatus(
                name="droid1", role="redroid-host", state="provisioned",
                ssh_host="10.0.0.5", ssh_port=22,
            )
        ],
    )
    targets, diagnostics = resolve_targets(
        no_devices, status, on="nope", role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.app.unknown_vm"]


def test_a_swapped_handset_refuses_with_rebind_required(resolved_lab_with_device) -> None:
    """`device.not_connected` ("device is not attached") is flatly untrue
    when a matching handset IS attached under a different serial. The slot
    must refuse with `device.rebind_required` instead, and the refusal has
    to name a flag that actually exists -- `--rebind` lives on `apply`,
    the one verb allowed to retarget a slot."""
    status = LabStatus(
        lab="generic-infra", backend="local-libvirt", expected_vms=0, provisioned_vms=0,
        devices=[
            DeviceStatus(
                name="pixel7", state="missing", serial="S1", rebind_candidates=["S2"]
            )
        ],
    )
    targets, diagnostics = resolve_targets(
        resolved_lab_with_device, status, on="pixel7", role=None,
        all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["device.rebind_required"]
    assert "S2" in diagnostics[0].message
    assert "apply generic-infra --rebind" in diagnostics[0].suggestion


def test_rebind_required_outranks_the_plain_missing_wording(
    resolved_lab_with_device,
) -> None:
    """A `missing` slot with no rebind candidate keeps the old, correct
    `device.not_connected` -- the new branch must not swallow it."""
    status = LabStatus(
        lab="generic-infra", backend="local-libvirt", expected_vms=0, provisioned_vms=0,
        devices=[DeviceStatus(name="pixel7", state="missing", serial="S1")],
    )
    _targets, diagnostics = resolve_targets(
        resolved_lab_with_device, status, on="pixel7", role=None,
        all_devices=False, user="ubuntu",
    )
    assert [d.id for d in diagnostics] == ["device.not_connected"]
