"""Smoke test: the package imports and exposes a version."""

import importlib


def test_top_level_package_imports() -> None:
    pkg = importlib.import_module("playground")
    assert hasattr(pkg, "__version__")
    assert isinstance(pkg.__version__, str)


def test_team_a_subpackages_import() -> None:
    for name in (
        "playground.config",
        "playground.models",
        "playground.validation",
        "playground.state",
        "playground.events",
        "playground.runs",
        "playground.logging",
    ):
        importlib.import_module(name)
