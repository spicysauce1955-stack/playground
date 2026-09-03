"""Tests for the VmRole-vs-ProviderConfig capability cross-check."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from ruamel.yaml import YAML

from playground.config.loader import LoadedConfig, load_config
from playground.models.kinds import Lab, parse_resource
from playground.validation.validator import _capabilities_for_vm, validate

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

_yaml = YAML(typ="safe")

CAPABILITY_UNSUPPORTED = "config.backend.capability_unsupported"


@pytest.fixture
def committed_load() -> LoadedConfig:
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return loaded


def _yaml_to_lab(text: str) -> Lab:
    raw = _yaml.load(dedent(text).lstrip("\n"))
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    return lab


def _redroid_lab(backend: str, name: str) -> Lab:
    return _yaml_to_lab(
        f"""
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: {name}
        spec:
          backend: {backend}
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: droid1
              role: redroid-host
              networks: [net]
        """
    )


def test_capabilities_for_vm_unions_the_inheritance_chain(
    committed_load: LoadedConfig,
) -> None:
    """redroid-host extends docker-host; the child must inherit and add."""
    lab = _redroid_lab("local-libvirt", "cap-union")
    caps = _capabilities_for_vm(committed_load, lab.spec.vms[0])
    assert caps["redroid"] is True
    assert caps["docker"] is True  # inherited from docker-host
    assert caps["swarm"] is True


def test_warns_when_backend_explicitly_declares_the_capability_false(
    committed_load: LoadedConfig,
) -> None:
    bad = _redroid_lab("local-vbox", "redroid-on-vbox")
    committed_load.labs[bad.metadata.name] = bad

    diagnostics = validate(committed_load, lab=bad.metadata.name)

    matching = [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]
    assert len(matching) == 1
    assert matching[0].severity == "warning"
    assert "redroid" in matching[0].message
    assert "local-vbox" in matching[0].message
    assert matching[0].key_path == "spec.vms[0].role"


def test_no_warning_when_backend_supports_the_capability(
    committed_load: LoadedConfig,
) -> None:
    good = _redroid_lab("cloud-digitalocean", "redroid-on-do")
    committed_load.labs[good.metadata.name] = good

    diagnostics = validate(committed_load, lab=good.metadata.name)

    assert not [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]


def test_no_warning_when_provider_is_silent_about_the_capability(
    committed_load: LoadedConfig,
) -> None:
    """An undeclared capability is not the same as an unsupported one."""
    provider = committed_load.providers["local-libvirt"]
    stripped = provider.spec.model_copy(update={"capabilities": {}})
    committed_load.providers["local-libvirt"] = provider.model_copy(
        update={"spec": stripped}
    )
    lab = _redroid_lab("local-libvirt", "silent-provider")
    committed_load.labs[lab.metadata.name] = lab

    diagnostics = validate(committed_load, lab=lab.metadata.name)

    assert not [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]


def test_no_warning_when_provider_omits_capabilities_entirely(
    committed_load: LoadedConfig,
) -> None:
    """ProviderConfigSpec only declares `driver`; capabilities may be absent."""
    from playground.models.kinds import ProviderConfig

    bare = parse_resource(
        _yaml.load(
            dedent(
                """
                apiVersion: playground/v1
                kind: ProviderConfig
                metadata:
                  name: local-libvirt
                spec:
                  driver: local-libvirt
                """
            ).lstrip("\n")
        )
    )
    assert isinstance(bare, ProviderConfig)
    committed_load.providers["local-libvirt"] = bare

    lab = _redroid_lab("local-libvirt", "bare-provider")
    committed_load.labs[lab.metadata.name] = lab

    diagnostics = validate(committed_load, lab=lab.metadata.name)

    assert not [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]


def test_false_role_capability_never_warns(committed_load: LoadedConfig) -> None:
    """Only capabilities the role actually asks for are checked."""
    lab = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: plain-node-on-vbox
        spec:
          backend: local-vbox
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: plain
              role: generic-node
              networks: [net]
        """
    )
    committed_load.labs[lab.metadata.name] = lab

    diagnostics = validate(committed_load, lab=lab.metadata.name)

    assert not [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]


def test_committed_config_emits_no_capability_warnings(
    committed_load: LoadedConfig,
) -> None:
    """Every committed lab must run on a backend that supports its roles."""
    diagnostics = validate(committed_load)
    matching = [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]
    assert matching == [], f"committed config has capability mismatches: {matching}"
