"""Resolve `playground capture` targeting flags to concrete devices.

"Which VMs can be captured?" is answered from resolved role
capabilities: a VM is capturable when its role chain declares
`capture: true` (config/roles/redroid-host.yaml). Same mechanism
`playground.android.targets` uses for `redroid`.

Returns `AndroidTarget` rather than a look-alike of its own so
`playground.android.runner.run_on_targets` fans capture commands out
unchanged -- that runner is typed `list[AndroidTarget]`, and in this
slice every capturable VM is an Android device.
"""

from __future__ import annotations

from playground.android.targets import AndroidTarget
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus

_CAPTURE_BACKENDS = ("local-libvirt", "cloud-digitalocean")
"""`local-vbox` declares `redroid: false` in its ProviderConfig -- Redroid
is unverified there, so there is no device whose traffic could be
captured."""


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

    if resolved.backend not in _CAPTURE_BACKENDS:
        return [], [
            Diagnostic(
                id="config.capture.backend_unsupported",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} uses backend "
                    f"{resolved.backend!r}, which has no Redroid device to "
                    "capture"
                ),
                source=source,
                key_path="spec.backend",
                suggestion=(
                    "capture is available on local-libvirt and "
                    "cloud-digitalocean; local-vbox declares redroid: false"
                ),
            )
        ]

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
                vm_name=name,
                ssh_host=vm_status.ssh_host,
                ssh_port=vm_status.ssh_port or 22,
                ssh_user=user,
            )
        )
    return targets, diagnostics
