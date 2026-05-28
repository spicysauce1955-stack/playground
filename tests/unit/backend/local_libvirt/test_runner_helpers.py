"""Unit tests for pure helpers in the local-libvirt runner.

The big ``execute_apply`` integration smoke lives in the live-infra
path; this file targets the small standalone helpers that are easier
to exercise on their own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.local_libvirt.runner import _wait_timeout_kwargs
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


def _resolved_with_libvirt_overrides(overrides: dict) -> object:
    loaded, _ = load_config(CONFIG_DIR)
    resolved = resolve_lab(loaded, "generic-infra")
    new_providers = {**resolved.providers}
    new_providers["local-libvirt"] = {
        **new_providers.get("local-libvirt", {}), **overrides,
    }
    return resolved.model_copy(update={"providers": new_providers})


def test_wait_timeout_kwargs_empty_when_no_overrides() -> None:
    """Default path: tofu defaults flow through wait.py's
    DEFAULT_SSH_TIMEOUT_SECONDS / DEFAULT_CLOUD_INIT_TIMEOUT_SECONDS."""
    resolved = _resolved_with_libvirt_overrides({})
    assert _wait_timeout_kwargs(resolved) == {}


def test_wait_timeout_kwargs_picks_up_ssh_override() -> None:
    """TCG (`domain_type: qemu`) boots are slow — operators raise the
    SSH gate so apply doesn't fail at wait-for-vms-ready."""
    resolved = _resolved_with_libvirt_overrides(
        {"wait_ssh_timeout_seconds": 1800},
    )
    assert _wait_timeout_kwargs(resolved) == {"ssh_timeout": 1800.0}


def test_wait_timeout_kwargs_picks_up_cloud_init_override() -> None:
    resolved = _resolved_with_libvirt_overrides(
        {"wait_cloud_init_timeout_seconds": 2400.0},
    )
    assert _wait_timeout_kwargs(resolved) == {"cloud_init_timeout": 2400.0}


def test_wait_timeout_kwargs_picks_up_both() -> None:
    resolved = _resolved_with_libvirt_overrides({
        "wait_ssh_timeout_seconds": 1800,
        "wait_cloud_init_timeout_seconds": 2400,
    })
    assert _wait_timeout_kwargs(resolved) == {
        "ssh_timeout": 1800.0,
        "cloud_init_timeout": 2400.0,
    }


def test_wait_timeout_kwargs_rejects_negative() -> None:
    resolved = _resolved_with_libvirt_overrides(
        {"wait_ssh_timeout_seconds": -1},
    )
    with pytest.raises(ValueError, match="wait_ssh_timeout_seconds"):
        _wait_timeout_kwargs(resolved)


def test_wait_timeout_kwargs_rejects_zero() -> None:
    # Zero is non-sensical too — a non-positive deadline would make the
    # wait loop return immediately as a false timeout.
    resolved = _resolved_with_libvirt_overrides(
        {"wait_cloud_init_timeout_seconds": 0},
    )
    with pytest.raises(ValueError, match="wait_cloud_init_timeout_seconds"):
        _wait_timeout_kwargs(resolved)


def test_wait_timeout_kwargs_rejects_non_numeric() -> None:
    resolved = _resolved_with_libvirt_overrides(
        {"wait_ssh_timeout_seconds": "1800"},
    )
    with pytest.raises(ValueError, match="wait_ssh_timeout_seconds"):
        _wait_timeout_kwargs(resolved)


def test_wait_timeout_kwargs_rejects_boolean() -> None:
    # `bool` is a subclass of `int` in Python — guard explicitly so
    # `wait_ssh_timeout_seconds: true` (a YAML mistype) doesn't pass.
    resolved = _resolved_with_libvirt_overrides(
        {"wait_ssh_timeout_seconds": True},
    )
    with pytest.raises(ValueError, match="wait_ssh_timeout_seconds"):
        _wait_timeout_kwargs(resolved)
