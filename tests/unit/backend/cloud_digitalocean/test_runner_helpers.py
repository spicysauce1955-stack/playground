"""Tests for cloud_digitalocean.runner pure/near-pure helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from playground.backend.cloud_digitalocean.plan import build_do_plan
from playground.backend.cloud_digitalocean.runner import (
    _prepare_tofu_dir,
    _provider_settings,
    _write_tfvars,
)
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"
TOFU_DO_DIR = REPO_ROOT / "tofu" / "cloud_digitalocean"


@pytest.fixture
def resolved_cloud_smoke():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "cloud-smoke")


# ---------------------------------------------------------------------------
# _provider_settings
# ---------------------------------------------------------------------------


def test_provider_settings_merges_config_defaults_with_lab_overrides(
    resolved_cloud_smoke,
):
    """Provider config defaults are loaded and lab overrides win."""
    lab = resolved_cloud_smoke.model_copy(
        update={
            "providers": {
                resolved_cloud_smoke.backend: {
                    "region": "sfo3",  # lab override — should win
                }
            }
        }
    )
    settings = _provider_settings(CONFIG_DIR, lab)
    # Lab override wins.
    assert settings.get("region") == "sfo3"
    # The committed provider config has image: ubuntu-24-04-x64 as default.
    assert "image" in settings


def test_provider_settings_config_defaults_present_without_lab_overrides(
    resolved_cloud_smoke,
):
    """Without lab overrides, ProviderConfig defaults are present."""
    # Use empty lab providers dict for this backend.
    lab = resolved_cloud_smoke.model_copy(
        update={"providers": {resolved_cloud_smoke.backend: {}}}
    )
    settings = _provider_settings(CONFIG_DIR, lab)
    # The committed provider config has region: nyc3.
    assert settings.get("region") == "nyc3"


def test_provider_settings_falls_back_to_lab_overrides_on_no_config_dir(
    resolved_cloud_smoke,
):
    lab = resolved_cloud_smoke.model_copy(
        update={
            "providers": {
                resolved_cloud_smoke.backend: {"region": "ams3"}
            }
        }
    )
    settings = _provider_settings(None, lab)
    assert settings.get("region") == "ams3"


def test_provider_settings_strips_driver_key(resolved_cloud_smoke):
    settings = _provider_settings(CONFIG_DIR, resolved_cloud_smoke)
    assert "driver" not in settings


def test_provider_settings_strips_token_env_key(resolved_cloud_smoke):
    settings = _provider_settings(CONFIG_DIR, resolved_cloud_smoke)
    assert "token_env" not in settings


def test_provider_settings_lab_overrides_win_over_config_defaults(
    resolved_cloud_smoke,
):
    lab = resolved_cloud_smoke.model_copy(
        update={
            "providers": {
                resolved_cloud_smoke.backend: {"size": "s-4vcpu-8gb"}
            }
        }
    )
    settings = _provider_settings(CONFIG_DIR, lab)
    assert settings["size"] == "s-4vcpu-8gb"


# ---------------------------------------------------------------------------
# _prepare_tofu_dir
# ---------------------------------------------------------------------------


def test_prepare_tofu_dir_copies_tf_files(tmp_path):
    """All *.tf files from source_root land in per_lab_dir."""
    if not TOFU_DO_DIR.exists():
        pytest.skip("tofu/cloud_digitalocean not present in this checkout")
    per_lab = tmp_path / "lab"
    _prepare_tofu_dir(TOFU_DO_DIR, per_lab)
    tf_files = list(per_lab.glob("*.tf"))
    assert len(tf_files) > 0, "Expected at least one .tf file to be copied"


def test_prepare_tofu_dir_copies_cloud_init_cfg(tmp_path):
    if not TOFU_DO_DIR.exists():
        pytest.skip("tofu/cloud_digitalocean not present in this checkout")
    per_lab = tmp_path / "lab"
    _prepare_tofu_dir(TOFU_DO_DIR, per_lab)
    assert (per_lab / "cloud_init.cfg").exists()


def test_prepare_tofu_dir_creates_parent_dirs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tf").write_text('terraform {}')
    per_lab = tmp_path / "deep" / "nested" / "lab"
    _prepare_tofu_dir(source, per_lab)
    assert per_lab.is_dir()


def test_prepare_tofu_dir_is_idempotent(tmp_path):
    """Calling _prepare_tofu_dir twice must not raise."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tf").write_text('terraform {}')
    per_lab = tmp_path / "lab"
    _prepare_tofu_dir(source, per_lab)
    _prepare_tofu_dir(source, per_lab)  # second call must not raise
    assert (per_lab / "main.tf").exists()


