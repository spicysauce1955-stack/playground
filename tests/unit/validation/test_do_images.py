"""cloud-digitalocean image / custom image / package_upgrade validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from playground.cli.tolerant_validation import validate_tolerant
from playground.config.loader import LoadedConfig, load_config
from playground.validation.validator import validate

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
URL = "https://dl.rockylinux.org/vault/rocky/8.8/images/x86_64/Rocky-8.qcow2"
IMAGE_ID = "config.backend.do_vm_image_invalid"
UPGRADE_ID = "config.backend.do_package_upgrade_invalid"
VM_PATH = "spec.vms[0].provider_overrides.cloud-digitalocean"


@pytest.fixture
def committed_load() -> LoadedConfig:
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return loaded


def _with_vm_overrides(loaded: LoadedConfig, *blocks: dict[str, Any]) -> None:
    """Rebuild cloud-smoke with one VM per ``cloud-digitalocean`` override block."""
    lab = loaded.labs["cloud-smoke"]
    base = lab.spec.vms[0]
    vms = [
        base.model_copy(update={
            "name": f"v{i}", "provider_overrides": {"cloud-digitalocean": b},
        })
        for i, b in enumerate(blocks)
    ]
    loaded.labs["cloud-smoke"] = lab.model_copy(
        update={"spec": lab.spec.model_copy(update={"vms": vms})}
    )


def _with_lab_provider(loaded: LoadedConfig, **kw: Any) -> None:
    lab = loaded.labs["cloud-smoke"]
    providers = {"cloud-digitalocean": {**lab.spec.providers["cloud-digitalocean"], **kw}}
    loaded.labs["cloud-smoke"] = lab.model_copy(
        update={"spec": lab.spec.model_copy(update={"providers": providers})}
    )


def _with_provider_config(loaded: LoadedConfig, **kw: Any) -> None:
    cfg = loaded.providers["cloud-digitalocean"]
    loaded.providers["cloud-digitalocean"] = cfg.model_copy(
        update={"spec": cfg.spec.model_copy(update=kw)}
    )


def _diags(loaded: LoadedConfig, diag_id: str):
    return [d for d in validate(loaded, lab="cloud-smoke") if d.id == diag_id]


def test_committed_config_has_none_of_these_errors(committed_load) -> None:
    all_diags = validate(committed_load)
    assert not [d for d in all_diags if d.id in (IMAGE_ID, UPGRADE_ID)]


@pytest.mark.parametrize(
    "block",
    [
        {"image": "rockylinux-8-x64"},
        {"image": 123456789},
        {"image": "123456789"},
        {"image_url": URL},
        {"image_url": URL, "image_distribution": "CentOS"},
        {"image_url": URL, "image_distribution": "Unknown"},
        {"image_url": URL, "package_upgrade": False, "size": "s-1vcpu-2gb"},
    ],
)
def test_valid_image_overrides_are_clean(committed_load, block) -> None:
    _with_vm_overrides(committed_load, block)
    assert _diags(committed_load, IMAGE_ID) == []
    assert _diags(committed_load, UPGRADE_ID) == []


@pytest.mark.parametrize("bad", ["", "  ", 0, -4, "0", "-4", True, 1.5, None, ["x"], {}])
def test_vm_image_must_be_slug_or_positive_id(committed_load, bad) -> None:
    _with_vm_overrides(committed_load, {"image": bad})
    [err] = _diags(committed_load, IMAGE_ID)
    assert err.severity == "error" and err.key_path == f"{VM_PATH}.image"


@pytest.mark.parametrize(
    "bad", ["http://example.com/x.qcow2", "example.com/x.qcow2", "https://", "", 7, None],
)
def test_image_url_must_be_https(committed_load, bad) -> None:
    _with_vm_overrides(committed_load, {"image_url": bad})
    [err] = _diags(committed_load, IMAGE_ID)
    assert err.key_path == f"{VM_PATH}.image_url"
    assert "https://" in err.message


def test_image_and_image_url_are_mutually_exclusive(committed_load) -> None:
    _with_vm_overrides(committed_load, {"image": "rockylinux-8-x64", "image_url": URL})
    [err] = _diags(committed_load, IMAGE_ID)
    assert err.key_path == VM_PATH and "mutually exclusive" in err.message


@pytest.mark.parametrize("bad", ["", "  ", 3, None])
def test_image_distribution_must_be_non_empty_string(committed_load, bad) -> None:
    _with_vm_overrides(committed_load, {"image_url": URL, "image_distribution": bad})
    [err] = _diags(committed_load, IMAGE_ID)
    assert err.key_path == f"{VM_PATH}.image_distribution"


def test_image_distribution_requires_image_url(committed_load) -> None:
    _with_vm_overrides(
        committed_load, {"image": "rockylinux-8-x64", "image_distribution": "CentOS"},
    )
    [err] = _diags(committed_load, IMAGE_ID)
    assert "image_url" in err.message and err.key_path.endswith(".image_distribution")


def test_image_distribution_rejected_by_the_provider(committed_load) -> None:
    """The DO API accepts "Rocky Linux"; the OpenTofu provider does not."""
    _with_vm_overrides(committed_load, {"image_url": URL, "image_distribution": "Rocky Linux"})
    [err] = _diags(committed_load, IMAGE_ID)
    assert "not accepted by the OpenTofu digitalocean provider" in err.message
    assert "CentOS" in err.message and "Unknown" in err.message


def test_same_url_with_different_distributions(committed_load) -> None:
    _with_vm_overrides(
        committed_load,
        {"image_url": URL, "image_distribution": "CentOS"},
        {"image_url": URL},  # defaults to Unknown
        {"image_url": URL, "image_distribution": "CentOS"},
    )
    [err] = _diags(committed_load, IMAGE_ID)
    assert err.key_path == "spec.vms[1].provider_overrides.cloud-digitalocean.image_distribution"
    assert "'v0'" in err.message


@pytest.mark.parametrize("bad", ["", 0, -1, True, ["x"]])
def test_lab_level_image_is_validated(committed_load, bad) -> None:
    _with_lab_provider(committed_load, image=bad)
    [err] = _diags(committed_load, IMAGE_ID)
    assert err.key_path == "spec.providers.cloud-digitalocean.image"


def test_lab_level_null_image_means_unset(committed_load) -> None:
    _with_lab_provider(committed_load, image=None)
    assert _diags(committed_load, IMAGE_ID) == []


def test_provider_config_image_is_validated_when_the_lab_does_not_override(
    committed_load,
) -> None:
    _with_provider_config(committed_load, image="")
    [err] = _diags(committed_load, IMAGE_ID)
    assert err.key_path == "spec.image"
    assert "providers" in err.source.path
    _with_lab_provider(committed_load, image="rockylinux-8-x64")
    assert _diags(committed_load, IMAGE_ID) == []  # the lab wins the merge


@pytest.mark.parametrize("bad", ["false", 0, 1, None, "yes"])
def test_package_upgrade_must_be_bool(committed_load, bad) -> None:
    _with_vm_overrides(committed_load, {"package_upgrade": bad})
    _with_lab_provider(committed_load, package_upgrade=bad)
    errs = _diags(committed_load, UPGRADE_ID)
    assert {e.key_path for e in errs} == {
        f"{VM_PATH}.package_upgrade", "spec.providers.cloud-digitalocean.package_upgrade",
    }
    assert {e.severity for e in errs} == {"error"}


def test_provider_config_package_upgrade_is_validated(committed_load) -> None:
    _with_provider_config(committed_load, package_upgrade="no")
    [err] = _diags(committed_load, UPGRADE_ID)
    assert err.key_path == "spec.package_upgrade"


@pytest.mark.parametrize("ok", [True, False])
def test_bool_package_upgrade_is_clean(committed_load, ok) -> None:
    _with_vm_overrides(committed_load, {"package_upgrade": ok})
    _with_lab_provider(committed_load, package_upgrade=ok)
    assert _diags(committed_load, UPGRADE_ID) == []


def test_non_mapping_block_is_left_to_the_size_check(committed_load) -> None:
    lab = committed_load.labs["cloud-smoke"]
    vm = lab.spec.vms[0].model_copy(update={"provider_overrides": {"cloud-digitalocean": "x"}})
    committed_load.labs["cloud-smoke"] = lab.model_copy(
        update={"spec": lab.spec.model_copy(update={"vms": [vm]})}
    )
    assert _diags(committed_load, IMAGE_ID) == [] and _diags(committed_load, UPGRADE_ID) == []
    assert _diags(committed_load, "config.backend.do_vm_size_invalid")


def test_checks_only_apply_to_cloud_digitalocean(committed_load) -> None:
    lab = committed_load.labs["generic-infra"]
    vm = lab.spec.vms[0].model_copy(update={
        "provider_overrides": {"cloud-digitalocean": {"image": "", "package_upgrade": "x"}},
    })
    committed_load.labs["generic-infra"] = lab.model_copy(
        update={"spec": lab.spec.model_copy(update={"vms": [vm, *lab.spec.vms[1:]]})}
    )
    diags = validate(committed_load, lab="generic-infra")
    assert not [d for d in diags if d.id in (IMAGE_ID, UPGRADE_ID)]


def test_read_only_verbs_downgrade_these_errors(committed_load) -> None:
    _with_vm_overrides(committed_load, {"image": "", "package_upgrade": "x"})
    tolerant = [
        d for d in validate_tolerant(committed_load, lab="cloud-smoke")
        if d.id in (IMAGE_ID, UPGRADE_ID)
    ]
    assert {d.id for d in tolerant} == {IMAGE_ID, UPGRADE_ID}
    assert {d.severity for d in tolerant} == {"warning"}
