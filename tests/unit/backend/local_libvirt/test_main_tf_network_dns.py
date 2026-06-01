"""Structural guard for BUG-4: every libvirt network must enable DNS.

The dmacvicar/libvirt provider defaults getDNSEnableFromResource to "no"
when no enabled `dns` block is present, emitting <dns enable='no'> and
disabling dnsmasq DNS on the network. Guests then get no upstream resolver
via DHCP (systemd-resolved's 127.0.0.53 stub has nothing to forward to), so
name resolution fails and ansible's `apt update` fails, even though
internet-by-IP works.

The original code only emitted the `dns` block when the lab pinned IPs
(var.vm_dns_hosts non-empty), so labs like generic-infra had DNS disabled.
DNS must be enabled unconditionally.

This is a structural guard on tofu/main.tf; the real behaviour requires a
live libvirt apply to validate.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
MAIN_TF = REPO_ROOT / "tofu" / "main.tf"


def test_dns_block_is_unconditional() -> None:
    """main.tf must declare a plain `dns { enabled = true }` block, NOT one
    gated behind `dynamic "dns"` (which omitted it when no IPs were pinned)."""
    text = MAIN_TF.read_text()
    assert 'dynamic "dns"' not in text, (
        'Found dynamic "dns" in tofu/main.tf; DNS was conditional on pinned '
        "IPs, so labs without pinned IPs (e.g. generic-infra) got DNS disabled. "
        "Declare an unconditional `dns { enabled = true }` block instead."
    )
    # A static `dns {` block with enabled = true on the next non-empty line.
    assert re.search(r"\n  dns \{\n\s*enabled\s*=\s*true", text), (
        "Expected an unconditional `dns { enabled = true }` block on the "
        "libvirt_network in tofu/main.tf so dnsmasq serves DNS to guests."
    )
