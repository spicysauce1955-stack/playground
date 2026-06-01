"""Query the local-vbox backend for a lab's observed state.

Read-only, like the libvirt status query, but the source of truth is
``VBoxManage list vms`` / ``list runningvms`` rather than tofu state. A
lab VM named ``<vm>`` maps to a VirtualBox machine named ``<lab>-<vm>``;
its observed state is ``running`` (powered on), ``provisioned``
(registered but off), or ``missing`` (not registered).
"""

from __future__ import annotations

from playground.backend.local_vbox.vbox import (
    list_running_vms,
    list_vms,
    nat_ssh_port,
    vboxmanage_available,
)
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus, VmStatus


def query_status(resolved: ResolvedLab) -> tuple[LabStatus, list[Diagnostic]]:
    """Build a :class:`LabStatus` from VirtualBox's registered VMs.

    Always returns a well-formed status (every VM ``missing`` when
    VBoxManage is absent), with diagnostics surfacing the absence.
    """
    diagnostics: list[Diagnostic] = []
    registered: set[str] = set()
    running: set[str] = set()

    if not vboxmanage_available():
        diagnostics.append(
            Diagnostic(
                id="runtime.status.vboxmanage_missing",
                severity="error",
                message="`VBoxManage` not found on PATH; cannot read vbox state",
                source=SourceLocation(path="host"),
                suggestion="install VirtualBox (apt install virtualbox)",
            )
        )
    else:
        registered = set(list_vms())
        running = set(list_running_vms())

    lab = resolved.lab_name
    vms: list[VmStatus] = []
    for vm in resolved.vms:
        vbox_name = f"{lab}-{vm.name}"
        if vbox_name in running:
            state = "running"
        elif vbox_name in registered:
            state = "provisioned"
        else:
            state = "missing"
        if state in ("running", "provisioned"):
            port = nat_ssh_port(vbox_name)
            ssh_host: str | None = "127.0.0.1"
            ssh_port: int | None = port
        else:
            ssh_host = None
            ssh_port = None
        vms.append(
            VmStatus(
                name=vm.name,
                role=vm.role,
                ip=None,
                state=state,  # type: ignore[arg-type]
                ssh_host=ssh_host,
                ssh_port=ssh_port,
            )
        )

    present = sum(1 for v in vms if v.state in ("running", "provisioned"))
    declared = {f"{lab}-{vm.name}" for vm in resolved.vms}
    unknown = sorted(
        name[len(lab) + 1:]
        for name in registered
        if name.startswith(f"{lab}-") and name not in declared
    )

    return (
        LabStatus(
            lab=lab,
            backend=resolved.backend,
            expected_vms=len(resolved.vms),
            provisioned_vms=present,
            vms=vms,
            unknown_vms=unknown,
        ),
        diagnostics,
    )


__all__ = ["query_status"]
