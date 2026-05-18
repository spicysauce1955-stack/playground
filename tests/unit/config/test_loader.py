"""Tests for the config loader."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from playground.config.loader import load_config

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


def test_load_committed_config_is_clean() -> None:
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == [], f"expected no diagnostics, got: {diagnostics}"
    assert loaded.defaults is not None
    assert loaded.artifacts is not None
    assert set(loaded.providers) == {"local-libvirt"}
    assert set(loaded.networks) == {"nat", "isolated", "routed"}
    assert set(loaded.roles) == {"generic-node", "docker-host", "router"}
    assert set(loaded.commands) == {"check-docker", "ping-network"}
    assert set(loaded.labs) == {"generic-infra"}


def test_load_tracks_resource_source_paths() -> None:
    loaded, diagnostics = load_config(CONFIG_DIR)

    assert diagnostics == []
    assert loaded.sources[("Lab", "generic-infra")].path == "config/labs/generic-infra.yaml"
    assert loaded.sources[("VmRole", "router")].path == "config/roles/router.yaml"


def _write(tmp: Path, rel: str, content: str) -> None:
    target = tmp / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(content).lstrip("\n"))


def test_loader_reports_parse_failure(tmp_path: Path) -> None:
    _write(tmp_path, "labs/broken.yaml", "spec: { unclosed:")
    loaded, diagnostics = load_config(tmp_path)
    assert loaded.labs == {}
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "config.yaml.parse_failed"
    assert diagnostics[0].severity == "error"
    assert diagnostics[0].source is not None
    assert diagnostics[0].source.path.endswith("broken.yaml")


def test_loader_reports_top_level_not_mapping(tmp_path: Path) -> None:
    _write(tmp_path, "labs/scalar.yaml", "just-a-string")
    loaded, diagnostics = load_config(tmp_path)
    assert loaded.labs == {}
    assert diagnostics[0].id == "config.yaml.parse_failed"
    assert "mapping" in diagnostics[0].message


def test_loader_reports_missing_kind(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "labs/no-kind.yaml",
        """
        apiVersion: playground/v1
        metadata:
          name: x
        spec: {}
        """,
    )
    loaded, diagnostics = load_config(tmp_path)
    assert any(d.id == "config.schema.kind_missing" for d in diagnostics)


def test_loader_reports_unknown_kind(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "labs/weird.yaml",
        """
        apiVersion: playground/v1
        kind: Doodad
        metadata:
          name: x
        spec: {}
        """,
    )
    loaded, diagnostics = load_config(tmp_path)
    assert any(d.id == "config.schema.unknown_kind" for d in diagnostics)


def test_loader_warns_on_kind_directory_mismatch(tmp_path: Path) -> None:
    # A CommandPreset placed under labs/ — the file parses but a warning
    # surfaces because the directory expects Lab.
    _write(
        tmp_path,
        "labs/misplaced.yaml",
        """
        apiVersion: playground/v1
        kind: CommandPreset
        metadata:
          name: misplaced
        spec:
          target:
            any: true
          command:
            shell: "echo hi"
          timeout_seconds: 5
        """,
    )
    loaded, diagnostics = load_config(tmp_path)
    mismatch = [d for d in diagnostics if d.id == "config.schema.kind_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0].severity == "warning"
    assert "misplaced" in (mismatch[0].source.path if mismatch[0].source else "")
    # The resource is still loaded — mismatch is a warning, not a fatal.
    assert loaded.commands.get("misplaced") is not None


def test_loader_reports_duplicate_name_for_vm_role(tmp_path: Path) -> None:
    role_yaml = """
        apiVersion: playground/v1
        kind: VmRole
        metadata:
          name: dup
        spec:
          provisioners: []
        """
    _write(tmp_path, "roles/a.yaml", role_yaml)
    _write(tmp_path, "roles/b.yaml", role_yaml)
    loaded, diagnostics = load_config(tmp_path)
    duplicates = [d for d in diagnostics if d.id == "config.identity.duplicate_name"]
    assert len(duplicates) == 1
    assert "VmRole" in duplicates[0].message
    assert "dup" in duplicates[0].message


def test_loader_reports_duplicate_defaults(tmp_path: Path) -> None:
    defaults_yaml = """
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
    _write(tmp_path, "defaults.yaml", defaults_yaml)
    # Second Defaults file under an unexpected directory — picked up by
    # discovery (not enforced by structure), should report duplicate.
    _write(tmp_path, "labs/also-defaults.yaml", defaults_yaml)
    loaded, diagnostics = load_config(tmp_path)
    duplicates = [d for d in diagnostics if d.id == "config.identity.duplicate_name"]
    assert any("Defaults" in d.message for d in duplicates)


def test_loader_emits_validation_failed_for_bad_field(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "networks/bad.yaml",
        """
        apiVersion: playground/v1
        kind: NetworkProfile
        metadata:
          name: bad
        spec:
          intent: mesh
          internet_access: true
          dns:
            enabled: true
        """,
    )
    loaded, diagnostics = load_config(tmp_path)
    bad = [d for d in diagnostics if d.id == "config.schema.validation_failed"]
    assert bad
    assert "spec.intent" in (bad[0].key_path or "")


def test_loader_raises_for_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        load_config(tmp_path / "does-not-exist")
