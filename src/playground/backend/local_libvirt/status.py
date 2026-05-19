"""Query the local-libvirt backend for a lab's observed state.

Read-only. Composes :func:`fetch_vm_ips` (subprocess wrapper around
``tofu output -json``) with the backend-neutral
:class:`~playground.models.status.LabStatus` model.
"""

from __future__ import annotations

from pathlib import Path

from playground.backend.local_libvirt.inventory import (
    TOFU_NO_STATE_DIAGNOSTIC_ID,
    fetch_vm_ips,
)
from playground.models.diagnostic import Diagnostic
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus, VmStatus


def query_status(
    resolved: ResolvedLab,
    tofu_dir: Path,
) -> tuple[LabStatus, list[Diagnostic]]:
    """Build a :class:`LabStatus` for ``resolved`` from tofu state.

    "No tofu state yet" is a valid status (nothing provisioned), not an
    error. Any other failure mode (missing binary, parse failed, command
    failed) is surfaced as a diagnostic.

    Library contract: this function **always** returns a well-formed
    :class:`LabStatus`, even when the underlying query failed — every
    lab VM is reported as ``missing`` in that case. Callers that need
    to gate on diagnostics (e.g. the CLI) check the diagnostic list
    explicitly; the status object is never ``None``.
    """
    vm_ips, fetch_diagnostics = fetch_vm_ips(tofu_dir)

    diagnostics = [
        d for d in fetch_diagnostics if d.id != TOFU_NO_STATE_DIAGNOSTIC_ID
    ]

    lab_vm_names = {vm.name for vm in resolved.vms}
    vms = [
        VmStatus(
            name=vm.name,
            role=vm.role,
            ip=vm_ips.get(vm.name),
            state="provisioned" if vm.name in vm_ips else "missing",
        )
        for vm in resolved.vms
    ]
    provisioned = sum(1 for v in vms if v.state == "provisioned")
    unknown = sorted(name for name in vm_ips if name not in lab_vm_names)

    return (
        LabStatus(
            lab=resolved.lab_name,
            backend=resolved.backend,
            expected_vms=len(resolved.vms),
            provisioned_vms=provisioned,
            vms=vms,
            unknown_vms=unknown,
        ),
        diagnostics,
    )


__all__ = ["query_status"]
