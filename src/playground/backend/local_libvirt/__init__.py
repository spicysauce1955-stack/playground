"""Local libvirt backend adapter.

Today's surface is read-side: render tofu ``-var-file`` inputs and an
Ansible inventory from a :class:`ResolvedLab` plus ``tofu output -json``.
Future slices will add plan/apply/destroy wrappers that consume the same
model.
"""

from playground.backend.local_libvirt.apply import (
    run_ansible_playbook,
    run_tofu_apply,
    run_tofu_destroy,
    tail_log,
)
from playground.backend.local_libvirt.inventory import (
    fetch_vm_ips,
    render_inventory,
)
from playground.backend.local_libvirt.status import query_status
from playground.backend.local_libvirt.tfvars import render_tfvars

__all__ = [
    "fetch_vm_ips",
    "query_status",
    "render_inventory",
    "render_tfvars",
    "run_ansible_playbook",
    "run_tofu_apply",
    "run_tofu_destroy",
    "tail_log",
]
