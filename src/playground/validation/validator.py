"""Cross-reference validation over a :class:`LoadedConfig`.

Runs after :func:`playground.config.load_config` and produces a list
of :class:`Diagnostic`. Does NOT resolve role inheritance or expand
command presets — that's the resolver's job. The validator's
contract is "given a typed load, does every name in every reference
point at something that exists?".

Diagnostic IDs (registered in ``ai/architecture/diagnostic_ids.md``):

- ``config.reference.unknown_role``
- ``config.reference.unknown_network``
- ``config.reference.unknown_command``
- ``config.reference.unknown_provider``
- ``config.reference.unknown_image``
- ``config.reference.unknown_network_profile``
- ``config.reference.ansible_role_missing`` (warning; see §11.3)
- ``config.role.inheritance_cycle``
- ``config.role.unknown_extends``
"""

from __future__ import annotations

from pathlib import Path

from playground.config.loader import LoadedConfig
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.kinds import Lab, VmRole

# A repo-relative path string baked into the Diagnostic.source.path.
# The loader populates DiscoveredFile.repo_relative_path but doesn't
# attach it to the parsed model; for v1 we use a synthetic source
# path that points at the directory and metadata.name.


def _source_for(kind: str, name: str) -> SourceLocation:
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
    warning per ``shared_contracts.md §11.3``. When ``None``, the check is
    skipped — this lets unit tests run without a real ansible/ tree.
    """
    diagnostics: list[Diagnostic] = []

    diagnostics.extend(_check_role_inheritance(loaded))
    diagnostics.extend(_check_ansible_roles(loaded, ansible_roles_dir))

    for lab in loaded.labs.values():
        diagnostics.extend(_check_lab(lab, loaded))

    return diagnostics


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
                        source=_source_for("VmRole", role.metadata.name),
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
                        source=_source_for("VmRole", current.metadata.name),
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
                        source=_source_for("VmRole", role.metadata.name),
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
    source = _source_for("Lab", lab.metadata.name)

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
        for net_idx, net_name in enumerate(vm.networks):
            if net_name not in declared_network_names:
                diagnostics.append(
                    Diagnostic(
                        id="config.reference.unknown_network",
                        severity="error",
                        message=(
                            f"VM {vm.name!r} attaches to undeclared network "
                            f"{net_name!r}"
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
                        source=_source_for("VmRole", role_name),
                        key_path="spec.image",
                    )
                )

    return diagnostics


__all__ = ["validate"]
