"""Selecting which VMs an `app` command acts on."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.android.targets import android_vm_names, resolve_targets
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.models.status import LabStatus, VmStatus

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def redroid_lab():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "redroid-cloud")


def _status(names: list[str]) -> LabStatus:
    # NOTE: the real model is `LabStatus(lab=..., expected_vms=..., ...)`
    # and `VmStatus.state` is the string literal `"running"` (VmState is a
    # `Literal`, not an enum — `VmState.running` does not exist). Verified
    # via `uv run python -c "from playground.models.status import
    # LabStatus, VmStatus, VmState; print(LabStatus.model_fields.keys());
    # print(VmStatus.model_fields.keys())"` per Task 3 Step 6.
    return LabStatus(
        lab="redroid-cloud",
        backend="cloud-digitalocean",
        expected_vms=len(names),
        provisioned_vms=len(names),
        vms=[
            VmStatus(
                name=n, role="redroid-host", state="running",
                ssh_host=f"10.0.0.{i + 2}", ssh_port=22,
            )
            for i, n in enumerate(names)
        ],
    )


def test_android_vm_names_selects_only_redroid_capable(redroid_lab) -> None:
    assert android_vm_names(redroid_lab) == ["droid1"]


def test_all_devices_resolves_every_android_vm(redroid_lab) -> None:
    targets, diagnostics = resolve_targets(
        redroid_lab, _status(["droid1"]),
        on=None, role=None, all_devices=True, user="ubuntu",
    )
    assert [t.vm_name for t in targets] == ["droid1"]
    assert diagnostics == []
    assert targets[0].ssh_user == "ubuntu"
    assert targets[0].ssh_port == 22


def test_single_android_vm_is_the_default_target(redroid_lab) -> None:
    """No flag needed when the lab has exactly one Android VM."""
    targets, diagnostics = resolve_targets(
        redroid_lab, _status(["droid1"]),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert [t.vm_name for t in targets] == ["droid1"]
    assert diagnostics == []


def test_unknown_vm_is_an_error(redroid_lab) -> None:
    targets, diagnostics = resolve_targets(
        redroid_lab, _status(["droid1"]),
        on="nope", role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.app.unknown_vm"]


def test_vm_without_an_ssh_endpoint_is_an_error(redroid_lab) -> None:
    status = LabStatus(
        lab="redroid-cloud", backend="cloud-digitalocean",
        expected_vms=1, provisioned_vms=0,
        vms=[VmStatus(name="droid1", role="redroid-host", state="missing")],
    )
    targets, diagnostics = resolve_targets(
        redroid_lab, status,
        on="droid1", role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.app.vm_not_reachable"]
