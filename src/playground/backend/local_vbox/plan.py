"""Translate a :class:`ResolvedLab` into a VirtualBox provisioning plan.

This is the local-vbox analogue of ``local_libvirt/tfvars.py``: a pure,
side-effect-free function that turns the backend-neutral resolved model
into the concrete inputs the VBoxManage layer consumes. No subprocesses,
no I/O — so it is fully unit-testable.

Networking model (see ``docs/architecture/CONTRACTS.md`` → local-vbox):

- **NIC 1 is always NAT** with a host port-forward to guest :22. That is
  the management/SSH plane ansible reaches at ``127.0.0.1:<host_port>``.
  The host port itself is assigned at apply time (it is a host-side
  runtime concern), so it is *not* part of this plan.
- **One additional NIC per lab network** is an "internal network"
  (``intnet``) named ``<lab>-<network>``. VirtualBox internal networks
  have no DHCP, so each NIC gets a **static IP** assigned here and
  written into the VM's cloud-init network-config, matched by MAC
  address. This gives VM-to-VM connectivity on isolated lab networks
  without depending on the host.

MACs are deterministic (derived from the vbox VM name + NIC index under
the VirtualBox OUI ``08:00:27``) so a re-plan is stable and cloud-init's
``match: macaddress`` lines line up with what VBoxManage configures.
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from typing import Literal

from playground.models.resolved import ResolvedLab, ResolvedVm

VBOX_OUI = "080027"
"""VirtualBox's registered OUI. VBoxManage wants MACs as 12 hex
chars with no separators."""

DEFAULT_IMAGE_KEY = "ubuntu-noble"
"""Artifact key used when a VM does not name a specific image."""


@dataclass(frozen=True)
class VboxNic:
    """One virtual NIC on a VM."""

    index: int
    """1-based NIC slot as VBoxManage numbers them (``--nic<index>``)."""
    kind: Literal["nat", "intnet"]
    mac: str
    """12 hex chars, no separators (VBoxManage ``--macaddress`` format)."""
    intnet_name: str | None = None
    """Internal-network name; set iff ``kind == 'intnet'``."""
    static_ip_cidr: str | None = None
    """Static address with prefix, e.g. ``10.20.20.10/24``; set for
    intnet NICs so cloud-init can configure it."""


@dataclass(frozen=True)
class VboxVmPlan:
    """Everything needed to create one VirtualBox VM."""

    vbox_name: str
    """The VirtualBox machine name, namespaced by lab so reset can scrub
    by prefix and labs don't collide: ``<lab>-<vm>``."""
    lab_vm_name: str
    """The lab's VM name — the key used in inventory / vm_ips maps."""
    role: str
    vcpu: int
    memory_mb: int
    disk_gb: int
    ssh_user: str
    ssh_public_key: str
    hostname: str
    fqdn: str
    nics: list[VboxNic]


@dataclass(frozen=True)
class VboxPlan:
    """The full provisioning plan for a lab."""

    lab_name: str
    image_key: str
    image_source: str
    """Upstream URL for the base cloud image (qcow2)."""
    image_cache_qcow2: str
    """Where the downloaded qcow2 is cached (from ArtifactSources)."""
    vms: list[VboxVmPlan]


