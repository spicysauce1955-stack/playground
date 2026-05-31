"""Tests for cloud_digitalocean.do — credentials + API client."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.cloud_digitalocean import do as do_module
from playground.backend.cloud_digitalocean.do import (
    DEFAULT_TOKEN_ENV,
    delete_droplet,
    droplet_summary,
    list_droplets_by_tag,
    read_token,
    token_env_name,
    token_present,
)
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_cloud_smoke():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "cloud-smoke")


# ---------------------------------------------------------------------------
# token_env_name
# ---------------------------------------------------------------------------


def test_token_env_name_uses_default_when_not_overridden(resolved_cloud_smoke):
    # The committed provider config sets token_env: DIGITALOCEAN_TOKEN.
    assert token_env_name(resolved_cloud_smoke) == DEFAULT_TOKEN_ENV


def test_token_env_name_honors_lab_override(resolved_cloud_smoke):
    lab = resolved_cloud_smoke.model_copy(
        update={
            "providers": {
                resolved_cloud_smoke.backend: {
                    "token_env": "MY_DO_TOKEN",
                }
            }
        }
    )
    assert token_env_name(lab) == "MY_DO_TOKEN"


def test_token_env_name_falls_back_to_default_on_empty_string(resolved_cloud_smoke):
    lab = resolved_cloud_smoke.model_copy(
        update={
            "providers": {
                resolved_cloud_smoke.backend: {
                    "token_env": "",
                }
            }
        }
    )
    assert token_env_name(lab) == DEFAULT_TOKEN_ENV


def test_token_env_name_returns_name_not_value(resolved_cloud_smoke, monkeypatch):
    """token_env_name must return the variable NAME, never its value."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "secret-value")
    result = token_env_name(resolved_cloud_smoke)
    assert result == "DIGITALOCEAN_TOKEN"
    assert "secret-value" not in result


# ---------------------------------------------------------------------------
# read_token / token_present
# ---------------------------------------------------------------------------


def test_read_token_returns_none_when_unset(resolved_cloud_smoke, monkeypatch):
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    assert read_token(resolved_cloud_smoke) is None


def test_read_token_returns_value_from_env(resolved_cloud_smoke, monkeypatch):
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token-value")
    result = read_token(resolved_cloud_smoke)
    assert result == "test-token-value"


def test_token_present_false_when_unset(resolved_cloud_smoke, monkeypatch):
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    assert token_present(resolved_cloud_smoke) is False


def test_token_present_true_when_set(resolved_cloud_smoke, monkeypatch):
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "some-token")
    assert token_present(resolved_cloud_smoke) is True


def test_token_present_uses_custom_env_var(resolved_cloud_smoke, monkeypatch):
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    monkeypatch.setenv("CUSTOM_TOKEN", "tok123")
    lab = resolved_cloud_smoke.model_copy(
        update={
            "providers": {
                resolved_cloud_smoke.backend: {"token_env": "CUSTOM_TOKEN"}
            }
        }
    )
    assert token_present(lab) is True


# ---------------------------------------------------------------------------
# list_droplets_by_tag — monkeypatched _request
# ---------------------------------------------------------------------------

_FAKE_DROPLET = {
    "id": 12345,
    "name": "cloud-smoke-node1",
    "status": "active",
    "networks": {
        "v4": [
            {"type": "public", "ip_address": "203.0.113.10"},
            {"type": "private", "ip_address": "10.0.0.5"},
        ]
    },
}


def test_list_droplets_by_tag_returns_droplets_on_200(monkeypatch):
    monkeypatch.setattr(
        do_module, "_request",
        lambda method, path, token, *, params=None: (
            200, {"droplets": [_FAKE_DROPLET]}
        ),
    )
    droplets, diags = list_droplets_by_tag("tok", "lab:cloud-smoke")
    assert len(droplets) == 1
    assert droplets[0]["name"] == "cloud-smoke-node1"
    assert diags == []


def test_list_droplets_by_tag_returns_empty_list_on_401(monkeypatch):
    monkeypatch.setattr(
        do_module, "_request",
        lambda method, path, token, *, params=None: (401, {}),
    )
    droplets, diags = list_droplets_by_tag("bad-tok", "lab:cloud-smoke")
    assert droplets == []
    assert len(diags) == 1
    assert diags[0].severity == "warning"


