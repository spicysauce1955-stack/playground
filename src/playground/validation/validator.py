"""Cross-reference validation over a :class:`LoadedConfig`.

Runs after :func:`playground.config.load_config` and produces a list
of :class:`Diagnostic`. Does NOT resolve role inheritance or expand
command presets — that's the resolver's job. The validator's
contract is "given a typed load, does every name in every reference
point at something that exists?".

Diagnostic IDs:

- ``config.required.defaults_missing``
- ``config.reference.unknown_role``
- ``config.reference.unknown_network``
- ``config.reference.unknown_command``
- ``config.reference.unknown_provider``
- ``config.reference.unknown_image``
- ``config.reference.unknown_network_profile``
- ``config.reference.unknown_workload_target``
- ``config.reference.ansible_role_missing`` (warning; see §11.3)
- ``config.role.inheritance_cycle``
- ``config.role.unknown_extends``
- ``config.budget.exceeded``
- ``config.artifact.offline_missing`` (VM images only in §3 — other artifact
  classes from ``requirements.md`` §5.13 are tracked for a later slice)
- ``config.backend.per_vm_resources_unsupported`` (warning; today's
  ``local-libvirt`` backend applies global ``var.vm_memory`` / ``var.vm_vcpu``)
- ``config.network.ip_not_in_cidr`` (per-VM pinned IP outside the lab
  network's CIDR)
- ``config.network.duplicate_ip`` (two VMs pin the same IP on the same
  network)
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Literal

from playground.config.loader import LoadedConfig
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.kinds import Lab, LabVm, Resources, VmRole


def _source_for(loaded: LoadedConfig, kind: str, name: str) -> SourceLocation:
    if source := loaded.sources.get((kind, name)):
        return source
    dir_name = {
        "Lab": "labs",
        "VmRole": "roles",
        "ProviderConfig": "providers",
        "NetworkProfile": "networks",
        "CommandPreset": "commands",
        "ArtifactSources": "artifacts",
        "Defaults": "",
    }.get(kind, "")
    if dir_name:
        return SourceLocation(path=str(Path("config") / dir_name / f"{name}.yaml"))
    if kind == "Defaults":
        return SourceLocation(path="config/defaults.yaml")
    if kind == "ArtifactSources":
        return SourceLocation(path="config/artifacts/sources.yaml")
    return SourceLocation(path="config/")


def validate(loaded: LoadedConfig, ansible_roles_dir: Path | None = None) -> list[Diagnostic]:
    """Run every cross-reference check and return collected diagnostics.

    ``ansible_roles_dir`` enables the ``config.reference.ansible_role_missing``
    warning. When ``None``, the check is
    skipped — this lets unit tests run without a real ansible/ tree.
    """
    diagnostics: list[Diagnostic] = []

    diagnostics.extend(_check_required_singletons(loaded))
    diagnostics.extend(_check_role_inheritance(loaded))
    diagnostics.extend(_check_ansible_roles(loaded, ansible_roles_dir))

    for lab in loaded.labs.values():
        diagnostics.extend(_check_lab(lab, loaded))

    return diagnostics


def _check_required_singletons(loaded: LoadedConfig) -> list[Diagnostic]:
    if loaded.defaults is not None:
        return []

    return [
        Diagnostic(
            id="config.required.defaults_missing",
            severity="error",
            message="Defaults document is required before labs can be resolved",
            source=SourceLocation(path="config/defaults.yaml"),
            suggestion="add a Defaults document under config/defaults.yaml",
        )
    ]


# ---------------------------------------------------------------------------
# Role-graph checks (independent of any lab)
# ---------------------------------------------------------------------------


def _check_role_inheritance(loaded: LoadedConfig) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for role in loaded.roles.values():
        seen: list[str] = []
        current: VmRole | None = role
        while current is not None:
            if current.metadata.name in seen:
                cycle = " -> ".join([*seen, current.metadata.name])
                diagnostics.append(
                    Diagnostic(
                        id="config.role.inheritance_cycle",
                        severity="error",
                        message=f"VmRole inheritance cycle: {cycle}",
                        source=_source_for(loaded, "VmRole", role.metadata.name),
                        key_path="spec.extends",
                    )
                )
                break
            seen.append(current.metadata.name)
            parent_name = current.spec.extends
            if parent_name is None:
                break
            parent = loaded.roles.get(parent_name)
            if parent is None:
                diagnostics.append(
                    Diagnostic(
                        id="config.role.unknown_extends",
                        severity="error",
                        message=(
                            f"VmRole {current.metadata.name!r} extends unknown "
                            f"role {parent_name!r}"
                        ),
                        source=_source_for(loaded, "VmRole", current.metadata.name),
                        key_path="spec.extends",
                        suggestion=(
                            f"add a VmRole named {parent_name!r} under "
                            "config/roles/ or fix the typo"
                        ),
                    )
                )
                break
            current = parent
    return diagnostics


def _check_ansible_roles(
    loaded: LoadedConfig, ansible_roles_dir: Path | None
) -> list[Diagnostic]:
    if ansible_roles_dir is None:
        return []
    diagnostics: list[Diagnostic] = []
    for role in loaded.roles.values():
        for provisioner in role.spec.provisioners:
            ansible_role = provisioner.ansible_role
            candidate = ansible_roles_dir / ansible_role
            if not candidate.is_dir():
                diagnostics.append(
                    Diagnostic(
                        id="config.reference.ansible_role_missing",
                        severity="warning",
                        message=(
                            f"VmRole {role.metadata.name!r} references ansible "
                            f"role {ansible_role!r} which is not present at "
                            f"{candidate}"
                        ),
                        source=_source_for(loaded, "VmRole", role.metadata.name),
                        key_path="spec.provisioners",
                        suggestion=(
                            "implement the ansible role under "
                            f"{ansible_roles_dir / ansible_role}/tasks/main.yml, "
                            "or remove the provisioner from the role preset"
                        ),
                    )
                )
    return diagnostics


# ---------------------------------------------------------------------------
# Per-lab checks
# ---------------------------------------------------------------------------


def _check_lab(lab: Lab, loaded: LoadedConfig) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    source = _source_for(loaded, "Lab", lab.metadata.name)

    if lab.spec.backend not in loaded.providers:
        diagnostics.append(
            Diagnostic(
                id="config.reference.unknown_provider",
                severity="error",
                message=(
                    f"Lab {lab.metadata.name!r} declares backend "
                    f"{lab.spec.backend!r} which has no ProviderConfig"
                ),
                source=source,
                key_path="spec.backend",
                suggestion=(
                    f"add config/providers/{lab.spec.backend}.yaml or "
                    "fix the backend name"
                ),
            )
        )

    declared_network_names = {n.name for n in lab.spec.networks}

    for idx, net in enumerate(lab.spec.networks):
        if net.profile not in loaded.networks:
            diagnostics.append(
                Diagnostic(
                    id="config.reference.unknown_network_profile",
                    severity="error",
                    message=(
                        f"network {net.name!r} uses profile {net.profile!r} "
                        "which is not defined"
                    ),
                    source=source,
                    key_path=f"spec.networks[{idx}].profile",
                )
            )

    for idx, vm in enumerate(lab.spec.vms):
        if vm.role not in loaded.roles:
            diagnostics.append(
                Diagnostic(
                    id="config.reference.unknown_role",
                    severity="error",
                    message=f"VM {vm.name!r} references unknown role {vm.role!r}",
                    source=source,
                    key_path=f"spec.vms[{idx}].role",
                    suggestion=(
                        f"add config/roles/{vm.role}.yaml or fix the role name"
                    ),
                )
            )
        for net_idx, vm_net in enumerate(vm.networks):
            if vm_net.name not in declared_network_names:
                diagnostics.append(
                    Diagnostic(
                        id="config.reference.unknown_network",
                        severity="error",
                        message=(
                            f"VM {vm.name!r} attaches to undeclared network "
                            f"{vm_net.name!r}"
                        ),
                        source=source,
                        key_path=f"spec.vms[{idx}].networks[{net_idx}]",
                    )
                )

    for idx, name in enumerate(lab.spec.commands.enabled):
        if name not in loaded.commands:
            diagnostics.append(
                Diagnostic(
                    id="config.reference.unknown_command",
                    severity="error",
                    message=f"enabled command preset {name!r} is not defined",
                    source=source,
                    key_path=f"spec.commands.enabled[{idx}]",
                )
            )

    for idx, wl in enumerate(lab.spec.workloads):
        diagnostics.extend(_check_workload_placement(lab, idx, source, loaded))
        for net_idx, net_name in enumerate(wl.networks):
            if net_name not in declared_network_names:
                diagnostics.append(
                    Diagnostic(
                        id="config.reference.unknown_network",
                        severity="error",
                        message=(
                            f"workload {wl.name!r} attaches to undeclared "
                            f"network {net_name!r}"
                        ),
                        source=source,
                        key_path=f"spec.workloads[{idx}].networks[{net_idx}]",
                    )
                )

    if loaded.artifacts is not None:
        known_images = set(loaded.artifacts.spec.vm_images)
        for role_name, role in loaded.roles.items():
            image = role.spec.image
            if image is not None and image not in known_images:
                diagnostics.append(
                    Diagnostic(
                        id="config.reference.unknown_image",
                        severity="error",
                        message=(
                            f"VmRole {role_name!r} references unknown VM image "
                            f"{image!r}"
                        ),
                        source=_source_for(loaded, "VmRole", role_name),
                        key_path="spec.image",
                    )
                )

    diagnostics.extend(_check_budget(lab, loaded, source))
    diagnostics.extend(_check_offline_artifacts(lab, loaded, source))
    diagnostics.extend(_check_backend_capability(lab, loaded, source))
    diagnostics.extend(_check_network_ips(lab, source))

    return diagnostics


def _check_network_ips(lab: Lab, source: SourceLocation) -> list[Diagnostic]:
    """Validate per-VM IP pins against the lab's network CIDRs.

    Two checks:

    - ``config.network.ip_not_in_cidr`` — the pinned IP must fall inside
      the corresponding ``LabNetwork.cidr``. Skipped silently when the
      VM references a network the lab doesn't declare; the existing
      ``config.reference.unknown_network`` check already covers that.
    - ``config.network.duplicate_ip`` — two VMs in the same lab can't
      pin the same IP on the same network.
    """
    diagnostics: list[Diagnostic] = []
    cidrs: dict[str, ipaddress.IPv4Network | ipaddress.IPv6Network] = {}
    for net in lab.spec.networks:
        try:
            cidrs[net.name] = ipaddress.ip_network(net.cidr, strict=False)
        except ValueError:
            # Malformed CIDR — surfaced by pydantic field constraints
            # elsewhere; not this check's concern.
            continue

    seen: dict[tuple[str, str], str] = {}  # (network, ip) -> first VM name
    for vm_idx, vm in enumerate(lab.spec.vms):
        for net_idx, vm_net in enumerate(vm.networks):
            if vm_net.ip is None:
                continue
            try:
                addr = ipaddress.ip_address(vm_net.ip)
            except ValueError:
                diagnostics.append(
                    Diagnostic(
                        id="config.network.ip_not_in_cidr",
                        severity="error",
                        message=(
                            f"VM {vm.name!r} pins network {vm_net.name!r} "
                            f"to {vm_net.ip!r}, which is not a valid IP"
                        ),
                        source=source,
                        key_path=f"spec.vms[{vm_idx}].networks[{net_idx}].ip",
                    )
                )
                continue
            cidr = cidrs.get(vm_net.name)
            if cidr is not None and addr not in cidr:
                diagnostics.append(
                    Diagnostic(
                        id="config.network.ip_not_in_cidr",
                        severity="error",
                        message=(
                            f"VM {vm.name!r} pins network {vm_net.name!r} to "
                            f"{vm_net.ip!r}, which is outside the network's "
                            f"CIDR ({cidr})"
                        ),
                        source=source,
                        key_path=f"spec.vms[{vm_idx}].networks[{net_idx}].ip",
                        suggestion=f"pick an address inside {cidr}",
                    )
                )
                continue
            key = (vm_net.name, vm_net.ip)
            if key in seen:
                diagnostics.append(
                    Diagnostic(
                        id="config.network.duplicate_ip",
                        severity="error",
                        message=(
                            f"VM {vm.name!r} pins network {vm_net.name!r} to "
                            f"{vm_net.ip!r}, but VM {seen[key]!r} already "
                            "pins the same IP"
                        ),
                        source=source,
                        key_path=f"spec.vms[{vm_idx}].networks[{net_idx}].ip",
                    )
                )
                continue
            seen[key] = vm.name
    return diagnostics


def _check_workload_placement(
    lab: Lab,
    workload_idx: int,
    source: SourceLocation,
    loaded: LoadedConfig,
) -> list[Diagnostic]:
    workload = lab.spec.workloads[workload_idx]
    placement = workload.placement
    vm_names = {vm.name for vm in lab.spec.vms}
    # target_role matches any role in a VM's extends chain — a workload
    # asking for `generic-node` must accept a `docker-host` VM that
    # extends it. _role_ancestors handles unknown / cyclic chains by
    # returning what it could reach; unknown_extends / inheritance_cycle
    # diagnostics surface those separately.
    vm_roles: set[str] = set()
    for vm in lab.spec.vms:
        vm_roles.update(_role_ancestors(loaded, vm.role))
    vm_tags = {tag for vm in lab.spec.vms for tag in vm.tags}

    target_name: str | None = None
    key_path: str | None = None
    target_kind: str | None = None

    if placement.target_role is not None:
        if placement.target_role in vm_roles:
            return []
        target_name = placement.target_role
        target_kind = "role"
        key_path = f"spec.workloads[{workload_idx}].placement.target_role"
    elif placement.target_vm is not None:
        if placement.target_vm in vm_names:
            return []
        target_name = placement.target_vm
        target_kind = "VM"
        key_path = f"spec.workloads[{workload_idx}].placement.target_vm"
    elif placement.target_tag is not None:
        if placement.target_tag in vm_tags:
            return []
        target_name = placement.target_tag
        target_kind = "tag"
        key_path = f"spec.workloads[{workload_idx}].placement.target_tag"
    else:
        return []

    return [
        Diagnostic(
            id="config.reference.unknown_workload_target",
            severity="error",
            message=(
                f"workload {workload.name!r} targets {target_kind} "
                f"{target_name!r}, but no VM in lab {lab.metadata.name!r} matches it"
            ),
            source=source,
            key_path=key_path,
            suggestion="update the workload placement or add a matching VM to the lab",
        )
    ]


def _check_budget(
    lab: Lab,
    loaded: LoadedConfig,
    source: SourceLocation,
) -> list[Diagnostic]:
    budget = lab.spec.budget or (loaded.defaults.spec.budget if loaded.defaults else None)
    if budget is None:
        return []

    vm_resources = [
        resources
        for vm in lab.spec.vms
        if (resources := _resources_for_vm(loaded, vm)) is not None
    ]
    totals = {
        "vms": len(lab.spec.vms),
        "vcpu": sum(resources.vcpu for resources in vm_resources),
        "memory_mb": sum(resources.memory_mb for resources in vm_resources),
        "disk_gb": sum(resources.disk_gb for resources in vm_resources),
        "containers": len(lab.spec.workloads),
    }
    limits = {
        "vms": budget.max_vms,
        "vcpu": budget.max_vcpu,
        "memory_mb": budget.max_memory_mb,
        "disk_gb": budget.max_disk_gb,
        "containers": budget.max_containers,
    }
    exceeded = [
        f"{name} {totals[name]} > {limits[name]}"
        for name in totals
        if totals[name] > limits[name]
    ]
    if not exceeded:
        return []

    severity: Literal["error", "warning"] = (
        "error" if budget.mode == "strict" else "warning"
    )
    return [
        Diagnostic(
            id="config.budget.exceeded",
            severity=severity,
            message=f"Lab {lab.metadata.name!r} exceeds budget: {', '.join(exceeded)}",
            source=source,
            key_path="spec.budget",
            suggestion="increase the lab budget or reduce VM/workload counts and sizes",
        )
    ]


def _check_offline_artifacts(
    lab: Lab,
    loaded: LoadedConfig,
    source: SourceLocation,
) -> list[Diagnostic]:
    offline = lab.spec.offline or (
        loaded.defaults is not None and loaded.defaults.spec.offline
    )
    if not offline:
        return []

    diagnostics: list[Diagnostic] = []
    vm_images = loaded.artifacts.spec.vm_images if loaded.artifacts is not None else {}

    for idx, vm in enumerate(lab.spec.vms):
        image_name = _image_for_vm(loaded, vm)
        if image_name is None:
            continue

        artifact = vm_images.get(image_name)
        if artifact is None:
            diagnostics.append(
                Diagnostic(
                    id="config.artifact.offline_missing",
                    severity="error",
                    message=(
                        f"lab {lab.metadata.name!r} runs offline but VM "
                        f"{vm.name!r} needs image {image_name!r} which is not "
                        "declared in ArtifactSources.spec.vm_images"
                    ),
                    source=source,
                    key_path=f"spec.vms[{idx}]",
                    suggestion=(
                        f"add spec.vm_images.{image_name} with a local_path in "
                        "config/artifacts/sources.yaml"
                    ),
                )
            )
            continue

        if not artifact.local_path:
            diagnostics.append(
                Diagnostic(
                    id="config.artifact.offline_missing",
                    severity="error",
                    message=(
                        f"lab {lab.metadata.name!r} runs offline but image "
                        f"{image_name!r} (used by VM {vm.name!r}) has no "
                        "local_path under ArtifactSources.spec.vm_images"
                    ),
                    source=_source_for(loaded, "ArtifactSources", "sources"),
                    key_path=f"spec.vm_images.{image_name}.local_path",
                    suggestion=(
                        f"set spec.vm_images.{image_name}.local_path in "
                        "config/artifacts/sources.yaml so offline labs can "
                        "find the image without network access"
                    ),
                )
            )

    return diagnostics


def _image_for_vm(loaded: LoadedConfig, vm: LabVm) -> str | None:
    # First-non-None-image-wins walk along ``spec.extends``. Must agree
    # with how the resolver's _flatten_role decides the image; if that
    # ever switches to a different merge rule (explicit null sentinels,
    # list-replace, etc.) this walker has to track it.
    for role_name in _role_ancestors(loaded, vm.role):
        role = loaded.roles.get(role_name)
        if role is not None and role.spec.image is not None:
            return role.spec.image

    if loaded.defaults is None:
        return None
    return loaded.defaults.spec.vm.image


def _role_ancestors(loaded: LoadedConfig, role_name: str) -> list[str]:
    """Return ``role_name`` plus every ancestor reachable via ``spec.extends``.

    Order is leaf -> root. Unknown or cyclic ancestors are clipped silently
    — :func:`_check_role_inheritance` reports those separately.
    """
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = role_name
    while current is not None and current not in seen:
        chain.append(current)
        seen.add(current)
        role = loaded.roles.get(current)
        if role is None:
            break
        current = role.spec.extends
    return chain


def _check_backend_capability(
    lab: Lab,
    loaded: LoadedConfig,
    source: SourceLocation,
) -> list[Diagnostic]:
    """Warn when the lab declares intent the chosen backend can't honor.

    Today only ``local-libvirt`` is checked: ``tofu/main.tf`` applies a
    single global ``var.vm_memory`` / ``var.vm_vcpu`` to every domain, so a
    lab with heterogeneous per-VM resources will not be reproduced
    accurately on apply. Permissive per engineering principle #10 — warn,
    don't block.
    """
    if lab.spec.backend != "local-libvirt":
        return []

    resources_per_vm = [_resources_for_vm(loaded, vm) for vm in lab.spec.vms]
    populated = [r for r in resources_per_vm if r is not None]
    if len(populated) < 2:
        return []

    first = (populated[0].vcpu, populated[0].memory_mb, populated[0].disk_gb)
    if all((r.vcpu, r.memory_mb, r.disk_gb) == first for r in populated[1:]):
        return []

    return [
        Diagnostic(
            id="config.backend.per_vm_resources_unsupported",
            severity="warning",
            message=(
                f"lab {lab.metadata.name!r} declares heterogeneous per-VM "
                "resources, but the local-libvirt backend applies global "
                "var.vm_memory/var.vm_vcpu uniformly. Per-VM resources will "
                "not be honored until tofu is enriched."
            ),
            source=source,
            key_path="spec.vms[*].resources",
            suggestion=(
                "tune var.vm_memory and var.vm_vcpu in tofu/terraform.tfvars "
                "to fit the largest VM, or wait for tofu support for per-VM "
                "resources"
            ),
        )
    ]


def _resources_for_vm(loaded: LoadedConfig, vm: LabVm) -> Resources | None:
    if vm.resources is not None:
        return vm.resources

    role_resources = _resources_for_role(loaded, vm.role)
    if role_resources is not None:
        return role_resources

    if loaded.defaults is None:
        return None
    return loaded.defaults.spec.vm.resources


def _resources_for_role(loaded: LoadedConfig, role_name: str) -> Resources | None:
    # Mirrors _image_for_vm's first-non-None walk along spec.extends.
    # Must agree with the resolver's _flatten_role resource merge rule.
    for ancestor in _role_ancestors(loaded, role_name):
        role = loaded.roles.get(ancestor)
        if role is not None and role.spec.resources is not None:
            return role.spec.resources
    return None


__all__ = ["validate"]
