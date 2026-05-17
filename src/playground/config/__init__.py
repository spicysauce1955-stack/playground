"""YAML config tree discovery, loading, and merge.

Owners: Team A. Contract: produces an unresolved intermediate model
that :mod:`playground.validation` then validates and
:mod:`playground.models` resolves.
"""

from playground.config.discovery import DiscoveredFile, discover_config_files
from playground.config.loader import LoadedConfig, load_config
from playground.config.resolver import resolve_lab

__all__ = [
    "DiscoveredFile",
    "LoadedConfig",
    "discover_config_files",
    "load_config",
    "resolve_lab",
]
