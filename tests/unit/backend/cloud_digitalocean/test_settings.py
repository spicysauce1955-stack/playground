"""Tests for cloud_digitalocean.settings.merge_provider_settings.

Verifies that the public function correctly merges ProviderConfig defaults
with lab-level overrides, producing the dict passed to build_do_plan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.cloud_digitalocean.settings import merge_provider_settings
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
# merge_provider_settings — delegated from runner._provider_settings
# ---------------------------------------------------------------------------


def test_merge_uses_config_defaults_when_lab_overrides_empty(resolved_cloud_smoke):
    """ProviderConfig region appears when lab omits it."""
    lab = resolved_cloud_smoke.model_copy(
        update={"providers": {resolved_cloud_smoke.backend: {}}}
    )
    settings = merge_provider_settings(lab, config_dir=CONFIG_DIR)
    # committed provider default
    assert settings.get("region") == "nyc3"


def test_merge_lab_override_wins_over_config_default(resolved_cloud_smoke):
    lab = resolved_cloud_smoke.model_copy(
        update={
            "providers": {
                resolved_cloud_smoke.backend: {"region": "sfo3"}
            }
        }
    )
    settings = merge_provider_settings(lab, config_dir=CONFIG_DIR)
    assert settings["region"] == "sfo3"


def test_merge_config_image_present_even_without_lab_override(resolved_cloud_smoke):
    lab = resolved_cloud_smoke.model_copy(
        update={"providers": {resolved_cloud_smoke.backend: {}}}
    )
    settings = merge_provider_settings(lab, config_dir=CONFIG_DIR)
    assert settings.get("image") == "ubuntu-24-04-x64"


def test_merge_falls_back_to_lab_only_on_no_config_dir(resolved_cloud_smoke):
    lab = resolved_cloud_smoke.model_copy(
        update={"providers": {resolved_cloud_smoke.backend: {"region": "ams3"}}}
    )
    settings = merge_provider_settings(lab, config_dir=None)
    assert settings["region"] == "ams3"
    # Without config_dir no provider defaults are loaded; image key absent.
    # (lab didn't set it either)
    assert "image" not in settings or settings.get("image") in (None, "")


def test_merge_strips_driver_key(resolved_cloud_smoke):
    settings = merge_provider_settings(resolved_cloud_smoke, config_dir=CONFIG_DIR)
    assert "driver" not in settings


def test_merge_strips_token_env_key(resolved_cloud_smoke):
    settings = merge_provider_settings(resolved_cloud_smoke, config_dir=CONFIG_DIR)
    assert "token_env" not in settings


def test_merge_size_from_provider_config_is_present(resolved_cloud_smoke):
    """The committed provider config sets size=s-1vcpu-1gb."""
    lab = resolved_cloud_smoke.model_copy(
        update={"providers": {resolved_cloud_smoke.backend: {}}}
    )
    settings = merge_provider_settings(lab, config_dir=CONFIG_DIR)
    assert settings.get("size") == "s-1vcpu-1gb"


def test_merge_size_lab_override_wins(resolved_cloud_smoke):
    lab = resolved_cloud_smoke.model_copy(
        update={
            "providers": {
                resolved_cloud_smoke.backend: {"size": "s-4vcpu-8gb"}
            }
        }
    )
    settings = merge_provider_settings(lab, config_dir=CONFIG_DIR)
    assert settings["size"] == "s-4vcpu-8gb"