def test_list_droplets_by_tag_returns_warning_on_transport_error(monkeypatch):
    monkeypatch.setattr(
        do_module, "_request",
        lambda method, path, token, *, params=None: (0, {}),
    )
    droplets, diags = list_droplets_by_tag("tok", "lab:x")
    assert droplets == []
    assert len(diags) == 1
    assert diags[0].id == "runtime.cloud.api_error"


def test_list_droplets_by_tag_empty_droplets_on_200_no_matching(monkeypatch):
    monkeypatch.setattr(
        do_module, "_request",
        lambda method, path, token, *, params=None: (200, {"droplets": []}),
    )
    droplets, diags = list_droplets_by_tag("tok", "lab:empty-lab")
    assert droplets == []
    assert diags == []


# ---------------------------------------------------------------------------
# delete_droplet — monkeypatched _request
# ---------------------------------------------------------------------------


def test_delete_droplet_204_is_success(monkeypatch):
    monkeypatch.setattr(
        do_module, "_request",
        lambda method, path, token, *, params=None: (204, {}),
    )
    diags = delete_droplet("tok", 99)
    assert diags == []


def test_delete_droplet_404_is_success(monkeypatch):
    monkeypatch.setattr(
        do_module, "_request",
        lambda method, path, token, *, params=None: (404, {}),
    )
    diags = delete_droplet("tok", 99)
    assert diags == []


def test_delete_droplet_500_returns_warning(monkeypatch):
    monkeypatch.setattr(
        do_module, "_request",
        lambda method, path, token, *, params=None: (500, {}),
    )
    diags = delete_droplet("tok", 99)
    assert len(diags) == 1
    assert diags[0].severity == "warning"


def test_delete_droplet_transport_error_is_silent(monkeypatch):
    monkeypatch.setattr(
        do_module, "_request",
        lambda method, path, token, *, params=None: (0, {}),
    )
    diags = delete_droplet("tok", 99)
    # exit code 0 (transport error) is tolerated — tag-sweep catches survivors.
    assert diags == []


# ---------------------------------------------------------------------------
# Token-leak guard
# ---------------------------------------------------------------------------


def test_list_droplets_by_tag_no_token_in_diagnostics(monkeypatch):
    """The token value must never appear in any returned Diagnostic."""
    secret = "super-secret-api-token-xyz"
    monkeypatch.setattr(
        do_module, "_request",
        lambda method, path, token, *, params=None: (403, {}),
    )
    _, diags = list_droplets_by_tag(secret, "lab:x")
    for d in diags:
        assert secret not in (d.message or ""), (
            f"token leaked in diagnostic message: {d.message!r}"
        )
        assert secret not in (d.suggestion or ""), (
            f"token leaked in diagnostic suggestion: {d.suggestion!r}"
        )


def test_delete_droplet_no_token_in_diagnostics(monkeypatch):
    secret = "super-secret-api-token-xyz"
    monkeypatch.setattr(
        do_module, "_request",
        lambda method, path, token, *, params=None: (500, {}),
    )
    diags = delete_droplet(secret, 42)
    for d in diags:
        assert secret not in (d.message or ""), (
            f"token leaked in diagnostic message: {d.message!r}"
        )
        assert secret not in (d.suggestion or ""), (
            f"token leaked in diagnostic suggestion: {d.suggestion!r}"
        )


# ---------------------------------------------------------------------------
# droplet_summary
# ---------------------------------------------------------------------------


def test_droplet_summary_extracts_public_ipv4():
    summary = droplet_summary(_FAKE_DROPLET)
    assert summary["public_ipv4"] == "203.0.113.10"


def test_droplet_summary_returns_none_ipv4_when_no_public_network():
    d = {
        "id": 1,
        "name": "test",
        "status": "active",
        "networks": {"v4": [{"type": "private", "ip_address": "10.0.0.1"}]},
    }
    summary = droplet_summary(d)
    assert summary["public_ipv4"] is None


def test_droplet_summary_returns_none_ipv4_on_empty_networks():
    d = {"id": 2, "name": "empty", "status": "new", "networks": {}}
    summary = droplet_summary(d)
    assert summary["public_ipv4"] is None


def test_droplet_summary_returns_correct_id_and_name():
    summary = droplet_summary(_FAKE_DROPLET)
    assert summary["id"] == 12345
    assert summary["name"] == "cloud-smoke-node1"
    assert summary["status"] == "active"
