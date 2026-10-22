"""VPC + egress-isolation inputs of the DO plan/tfvars, and the tofu root."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.cloud_digitalocean.plan import _vpc_cidr, build_do_plan
from playground.backend.cloud_digitalocean.tfvars import render_do_tfvars
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"
TOFU_DIR = REPO_ROOT / "tofu" / "cloud_digitalocean"
_SSH_KEY = "ssh-ed25519 AAAATESTKEY user@host"


@pytest.fixture
def plan():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return build_do_plan(resolve_lab(loaded, "cloud-smoke"), provider_settings={})


def test_vpc_cidr_comes_from_the_lab_network(plan) -> None:
    assert plan.vpc_cidr == "10.60.0.0/24"
    assert render_do_tfvars(plan, ssh_public_key=_SSH_KEY)["vpc_cidr"] == "10.60.0.0/24"


def test_egress_isolated_defaults_false(plan) -> None:
    assert render_do_tfvars(plan, ssh_public_key=_SSH_KEY)["egress_isolated"] is False


def test_egress_isolated_kwarg_is_rendered(plan) -> None:
    out = render_do_tfvars(plan, ssh_public_key=_SSH_KEY, egress_isolated=True)
    assert out["egress_isolated"] is True


def test_vpc_cidr_is_normalised_to_the_network_address() -> None:
    class _Net:
        cidr = "10.61.0.7/24"

    class _Lab:
        networks = [_Net()]

    assert _vpc_cidr(_Lab()) == "10.61.0.0/24"  # type: ignore[arg-type]


def test_vpc_cidr_empty_without_networks() -> None:
    class _Lab:
        networks: list[object] = []

    assert _vpc_cidr(_Lab()) == ""  # type: ignore[arg-type]


# ---- committed tofu root guards -------------------------------------------


def test_tofu_places_droplets_in_the_lab_vpc() -> None:
    main = (TOFU_DIR / "main.tf").read_text()
    assert 'resource "digitalocean_vpc" "lab"' in main
    assert "vpc_uuid = digitalocean_vpc.lab.id" in main
    assert "ip_range    = var.vpc_cidr" in main


def test_tofu_isolated_egress_targets_droplet_ids_not_the_vpc_cidr() -> None:
    """The VPC resolver (e.g. 10.97.0.254) lives INSIDE the CIDR and forwards
    outside lookups, so isolated egress must use droplet ids."""
    main = (TOFU_DIR / "main.tf").read_text()
    open_ = 'destination_addresses   = var.egress_isolated ? [] : ["0.0.0.0/0", "::/0"]'
    ids = "destination_droplet_ids = var.egress_isolated ? digitalocean_droplet.node[*].id : []"
    assert main.count(open_) == 3
    assert main.count(ids) == 3
    assert "egress_destinations" not in main
    assert "destination_addresses = [var.vpc_cidr]" not in main


def test_tofu_allows_lab_internal_traffic_inbound() -> None:
    main = (TOFU_DIR / "main.tf").read_text()
    assert main.count("source_addresses = [var.vpc_cidr]") == 3


def test_tofu_vm_ips_stays_public_and_private_ips_are_added() -> None:
    out = (TOFU_DIR / "outputs.tf").read_text()
    assert "digitalocean_droplet.node[idx].ipv4_address\n" in out
    assert 'output "vm_private_ips"' in out
    assert "ipv4_address_private" in out


def test_tofu_firewall_targets_droplet_ids_only_not_tags() -> None:
    """Firewall `tags` select targets; var.tags has the account-wide tag."""
    import re

    main = (TOFU_DIR / "main.tf").read_text()
    block = re.search(r'resource "digitalocean_firewall" "lab" \{(.*?)\n\}\n', main, re.S)
    assert block is not None
    body = block.group(1)
    assert "droplet_ids = digitalocean_droplet.node[*].id" in body
    assert not re.search(r"^\s*tags\s*=", body, re.M)
