"""Tests for backend dispatch routing + unsupported-backend handling."""

from __future__ import annotations

from pathlib import Path

from playground.backend import dispatch
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


def _resolved(name: str):
    loaded, _ = load_config(CONFIG_DIR)
    return resolve_lab(loaded, name)


def test_is_supported() -> None:
    assert dispatch.is_supported("local-libvirt")
    assert dispatch.is_supported("local-vbox")
    assert not dispatch.is_supported("aws")


def test_unsupported_backend_diagnostic() -> None:
    d = dispatch.unsupported_backend_diagnostic("aws")
    assert d.id == "runtime.backend.unsupported"
    assert d.severity == "error"
    assert "aws" in d.message


def test_apply_routes_to_vbox(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_vbox_apply(**kwargs):
        seen.update(kwargs)
        seen["routed"] = "vbox"
        return None, []

    def fake_libvirt_apply(**_kw):
        seen["routed"] = "libvirt"
        return None, []

    monkeypatch.setattr(dispatch.local_vbox, "execute_apply", fake_vbox_apply)
    monkeypatch.setattr(dispatch.local_libvirt, "execute_apply", fake_libvirt_apply)

    dispatch.execute_apply(
        resolved=_resolved("vbox-smoke"),
        state_dir=Path("/tmp/s"),
        tofu_dir=Path("/tmp/t"),
        ansible_dir=Path("/tmp/a"),
        config_dir=Path("/tmp/c"),
        bus=EventBus(),
    )
    assert seen["routed"] == "vbox"
    # vbox adapter is not handed tofu_dir.
    assert "tofu_dir" not in seen


def test_apply_routes_to_libvirt(monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        dispatch.local_libvirt, "execute_apply",
        lambda **_kw: (seen.update(routed="libvirt"), (None, []))[1],
    )
    monkeypatch.setattr(
        dispatch.local_vbox, "execute_apply",
        lambda **_kw: (seen.update(routed="vbox"), (None, []))[1],
    )
    dispatch.execute_apply(
        resolved=_resolved("generic-infra"),
        state_dir=Path("/tmp/s"),
        tofu_dir=Path("/tmp/t"),
        ansible_dir=Path("/tmp/a"),
        config_dir=Path("/tmp/c"),
        bus=EventBus(),
    )
    assert seen["routed"] == "libvirt"


def test_status_routes_to_vbox(monkeypatch) -> None:
    monkeypatch.setattr(
        dispatch.local_vbox, "query_status",
        lambda resolved: ("VBOX_STATUS", []),
    )
    out, diags = dispatch.query_status(_resolved("vbox-smoke"), Path("/tmp/t"))
    assert out == "VBOX_STATUS"
    assert diags == []
