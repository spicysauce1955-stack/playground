"""Load a config tree into typed kind models.

The loader produces a :class:`LoadedConfig` collection plus a list of
:class:`Diagnostic`. It does NOT perform cross-reference checks (that's
the validator's job) and does NOT resolve presets into a ``ResolvedLab``
(that's the resolver's job).

Diagnostic IDs emitted here:

- ``config.yaml.parse_failed``
- ``config.schema.kind_missing``
- ``config.schema.kind_mismatch``
- ``config.schema.unknown_kind``
- ``config.schema.validation_failed``
- ``config.identity.duplicate_name``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from playground.config.discovery import DiscoveredFile, discover_config_files
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.kinds import (
    KNOWN_KINDS,
    ArtifactSources,
    CommandPreset,
    Defaults,
    Lab,
    NetworkProfile,
    ProviderConfig,
    VmRole,
    parse_resource,
)

T = TypeVar("T")


@dataclass
class LoadedConfig:
    """All resources parsed from a config tree, grouped by kind.

    Each map is keyed by ``metadata.name``. The Defaults singleton is
    stored under the empty-string key when present.
    """

    defaults: Defaults | None = None
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    artifacts: ArtifactSources | None = None
    networks: dict[str, NetworkProfile] = field(default_factory=dict)
    roles: dict[str, VmRole] = field(default_factory=dict)
    commands: dict[str, CommandPreset] = field(default_factory=dict)
    labs: dict[str, Lab] = field(default_factory=dict)


def load_config(config_dir: Path) -> tuple[LoadedConfig, list[Diagnostic]]:
    """Walk ``config_dir`` and produce a typed :class:`LoadedConfig`.

    Errors are returned as Diagnostics rather than raised; an unparseable
    file does not abort the load — subsequent files are still parsed.
    """
    yaml = YAML(typ="safe")
    loaded = LoadedConfig()
    diagnostics: list[Diagnostic] = []

    for discovered in discover_config_files(config_dir):
        parsed = _parse_yaml(yaml, discovered, diagnostics)
        if parsed is None:
            continue

        resource = _validate_resource(parsed, discovered, diagnostics)
        if resource is None:
            continue

        _file_into_collection(resource, discovered, loaded, diagnostics)

    return loaded, diagnostics


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse_yaml(
    yaml: YAML,
    discovered: DiscoveredFile,
    diagnostics: list[Diagnostic],
) -> dict[str, Any] | None:
    try:
        data = yaml.load(discovered.path.read_text())
    except YAMLError as exc:
        diagnostics.append(
            Diagnostic(
                id="config.yaml.parse_failed",
                severity="error",
                message=f"YAML parse failed: {exc}",
                source=SourceLocation(path=discovered.repo_relative_path),
            )
        )
        return None

    if not isinstance(data, dict):
        diagnostics.append(
            Diagnostic(
                id="config.yaml.parse_failed",
                severity="error",
                message=(
                    f"top-level YAML must be a mapping, got {type(data).__name__}"
                ),
                source=SourceLocation(path=discovered.repo_relative_path),
            )
        )
        return None

    return data


def _validate_resource(
    raw: dict[str, Any],
    discovered: DiscoveredFile,
    diagnostics: list[Diagnostic],
) -> Any | None:
    kind = raw.get("kind")
    if not kind:
        diagnostics.append(
            Diagnostic(
                id="config.schema.kind_missing",
                severity="error",
                message="missing top-level 'kind' field",
                source=SourceLocation(path=discovered.repo_relative_path),
                key_path="kind",
            )
        )
        return None

    if kind not in KNOWN_KINDS:
        diagnostics.append(
            Diagnostic(
                id="config.schema.unknown_kind",
                severity="error",
                message=f"unknown kind {kind!r}; expected one of {sorted(KNOWN_KINDS)}",
                source=SourceLocation(path=discovered.repo_relative_path),
                key_path="kind",
            )
        )
        return None

    if discovered.expected_kind and kind != discovered.expected_kind:
        diagnostics.append(
            Diagnostic(
                id="config.schema.kind_mismatch",
                severity="warning",
                message=(
                    f"file under {discovered.repo_relative_path!r} declares "
                    f"kind {kind!r} but its directory expects "
                    f"{discovered.expected_kind!r}"
                ),
                source=SourceLocation(path=discovered.repo_relative_path),
                key_path="kind",
                suggestion=(
                    f"move the file under config/{_dir_for_kind(kind)}/ or "
                    "change its kind field"
                ),
            )
        )

    try:
        return parse_resource(raw)
    except ValidationError as exc:
        for error in exc.errors():
            diagnostics.append(
                Diagnostic(
                    id="config.schema.validation_failed",
                    severity="error",
                    message=error["msg"],
                    source=SourceLocation(path=discovered.repo_relative_path),
                    key_path=".".join(str(loc) for loc in error["loc"]),
                )
            )
        return None
    except ValueError as exc:
        diagnostics.append(
            Diagnostic(
                id="config.schema.validation_failed",
                severity="error",
                message=str(exc),
                source=SourceLocation(path=discovered.repo_relative_path),
            )
        )
        return None


def _file_into_collection(
    resource: Any,
    discovered: DiscoveredFile,
    loaded: LoadedConfig,
    diagnostics: list[Diagnostic],
) -> None:
    name = resource.metadata.name

    def _check_duplicate(collection: dict[str, Any]) -> bool:
        if name in collection:
            diagnostics.append(
                Diagnostic(
                    id="config.identity.duplicate_name",
                    severity="error",
                    message=(
                        f"duplicate {resource.kind} {name!r}: also defined "
                        "earlier in the load order"
                    ),
                    source=SourceLocation(path=discovered.repo_relative_path),
                    key_path="metadata.name",
                )
            )
            return True
        return False

    if isinstance(resource, Defaults):
        if loaded.defaults is not None:
            diagnostics.append(
                Diagnostic(
                    id="config.identity.duplicate_name",
                    severity="error",
                    message="more than one Defaults document loaded",
                    source=SourceLocation(path=discovered.repo_relative_path),
                )
            )
            return
        loaded.defaults = resource
    elif isinstance(resource, ArtifactSources):
        if loaded.artifacts is not None:
            diagnostics.append(
                Diagnostic(
                    id="config.identity.duplicate_name",
                    severity="error",
                    message="more than one ArtifactSources document loaded",
                    source=SourceLocation(path=discovered.repo_relative_path),
                )
            )
            return
        loaded.artifacts = resource
    elif isinstance(resource, ProviderConfig):
        if not _check_duplicate(loaded.providers):
            loaded.providers[name] = resource
    elif isinstance(resource, NetworkProfile):
        if not _check_duplicate(loaded.networks):
            loaded.networks[name] = resource
    elif isinstance(resource, VmRole):
        if not _check_duplicate(loaded.roles):
            loaded.roles[name] = resource
    elif isinstance(resource, CommandPreset):
        if not _check_duplicate(loaded.commands):
            loaded.commands[name] = resource
    elif isinstance(resource, Lab):
        if not _check_duplicate(loaded.labs):
            loaded.labs[name] = resource


def _dir_for_kind(kind: str) -> str:
    return {
        "Defaults": "",
        "ProviderConfig": "providers",
        "ArtifactSources": "artifacts",
        "NetworkProfile": "networks",
        "VmRole": "roles",
        "CommandPreset": "commands",
        "Lab": "labs",
    }.get(kind, "?")


__all__ = ["LoadedConfig", "load_config"]
