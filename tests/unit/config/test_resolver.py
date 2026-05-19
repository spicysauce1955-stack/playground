"""Tests for the resolver: LoadedConfig → ResolvedLab."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_generic_infra():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


def test_resolves_lab_metadata(resolved_generic_infra) -> None:
    lab = resolved_generic_infra
    assert lab.lab_name == "generic-infra"
    assert lab.backend == "local-libvirt"
    assert lab.offline is False
    assert lab.budget.mode == "permissive"


def test_resolves_networks_with_intent_from_profile(resolved_generic_infra) -> None:
    by_name = {n.name: n for n in resolved_generic_infra.networks}
    assert by_name["edge"].intent == "nat"
    assert by_name["edge"].internet_access is True
    assert by_name["lab-private"].intent == "isolated"
    assert by_name["lab-private"].internet_access is False
    assert by_name["routed-a"].intent == "routed"
    assert by_name["routed-a"].internet_access == "configurable"


def test_resolves_vms_with_role_inheritance(resolved_generic_infra) -> None:
    by_name = {v.name: v for v in resolved_generic_infra.vms}

    # node1: pure generic-node, no explicit resources → role's resources
    # (1/2048/20) per generic-node.yaml.
    node = by_name["node1"]
    assert node.role == "generic-node"
    assert node.image == "ubuntu-noble"
    assert node.vcpu == 1
    assert node.memory_mb == 2048
    assert node.disk_gb == 20
    assert node.ssh.user == "ubuntu"
    assert node.provisioners == []
    assert node.capabilities == {}
    assert node.routing is None  # role doesn't declare routing

    # docker1: explicit per-VM resources override role resources.
    # capabilities inherited from docker-host.
    docker = by_name["docker1"]
    assert docker.role == "docker-host"
    assert docker.vcpu == 2
    assert docker.memory_mb == 4096
    assert docker.disk_gb == 40
    assert docker.capabilities == {"docker": True, "compose": True, "swarm": True}
    assert docker.provisioners == [{"ansible_role": "docker"}]

    # router1: no per-VM resources → inherits from extends chain
    # router → generic-node, so resources come from generic-node.
    router = by_name["router1"]
    assert router.role == "router"
    assert router.vcpu == 1
    assert router.memory_mb == 2048
    assert router.capabilities == {"routing": True}
    assert router.provisioners == [{"ansible_role": "router"}]
    assert router.routing is not None
    assert router.routing.mode == "automatic"
    assert router.routing.allow_overrides is True


def test_resolves_workloads(resolved_generic_infra) -> None:
    assert len(resolved_generic_infra.workloads) == 1
    wl = resolved_generic_infra.workloads[0]
    assert wl.name == "demo-compose"
    assert wl.type == "compose"
    assert wl.placement.target_role == "docker-host"
    assert wl.networks == ["lab-private"]


def test_expands_commands_into_full_bodies(resolved_generic_infra) -> None:
    names = [c.name for c in resolved_generic_infra.commands]
    assert names == ["check-docker", "ping-network"]
    by_name = {c.name: c for c in resolved_generic_infra.commands}
    assert by_name["check-docker"].target.role == "docker-host"
    assert by_name["check-docker"].timeout_seconds == 30
    assert by_name["ping-network"].target.any is True
    assert by_name["ping-network"].timeout_seconds == 60


def test_resolves_artifacts(resolved_generic_infra) -> None:
    images = resolved_generic_infra.artifacts.vm_images
    assert "ubuntu-noble" in images
    assert images["ubuntu-noble"].version == "24.04"
    assert images["ubuntu-noble"].source.startswith("https://")
    assert images["ubuntu-noble"].local_path is not None
    assert "dmacvicar-libvirt" in resolved_generic_infra.artifacts.tofu_providers


def test_network_profiles_indexed_by_name(resolved_generic_infra) -> None:
    profiles = resolved_generic_infra.network_profiles
    assert set(profiles) == {"nat", "isolated", "routed"}
    assert profiles["nat"].internet_access is True


def test_source_map_seeded(resolved_generic_infra) -> None:
    assert "spec" in resolved_generic_infra.source_map
    assert resolved_generic_infra.source_map["spec"].endswith("generic-infra.yaml")


def test_resolver_propagates_network_ips_and_extra_hosts(tmp_path) -> None:
    """A lab using the new per-VM-network IP + extra_hosts shape
    should land both on ResolvedVm."""
    from textwrap import dedent

    config_dir = tmp_path / "config"
    # Mirror the committed tree just enough for the resolver.
    for sub in ("artifacts", "commands", "labs", "networks", "providers", "roles"):
        (config_dir / sub).mkdir(parents=True, exist_ok=True)
    # Reuse the committed config for everything except the lab itself.
    import shutil as _shutil
    for sub in ("artifacts", "commands", "networks", "providers", "roles"):
        for f in (CONFIG_DIR / sub).iterdir():
            _shutil.copy(f, config_dir / sub / f.name)
    _shutil.copy(CONFIG_DIR / "defaults.yaml", config_dir / "defaults.yaml")
    (config_dir / "labs" / "static-ip.yaml").write_text(
        dedent(
            """
            apiVersion: playground/v1
            kind: Lab
            metadata:
              name: static-ip
            spec:
              backend: local-libvirt
              networks:
                - name: deploy-net
                  profile: isolated
                  cidr: 10.20.40.0/24
              vms:
                - name: vm-a
                  role: generic-node
                  networks:
                    - name: deploy-net
                      ip: 10.20.40.20
                  extra_hosts:
                    - "10.20.40.21 vm-b"
                - name: vm-b
                  role: generic-node
                  networks:
                    - name: deploy-net
                      ip: 10.20.40.21
                  extra_hosts:
                    - "10.20.40.20 vm-a"
            """
        ).lstrip("\n")
    )

    loaded, diagnostics = load_config(config_dir)
    assert diagnostics == []
    resolved = resolve_lab(loaded, "static-ip")

    by_name = {vm.name: vm for vm in resolved.vms}
    assert by_name["vm-a"].networks == ["deploy-net"]
    assert by_name["vm-a"].network_ips == {"deploy-net": "10.20.40.20"}
    assert by_name["vm-a"].extra_hosts == ["10.20.40.21 vm-b"]
    assert by_name["vm-b"].network_ips == {"deploy-net": "10.20.40.21"}
    assert by_name["vm-b"].extra_hosts == ["10.20.40.20 vm-a"]


def test_resolver_legacy_networks_have_empty_network_ips(
    resolved_generic_infra,
) -> None:
    """The committed `generic-infra` lab uses the legacy `networks: [name, ...]`
    shape — every VM gets an empty network_ips map (no IPs pinned)."""
    for vm in resolved_generic_infra.vms:
        assert vm.network_ips == {}
        assert vm.extra_hosts == []


def test_resolver_dns_domain_defaults_to_lab_name(resolved_generic_infra) -> None:
    """A lab without an explicit `spec.dns_domain` should default to
    ``<lab_name>.lab``."""
    assert resolved_generic_infra.dns_domain == "generic-infra.lab"


def test_resolver_dns_domain_respects_override(tmp_path) -> None:
    from textwrap import dedent

    config_dir = tmp_path / "config"
    for sub in ("artifacts", "commands", "labs", "networks", "providers", "roles"):
        (config_dir / sub).mkdir(parents=True, exist_ok=True)
    import shutil as _shutil
    for sub in ("artifacts", "commands", "networks", "providers", "roles"):
        for f in (CONFIG_DIR / sub).iterdir():
            _shutil.copy(f, config_dir / sub / f.name)
    _shutil.copy(CONFIG_DIR / "defaults.yaml", config_dir / "defaults.yaml")
    (config_dir / "labs" / "custom-dns.yaml").write_text(
        dedent(
            """
            apiVersion: playground/v1
            kind: Lab
            metadata:
              name: custom-dns
            spec:
              backend: local-libvirt
              dns_domain: demo.internal
              networks:
                - name: net-a
                  profile: isolated
                  cidr: 10.20.40.0/24
              vms:
                - name: vm-a
                  role: generic-node
                  networks: [net-a]
            """
        ).lstrip("\n")
    )

    loaded, diagnostics = load_config(config_dir)
    assert diagnostics == []
    resolved = resolve_lab(loaded, "custom-dns")
    assert resolved.dns_domain == "demo.internal"


def test_unknown_lab_raises_keyerror() -> None:
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    with pytest.raises(KeyError):
        resolve_lab(loaded, "never-defined")
