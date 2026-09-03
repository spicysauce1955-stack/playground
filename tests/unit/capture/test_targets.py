"""Which VMs can be captured, and how the refusals read.

Mirrors tests/unit/android/test_targets.py: build a ResolvedLab from the
committed config, synthesize a LabStatus, and assert on diagnostic ids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.capture.targets import capture_vm_names, resolve_capture_targets
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


@pytest.fixture
def generic_lab():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


def _status(resolved, *, reachable: bool = True) -> LabStatus:
    return LabStatus(
        lab=resolved.lab_name,
        backend=resolved.backend,
        expected_vms=len(resolved.vms),
        provisioned_vms=len(resolved.vms),
        vms=[
            VmStatus(
                name=vm.name,
                role=vm.role,
                state="running",
                ssh_host="127.0.0.1" if reachable else "",
                ssh_port=22,
            )
            for vm in resolved.vms
        ],
    )


def test_capture_vm_names_follows_the_capability(redroid_lab) -> None:
    assert capture_vm_names(redroid_lab) == ["droid1"]


def test_a_lab_with_no_capturable_vm_is_a_clean_error(generic_lab) -> None:
    targets, diagnostics = resolve_capture_targets(
        generic_lab, _status(generic_lab),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.capture.no_capture_vms"]


def test_single_device_needs_no_targeting_flag(redroid_lab) -> None:
    targets, diagnostics = resolve_capture_targets(
        redroid_lab, _status(redroid_lab),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert diagnostics == []
    assert [t.vm_name for t in targets] == ["droid1"]
    assert targets[0].ssh_user == "ubuntu"


def test_unknown_vm_names_the_candidates(redroid_lab) -> None:
    targets, diagnostics = resolve_capture_targets(
        redroid_lab, _status(redroid_lab),
        on="nope", role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.capture.unknown_vm"]
    assert "droid1" in diagnostics[0].message


def test_capture_disabled_in_the_lab_is_refused(redroid_lab) -> None:
    """enabled: false skips provisioning entirely, so there is no unit to
    start. Say that, rather than failing later on a missing unit."""
    disabled = redroid_lab.model_copy(
        update={"capture": redroid_lab.capture.model_copy(update={"enabled": False})}
    )
    targets, diagnostics = resolve_capture_targets(
        disabled, _status(disabled),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.capture.disabled"]


def test_vbox_is_refused_with_capture_specific_wording(redroid_lab) -> None:
    """Redroid is unverified on local-vbox, so there is no device to
    capture. The message must not talk about idle-compute billing, which
    is what the shared verb_not_supported helper says."""
    vbox = redroid_lab.model_copy(update={"backend": "local-vbox"})
    targets, diagnostics = resolve_capture_targets(
        vbox, _status(vbox),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.capture.backend_unsupported"]
    assert "billing" not in diagnostics[0].message.lower()


def test_unreachable_vm_says_apply_first(redroid_lab) -> None:
    targets, diagnostics = resolve_capture_targets(
        redroid_lab, _status(redroid_lab, reachable=False),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.capture.vm_not_reachable"]
