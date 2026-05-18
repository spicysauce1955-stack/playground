"""Walk a config tree and yield file paths grouped by directory.

Directory layout drives kind expectations but does not strictly enforce them:
a CommandPreset under ``labs/`` is still parsed and a Diagnostic surfaces the
mismatch downstream.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiscoveredFile:
    path: Path
    """Absolute path to the YAML file."""

    repo_relative_path: str
    """Path relative to ``config_dir.parent`` — used in ``Diagnostic.source.path``."""

    expected_kind: str | None
    """Kind we expect based on the directory; ``None`` when ambiguous."""


_DIRECTORY_KIND_MAP: dict[str, str] = {
    "providers": "ProviderConfig",
    "artifacts": "ArtifactSources",
    "networks": "NetworkProfile",
    "roles": "VmRole",
    "commands": "CommandPreset",
    "labs": "Lab",
}


def discover_config_files(config_dir: Path) -> Iterator[DiscoveredFile]:
    """Yield every ``*.yaml`` file under ``config_dir``.

    Symlinks are followed; hidden files are skipped. Order is stable
    (sorted by path) so test parametrization and diagnostics are
    deterministic.
    """
    if not config_dir.is_dir():
        raise NotADirectoryError(f"config_dir is not a directory: {config_dir}")

    base_for_relative = config_dir.parent
    for path in sorted(config_dir.rglob("*.yaml")):
        relative_to_config = path.relative_to(config_dir)
        if any(part.startswith(".") for part in relative_to_config.parts):
            continue
        yield DiscoveredFile(
            path=path,
            repo_relative_path=str(path.relative_to(base_for_relative)),
            expected_kind=_expected_kind_for(path, config_dir),
        )


def _expected_kind_for(path: Path, config_dir: Path) -> str | None:
    if path.name == "defaults.yaml" and path.parent == config_dir:
        return "Defaults"
    relative = path.relative_to(config_dir)
    if len(relative.parts) >= 2:
        return _DIRECTORY_KIND_MAP.get(relative.parts[0])
    return None


__all__ = ["DiscoveredFile", "discover_config_files"]
