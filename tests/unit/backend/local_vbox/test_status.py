"""Tests for local-vbox status.query_status ssh_host/ssh_port fields.

Regression guard for NOTE-2: VmStatus must expose a backend-neutral SSH
endpoint. vbox VMs are reached via NAT (127.0.0.1:<forwarded port>);
missing VMs expose neither field.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.local_vbox import status as status_module
from playground.backend.local_vbox.status import query_status
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_vbox_smoke():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "vbox-smoke")


# ---------------------------------------------------------------------------
# Running VM => ssh_host=127.0.0.1, ssh_port=<nat port>
# ---------------------------------------------------------------------------


def test_running_vm_reports_ssh_host_localhost(
    resolved_vbox_smoke, monkeypatch
) -> None:
    """A running vbox VM must report ssh_host='127.0.0.1' (NAT endpoint)."""
    lab = resolved_vbox_smoke.lab_name
    vm_name = resolved_vbox_smoke.vms[0].name
    vbox_name = f"{lab}-{vm_name}"

    monkeypatch.setattr(status_module, "vboxmanage_available", lambda: True)
    monkeypatch.setattr(status_module, "list_vms", lambda: [vbox_name])
    monkeypatch.setattr(status_module, "list_running_vms", lambda: [vbox_name])
    monkeypatch.setattr(status_module, "nat_ssh_port", lambda name: 2222)

    lab_status, diags = query_status(resolved_vbox_smoke)

    vm = next(v for v in lab_status.vms if v.name == vm_name)
    assert vm.state == "running"
    assert vm.ssh_host == "127.0.0.1"


def test_running_vm_reports_nat_forwarded_port(
    resolved_vbox_smoke, monkeypatch
) -> None:
    """A running vbox VM must report the NAT-forwarded port from nat_ssh_port."""
    lab = resolved_vbox_smoke.lab_name
    vm_name = resolved_vbox_smoke.vms[0].name
    vbox_name = f"{lab}-{vm_name}"

    monkeypatch.setattr(status_module, "vboxmanage_available", lambda: True)
    monkeypatch.setattr(status_module, "list_vms", lambda: [vbox_name])
    monkeypatch.setattr(status_module, "list_running_vms", lambda: [vbox_name])
    monkeypatch.setattr(status_module, "nat_ssh_port", lambda name: 2247)

    lab_status, _ = query_status(resolved_vbox_smoke)

    vm = next(v for v in lab_status.vms if v.name == vm_name)
    assert vm.ssh_port == 2247


def test_provisioned_vm_reports_ssh_endpoint(
    resolved_vbox_smoke, monkeypatch
) -> None:
    """A registered-but-off VM (state=provisioned) also exposes the SSH endpoint."""
    lab = resolved_vbox_smoke.lab_name
    vm_name = resolved_vbox_smoke.vms[0].name
    vbox_name = f"{lab}-{vm_name}"

    monkeypatch.setattr(status_module, "vboxmanage_available", lambda: True)
    monkeypatch.setattr(status_module, "list_vms", lambda: [vbox_name])
    monkeypatch.setattr(status_module, "list_running_vms", lambda: [])  # off
    monkeypatch.setattr(status_module, "nat_ssh_port", lambda name: 2230)

    lab_status, _ = query_status(resolved_vbox_smoke)

    vm = next(v for v in lab_status.vms if v.name == vm_name)
    assert vm.state == "provisioned"
    assert vm.ssh_host == "127.0.0.1"
    assert vm.ssh_port == 2230


# ---------------------------------------------------------------------------
# Missing VM => ssh_host=None, ssh_port=None
# ---------------------------------------------------------------------------


def test_missing_vm_reports_no_ssh_endpoint(
    resolved_vbox_smoke, monkeypatch
) -> None:
    """A missing vbox VM must report ssh_host=None and ssh_port=None."""
    monkeypatch.setattr(status_module, "vboxmanage_available", lambda: True)
    monkeypatch.setattr(status_module, "list_vms", lambda: [])
    monkeypatch.setattr(status_module, "list_running_vms", lambda: [])
    monkeypatch.setattr(status_module, "nat_ssh_port", lambda name: None)

    lab_status, _ = query_status(resolved_vbox_smoke)

    for vm in lab_status.vms:
        assert vm.state == "missing"
        assert vm.ssh_host is None
        assert vm.ssh_port is None
