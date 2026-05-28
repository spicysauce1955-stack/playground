"""Tests for the local-libvirt tfvars renderer."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.local_libvirt.tfvars import render_tfvars
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_generic_infra():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


def test_render_tfvars_emits_lab_vm_names_in_declaration_order(
    resolved_generic_infra,
) -> None:
    # Order must match lab.spec.vms — tofu's libvirt_domain count.index
    # depends on the list position for disk + cloud-init pairing.
    payload = render_tfvars(resolved_generic_infra)

    assert payload["vm_names"] == ["node1", "docker1", "router1"]


def test_render_tfvars_emits_networks_from_lab(resolved_generic_infra) -> None:
    payload = render_tfvars(resolved_generic_infra)

    assert payload["networks"] == [
        {"name": "edge", "cidr": "10.20.10.0/24"},
        {"name": "lab-private", "cidr": "10.20.20.0/24"},
        {"name": "routed-a", "cidr": "10.20.30.0/24"},
    ]


def test_render_tfvars_emits_vm_networks_per_vm(resolved_generic_infra) -> None:
    payload = render_tfvars(resolved_generic_infra)

    assert payload["vm_networks"] == {
        "node1": ["lab-private"],
        "docker1": ["edge", "lab-private"],
        "router1": ["edge", "lab-private", "routed-a"],
    }


def test_render_tfvars_omits_vm_network_ips_when_no_pins(
    resolved_generic_infra,
) -> None:
    # generic-infra uses the legacy networks: [name, ...] shape — no
    # per-VM IPs pinned anywhere. The renderer should omit the key so
    # tofu's default empty map applies.
    payload = render_tfvars(resolved_generic_infra)

    assert "vm_network_ips" not in payload


def test_render_tfvars_emits_vm_network_ips_when_lab_pins(
    resolved_generic_infra,
) -> None:
    # Mutate the resolved lab to pin IPs on docker1.
    docker = next(vm for vm in resolved_generic_infra.vms if vm.name == "docker1")
    pinned = docker.model_copy(
        update={"network_ips": {"edge": "10.20.10.42", "lab-private": "10.20.20.42"}}
    )
    others = [vm for vm in resolved_generic_infra.vms if vm.name != "docker1"]
    lab = resolved_generic_infra.model_copy(update={"vms": [*others, pinned]})

    payload = render_tfvars(lab)

    assert payload["vm_network_ips"] == {
        "docker1": {"edge": "10.20.10.42", "lab-private": "10.20.20.42"},
    }


def test_render_tfvars_handles_empty_lab(resolved_generic_infra) -> None:
    empty = resolved_generic_infra.model_copy(update={"vms": [], "networks": []})

    # dns_domain is always populated by the resolver, so it surfaces
    # in the tfvars payload even for empty labs.
    assert render_tfvars(empty) == {
        "vm_names": [],
        "dns_domain": "generic-infra.lab",
    }


def test_render_tfvars_emits_dns_domain_default(resolved_generic_infra) -> None:
    payload = render_tfvars(resolved_generic_infra)
    assert payload["dns_domain"] == "generic-infra.lab"


def test_render_tfvars_emits_dns_domain_override(resolved_generic_infra) -> None:
    custom = resolved_generic_infra.model_copy(update={"dns_domain": "demo.internal"})
    payload = render_tfvars(custom)
    assert payload["dns_domain"] == "demo.internal"


def test_render_tfvars_omits_vm_dns_hosts_when_no_pins(
    resolved_generic_infra,
) -> None:
    # No VMs pin IPs → no DNS hosts to register with libvirt.
    payload = render_tfvars(resolved_generic_infra)
    assert "vm_dns_hosts" not in payload


def test_render_tfvars_emits_vm_dns_hosts_per_network(
    resolved_generic_infra,
) -> None:
    # Pin two VMs on the same network — they should both land under
    # the same `vm_dns_hosts[net]` list, with short hostnames.
    docker = next(vm for vm in resolved_generic_infra.vms if vm.name == "docker1")
    node = next(vm for vm in resolved_generic_infra.vms if vm.name == "node1")
    docker_pinned = docker.model_copy(
        update={"network_ips": {"lab-private": "10.20.20.42"}}
    )
    node_pinned = node.model_copy(
        update={"network_ips": {"lab-private": "10.20.20.43"}}
    )
    router = next(vm for vm in resolved_generic_infra.vms if vm.name == "router1")
    lab = resolved_generic_infra.model_copy(
        update={"vms": [node_pinned, docker_pinned, router]}
    )

    payload = render_tfvars(lab)

    assert payload["vm_dns_hosts"] == {
        "lab-private": [
            {"hostname": "node1", "ip": "10.20.20.43"},
            {"hostname": "docker1", "ip": "10.20.20.42"},
        ],
    }


def test_render_tfvars_is_pure_no_diagnostics_returned(
    resolved_generic_infra,
) -> None:
    # The backend-capability warning lives in validator.py now; render_tfvars
    # is a pure data transformer.
    payload = render_tfvars(resolved_generic_infra)

    assert isinstance(payload, dict)
    # vm_names is always emitted; the lab-derived networks/vm_networks
    # may or may not be present depending on the lab. Today's
    # generic-infra produces all three.
    assert "vm_names" in payload


def test_render_tfvars_omits_cpu_mode_when_lab_does_not_override(
    resolved_generic_infra,
) -> None:
    # When the lab's spec.providers.local-libvirt has no `cpu_mode`,
    # tfvars must NOT emit it — the operator's tofu default
    # (host-passthrough) then applies, matching the historic behavior.
    payload = render_tfvars(resolved_generic_infra)

    assert "cpu_mode" not in payload


def test_render_tfvars_emits_cpu_mode_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue 1 (2026-05-28): non-Redroid labs on hosts where VMX
    passthrough crashes guests must be able to opt out via
    `spec.providers.local-libvirt.cpu_mode: host-model`."""
    loaded, _ = load_config(CONFIG_DIR)
    resolved = resolve_lab(loaded, "generic-infra")
    # Inject the override via model_copy since ResolvedLab is frozen.
    new_providers = {**resolved.providers}
    libvirt_overrides = {**new_providers.get("local-libvirt", {}), "cpu_mode": "host-model"}
    new_providers["local-libvirt"] = libvirt_overrides
    resolved = resolved.model_copy(update={"providers": new_providers})

    payload = render_tfvars(resolved)
    assert payload["cpu_mode"] == "host-model"


