"""Tests for the pure cloud-digitalocean provisioning planner."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.cloud_digitalocean.plan import (
    DEFAULT_IMAGE,
    DEFAULT_REGION,
    DEFAULT_SIZE,
    build_do_plan,
    ownership_tags,
)
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_cloud_smoke():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "cloud-smoke")


# ---------------------------------------------------------------------------
# ownership_tags
# ---------------------------------------------------------------------------


def test_ownership_tags_returns_three_tags() -> None:
    tags = ownership_tags("my-lab")
    assert len(tags) == 3


def test_ownership_tags_playground_tag() -> None:
    tags = ownership_tags("my-lab")
    assert "playground" in tags


def test_ownership_tags_lab_namespaced() -> None:
    tags = ownership_tags("my-lab")
    assert "lab:my-lab" in tags


def test_ownership_tags_backend_tag() -> None:
    tags = ownership_tags("my-lab")
    assert "backend:cloud-digitalocean" in tags


def test_ownership_tags_are_lab_specific() -> None:
    tags_a = ownership_tags("lab-a")
    tags_b = ownership_tags("lab-b")
    assert tags_a != tags_b
    assert "lab:lab-a" in tags_a
    assert "lab:lab-b" in tags_b


def test_ownership_tags_only_valid_do_characters() -> None:
    # DigitalOcean tag names: letters, digits, ':', '-', '_'.
    import re
    for tag in ownership_tags("my-test-lab"):
        assert re.fullmatch(r"[A-Za-z0-9:_-]+", tag), (
            f"tag {tag!r} contains characters not allowed by DigitalOcean"
        )


# ---------------------------------------------------------------------------
# build_do_plan — defaults
# ---------------------------------------------------------------------------


def test_build_do_plan_uses_module_defaults_when_settings_empty(
    resolved_cloud_smoke,
) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    assert plan.region == DEFAULT_REGION
    assert plan.size == DEFAULT_SIZE
    assert plan.image == DEFAULT_IMAGE


def test_build_do_plan_lab_name_matches_resolved(resolved_cloud_smoke) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    assert plan.lab_name == "cloud-smoke"


def test_build_do_plan_name_prefix_equals_lab_name(resolved_cloud_smoke) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    assert plan.name_prefix == plan.lab_name


def test_build_do_plan_dns_domain_from_resolved(resolved_cloud_smoke) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    assert plan.dns_domain == resolved_cloud_smoke.dns_domain
    assert plan.dns_domain  # non-empty


# ---------------------------------------------------------------------------
# build_do_plan — provider_settings overrides
# ---------------------------------------------------------------------------


def test_build_do_plan_honors_region_override(resolved_cloud_smoke) -> None:
    plan = build_do_plan(
        resolved_cloud_smoke,
        provider_settings={"region": "sfo3"},
    )
    assert plan.region == "sfo3"


def test_build_do_plan_honors_size_override(resolved_cloud_smoke) -> None:
    plan = build_do_plan(
        resolved_cloud_smoke,
        provider_settings={"size": "s-2vcpu-4gb"},
    )
    assert plan.size == "s-2vcpu-4gb"


def test_build_do_plan_honors_image_override(resolved_cloud_smoke) -> None:
    plan = build_do_plan(
        resolved_cloud_smoke,
        provider_settings={"image": "ubuntu-22-04-x64"},
    )
    assert plan.image == "ubuntu-22-04-x64"


def test_build_do_plan_falsy_region_falls_back_to_default(
    resolved_cloud_smoke,
) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={"region": ""})
    assert plan.region == DEFAULT_REGION


def test_build_do_plan_none_size_falls_back_to_default(resolved_cloud_smoke) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={"size": None})
    assert plan.size == DEFAULT_SIZE


# ---------------------------------------------------------------------------
# build_do_plan — vm_names
# ---------------------------------------------------------------------------


def test_build_do_plan_vm_names_from_resolved_vms(resolved_cloud_smoke) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    expected = [vm.name for vm in resolved_cloud_smoke.vms]
    assert plan.vm_names == expected


def test_build_do_plan_vm_names_in_declaration_order(resolved_cloud_smoke) -> None:
    # cloud-smoke has exactly one VM; use model_copy to add a second and
    # verify order is preserved.
    vm0 = resolved_cloud_smoke.vms[0]
    vm1 = vm0.model_copy(update={"name": "node2"})
    lab = resolved_cloud_smoke.model_copy(update={"vms": [vm0, vm1]})
    plan = build_do_plan(lab, provider_settings={})
    assert plan.vm_names == ["node1", "node2"]


def test_build_do_plan_vm_count_property(resolved_cloud_smoke) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    assert plan.vm_count == len(resolved_cloud_smoke.vms)


# ---------------------------------------------------------------------------
# build_do_plan — ssh_key_fingerprints
# ---------------------------------------------------------------------------


def test_build_do_plan_ssh_key_fingerprints_empty_by_default(
    resolved_cloud_smoke,
) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    assert plan.ssh_key_fingerprints == []


def test_build_do_plan_ssh_key_fingerprints_from_settings(
    resolved_cloud_smoke,
) -> None:
    fps = ["aa:bb:cc:dd:ee:ff:00:11"]
    plan = build_do_plan(
        resolved_cloud_smoke,
        provider_settings={"ssh_key_fingerprints": fps},
    )
    assert plan.ssh_key_fingerprints == fps


# ---------------------------------------------------------------------------
# build_do_plan — firewall_ssh_cidrs
# ---------------------------------------------------------------------------


def test_build_do_plan_firewall_ssh_cidrs_empty_by_default(
    resolved_cloud_smoke,
) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    assert plan.firewall_ssh_cidrs == []


def test_build_do_plan_firewall_ssh_cidrs_from_nested_firewall_key(
    resolved_cloud_smoke,
) -> None:
    plan = build_do_plan(
        resolved_cloud_smoke,
        provider_settings={"firewall": {"ssh_cidrs": ["203.0.113.0/24"]}},
    )
    assert plan.firewall_ssh_cidrs == ["203.0.113.0/24"]


def test_build_do_plan_firewall_ssh_cidrs_flat_fallback(
    resolved_cloud_smoke,
) -> None:
    plan = build_do_plan(
        resolved_cloud_smoke,
        provider_settings={"firewall_ssh_cidrs": ["10.0.0.0/8"]},
    )
    assert plan.firewall_ssh_cidrs == ["10.0.0.0/8"]


def test_build_do_plan_nested_firewall_wins_over_flat(resolved_cloud_smoke) -> None:
    plan = build_do_plan(
        resolved_cloud_smoke,
        provider_settings={
            "firewall": {"ssh_cidrs": ["203.0.113.0/24"]},
            "firewall_ssh_cidrs": ["10.0.0.0/8"],
        },
    )
    assert plan.firewall_ssh_cidrs == ["203.0.113.0/24"]


def test_build_do_plan_empty_firewall_block_yields_empty_cidrs(
    resolved_cloud_smoke,
) -> None:
    plan = build_do_plan(
        resolved_cloud_smoke,
        provider_settings={"firewall": {"ssh_cidrs": []}},
    )
    assert plan.firewall_ssh_cidrs == []


# ---------------------------------------------------------------------------
# build_do_plan — tags
# ---------------------------------------------------------------------------


def test_build_do_plan_tags_are_ownership_tags(resolved_cloud_smoke) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    assert plan.tags == ownership_tags(resolved_cloud_smoke.lab_name)


def test_build_do_plan_tags_contain_lab_name_tag(resolved_cloud_smoke) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    assert f"lab:{resolved_cloud_smoke.lab_name}" in plan.tags


# ---------------------------------------------------------------------------
# DoPlan — frozen dataclass invariants
# ---------------------------------------------------------------------------


def test_do_plan_is_frozen(resolved_cloud_smoke) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    with pytest.raises((AttributeError, TypeError)):
        plan.region = "ams3"  # type: ignore[misc]


def test_do_plan_vm_count_matches_vm_names_length(resolved_cloud_smoke) -> None:
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    assert plan.vm_count == len(plan.vm_names)
