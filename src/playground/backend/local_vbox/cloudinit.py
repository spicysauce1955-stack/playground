"""Render NoCloud cloud-init for a vbox VM and build its seed ISO.

VirtualBox has no equivalent of libvirt's cloud-init disk wiring, so we
hand cloud-init a **NoCloud** datasource: a tiny ISO labelled ``cidata``
holding ``user-data``, ``meta-data`` and ``network-config``. VirtualBox
attaches it as a second optical drive; cloud-init on the Ubuntu cloud
image finds the ``cidata`` label automatically on first boot.

The ``user-data`` mirrors ``tofu/cloud_init.cfg`` (hostname/fqdn, SSH key
injection for the lab's ssh_user, package update/upgrade, password auth
off) so a lab behaves the same on either backend.

The ``network-config`` (netplan v2) is what makes the NAT + intnet model
work: the NAT NIC is matched by MAC and set to DHCP (VirtualBox's NAT
engine serves it), and every intnet NIC is matched by MAC and given the
static address the planner assigned. Matching by MAC avoids depending on
fragile guest interface names (enp0s3 vs enp0s8).

Render functions are pure (return strings); only :func:`build_seed_iso`
touches the filesystem / shells out to ``genisoimage``.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from playground.backend.local_vbox.plan import VboxVmPlan
from playground.models.diagnostic import Diagnostic, SourceLocation


def _dump_yaml(data: Any) -> str:
    """Dump ``data`` to a YAML string. ruamel's safe dumper sorts mapping
    keys alphabetically — harmless here since neither cloud-config nor
    netplan assigns meaning to key order."""
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()

_ISO_TOOLS = ("genisoimage", "mkisofs", "xorrisofs")
"""ISO authoring tools we know how to drive, in preference order. All
accept ``-output -volid -joliet -rock``."""


def render_user_data(vm: VboxVmPlan) -> str:
    """The cloud-config ``user-data`` body for one VM."""
    payload = {
        "hostname": vm.hostname,
        "fqdn": vm.fqdn,
        "preserve_hostname": False,
        "users": [
            {
                "name": vm.ssh_user,
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "groups": "users, admin",
                "lock_passwd": True,
                "ssh_authorized_keys": [vm.ssh_public_key.strip()],
            }
        ],
        "package_update": True,
        "package_upgrade": True,
        "package_reboot_if_required": False,
        "ssh_pwauth": False,
    }
    body = _dump_yaml(payload)
    return "#cloud-config\n" + body


def render_meta_data(vm: VboxVmPlan) -> str:
    """The NoCloud ``meta-data`` body. ``instance-id`` is stable per VM
    so a re-create of the same VM is treated as the same instance."""
    payload = {
        "instance-id": vm.vbox_name,
        "local-hostname": vm.hostname,
    }
    return _dump_yaml(payload)


def render_network_config(vm: VboxVmPlan) -> str:
    """The netplan-v2 ``network-config`` body.

    Lists *every* NIC (matched by MAC): the NAT NIC as DHCP and each
    intnet NIC with its static address. Listing all of them is required —
    once a NoCloud network-config is present it fully replaces the cloud
    image's default netplan, so an omitted NIC would come up unconfigured.
    """
    ethernets: dict[str, dict] = {}
    for nic in vm.nics:
        iface_key = f"vnic{nic.index}"
        # Match purely by MAC and configure the interface in place — no
        # set-name. set-name renames the device on first boot, which is
        # an extra failure surface we don't need since the MAC match
        # already selects the right NIC.
        entry: dict = {"match": {"macaddress": _colonize(nic.mac)}}
        if nic.kind == "nat":
            entry["dhcp4"] = True
        else:
            entry["dhcp4"] = False
            if nic.static_ip_cidr:
                entry["addresses"] = [nic.static_ip_cidr]
        ethernets[iface_key] = entry

    payload = {"version": 2, "ethernets": ethernets}
    return _dump_yaml(payload)


def needs_network_config(vm: VboxVmPlan) -> bool:
    """Whether this VM needs a custom NoCloud network-config.

    Only when it has intnet NICs requiring static IPs. A NAT-only VM is
    left to the cloud image's default netplan (DHCP on all NICs), which
    VirtualBox's NAT engine serves — the lowest-risk first boot.
    """
    return any(nic.kind == "intnet" for nic in vm.nics)


def build_seed_iso(
    vm: VboxVmPlan, *, out_dir: Path
) -> tuple[Path | None, list[Diagnostic]]:
    """Write the three NoCloud files and pack them into a ``cidata`` ISO.

    Returns ``(iso_path, diagnostics)``. ``iso_path`` is ``None`` (with an
    error diagnostic) when no ISO authoring tool is on PATH or the tool
    fails. The ISO is written to ``out_dir/<vbox_name>-seed.iso``; the
    intermediate text files are kept alongside it for debugging.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / vm.vbox_name
    work.mkdir(parents=True, exist_ok=True)
    (work / "user-data").write_text(render_user_data(vm))
    (work / "meta-data").write_text(render_meta_data(vm))
    iso_inputs = [work / "user-data", work / "meta-data"]
    if needs_network_config(vm):
        (work / "network-config").write_text(render_network_config(vm))
        iso_inputs.append(work / "network-config")

    tool = next((t for t in _ISO_TOOLS if shutil.which(t)), None)
    if tool is None:
        return None, [
            Diagnostic(
                id="runtime.vbox.iso_tool_missing",
                severity="error",
                message=(
                    "no ISO authoring tool found (need one of "
                    f"{', '.join(_ISO_TOOLS)}) to build the cloud-init seed"
                ),
                source=SourceLocation(path="host"),
                suggestion="install genisoimage (apt install genisoimage)",
            )
        ]

    iso_path = out_dir / f"{vm.vbox_name}-seed.iso"
    cmd = [
        tool,
        "-output", str(iso_path),
        "-volid", "cidata",
        "-joliet", "-rock",
        *(str(p) for p in iso_inputs),
    ]
    result = subprocess.run(  # noqa: S603 — explicit args, no shell
        cmd, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None, [
            Diagnostic(
                id="runtime.vbox.iso_build_failed",
                severity="error",
                message=(
                    f"{tool} exited {result.returncode} building the "
                    f"cloud-init seed ISO: {result.stderr.strip()[:300]}"
                ),
                source=SourceLocation(path=str(iso_path)),
            )
        ]
    return iso_path, []


def _colonize(mac12: str) -> str:
    """``080027abcdef`` -> ``08:00:27:ab:cd:ef`` (lowercase)."""
    m = mac12.lower()
    return ":".join(m[i : i + 2] for i in range(0, 12, 2))


__all__ = [
    "build_seed_iso",
    "needs_network_config",
    "render_meta_data",
    "render_network_config",
    "render_user_data",
]
