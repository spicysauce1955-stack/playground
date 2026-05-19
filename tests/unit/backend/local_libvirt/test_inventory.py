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


@pytest.fixture
def lab_ips() -> dict[str, str]:
    """IPs keyed by the committed lab's VM names."""
    return {
        "node1": "10.0.10.42",
        "docker1": "10.0.10.43",
        "router1": "10.0.10.44",
    }


# ---------------------------------------------------------------------------
# render_inventory — pure function
# ---------------------------------------------------------------------------


def test_render_inventory_emits_one_host_per_vm(
    resolved_generic_infra, lab_ips: dict[str, str]
) -> None:
    body, diagnostics = render_inventory(resolved_generic_infra, lab_ips)

    assert diagnostics == []
    assert "[playground]" in body
    assert "node1 ansible_host=10.0.10.42 ansible_user=ubuntu pg_role=generic-node" in body
    assert "docker1 ansible_host=10.0.10.43 ansible_user=ubuntu pg_role=docker-host" in body
    assert "router1 ansible_host=10.0.10.44 ansible_user=ubuntu pg_role=router" in body
    assert "pg_lab=generic-infra" in body


def test_render_inventory_emits_pg_workloads_for_scheduled_host(
    resolved_generic_infra, lab_ips: dict[str, str]
) -> None:
    body, diagnostics = render_inventory(resolved_generic_infra, lab_ips)

    assert diagnostics == []
    # demo-compose is scheduled on docker1 (target_role=docker-host).
    # docker1's host line should carry a pg_workloads JSON payload.
    docker_line = next(
        line for line in body.splitlines() if line.startswith("docker1 ")
    )
    assert "pg_workloads=" in docker_line
    assert "demo-compose" in docker_line
    # node1 has no scheduled workloads.
    node_line = next(line for line in body.splitlines() if line.startswith("node1 "))
    assert "pg_workloads=" not in node_line


def test_render_inventory_emits_pg_extra_hosts_when_set(
    resolved_generic_infra, lab_ips: dict[str, str]
) -> None:
    # Pin extra_hosts on docker1 only.
    docker = next(vm for vm in resolved_generic_infra.vms if vm.name == "docker1")
    pinned = docker.model_copy(
        update={"extra_hosts": ["10.0.10.99 db", "10.0.10.100 cache"]}
    )
    others = [vm for vm in resolved_generic_infra.vms if vm.name != "docker1"]
    lab = resolved_generic_infra.model_copy(update={"vms": [*others, pinned]})

    body, diagnostics = render_inventory(lab, lab_ips)

    assert diagnostics == []
    docker_line = next(line for line in body.splitlines() if line.startswith("docker1 "))
    assert "pg_extra_hosts=" in docker_line
    assert "10.0.10.99 db" in docker_line
    # VMs without extra_hosts get no key.
    node_line = next(line for line in body.splitlines() if line.startswith("node1 "))
    assert "pg_extra_hosts" not in node_line


def test_render_inventory_emits_swarm_groups_when_swarm_workload_present(
    resolved_generic_infra, lab_ips: dict[str, str]
) -> None:
    # Flip the committed compose workload to swarm and add a second
    # docker-capable VM so we see both manager and worker groups.
    docker1 = resolved_generic_infra.vms[1]
    docker2 = docker1.model_copy(update={"name": "docker2"})
    new_vms = [resolved_generic_infra.vms[0], docker1, docker2,
               resolved_generic_infra.vms[2]]
    original = resolved_generic_infra.workloads[0]
    swarm_wl = original.model_copy(update={"type": "swarm"})
    lab = resolved_generic_infra.model_copy(
        update={"vms": new_vms, "workloads": [swarm_wl]}
    )
    ips = {**lab_ips, "docker2": "10.0.10.45"}

    body, diagnostics = render_inventory(lab, ips)

    assert diagnostics == []
    # docker1 has pg_swarm_role=manager; docker2 has worker.
    docker1_line = next(line for line in body.splitlines() if line.startswith("docker1 "))
    docker2_line = next(line for line in body.splitlines() if line.startswith("docker2 "))
    assert "pg_swarm_role=manager" in docker1_line
    assert "pg_swarm_role=worker" in docker2_line
    # node1 / router1 lack docker capability → no swarm role attribute.
    node_line = next(line for line in body.splitlines() if line.startswith("node1 "))
    assert "pg_swarm_role" not in node_line
    # Groups present.
    assert "[swarm_manager]\ndocker1" in body
    assert "[swarm_worker]\ndocker2" in body


