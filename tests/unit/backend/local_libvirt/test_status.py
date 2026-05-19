"""Tests for the local-libvirt status query."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from playground.backend.local_libvirt.status import query_status
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_generic_infra():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


def _write_tofu_shim(tmp_path: Path, payload: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tofu = bin_dir / "tofu"
    tofu.write_text(f"#!/usr/bin/env bash\ncat <<'EOF'\n{payload}\nEOF\n")
    tofu.chmod(tofu.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def test_query_status_reports_all_vms_provisioned(
    resolved_generic_infra, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.dumps(
        {
            "vm_ips": {
                "sensitive": False,
                "type": ["map", "string"],
                "value": {
                    "node1": "10.0.10.42",
                    "docker1": "10.0.10.43",
                    "router1": "10.0.10.44",
                },
            }
        }
    )
    bin_dir = _write_tofu_shim(tmp_path, payload)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    status, diagnostics = query_status(resolved_generic_infra, tmp_path)

    assert diagnostics == []
    assert status.lab == "generic-infra"
    assert status.expected_vms == 3
    assert status.provisioned_vms == 3
    assert [v.state for v in status.vms] == ["provisioned"] * 3
    assert {v.ip for v in status.vms} == {"10.0.10.42", "10.0.10.43", "10.0.10.44"}


def test_query_status_marks_missing_vms(
    resolved_generic_infra, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only docker1 is in tofu state; node1 and router1 are missing.
    payload = json.dumps(
        {"vm_ips": {"value": {"docker1": "10.0.10.43"}}}
    )
    bin_dir = _write_tofu_shim(tmp_path, payload)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    status, diagnostics = query_status(resolved_generic_infra, tmp_path)

    assert diagnostics == []
    assert status.provisioned_vms == 1
    by_name = {v.name: v for v in status.vms}
    assert by_name["node1"].state == "missing"
    assert by_name["node1"].ip is None
    assert by_name["docker1"].state == "provisioned"
    assert by_name["docker1"].ip == "10.0.10.43"
    assert by_name["router1"].state == "missing"


def test_query_status_treats_empty_tofu_state_as_zero_provisioned(
    resolved_generic_infra, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `tofu output -json` on an un-applied module returns `{}`. Status
    # must report that as "nothing provisioned yet", not an error.
    bin_dir = _write_tofu_shim(tmp_path, "{}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    status, diagnostics = query_status(resolved_generic_infra, tmp_path)

    assert diagnostics == []
    assert status.provisioned_vms == 0
    assert all(v.state == "missing" for v in status.vms)
    assert all(v.ip is None for v in status.vms)


def test_query_status_surfaces_real_errors(
    resolved_generic_infra, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PATH is empty -> tofu binary missing -> real error, not benign.
    monkeypatch.setenv("PATH", "")

    status, diagnostics = query_status(resolved_generic_infra, tmp_path)

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_binary_missing"
    # Status is still well-formed even when the query failed — VMs
    # listed as missing.
    assert status.provisioned_vms == 0


def test_query_status_lists_unknown_tofu_domains(
    resolved_generic_infra, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tofu state has a domain the lab doesn't reference (leftover from
    # a former lab or a manual `tofu apply`). Status should still pair
    # the matching ones and surface the unknowns separately.
    payload = json.dumps(
        {
            "vm_ips": {
                "value": {
                    "node1": "10.0.10.42",
                    "ghost-vm": "10.0.10.99",
                }
            }
        }
    )
    bin_dir = _write_tofu_shim(tmp_path, payload)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    status, diagnostics = query_status(resolved_generic_infra, tmp_path)

    assert diagnostics == []
    assert status.provisioned_vms == 1
    assert status.unknown_vms == ["ghost-vm"]


def test_query_status_surfaces_parse_failures(
    resolved_generic_infra, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `tofu output -json` returning malformed JSON is a real error — not
    # benign like tofu_no_state. The diagnostic must propagate to the
    # caller, and the status object must still be well-formed.
    bin_dir = _write_tofu_shim(tmp_path, "this is not json")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    status, diagnostics = query_status(resolved_generic_infra, tmp_path)

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_parse_failed"
    assert status.provisioned_vms == 0
    assert all(v.state == "missing" for v in status.vms)