def _resolved_with_libvirt_overrides(overrides: dict) -> object:
    """Helper: load generic-infra and overlay `spec.providers.local-libvirt`."""
    loaded, _ = load_config(CONFIG_DIR)
    resolved = resolve_lab(loaded, "generic-infra")
    new_providers = {**resolved.providers}
    libvirt_overrides = {**new_providers.get("local-libvirt", {}), **overrides}
    new_providers["local-libvirt"] = libvirt_overrides
    return resolved.model_copy(update={"providers": new_providers})


def test_render_tfvars_omits_cpu_features_disable_when_lab_does_not_set_it(
    resolved_generic_infra,
) -> None:
    # When the lab doesn't set `cpu_features_disable`, tfvars must not emit
    # the key so tofu's default (empty list = no xslt) applies.
    payload = render_tfvars(resolved_generic_infra)
    assert "cpu_features_disable" not in payload


def test_render_tfvars_emits_cpu_features_disable_when_lab_sets_it() -> None:
    """Workaround for `kvm_intel: vmread/vmwrite failed` on nested hosts —
    `spec.providers.local-libvirt.cpu_features_disable: [vmx]` strips the
    flag from the libvirt CPU XML via xslt."""
    resolved = _resolved_with_libvirt_overrides({"cpu_features_disable": ["vmx"]})
    payload = render_tfvars(resolved)
    assert payload["cpu_features_disable"] == ["vmx"]


def test_render_tfvars_omits_cpu_features_disable_when_explicit_empty_list() -> None:
    # An explicit empty list must be treated like "not set" — emitting
    # an empty list would still trigger the dynamic xml block on the
    # tofu side (length check is `> 0` so it's still safe, but
    # belt-and-braces: omit so the payload stays clean).
    resolved = _resolved_with_libvirt_overrides({"cpu_features_disable": []})
    payload = render_tfvars(resolved)
    assert "cpu_features_disable" not in payload


def test_render_tfvars_rejects_non_list_cpu_features_disable() -> None:
    resolved = _resolved_with_libvirt_overrides({"cpu_features_disable": "vmx"})
    with pytest.raises(ValueError, match="cpu_features_disable"):
        render_tfvars(resolved)


def test_render_tfvars_rejects_non_string_entries_in_cpu_features_disable() -> None:
    resolved = _resolved_with_libvirt_overrides(
        {"cpu_features_disable": ["vmx", 42]},
    )
    with pytest.raises(ValueError, match="cpu_features_disable"):
        render_tfvars(resolved)


def test_render_tfvars_rejects_empty_string_in_cpu_features_disable() -> None:
    # An empty CPU flag name makes no sense and would produce an
    # invalid xslt template — surface fast at render time.
    resolved = _resolved_with_libvirt_overrides({"cpu_features_disable": [""]})
    with pytest.raises(ValueError, match="cpu_features_disable"):
        render_tfvars(resolved)


def test_render_tfvars_omits_domain_type_when_lab_does_not_set_it(
    resolved_generic_infra,
) -> None:
    payload = render_tfvars(resolved_generic_infra)
    assert "domain_type" not in payload


def test_render_tfvars_emits_domain_type_kvm() -> None:
    # Even when the operator explicitly sets the default value, emit it
    # — the payload-omit policy elsewhere is "omit when not set", not
    # "omit when value matches default."
    resolved = _resolved_with_libvirt_overrides({"domain_type": "kvm"})
    payload = render_tfvars(resolved)
    assert payload["domain_type"] == "kvm"


def test_render_tfvars_emits_domain_type_qemu() -> None:
    """The TCG fallback — opting into software emulation when L0 won't
    permit nested VMX."""
    resolved = _resolved_with_libvirt_overrides({"domain_type": "qemu"})
    payload = render_tfvars(resolved)
    assert payload["domain_type"] == "qemu"


def test_render_tfvars_rejects_invalid_domain_type() -> None:
    resolved = _resolved_with_libvirt_overrides({"domain_type": "xen"})
    with pytest.raises(ValueError, match="domain_type"):
        render_tfvars(resolved)


def test_render_tfvars_rejects_non_string_domain_type() -> None:
    resolved = _resolved_with_libvirt_overrides({"domain_type": 42})
    with pytest.raises(ValueError, match="domain_type"):
        render_tfvars(resolved)