def test_render_inventory_omits_swarm_groups_when_no_swarm_workload(
    resolved_generic_infra, lab_ips: dict[str, str]
) -> None:
    body, _ = render_inventory(resolved_generic_infra, lab_ips)

    assert "[swarm_manager]" not in body
    assert "[swarm_worker]" not in body


def test_render_inventory_escapes_single_quotes_in_workload_payload(
    resolved_generic_infra, lab_ips: dict[str, str]
) -> None:
    # An env value with an embedded single quote would otherwise break
    # the host_vars line — verify the renderer shell-escapes it.
    original = resolved_generic_infra.workloads[0]
    quoted = original.model_copy(
        update={"environment": {"MSG": "it's fine"}}
    )
    lab = resolved_generic_infra.model_copy(update={"workloads": [quoted]})

    body, diagnostics = render_inventory(lab, lab_ips)

    assert diagnostics == []
    docker_line = next(
        line for line in body.splitlines() if line.startswith("docker1 ")
    )
    # `'\''` is the bash idiom for "close, escape, reopen" — what the
    # renderer must emit to survive the inventory-INI parser.
    assert "it'\\''s fine" in docker_line


def test_render_inventory_emits_per_role_groups(
    resolved_generic_infra, lab_ips: dict[str, str]
) -> None:
    body, _ = render_inventory(resolved_generic_infra, lab_ips)

    # generic-infra: node1=generic-node, docker1=docker-host, router1=router
    # dashes normalized to underscores so group names are valid Ansible.
    assert "[docker_host]\ndocker1" in body
    assert "[generic_node]\nnode1" in body
    assert "[router]\nrouter1" in body


def test_render_inventory_groups_multiple_vms_per_role(resolved_generic_infra) -> None:
    # Add a second generic-node VM to the lab — both should land in the
    # [generic_node] group.
    extra_vm = resolved_generic_infra.vms[0].model_copy(update={"name": "node2"})
    two_nodes = resolved_generic_infra.model_copy(
        update={"vms": [*resolved_generic_infra.vms, extra_vm]}
    )
    ips = {
        "node1": "10.0.10.42",
        "node2": "10.0.10.45",
        "docker1": "10.0.10.43",
        "router1": "10.0.10.44",
    }

    body, _ = render_inventory(two_nodes, ips)

    # Both node1 and node2 listed under [generic_node].
    generic_node_section = body.split("[generic_node]", 1)[1].split("\n[", 1)[0]
    assert "node1" in generic_node_section
    assert "node2" in generic_node_section
    # And they're still in the [playground] group with full host vars.
    assert "node2 ansible_host=10.0.10.45" in body


def test_render_inventory_omits_role_group_when_vm_has_no_ip(
    resolved_generic_infra,
) -> None:
    # Only docker1 has an IP; the [docker_host] group should still appear
    # but [generic_node] and [router] should not.
    body, diagnostics = render_inventory(
        resolved_generic_infra, {"docker1": "10.0.10.43"}
    )

    assert any(d.id == "config.inventory.vm_ip_not_found" for d in diagnostics)
    assert "[docker_host]" in body
    assert "[generic_node]" not in body
    assert "[router]" not in body


def test_render_inventory_preserves_networks(
    resolved_generic_infra, lab_ips: dict[str, str]
) -> None:
    body, _ = render_inventory(resolved_generic_infra, lab_ips)

    assert "pg_networks=edge,lab-private" in body  # docker1
    assert "pg_networks=edge,lab-private,routed-a" in body  # router1


def test_render_inventory_lab_metadata_and_source_pointer(
    resolved_generic_infra, lab_ips: dict[str, str]
) -> None:
    body, _ = render_inventory(resolved_generic_infra, lab_ips)

    assert "# Lab: generic-infra" in body
    assert "# Source: config/labs/generic-infra.yaml" in body
    # New pairing comment reflects name-keyed matching, not order-based.
    assert "lab VM name -> tofu domain name" in body


def test_render_inventory_flags_missing_vm(resolved_generic_infra) -> None:
    # Only docker1 is in the map; node1 and router1 are missing.
    body, diagnostics = render_inventory(resolved_generic_infra, {"docker1": "10.0.10.43"})

    missing = [d for d in diagnostics if d.id == "config.inventory.vm_ip_not_found"]
    assert len(missing) == 2
    assert all(d.severity == "error" for d in missing)
    assert {d.key_path for d in missing} == {"spec.vms[0].name", "spec.vms[2].name"}
    # Best-effort body still includes the VM that did match.
    assert "docker1 ansible_host=10.0.10.43" in body
    assert "node1 ansible_host" not in body
    assert "router1 ansible_host" not in body


