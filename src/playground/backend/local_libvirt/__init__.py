"""Local libvirt backend adapter.

Today's surface is read-side: render an Ansible inventory from a
``ResolvedLab`` and the output of ``tofu output -json``. Future slices
will add plan/apply/destroy wrappers that consume the same model.
"""

from playground.backend.local_libvirt.inventory import (
    fetch_vm_ips,
    render_inventory,
)

__all__ = ["fetch_vm_ips", "render_inventory"]
