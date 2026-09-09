"""Shared fixtures for `tests/unit/android` -- Android targeting tests.

Both fixtures start from a committed lab (mirrors the pattern already
used by `test_targets.py`/`tests/unit/capture/test_targets.py`: build a
`ResolvedLab` from real `config/`, then `model_copy` in the bit under
test) rather than hand-building a `ResolvedLab` from every required
field -- that model has ~20 fields and most of them are irrelevant to
targeting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.models.resolved import ResolvedDevice

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_lab_with_device():
    """A lab with a `pixel7` device slot and NO redroid-capable VM.

    Based on `generic-infra`, whose VMs (`node1`, `docker1`, `router1`)
    carry no `redroid` capability -- so this fixture exercises the
    device-only path of `resolve_targets` without an Android VM getting
    in the way.
    """
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    resolved = resolve_lab(loaded, "generic-infra")
    return resolved.model_copy(
        update={"devices": [ResolvedDevice(name="pixel7", kind="android")]}
    )


@pytest.fixture
def resolved_mixed_lab():
    """A lab with BOTH a redroid-host VM (`droid1`) and a `pixel7` device.

    Based on `redroid-cloud`, whose sole VM already declares the
    `redroid` capability.
    """
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    resolved = resolve_lab(loaded, "redroid-cloud")
    return resolved.model_copy(
        update={"devices": [ResolvedDevice(name="pixel7", kind="android")]}
    )
