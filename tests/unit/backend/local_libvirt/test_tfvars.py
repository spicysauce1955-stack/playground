"""Tests for the local-libvirt tfvars renderer."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.local_libvirt.tfvars import render_tfvars
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_generic_infra():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


def test_render_tfvars_emits_lab_vm_names_in_declaration_order(
    resolved_generic_infra,
) -> None:
    # Order must match lab.spec.vms — tofu's libvirt_domain count.index
    # depends on the list position for disk + cloud-init pairing.
    payload = render_tfvars(resolved_generic_infra)

    assert payload == {"vm_names": ["node1", "docker1", "router1"]}


def test_render_tfvars_handles_empty_lab(resolved_generic_infra) -> None:
    empty = resolved_generic_infra.model_copy(update={"vms": []})

    assert render_tfvars(empty) == {"vm_names": []}


def test_render_tfvars_is_pure_no_diagnostics_returned(
    resolved_generic_infra,
) -> None:
    # The backend-capability warning lives in validator.py now; render_tfvars
    # is a pure data transformer. Pinning the signature so the warning
    # can't drift back into the renderer.
    payload = render_tfvars(resolved_generic_infra)

    assert isinstance(payload, dict)
    assert set(payload) == {"vm_names"}
