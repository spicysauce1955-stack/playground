"""Validation for android_app workloads."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from ruamel.yaml import YAML

from playground.config.loader import LoadedConfig, load_config
from playground.models.kinds import Lab, parse_resource
from playground.validation.validator import validate

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"
_yaml = YAML(typ="safe")


@pytest.fixture
def committed_load() -> LoadedConfig:
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return loaded


def _lab(name: str, vm_role: str, workload: str) -> Lab:
    raw = _yaml.load(dedent(f"""
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: {name}
        spec:
          backend: local-libvirt
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: vm1
              role: {vm_role}
              networks: [net]
          workloads:
        {workload}
        """).lstrip("\n"))
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    return lab


def _ids(loaded: LoadedConfig, lab: Lab) -> list[str]:
    loaded.labs[lab.metadata.name] = lab
    return [d.id for d in validate(loaded, lab=lab.metadata.name)]


def test_android_app_without_options_is_an_error(committed_load) -> None:
    lab = _lab("no-opts", "redroid-host", """
            - name: a
              type: android_app
              source: ./a.apk
              placement: {target_role: redroid-host}
    """)
    assert "config.workload.android_options_missing" in _ids(committed_load, lab)


def test_android_options_on_a_container_workload_warns(committed_load) -> None:
    lab = _lab("ignored", "docker-host", """
            - name: a
              type: container
              source: nginx:alpine
              placement: {target_role: docker-host}
              android:
                package: com.x
    """)
    assert "config.workload.android_options_ignored" in _ids(committed_load, lab)


def test_android_app_targeting_a_non_redroid_vm_warns(committed_load) -> None:
    lab = _lab("wrong-target", "docker-host", """
            - name: a
              type: android_app
              source: ./a.apk
              placement: {target_role: docker-host}
              android:
                package: com.x
    """)
    ids = _ids(committed_load, lab)
    assert "config.workload.android_target_not_capable" in ids


def test_well_formed_android_workload_is_clean(committed_load) -> None:
    lab = _lab("good", "redroid-host", """
            - name: a
              type: android_app
              source: ./a.apk
              placement: {target_role: redroid-host}
              android:
                package: com.x
    """)
    ids = _ids(committed_load, lab)
    assert "config.workload.android_options_missing" not in ids
    assert "config.workload.android_target_not_capable" not in ids
    assert "config.workload.android_options_ignored" not in ids