def test_prepare_tofu_dir_does_not_delete_tfstate(tmp_path):
    """Existing terraform.tfstate must survive a re-run."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tf").write_text('terraform {}')
    per_lab = tmp_path / "lab"
    per_lab.mkdir()
    tfstate = per_lab / "terraform.tfstate"
    tfstate.write_text('{"version": 4}')
    _prepare_tofu_dir(source, per_lab)
    assert tfstate.exists(), "terraform.tfstate should not be removed"


def test_prepare_tofu_dir_does_not_delete_dot_terraform(tmp_path):
    """Existing .terraform/ must survive a re-run."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tf").write_text('terraform {}')
    per_lab = tmp_path / "lab"
    dot_tf = per_lab / ".terraform"
    dot_tf.mkdir(parents=True)
    (dot_tf / "providers.json").write_text("{}")
    _prepare_tofu_dir(source, per_lab)
    assert dot_tf.exists(), ".terraform/ should not be removed"


# ---------------------------------------------------------------------------
# _write_tfvars
# ---------------------------------------------------------------------------


def test_write_tfvars_produces_valid_json(tmp_path, resolved_cloud_smoke):
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    path = _write_tfvars(plan, "ssh-rsa AAAA fake-key", tmp_path)
    data = json.loads(path.read_text())
    assert isinstance(data, dict)


def test_write_tfvars_keys_match_allowlist(tmp_path, resolved_cloud_smoke):
    from playground.backend.cloud_digitalocean.tfvars import _TFVARS_KEYS
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    path = _write_tfvars(plan, "ssh-rsa AAAA fake-key", tmp_path)
    data = json.loads(path.read_text())
    assert set(data.keys()) == _TFVARS_KEYS


def test_write_tfvars_contains_no_token(tmp_path, resolved_cloud_smoke):
    """The tfvars file must never contain any value resembling an API token."""
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    path = _write_tfvars(plan, "ssh-rsa AAAA fake-key", tmp_path)
    content = path.read_text()
    # 'token' as a key/value pattern should not appear.
    assert "token" not in content.lower(), (
        f"Unexpected 'token' in tfvars: {content}"
    )


def test_write_tfvars_returns_path_in_per_lab_dir(tmp_path, resolved_cloud_smoke):
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    path = _write_tfvars(plan, "", tmp_path)
    assert path.parent == tmp_path
    assert path.suffix == ".json"


def test_write_tfvars_lab_name_in_filename(tmp_path, resolved_cloud_smoke):
    plan = build_do_plan(resolved_cloud_smoke, provider_settings={})
    path = _write_tfvars(plan, "", tmp_path)
    assert plan.lab_name in path.name


# ---------------------------------------------------------------------------
# _prepare_tofu_dir — stale .tf removal (Fix 3)
# ---------------------------------------------------------------------------


def test_prepare_tofu_dir_removes_stale_tf_not_in_source(tmp_path):
    """A .tf file present in per_lab_dir but absent from source is removed."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tf").write_text('terraform {}')

    per_lab = tmp_path / "lab"
    per_lab.mkdir()
    stale = per_lab / "old_module.tf"
    stale.write_text('# stale')

    _prepare_tofu_dir(source, per_lab)

    assert not stale.exists(), "stale .tf file should be removed"
    assert (per_lab / "main.tf").exists(), "new .tf file should be present"


def test_prepare_tofu_dir_removes_stale_cloud_init_cfg(tmp_path):
    """cloud_init.cfg in per_lab_dir but absent from source is removed."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tf").write_text('terraform {}')
    # source does NOT have cloud_init.cfg

    per_lab = tmp_path / "lab"
    per_lab.mkdir()
    stale_cfg = per_lab / "cloud_init.cfg"
    stale_cfg.write_text('# stale')

    _prepare_tofu_dir(source, per_lab)

    assert not stale_cfg.exists(), "stale cloud_init.cfg should be removed"


def test_prepare_tofu_dir_does_not_remove_tfvars(tmp_path):
    """*.tfvars.json files must not be touched by _prepare_tofu_dir."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tf").write_text('terraform {}')

    per_lab = tmp_path / "lab"
    per_lab.mkdir()
    tfvars = per_lab / "mylab.tfvars.json"
    tfvars.write_text('{}')

    _prepare_tofu_dir(source, per_lab)

    assert tfvars.exists(), "*.tfvars.json must not be removed"