def test_render_inventory_suggestion_lists_known_names(
    resolved_generic_infra,
) -> None:
    # When a VM is missing, the diagnostic suggestion should list what *is*
    # available, so the operator can spot a typo without leaving the CLI.
    _, diagnostics = render_inventory(
        resolved_generic_infra,
        {"docker-host-1": "10.0.10.99"},
    )

    for d in diagnostics:
        assert "docker-host-1" in (d.suggestion or "")


def test_render_inventory_handles_empty_map(resolved_generic_infra) -> None:
    body, diagnostics = render_inventory(resolved_generic_infra, {})

    assert len(diagnostics) == 3  # one per lab VM
    assert all(d.id == "config.inventory.vm_ip_not_found" for d in diagnostics)
    # Body still well-formed; no host lines between [playground] and [playground:vars]
    section = body.split("[playground]", 1)[1].split("[playground:vars]", 1)[0]
    assert section.strip() == ""
    assert "pg_lab=generic-infra" in body


def test_render_inventory_zero_vms_zero_ips(resolved_generic_infra) -> None:
    empty = resolved_generic_infra.model_copy(update={"vms": [], "workloads": []})

    body, diagnostics = render_inventory(empty, {})

    assert diagnostics == []
    assert "[playground]" in body
    assert "[playground:vars]" in body
    section = body.split("[playground]", 1)[1].split("[playground:vars]", 1)[0]
    assert section.strip() == ""


def test_render_inventory_ignores_extra_ips_silently(
    resolved_generic_infra, lab_ips: dict[str, str]
) -> None:
    # Tofu may know about VMs the lab doesn't reference (e.g. a former lab's
    # leftover state). The renderer should pair what the lab declares and
    # ignore the rest without complaining.
    extra = {**lab_ips, "ghost-vm": "10.0.10.99"}

    body, diagnostics = render_inventory(resolved_generic_infra, extra)

    assert diagnostics == []
    assert "ghost-vm" not in body


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


def test_fetch_vm_ips_parses_name_keyed_map(tmp_path, monkeypatch) -> None:
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
    bin_dir = _write_fake_tofu(tmp_path, payload)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert diagnostics == []
    assert ips == {
        "node1": "10.0.10.42",
        "docker1": "10.0.10.43",
        "router1": "10.0.10.44",
    }


def test_fetch_vm_ips_rejects_legacy_list_shape(tmp_path, monkeypatch) -> None:
    # Pre-§4b tofu state emitted vm_ips as a tuple. After upgrading
    # tofu/outputs.tf the operator must re-apply; until they do, the
    # renderer must refuse rather than silently produce wrong output.
    payload = json.dumps(
        {
            "vm_ips": {
                "value": ["10.0.10.42", "10.0.10.43"],
            }
        }
    )
    bin_dir = _write_fake_tofu(tmp_path, payload)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == {}
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_parse_failed"
    assert "map of string to string" in diagnostics[0].message


def test_fetch_vm_ips_reports_missing_binary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == {}
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_binary_missing"


def test_fetch_vm_ips_reports_no_state(tmp_path, monkeypatch) -> None:
    bin_dir = _write_fake_tofu(tmp_path, "{}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == {}
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_no_state"


def test_fetch_vm_ips_reports_nonzero_exit(tmp_path, monkeypatch) -> None:
    bin_dir = _write_fake_tofu(tmp_path, "boom", exit_code=2)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == {}
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_command_failed"
    assert "exited 2" in diagnostics[0].message


def test_fetch_vm_ips_reports_parse_failure(tmp_path, monkeypatch) -> None:
    bin_dir = _write_fake_tofu(tmp_path, "not json at all")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == {}
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_parse_failed"


def test_fetch_vm_ips_reports_wrong_value_shape(tmp_path, monkeypatch) -> None:
    payload = json.dumps({"vm_ips": {"value": "not-a-map"}})
    bin_dir = _write_fake_tofu(tmp_path, payload)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == {}
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

    assert ips == {}
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_command_failed"


def test_fetch_vm_ips_reports_subprocess_filenotfound(tmp_path, monkeypatch) -> None:
    bin_dir = _write_fake_tofu(tmp_path, "{}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def _raise(*args, **kwargs):
        raise FileNotFoundError("tofu vanished")

    monkeypatch.setattr("playground.backend.local_libvirt.inventory.subprocess.run", _raise)

    ips, diagnostics = fetch_vm_ips(tmp_path)

    assert ips == {}
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.inventory.tofu_command_failed"
