"""Resolve `playground capture` targeting flags to concrete devices.

"Which VMs can be captured?" is answered from resolved role
capabilities: a VM is capturable when its role chain declares
`capture: true` (config/roles/redroid-host.yaml). Same mechanism
`playground.android.targets` uses for `redroid`.

Capturability is a property of the TARGET, not of `spec.backend` -- an
earlier version of this module refused every target in a lab whose
backend wasn't `local-libvirt`/`cloud-digitalocean`, which would also
have wrongly refused a physical handset attached to a `local-vbox` lab
(vbox only declares `redroid: false`; it says nothing about a handset
plugged into the host running it). So backend plays no part here: each
candidate is checked for its OWN capture capability instead. Physical
devices are the one population that's still unconditionally excluded in
this slice -- `config.capture.device_unsupported` says so plainly, so
the verb fails honestly rather than silently capturing nothing.

Returns `AndroidTarget` rather than a look-alike of its own so
`playground.android.runner.run_on_targets` fans capture commands out
unchanged -- that runner is typed `list[AndroidTarget]`, and in this
slice every capturable target is a Redroid VM (`GuestAdb`).
"""

from __future__ import annotations

from playground.android.targets import AndroidTarget
from playground.android.transport import GuestAdb
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus

__all__ = ["capture_vm_names", "resolve_capture_targets"]


def capture_vm_names(resolved: ResolvedLab) -> list[str]:
    """VMs whose role declares the `capture` capability, in lab order."""
    return [vm.name for vm in resolved.vms if vm.capabilities.get("capture")]


def resolve_capture_targets(
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

    if not resolved.capture.enabled:
        return [], [
            Diagnostic(
                id="config.capture.disabled",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} sets spec.capture.enabled: "
                    "false, so no capture tooling was installed"
                ),
                source=source,
                key_path="spec.capture.enabled",
                suggestion=(
                    "set spec.capture.enabled: true and re-run "
                    f"`playground apply {resolved.lab_name}`"
                ),
            )
        ]

    capturable = capture_vm_names(resolved)
    device_names = [device.name for device in resolved.devices]

    # An explicitly-named device gets its own diagnostic ahead of every
    # other check -- it IS a recognized target, it is simply not a
    # capturable one yet, and that is a different fact from "no capture
    # VM exists in this lab" or "no VM by that name exists".
    if on is not None and on in device_names:
        return [], [
            Diagnostic(
                id="config.capture.device_unsupported",
                severity="error",
                message=(
                    f"device {on!r} in lab {resolved.lab_name!r} cannot be "
                    "captured -- capture from a physical handset ships in "
                    "Plan 2"
                ),
                source=source,
                key_path="spec.devices",
            )
        ]

    if not capturable:
        return [], [
            Diagnostic(
                id="config.capture.no_capture_vms",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} has no VM with the `capture` "
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
        if on not in capturable:
            return [], [
                Diagnostic(
                    id="config.capture.unknown_vm",
                    severity="error",
                    message=(
                        f"VM {on!r} is not a capturable device in lab "
                        f"{resolved.lab_name!r} (devices: {capturable})"
                    ),
                    source=source,
                    key_path="spec.vms",
                )
            ]
        wanted = [on]
    elif role is not None:
        wanted = [
            vm.name for vm in resolved.vms
            if role in vm.roles and vm.capabilities.get("capture")
        ]
        if not wanted:
            return [], [
                Diagnostic(
                    id="config.capture.unknown_vm",
                    severity="error",
                    message=(
                        f"no capturable device in lab {resolved.lab_name!r} "
                        f"has role {role!r}"
                    ),
                    source=source,
                )
            ]
    elif all_devices or len(capturable) == 1:
        wanted = capturable
    else:
        return [], [
            Diagnostic(
                id="config.capture.target_required",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} has {len(capturable)} "
                    "capturable devices; pass --on, --role, or --all"
                ),
                source=source,
                suggestion=f"devices: {', '.join(capturable)}",
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
                    id="config.capture.vm_not_reachable",
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
                name=name,
                transport=GuestAdb(
                    ssh_host=vm_status.ssh_host,
                    ssh_port=vm_status.ssh_port or 22,
                    ssh_user=user,
                ),
            )
        )
    return targets, diagnostics