def build_vbox_plan(
    resolved: ResolvedLab,
    *,
    ssh_public_key: str,
) -> VboxPlan:
    """Build a :class:`VboxPlan` from a resolved lab. Pure.

    ``ssh_public_key`` is the public key body to inject into every VM's
    cloud-init (the runner reads it from disk; keeping it a parameter
    keeps this function pure and testable).
    """
    networks_by_name = {n.name: n for n in resolved.networks}

    # Per-network running counter so auto-assigned IPs are stable and
    # collision-free across VMs sharing a network.
    auto_host_index: dict[str, int] = {}

    vm_plans: list[VboxVmPlan] = []
    for vm in resolved.vms:
        nics: list[VboxNic] = [
            VboxNic(index=1, kind="nat", mac=_mac_for(vm.name, 1)),
        ]
        for net_name in vm.networks:
            nic_index = len(nics) + 1
            net = networks_by_name.get(net_name)
            static = _static_ip_for(
                vm=vm,
                net_name=net_name,
                cidr=net.cidr if net is not None else None,
                auto_host_index=auto_host_index,
            )
            nics.append(
                VboxNic(
                    index=nic_index,
                    kind="intnet",
                    mac=_mac_for(vm.name, nic_index),
                    intnet_name=f"{resolved.lab_name}-{net_name}",
                    static_ip_cidr=static,
                )
            )

        vm_plans.append(
            VboxVmPlan(
                vbox_name=f"{resolved.lab_name}-{vm.name}",
                lab_vm_name=vm.name,
                role=vm.role,
                vcpu=vm.vcpu,
                memory_mb=vm.memory_mb,
                disk_gb=vm.disk_gb,
                ssh_user=vm.ssh.user,
                ssh_public_key=ssh_public_key,
                hostname=vm.name,
                fqdn=f"{vm.name}.{resolved.dns_domain}",
                nics=nics,
            )
        )

    image_key, image_source, image_cache = _resolve_image(resolved)
    return VboxPlan(
        lab_name=resolved.lab_name,
        image_key=image_key,
        image_source=image_source,
        image_cache_qcow2=image_cache,
        vms=vm_plans,
    )


def _resolve_image(resolved: ResolvedLab) -> tuple[str, str, str]:
    """Pick the base image (key, upstream source, local cache path).

    Prefers the artifact the lab's VMs reference; falls back to
    ``ubuntu-noble``. Raises ``KeyError`` if neither the named image nor
    the default is present in ``resolved.artifacts`` — the validator is
    expected to have caught a dangling image reference before here.
    """
    images = resolved.artifacts.vm_images
    # All current labs use a single base image; honor the first VM's
    # image key when set, else the default.
    key = DEFAULT_IMAGE_KEY
    for vm in resolved.vms:
        if vm.image and vm.image in images:
            key = vm.image
            break
    image = images.get(key) or images.get(DEFAULT_IMAGE_KEY)
    if image is None:
        raise KeyError(
            f"no base image {key!r} (or {DEFAULT_IMAGE_KEY!r}) in "
            "resolved artifacts; check config/artifacts/sources.yaml"
        )
    local = image.local_path or (
        f".playground/cache/artifacts/vm-images/{key}/image.qcow2"
    )
    return key, image.source, local


def _mac_for(vm_name: str, nic_index: int) -> str:
    """Deterministic VBox-OUI MAC for ``(vm, nic)``.

    The low 3 bytes come from a hash of the name+index so re-planning is
    stable and the same MAC the planner hands VBoxManage is the one
    cloud-init matches on.
    """
    digest = hashlib.sha256(f"{vm_name}/{nic_index}".encode()).hexdigest()
    return f"{VBOX_OUI}{digest[:6]}"


def _static_ip_for(
    *,
    vm: ResolvedVm,
    net_name: str,
    cidr: str | None,
    auto_host_index: dict[str, int],
) -> str | None:
    """Static address-with-prefix for an intnet NIC, or ``None``.

    Uses the lab's pinned IP when present; otherwise auto-assigns a
    host address starting at ``.10`` within the network CIDR. Returns
    ``None`` when the network has no usable CIDR (the NIC is still
    created for L2 connectivity, just left unconfigured at L3).
    """
    if cidr is None:
        return None
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None
    prefixlen = network.prefixlen

    pinned = vm.network_ips.get(net_name)
    if pinned:
        return f"{pinned}/{prefixlen}"

    # Auto-assign: .10, .11, ... per network. host_index 0 -> .10.
    idx = auto_host_index.get(net_name, 0)
    auto_host_index[net_name] = idx + 1
    host = network.network_address + 10 + idx
    if host not in network:
        return None
    return f"{host}/{prefixlen}"


__all__ = [
    "DEFAULT_IMAGE_KEY",
    "VBOX_OUI",
    "VboxNic",
    "VboxPlan",
    "VboxVmPlan",
    "build_vbox_plan",
]
