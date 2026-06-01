"""Structural guard for BUG-3a: wait_for_lease must only be true for NIC 0.

Multi-NIC VMs (docker1, router1 in generic-infra) timed out during tofu
apply because wait_for_lease = true was set on every NIC in the dynamic
network_interface block. Only the first NIC's DHCP lease is needed for
the vm_ips output — secondary NICs use internal networks with no DHCP or
with static IPs and tofu should not block on them.

This test reads tofu/main.tf as text and verifies the fix is in place.
It is a structural guard; the real behaviour requires a live libvirt apply
to validate (tofu blocks until a DHCP ack arrives on each NIC).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
MAIN_TF = REPO_ROOT / "tofu" / "main.tf"


def test_wait_for_lease_is_conditional_on_first_nic() -> None:
    """main.tf must contain the key==0 guard, not a bare true literal."""
    text = MAIN_TF.read_text()
    assert "wait_for_lease = network_interface.key == 0" in text, (
        "Expected 'wait_for_lease = network_interface.key == 0' in tofu/main.tf; "
        "secondary NICs must not block on a DHCP lease."
    )


def test_wait_for_lease_is_not_unconditionally_true() -> None:
    """main.tf must NOT contain a bare 'wait_for_lease = true'.

    This guards against reverting to the original bug where every NIC in
    the dynamic network_interface block would block tofu apply waiting for
    a DHCP lease, causing multi-NIC VMs to time out.
    """
    text = MAIN_TF.read_text()
    assert "wait_for_lease = true" not in text, (
        "Found 'wait_for_lease = true' in tofu/main.tf; "
        "this causes multi-NIC VMs to time out waiting for DHCP on secondary NICs. "
        "Use 'wait_for_lease = network_interface.key == 0' instead."
    )
