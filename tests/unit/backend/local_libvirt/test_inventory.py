"""Tests for the local-libvirt inventory renderer."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from playground.backend.local_libvirt.inventory import (
    fetch_vm_ips,
    render_inventory,
)
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_generic_infra():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


# ---------------------------------------------------------------------------
# render_inventory — pure function
# ---------------------------------------------------------------------------


def test_render_inventory_emits_one_host_per_vm(resolved_generic_infra) -> None:
    ips = ["10.0.10.2", "10.0.10.3", "10.0.10.4"]

    body, diagnostics = render_inventory(resolved_generic_infra, ips)

    assert diagnostics == []
    assert "[playground]" in body
    assert "node1 ansible_host=10.0.10.2 ansible_user=ubuntu pg_role=generic-node" in body
    assert "docker1 ansible_host=10.0.10.3 ansible_user=ubuntu pg_role=docker-host" in body
    assert "router1 ansible_host=10.0.10.4 ansible_user=ubuntu pg_role=router" in body
    assert "pg_lab=generic-infra" in body


def test_render_inventory_preserves_networks_and_tags(resolved_generic_infra) -> None:
    ips = ["10.0.10.2", "10.0.10.3", "10.0.10.4"]

    body, _ = render_inventory(resolved_generic_infra, ips)

    # docker1 attaches to two networks; router1 to three
    assert "pg_networks=edge,lab-private" in body
    assert "pg_networks=edge,lab-private,routed-a" in body


def test_render_inventory_lab_metadata_and_source_pointer(
    resolved_generic_infra,
) -> None:
    body, _ = render_inventory(resolved_generic_infra, ["10.0.10.2"] * 3)

    assert "# Lab: generic-infra" in body
    assert "# Source: config/labs/generic-infra.yaml" in body


def test_render_inventory_flags_count_mismatch(resolved_generic_infra) -> None:
    # Lab has 3 VMs; supply only 1 IP
    body, diagnostics = render_inventory(resolved_generic_infra, ["10.0.10.2"])

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.count_mismatch"
    assert diagnostics[0].severity == "error"
    assert "3 VMs" in diagnostics[0].message
    assert "1 IPs" in diagnostics[0].message
    # Best-effort body still emitted — the prefix is rendered
    assert "node1 ansible_host=10.0.10.2" in body
    assert "docker1" not in body  # nothing to pair with


def test_render_inventory_handles_empty_vm_list(resolved_generic_infra) -> None:
    # Lab with VMs vs no IPs — count_mismatch fires, body still well-formed
    body, diagnostics = render_inventory(resolved_generic_infra, [])

    assert any(d.id == "config.inventory.count_mismatch" for d in diagnostics)
    assert "[playground]" in body
    assert "[playground:vars]" in body
    assert "pg_lab=generic-infra" in body


# ---------------------------------------------------------------------------
# fetch_vm_ips — subprocess-driven, uses a fake tofu binary on PATH
# ---------------------------------------------------------------------------


def _write_fake_tofu(tmp_path: Path, body: str, exit_code: int = 0) -> Path:
    """Write a `tofu` shim into ``tmp_path/bin`` and return that bin dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tofu = bin_dir / "tofu"
    tofu.write_text(
        f"#!/usr/bin/env bash\ncat <<'EOF'\n{body}\nEOF\nexit {exit_code}\n"
    )
    tofu.chmod(tofu.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def test_fetch_vm_ips_parses_real_shape(tmp_path, monkeypatch) -> None:
    payload = json.dumps(
        {
            "vm_ips": {
                "sensitive": False,
                "type": ["tuple", ["string", "string", "string"]],
                "value": ["10.0.10.2", "10.0.10.3", "10.0.10.4"],
            }
        }
    )
    bin_dir = _write_fake_tofu(tmp_path, payload)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert diagnostics == []
    assert ips == ["10.0.10.2", "10.0.10.3", "10.0.10.4"]


def test_fetch_vm_ips_reports_missing_binary(tmp_path, monkeypatch) -> None:
    # Empty PATH so `tofu` is not resolvable
    monkeypatch.setenv("PATH", "")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == []
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_binary_missing"


def test_fetch_vm_ips_reports_no_state(tmp_path, monkeypatch) -> None:
    bin_dir = _write_fake_tofu(tmp_path, "{}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == []
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_no_state"


def test_fetch_vm_ips_reports_nonzero_exit(tmp_path, monkeypatch) -> None:
    bin_dir = _write_fake_tofu(tmp_path, "boom", exit_code=2)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == []
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_command_failed"
    assert "exited 2" in diagnostics[0].message


def test_fetch_vm_ips_reports_parse_failure(tmp_path, monkeypatch) -> None:
    bin_dir = _write_fake_tofu(tmp_path, "not json at all")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == []
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_parse_failed"


def test_fetch_vm_ips_reports_timeout(tmp_path, monkeypatch) -> None:
    bin_dir = _write_fake_tofu(tmp_path, "{}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    import subprocess as _sp

    def _raise(*args, **kwargs):
        raise _sp.TimeoutExpired(cmd="tofu", timeout=30)

    monkeypatch.setattr("playground.backend.local_libvirt.inventory.subprocess.run", _raise)

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == []
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_command_failed"


def test_fetch_vm_ips_reports_subprocess_filenotfound(tmp_path, monkeypatch) -> None:
    # shutil.which finds the binary but subprocess.run raises (race / symlink).
    bin_dir = _write_fake_tofu(tmp_path, "{}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def _raise(*args, **kwargs):
        raise FileNotFoundError("tofu vanished")

    monkeypatch.setattr("playground.backend.local_libvirt.inventory.subprocess.run", _raise)

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == []
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_command_failed"


def test_render_inventory_zero_vms_zero_ips(resolved_generic_infra) -> None:
    # Synthesize a lab with no VMs by deep-copying with vms=[]; render must
    # produce a well-formed body with no count_mismatch.
    empty = resolved_generic_infra.model_copy(update={"vms": []})

    body, diagnostics = render_inventory(empty, [])

    assert diagnostics == []
    assert "[playground]" in body
    assert "[playground:vars]" in body
    assert "pg_lab=generic-infra" in body
    # No host lines between [playground] and [playground:vars]
    section = body.split("[playground]", 1)[1].split("[playground:vars]", 1)[0]
    assert section.strip() == ""


def test_fetch_vm_ips_reports_wrong_value_shape(tmp_path, monkeypatch) -> None:
    payload = json.dumps({"vm_ips": {"value": "not-a-list"}})
    bin_dir = _write_fake_tofu(tmp_path, payload)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == []
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_parse_failed"
