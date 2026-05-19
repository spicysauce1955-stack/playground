"""Lower a :class:`LoadedConfig` into a :class:`ResolvedLab`.

Resolution pipeline:

1. Apply Defaults.spec as the base.
2. Layer the Lab's spec.
3. For each VM: walk the role-extension chain, then layer VM overrides.
4. Expand commands.enabled name list → ResolvedCommand bodies.
5. Resolve ArtifactSources for declared images.
6. Apply runtime overrides (no-op for v1; placeholder for v2).
7. Populate source_map.

The resolver does NOT re-run cross-reference checks — the caller is
expected to have run :func:`playground.validation.validate` and gated
on no ``error`` diagnostics.
"""

from __future__ import annotations

from typing import Any

from playground.config.loader import LoadedConfig
from playground.models.kinds import Defaults, Lab, LabVm, VmRoleSpec
from playground.models.resolved import (
    ResolvedArtifactImage,
    ResolvedArtifacts,
    ResolvedCommand,
    ResolvedDefaults,
    ResolvedLab,
    ResolvedNetwork,
    ResolvedVm,
    ResolvedWorkload,
)


def resolve_lab(
    loaded: LoadedConfig,
    lab_name: str,
) -> ResolvedLab:
    """Produce a :class:`ResolvedLab` for ``lab_name`` from ``loaded``.

    Raises ``KeyError`` if the lab name is unknown. Cross-references
    are assumed validated; if a role/network/command/image is missing
    the function will raise ``KeyError`` rather than silently produce
    a broken model.
    """
    lab = loaded.labs[lab_name]
    defaults = loaded.defaults
    if defaults is None:
        raise ValueError("Defaults document is required but was not loaded")

    resolved_defaults = ResolvedDefaults(
        backend=defaults.spec.backend,
        offline=defaults.spec.offline,
        budget=defaults.spec.budget,
        retention=defaults.spec.retention,
    )

    networks = [_resolve_network(net, lab, loaded) for net in lab.spec.networks]
    vms = [_resolve_vm(vm, lab, loaded, defaults) for vm in lab.spec.vms]
    workloads = [_resolve_workload(wl) for wl in lab.spec.workloads]
    commands = [_resolve_command(name, loaded) for name in lab.spec.commands.enabled]
    artifacts = _resolve_artifacts(loaded)
    source_map = _build_source_map(lab, loaded)

    network_profiles = {
        net.profile: loaded.networks[net.profile].spec for net in lab.spec.networks
    }

    return ResolvedLab(
        lab_name=lab.metadata.name,
        description=lab.metadata.description,
        tags=lab.metadata.tags,
        backend=lab.spec.backend,
        offline=lab.spec.offline or defaults.spec.offline,
        budget=lab.spec.budget or defaults.spec.budget,
        # Derive a default DNS domain from the lab name when the lab
        # doesn't override; tofu's libvirt_network resources serve this
        # via dnsmasq so cross-VM resolution works without /etc/hosts.
        dns_domain=lab.spec.dns_domain or f"{lab.metadata.name}.lab",
        defaults=resolved_defaults,
        providers={name: dict(overrides) for name, overrides in lab.spec.providers.items()},
        networks=networks,
        vms=vms,
        workloads=workloads,
        commands=commands,
        artifacts=artifacts,
        network_profiles=network_profiles,
        source_map=source_map,
    )


# ---------------------------------------------------------------------------
# Network / workload / command resolution
# ---------------------------------------------------------------------------


def _resolve_network(net: Any, lab: Lab, loaded: LoadedConfig) -> ResolvedNetwork:
    profile_spec = loaded.networks[net.profile].spec
    return ResolvedNetwork(
        name=net.name,
        intent=profile_spec.intent,
        cidr=net.cidr,
        internet_access=profile_spec.internet_access,
        dns_enabled=profile_spec.dns.enabled,
    )


def _resolve_workload(wl: Any) -> ResolvedWorkload:
    return ResolvedWorkload(
        name=wl.name,
        type=wl.type,
        source=wl.source,
        placement=wl.placement,
        networks=list(wl.networks),
        ports=list(wl.ports),
        volumes=list(wl.volumes),
        environment=dict(wl.environment),
        resources=(
            {
                "vcpu": wl.resources.vcpu,
                "memory_mb": wl.resources.memory_mb,
                "disk_gb": wl.resources.disk_gb,
            }
            if wl.resources is not None
            else None
        ),
        tags=list(wl.tags),
    )


def _resolve_command(name: str, loaded: LoadedConfig) -> ResolvedCommand:
    preset = loaded.commands[name]
    return ResolvedCommand(
        name=preset.metadata.name,
        description=preset.metadata.description,
        target=preset.spec.target,
        shell=preset.spec.command.shell,
        working_directory=preset.spec.working_directory,
        environment=dict(preset.spec.environment),
        timeout_seconds=preset.spec.timeout_seconds,
        escalate=preset.spec.escalation.become,
    )


# ---------------------------------------------------------------------------
# VM resolution with role-inheritance flatten
# ---------------------------------------------------------------------------


