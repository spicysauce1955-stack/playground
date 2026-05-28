"""Local VirtualBox backend adapter.

Provisions VMs with the ``VBoxManage`` CLI from a base Ubuntu cloud image
(qcow2 → VDI via ``qemu-img``), seeds them with a NoCloud cloud-init ISO,
and reaches them over SSH via per-VM NAT port-forwards. The configure
half (wait-for-vms-ready → ansible → verify) is the same backend-neutral
code the libvirt adapter uses. See ``docs/architecture/CONTRACTS.md`` →
local-vbox.
"""

from playground.backend.local_vbox.runner import (
    execute_apply,
    execute_destroy,
    execute_reset,
)
from playground.backend.local_vbox.status import query_status

__all__ = [
    "execute_apply",
    "execute_destroy",
    "execute_reset",
    "query_status",
]
