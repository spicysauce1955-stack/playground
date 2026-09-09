"""Resolve `playground app` targeting flags to concrete Android devices.

"Which VMs are Android?" is answered from resolved role capabilities:
a VM is a device when its role chain declares `redroid: true`. That key
became load-bearing with the cloud-redroid work
(config/providers/*.yaml, src/playground/validation/validator.py).

Physical handsets (``resolved.devices``, populated by `spec.devices`) are
the second Android target population. They carry no `redroid` capability
-- there is no VM role involved -- so they are appended to the redroid VM
list wholesale rather than filtered. A handset never needs an SSH tunnel:
its serial (from `DeviceStatus`, the live adopt/probe result) is handed
straight to a `LocalAdb` transport, and `android.runner` already knows how
to drive that transport as well as `GuestAdb`.
"""

from __future__ import annotations

from playground.android.transport import AndroidTarget, GuestAdb, LocalAdb
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import DeviceStatus, LabStatus

__all__ = [
    "AndroidTarget",
    "android_vm_names",
    "device_status_diagnostic",
    "resolve_targets",
]


def android_vm_names(resolved: ResolvedLab) -> list[str]:
    """VMs whose role declares the `redroid` capability, in lab order."""
    return [vm.name for vm in resolved.vms if vm.capabilities.get("redroid")]


def device_status_diagnostic(
    name: str,
    device_status: DeviceStatus,
    source: SourceLocation,
    *,
    lab: str | None = None,
) -> Diagnostic:
    """Explain why an attached-but-unusable device slot isn't a target.

    `unauthorized` gets its own id naming the RSA-prompt remedy -- it is
    the most common real-world failure and "not reachable" would send an
    operator hunting for a network problem that doesn't exist.

    `rebind_candidates` (a bound serial that is gone while a DIFFERENT
    attached handset matches the slot's selector -- the swapped-cable
    case) gets `device.rebind_required`, because "device is not attached"
    would be flatly untrue with a matching phone on the bus. The
    suggestion names `playground apply --rebind` rather than a bare
    `--rebind`: that flag lives on `apply`, which is the one verb allowed
    to retarget a slot, and naming a flag the current verb does not
    accept is exactly the dead-end this diagnostic exists to avoid.

    Every other non-`connected` state (and a `connected` state missing
    its serial, which should not happen but is not trusted blindly) falls
    back to the generic `device.not_connected`.
    """
    if device_status.rebind_candidates:
        found = ", ".join(device_status.rebind_candidates)
        lab_name = lab or "<lab>"
        return Diagnostic(
            id="device.rebind_required",
            severity="error",
            message=(
                f"device {name!r} is bound to serial {device_status.serial!r}, "
                f"which is not attached, but a different handset matching its "
                f"selector is ({found})"
            ),
            source=source,
            suggestion=(
                f"run `playground apply {lab_name} --rebind` to point this slot "
                "at the attached handset; playground never retargets silently"
            ),
        )
    if device_status.state == "unauthorized":
        return Diagnostic(
            id="device.unauthorized",
            severity="error",
            message=(
                f"device {name!r} (serial {device_status.serial}) has not "
                "accepted this machine's adb key"
            ),
            source=source,
            suggestion="tap Allow on the handset's RSA prompt, then re-run",
        )
    return Diagnostic(
        id="device.not_connected",
        severity="error",
        message=f"device {name!r} is not attached",
        source=source,
        suggestion="plug the handset in and re-run `playground apply`",
    )


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
    vm_names = android_vm_names(resolved)
    device_names = [device.name for device in resolved.devices]
    android = vm_names + device_names

    if not android:
        return [], [
            Diagnostic(
                id="config.app.no_android_vms",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} has no VM with the `redroid` "
                    "capability and no device slot"
                ),
                source=source,
                suggestion=(
                    "give a VM the `redroid-host` role, add a device slot, "
                    "or run against a lab that has one"
                ),
            )
        ]

    if on is not None:
        if on not in android:
            if device_names:
                return [], [
                    Diagnostic(
                        id="config.device.unknown_device",
                        severity="error",
                        message=(
                            f"{on!r} is not an Android VM or device in lab "
                            f"{resolved.lab_name!r} (VMs: {vm_names}, "
                            f"devices: {device_names})"
                        ),
                        source=source,
                        key_path="spec.vms",
                    )
                ]
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
        # Device slots have `tags` but no `role` -- `--role` selects
        # among Android VMs only. A device is never silently dropped by
        # this: it has no `role` to match in the first place, so it is
        # simply not a candidate `--role` could ever have picked.
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
    devices_by_name = {device.name: device for device in status.devices}
    device_name_set = set(device_names)
    targets: list[AndroidTarget] = []
    diagnostics: list[Diagnostic] = []
    for name in wanted:
        if name in device_name_set:
            # Membership is checked against the RESOLVED device slots, not
            # `status.devices` -- a slot with no status entry at all (never
            # adopted/probed) is a device that must fail loudly, not one
            # that falls through and gets misreported as an unreachable VM.
            device_status = devices_by_name.get(name) or DeviceStatus(
                name=name, state="missing"
            )
            if device_status.state != "connected" or not device_status.serial:
                diagnostics.append(
                    device_status_diagnostic(
                        name, device_status, source, lab=resolved.lab_name
                    )
                )
                continue
            targets.append(
                AndroidTarget(name=name, transport=LocalAdb(device_status.serial))
            )
            continue

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
                name=name,
                transport=GuestAdb(
                    ssh_host=vm_status.ssh_host,
                    ssh_port=vm_status.ssh_port or 22,
                    ssh_user=user,
                ),
            )
        )
    return targets, diagnostics
