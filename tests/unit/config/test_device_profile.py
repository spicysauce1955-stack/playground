"""Tests for the DeviceProfile kind: loading, the .md sidecar, and the
resolver's profile -> lab device layering.
"""

from __future__ import annotations

from pathlib import Path

from playground.config.loader import load_config
from playground.models.kinds import DeviceMatch

_DEFAULTS_YAML = """\
apiVersion: playground/v1
kind: Defaults
metadata:
  name: defaults
spec:
  backend: local-libvirt
  offline: false
  budget:
    mode: permissive
    max_vcpu: 1
    max_memory_mb: 512
    max_disk_gb: 10
    max_vms: 1
    max_containers: 0
  vm:
    image: ubuntu-noble
    resources:
      vcpu: 1
      memory_mb: 512
      disk_gb: 10
    ssh:
      user: ubuntu
  network:
    profile: nat
  retention:
    runs:
      keep_last: 1
      max_age_days: 1
    logs:
      keep_per_run: true
      compress_after_days: 1
"""


def _write_defaults(config_dir: Path) -> None:
    (config_dir / "defaults.yaml").write_text(_DEFAULTS_YAML, encoding="utf-8")


def _write_profile(config_dir: Path) -> None:
    (config_dir / "devices").mkdir(parents=True, exist_ok=True)
    (config_dir / "devices" / "my-pixel.yaml").write_text(
        "apiVersion: playground/v1\n"
        "kind: DeviceProfile\n"
        "metadata:\n"
        "  name: my-pixel\n"
        "spec:\n"
        "  match:\n"
        "    serial: 1A2B3C4D5E\n"
        "  require:\n"
        "    root: true\n",
        encoding="utf-8",
    )


def resolve_lab_fixture(config_dir: Path, *, profile: str, require_min_sdk: int):
    from playground.config.loader import load_config
    from playground.config.resolver import resolve_lab

    _write_defaults(config_dir)
    (config_dir / "labs").mkdir(parents=True, exist_ok=True)
    (config_dir / "labs" / "l.yaml").write_text(
        "apiVersion: playground/v1\n"
        "kind: Lab\n"
        "metadata:\n  name: l\n"
        "spec:\n"
        "  backend: local-libvirt\n"
        "  devices:\n"
        f"    - name: pixel7\n      profile: {profile}\n"
        f"      require:\n        min_sdk: {require_min_sdk}\n",
        encoding="utf-8",
    )
    loaded, _ = load_config(config_dir)
    return resolve_lab(loaded, "l")


def test_device_profile_loads(tmp_path):
    _write_profile(tmp_path)
    loaded, diagnostics = load_config(tmp_path)
    assert not [d for d in diagnostics if d.severity == "error"]
    assert loaded.device_profiles["my-pixel"].spec.match == DeviceMatch(serial="1A2B3C4D5E")


def test_markdown_sidecar_is_skipped_not_parsed(tmp_path):
    """config/ discovery globs YAML and validates every file as a kind. A
    note sidecar must be ignored, not tripped over."""
    _write_profile(tmp_path)
    (tmp_path / "devices" / "my-pixel.md").write_text(
        "# my-pixel\n\nBootloader unlocked 2026-03.\n", encoding="utf-8"
    )
    loaded, diagnostics = load_config(tmp_path)
    assert not [d for d in diagnostics if d.severity == "error"]
    assert "my-pixel" in loaded.device_profiles


def test_lab_device_layers_over_its_profile(tmp_path):
    """A lab-level `require` wins; anything it omits comes from the profile."""
    _write_profile(tmp_path)
    resolved = resolve_lab_fixture(tmp_path, profile="my-pixel", require_min_sdk=30)
    device = resolved.devices[0]
    assert device.match.serial == "1A2B3C4D5E"  # from the profile
    assert device.require.root is True  # from the profile
    assert device.require.min_sdk == 30  # from the lab
