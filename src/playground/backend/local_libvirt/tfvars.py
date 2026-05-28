"""Render an OpenTofu ``-var-file`` payload from :class:`ResolvedLab`.

Closes the last manual handoff between the lab YAML and the tofu module.
Without this, the operator would have to copy ``lab.spec.vms[*].name``
into ``var.vm_names`` (and now the lab's networks / per-VM IPs into
``var.networks`` / ``var.vm_networks`` / ``var.vm_network_ips``) by hand
for ``playground apply`` to provision the right topology.

What gets emitted:

- ``vm_names`` — declaration-order list of lab VM names.
- ``networks`` — list of ``{name, cidr}`` from ``lab.spec.networks``.
  Tofu creates one ``libvirt_network`` per entry.
- ``vm_networks`` — ``{vm_name: [net_name, ...]}`` derived from
  ``ResolvedVm.networks``. Each network becomes a
  ``network_interface`` on that VM.
- ``vm_network_ips`` — ``{vm_name: {net_name: ip}}`` derived from
  ``ResolvedVm.network_ips``. Pinned IPs become the interface's
  ``addresses``.
- ``dns_domain`` — the lab's DNS domain (always populated;
  resolver defaults to ``<lab_name>.lab``). Becomes each
  ``libvirt_network``'s ``domain`` so dnsmasq serves
  ``<vm>.<dns_domain>`` records.
- ``vm_dns_hosts`` — ``{net_name: [{hostname, ip}, ...]}`` derived
  from every (vm, network) pair where the lab pins an IP. Becomes
  authoritative ``dns { hosts { ... } }`` blocks on the matching
  ``libvirt_network`` so cross-VM hostname resolution works
  without ``/etc/hosts`` entries.

Per-VM resources (``memory_mb``, ``vcpu``, ``disk_gb``) are intentionally
**not** emitted — today's ``tofu/main.tf`` accepts only global
``var.vm_memory`` / ``var.vm_vcpu`` and a hardcoded 20 GB disk. The
``config.backend.per_vm_resources_unsupported`` validator warning fires
when a lab declares heterogeneous per-VM resources that today's tofu
cannot honor.

This module is a pure data transformer — see ``validator.py`` for the
diagnostics surface.
"""

from __future__ import annotations

from typing import Any

from playground.models.resolved import ResolvedLab


def render_tfvars(resolved: ResolvedLab) -> dict[str, Any]:
    """Produce a tofu ``-var-file`` payload (JSON-shaped dict) for ``resolved``.

    Pure function: no I/O, no diagnostics. The caller serializes the
    returned dict with :func:`json.dumps` and writes it under
    ``.playground/state/tofu/``. Backend-capability warnings about
    heterogeneous per-VM resources are emitted by
    :func:`playground.validation.validate` so they also surface under
    ``playground validate``.

    Fields are omitted when empty so the tofu module's defaults apply.
    """
    payload: dict[str, Any] = {
        "vm_names": [vm.name for vm in resolved.vms],
        "dns_domain": resolved.dns_domain,
    }

    if resolved.networks:
        payload["networks"] = [
            {"name": net.name, "cidr": net.cidr} for net in resolved.networks
        ]

    vm_networks = {
        vm.name: list(vm.networks)
        for vm in resolved.vms
        if vm.networks
    }
    if vm_networks:
        payload["vm_networks"] = vm_networks

    vm_network_ips = {
        vm.name: dict(vm.network_ips)
        for vm in resolved.vms
        if vm.network_ips
    }
    if vm_network_ips:
        payload["vm_network_ips"] = vm_network_ips

    vm_dns_hosts: dict[str, list[dict[str, str]]] = {}
    for vm in resolved.vms:
        for net_name, ip in vm.network_ips.items():
            vm_dns_hosts.setdefault(net_name, []).append(
                {"hostname": vm.name, "ip": ip}
            )
    if vm_dns_hosts:
        payload["vm_dns_hosts"] = vm_dns_hosts

    # CPU mode override — lab YAML's `spec.providers.local-libvirt.cpu_mode`.
    # Omitted entirely when unset so tofu's default (`host-passthrough`)
    # applies. Non-Redroid labs running on hosts where VMX passthrough
    # fails can set `cpu_mode: host-model` to avoid the kvm_intel
    # vmread/vmwrite startup crash.
    provider_cfg = resolved.providers.get("local-libvirt", {}) or {}
    cpu_mode = provider_cfg.get("cpu_mode")
    if isinstance(cpu_mode, str) and cpu_mode:
        payload["cpu_mode"] = cpu_mode

    return payload


__all__ = ["render_tfvars"]
