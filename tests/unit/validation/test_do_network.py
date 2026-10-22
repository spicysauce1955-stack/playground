"""cloud-digitalocean network checks: one lab network becomes one VPC."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from ruamel.yaml import YAML

from playground.config.loader import LoadedConfig, load_config
from playground.models.kinds import Lab, parse_resource
from playground.validation.validator import validate

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_yaml = YAML(typ="safe")


@pytest.fixture
def committed_load() -> LoadedConfig:
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return loaded


def _do_lab(
    networks_yaml: str,
    vm_net_yaml: str = "[net]",
    backend: str = "cloud-digitalocean",
) -> Lab:
    text = dedent(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: do-net
        spec:
          backend: {backend}
          networks:
        {networks}
          vms:
            - name: vm1
              role: generic-node
              networks: {vm_net}
        """
    ).lstrip("\n").format(backend=backend, networks=networks_yaml, vm_net=vm_net_yaml)
    lab = parse_resource(_yaml.load(text))
    assert isinstance(lab, Lab)
    return lab


def _net(cidr: str, name: str = "net") -> str:
    return f"    - {{name: {name}, profile: nat, cidr: '{cidr}'}}"


def _do_diags(committed_load: LoadedConfig, lab: Lab):
    committed_load.labs[lab.metadata.name] = lab
    return [d for d in validate(committed_load) if d.id.startswith("config.network.do_")]


def test_valid_lab_is_silent(committed_load: LoadedConfig) -> None:
    assert _do_diags(committed_load, _do_lab(_net("10.61.0.0/24"))) == []


def test_requires_exactly_one_network(committed_load: LoadedConfig) -> None:
    lab = _do_lab(_net("10.61.0.0/24") + "\n" + _net("10.62.0.0/24", "net2"))
    matching = _do_diags(committed_load, lab)
    assert [d.id for d in matching] == ["config.network.do_single_network"]
    assert matching[0].severity == "error"


@pytest.mark.parametrize(
    "cidr",
    ["8.8.8.0/24", "10.61.0.0/15", "10.61.0.0/29", "172.32.0.0/24", "not-a-cidr", "fd00::/64"],
)
def test_rejects_bad_cidr(committed_load: LoadedConfig, cidr: str) -> None:
    matching = [
        d
        for d in _do_diags(committed_load, _do_lab(_net(cidr)))
        if d.id == "config.network.do_vpc_cidr_invalid"
    ]
    assert len(matching) == 1
    assert matching[0].severity == "error"
    assert matching[0].key_path == "spec.networks[0].cidr"


@pytest.mark.parametrize("cidr", ["10.0.0.0/16", "172.16.4.0/28", "192.168.1.0/24"])
def test_accepts_rfc1918_prefix_bounds(committed_load: LoadedConfig, cidr: str) -> None:
    assert _do_diags(committed_load, _do_lab(_net(cidr))) == []


def test_warns_on_pinned_ip(committed_load: LoadedConfig) -> None:
    lab = _do_lab(_net("10.61.0.0/24"), "[{name: net, ip: 10.61.0.5}]")
    matching = _do_diags(committed_load, lab)
    assert [d.id for d in matching] == ["config.network.do_ip_pin_ignored"]
    assert matching[0].severity == "warning"


def test_checks_skip_other_backends(committed_load: LoadedConfig) -> None:
    lab = _do_lab(_net("8.8.8.0/24"), backend="local-libvirt")
    assert _do_diags(committed_load, lab) == []
