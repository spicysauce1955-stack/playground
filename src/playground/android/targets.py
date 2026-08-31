"""Resolve `playground app` targeting flags to concrete Android devices.

"Which VMs are Android?" is answered from resolved role capabilities:
a VM is a device when its role chain declares `redroid: true`. That key
became load-bearing with the cloud-redroid work
(config/providers/*.yaml, src/playground/validation/validator.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus


@dataclass(frozen=True)
class AndroidTarget:
    """One reachable Android device."""

    vm_name: str
    ssh_host: str
    ssh_port: int
    ssh_user: str


def android_vm_names(resolved: ResolvedLab) -> list[str]:
    """VMs whose role declares the `redroid` capability, in lab order."""
    return [vm.name for vm in resolved.vms if vm.capabilities.get("redroid")]


def resolve_targets(
    resolved: ResolvedLab,
    status: LabStatus,
    *,
    on: str | None,
    role: str | None,
    all_devices: bool,
    user: str,
) -> tuple[list[AndroidTarget], list[Diagnostic]]:
    """Turn targeting flags into devices, or explain why none matched."""
    source = SourceLocation(path=f"config/labs/{resolved.lab_name}.yaml")
    android = android_vm_names(resolved)

    if not android:
        return [], [
            Diagnostic(
                id="config.app.no_android_vms",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} has no VM with the `redroid` "
                    "capability"
                ),
                source=source,
                suggestion=(
                    "give a VM the `redroid-host` role, or run against a lab "
                    "that has one"
                ),
            )
        ]

    if on is not None:
        if on not in android:
            return [], [
                Diagnostic(
                    id="config.app.unknown_vm",
                    severity="error",
                    message=(
                        f"VM {on!r} is not an Android device in lab "
                        f"{resolved.lab_name!r} (devices: {android})"
                    ),
                    source=source,
                    key_path="spec.vms",
                )
            ]
        wanted = [on]
    elif role is not None:
        wanted = [
            vm.name for vm in resolved.vms
            if role in vm.roles and vm.capabilities.get("redroid")
        ]
        if not wanted:
            return [], [
                Diagnostic(
                    id="config.app.unknown_vm",
                    severity="error",
                    message=(
                        f"no Android device in lab {resolved.lab_name!r} has "
                        f"role {role!r}"
                    ),
                    source=source,
                )
            ]
    elif all_devices:
        wanted = android
    elif len(android) == 1:
        wanted = android
    else:
        return [], [
            Diagnostic(
                id="config.app.target_required",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} has {len(android)} Android "
                    "devices; pass --on, --role, or --all"
                ),
                source=source,
                suggestion=f"devices: {', '.join(android)}",
            )
        ]

    by_name = {vm.name: vm for vm in status.vms}
    targets: list[AndroidTarget] = []
    diagnostics: list[Diagnostic] = []
    for name in wanted:
        vm_status = by_name.get(name)
        if vm_status is None or not vm_status.ssh_host:
            diagnostics.append(
                Diagnostic(
                    id="config.app.vm_not_reachable",
                    severity="error",
                    message=(
                        f"VM {name!r} has no reachable SSH endpoint — has the "
                        "lab been applied?"
                    ),
                    source=source,
                    suggestion=f"run `playground apply {resolved.lab_name}` first",
                )
            )
            continue
        targets.append(
            AndroidTarget(
                vm_name=name,
                ssh_host=vm_status.ssh_host,
                ssh_port=vm_status.ssh_port or 22,
                ssh_user=user,
            )
        )
    return targets, diagnostics