def _resolve_vm(
    vm: LabVm,
    lab: Lab,
    loaded: LoadedConfig,
    defaults: Defaults,
) -> ResolvedVm:
    flattened_role = _flatten_role(vm.role, loaded)
    role_chain = _role_ancestry(vm.role, loaded)

    # VM may override resources; otherwise inherit from role, then defaults.
    if vm.resources is not None:
        vcpu, memory_mb, disk_gb = (
            vm.resources.vcpu,
            vm.resources.memory_mb,
            vm.resources.disk_gb,
        )
    elif flattened_role.resources is not None:
        vcpu, memory_mb, disk_gb = (
            flattened_role.resources.vcpu,
            flattened_role.resources.memory_mb,
            flattened_role.resources.disk_gb,
        )
    else:
        vcpu = defaults.spec.vm.resources.vcpu
        memory_mb = defaults.spec.vm.resources.memory_mb
        disk_gb = defaults.spec.vm.resources.disk_gb

    image = flattened_role.image or defaults.spec.vm.image
    ssh = flattened_role.ssh or defaults.spec.vm.ssh

    return ResolvedVm(
        name=vm.name,
        role=vm.role,
        roles=role_chain,
        image=image,
        vcpu=vcpu,
        memory_mb=memory_mb,
        disk_gb=disk_gb,
        networks=[n.name for n in vm.networks],
        network_ips={n.name: n.ip for n in vm.networks if n.ip is not None},
        ssh=ssh,
        provisioners=[{"ansible_role": p.ansible_role} for p in flattened_role.provisioners],
        capabilities=dict(flattened_role.capabilities),
        routing=flattened_role.routing,
        tags=list(vm.tags),
        extra_hosts=list(vm.extra_hosts),
        provider_overrides=dict(vm.provider_overrides),
    )


def _role_ancestry(role_name: str, loaded: LoadedConfig) -> list[str]:
    """Return ``[role_name, parent, grandparent, ...]`` walking ``spec.extends``.

    Pre-validation has already rejected cycles and unknown parents.
    """
    chain: list[str] = []
    current: str | None = role_name
    while current is not None:
        chain.append(current)
        current = loaded.roles[current].spec.extends
    return chain


def _flatten_role(role_name: str, loaded: LoadedConfig) -> VmRoleSpec:
    """Walk the extends-chain from root → leaf and deep-merge specs.

    The validator already rejected cycles and unknown extends targets;
    this function trusts that invariant and raises ``KeyError`` if
    violated.
    """
    chain: list[VmRoleSpec] = []
    seen: set[str] = set()
    current = role_name
    while True:
        if current in seen:
            raise ValueError(f"role inheritance cycle at {current!r}")
        seen.add(current)
        spec = loaded.roles[current].spec
        chain.append(spec)
        if spec.extends is None:
            break
        current = spec.extends

    # Root is at the end of the chain; merge root → leaf.
    accumulator: dict[str, Any] = {}
    for spec in reversed(chain):
        _deep_merge_spec(accumulator, spec)

    accumulator.pop("extends", None)
    return VmRoleSpec(**accumulator)


def _deep_merge_spec(into: dict[str, Any], spec: VmRoleSpec) -> None:
    """Deep-merge role specs: maps recurse, lists replace."""
    incoming = spec.model_dump()
    for key, value in incoming.items():
        if value is None:
            continue
        if key == "capabilities":
            existing = into.setdefault(key, {})
            existing.update(value)
        elif key == "provisioners":
            # list-replace semantics: child wins entirely
            into[key] = list(value)
        elif isinstance(value, dict):
            existing = into.setdefault(key, {})
            if isinstance(existing, dict):
                existing.update(value)
            else:
                into[key] = value
        else:
            into[key] = value


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def _resolve_artifacts(loaded: LoadedConfig) -> ResolvedArtifacts:
    artifacts = loaded.artifacts
    if artifacts is None:
        return ResolvedArtifacts()

    vm_images: dict[str, ResolvedArtifactImage] = {}
    for name, src in artifacts.spec.vm_images.items():
        vm_images[name] = ResolvedArtifactImage(
            type=src.type,
            version=src.version,
            source=src.default_source,
            local_path=src.local_path,
            available_locally=False,
            available_remote=True,
        )

    return ResolvedArtifacts(
        vm_images=vm_images,
        tofu_providers={k: v.model_dump() for k, v in artifacts.spec.tofu_providers.items()},
        ansible_collections={
            k: v.model_dump() for k, v in artifacts.spec.ansible_collections.items()
        },
        docker_images={k: v.model_dump() for k, v in artifacts.spec.docker_images.items()},
    )


def _build_source_map(lab: Lab, loaded: LoadedConfig) -> dict[str, str]:
    """Build a coarse source map for the resolved lab.

    v1 emits a coarse path at the resource level. Full per-key origins
    can be added later without changing the resolved model shape.
    """
    source = loaded.sources.get(("Lab", lab.metadata.name))
    path = source.path if source is not None else f"config/labs/{lab.metadata.name}.yaml"
    return {
        "spec": path,
    }


__all__ = ["resolve_lab"]
