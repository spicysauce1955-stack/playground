"""Tests for dispatch routing to the cloud-digitalocean backend.

Covers:
- ``is_supported("cloud-digitalocean")`` is True.
- ``execute_suspend`` / ``execute_resume`` on local backends return
  ``(None, [diag])`` with ``id="runtime.backend.verb_not_supported"``.
- ``estimate_cost`` returns ``None`` for a libvirt lab and a
  ``CostEstimate`` for the ``cloud-smoke`` lab.
- DO routing: ``execute_apply``, ``execute_destroy``, ``execute_reset``,
  ``execute_suspend``, ``execute_resume``, ``query_status`` all forward
  to the cloud_digitalocean module with the right kwargs (no real
  tofu/network).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend import cloud_digitalocean as do_backend
from playground.backend import dispatch
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus
from playground.planner.plan import CostEstimate

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


def _resolved(name: str):
    loaded, _ = load_config(CONFIG_DIR)
    return resolve_lab(loaded, name)


# ---------------------------------------------------------------------------
# is_supported
# ---------------------------------------------------------------------------


def test_is_supported_cloud_digitalocean() -> None:
    assert dispatch.is_supported("cloud-digitalocean")


def test_is_supported_local_backends() -> None:
    assert dispatch.is_supported("local-libvirt")
    assert dispatch.is_supported("local-vbox")


def test_is_supported_unknown_returns_false() -> None:
    assert not dispatch.is_supported("aws")


# ---------------------------------------------------------------------------
# verb_not_supported_diagnostic
# ---------------------------------------------------------------------------


def test_verb_not_supported_diagnostic_shape() -> None:
    diag = dispatch.verb_not_supported_diagnostic("suspend", "local-libvirt")
    assert diag.id == "runtime.backend.verb_not_supported"
    assert diag.severity == "error"
    assert "local-libvirt" in diag.message
    assert "suspend" in diag.message


# ---------------------------------------------------------------------------
# execute_suspend — local backends return (None, [diag])
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lab_name", ["generic-infra", "vbox-smoke"])
def test_execute_suspend_unsupported_for_local_backends(lab_name: str) -> None:
    resolved = _resolved(lab_name)
    finished, diags = dispatch.execute_suspend(
        resolved=resolved,
        state_dir=Path("/tmp/s"),
        tofu_dir=Path("/tmp/t"),
        bus=EventBus(),
    )
    assert finished is None
    assert len(diags) == 1
    assert diags[0].id == "runtime.backend.verb_not_supported"
    assert diags[0].severity == "error"


# ---------------------------------------------------------------------------
# execute_resume — local backends return (None, [diag])
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lab_name", ["generic-infra", "vbox-smoke"])
def test_execute_resume_unsupported_for_local_backends(lab_name: str) -> None:
    resolved = _resolved(lab_name)
    finished, diags = dispatch.execute_resume(
        resolved=resolved,
        state_dir=Path("/tmp/s"),
        tofu_dir=Path("/tmp/t"),
        ansible_dir=Path("/tmp/a"),
        config_dir=Path("/tmp/c"),
        bus=EventBus(),
    )
    assert finished is None
    assert len(diags) == 1
    assert diags[0].id == "runtime.backend.verb_not_supported"
    assert diags[0].severity == "error"


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_returns_none_for_libvirt() -> None:
    resolved = _resolved("generic-infra")
    result = dispatch.estimate_cost(resolved)
    assert result is None


def test_estimate_cost_returns_cost_estimate_for_cloud_smoke() -> None:
    resolved = _resolved("cloud-smoke")
    result = dispatch.estimate_cost(resolved)
    assert result is not None
    assert isinstance(result, CostEstimate)
    assert result.hourly_usd > 0
    assert result.monthly_usd > 0
    assert result.advisory is True


# ---------------------------------------------------------------------------
# DO routing — monkeypatched; no real tofu/network
# ---------------------------------------------------------------------------


def test_execute_apply_routes_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_do_apply(**kwargs):
        seen.update(kwargs)
        seen["routed"] = "do"
        return None, []

    monkeypatch.setattr(do_backend, "execute_apply", fake_do_apply)
    resolved = _resolved("cloud-smoke")

    dispatch.execute_apply(
        resolved=resolved,
        state_dir=Path("/tmp/s"),
        tofu_dir=Path("/tmp/t"),
        ansible_dir=Path("/tmp/a"),
        config_dir=Path("/tmp/c"),
        bus=EventBus(),
    )

    assert seen["routed"] == "do"
    assert seen["resolved"] is resolved
    assert seen["state_dir"] == Path("/tmp/s")
    assert seen["tofu_dir"] == Path("/tmp/t")
    assert seen["ansible_dir"] == Path("/tmp/a")
    assert seen["config_dir"] == Path("/tmp/c")


def test_execute_destroy_routes_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_do_destroy(**kwargs):
        seen.update(kwargs)
        return None, []

    monkeypatch.setattr(do_backend, "execute_destroy", fake_do_destroy)
    resolved = _resolved("cloud-smoke")

    dispatch.execute_destroy(
        resolved=resolved,
        state_dir=Path("/tmp/s"),
        tofu_dir=Path("/tmp/t"),
        bus=EventBus(),
    )

    assert seen["resolved"] is resolved
    assert seen["state_dir"] == Path("/tmp/s")
    assert seen["tofu_dir"] == Path("/tmp/t")


def test_execute_reset_routes_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_do_reset(**kwargs):
        seen.update(kwargs)
        return None, []

    monkeypatch.setattr(do_backend, "execute_reset", fake_do_reset)
    resolved = _resolved("cloud-smoke")

    dispatch.execute_reset(
        resolved=resolved,
        state_dir=Path("/tmp/s"),
        tofu_dir=Path("/tmp/t"),
        bus=EventBus(),
    )

    assert seen["resolved"] is resolved
    assert seen["state_dir"] == Path("/tmp/s")
    assert seen["tofu_dir"] == Path("/tmp/t")


def test_execute_suspend_routes_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_do_suspend(**kwargs):
        seen.update(kwargs)
        return None, []

    monkeypatch.setattr(do_backend, "execute_suspend", fake_do_suspend)
    resolved = _resolved("cloud-smoke")

    dispatch.execute_suspend(
        resolved=resolved,
        state_dir=Path("/tmp/s"),
        tofu_dir=Path("/tmp/t"),
        bus=EventBus(),
    )

    assert seen["resolved"] is resolved
    assert seen["state_dir"] == Path("/tmp/s")
    assert seen["tofu_dir"] == Path("/tmp/t")


def test_execute_resume_routes_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_do_resume(**kwargs):
        seen.update(kwargs)
        return None, []

    monkeypatch.setattr(do_backend, "execute_resume", fake_do_resume)
    resolved = _resolved("cloud-smoke")

    dispatch.execute_resume(
        resolved=resolved,
        state_dir=Path("/tmp/s"),
        tofu_dir=Path("/tmp/t"),
        ansible_dir=Path("/tmp/a"),
        config_dir=Path("/tmp/c"),
        bus=EventBus(),
    )

    assert seen["resolved"] is resolved
    assert seen["state_dir"] == Path("/tmp/s")
    assert seen["tofu_dir"] == Path("/tmp/t")
    assert seen["ansible_dir"] == Path("/tmp/a")
    assert seen["config_dir"] == Path("/tmp/c")


def test_query_status_routes_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    from playground.models.status import LabStatus

    fake_status = LabStatus(
        lab="cloud-smoke",
        backend="cloud-digitalocean",
        expected_vms=1,
        provisioned_vms=0,
        vms=[],
        unknown_vms=[],
    )

    def fake_do_status(resolved):
        return fake_status, []

    monkeypatch.setattr(do_backend, "query_status", fake_do_status)
    resolved = _resolved("cloud-smoke")

    status, diags = dispatch.query_status(resolved, Path("/tmp/t"))

    assert status is fake_status
    assert diags == []


# ---------------------------------------------------------------------------
# estimate_cost — uses merged provider settings (Fix 1)
# ---------------------------------------------------------------------------


def test_estimate_cost_uses_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """estimate_cost(config_dir=...) should call merge_provider_settings with
    config_dir so provider-config defaults (not just lab overrides) are used."""
    seen: dict[str, object] = {}

    def fake_merge(resolved, *, config_dir=None, loaded=None):
        seen["config_dir"] = config_dir
        seen["loaded"] = loaded
        return {"size": "s-2vcpu-4gb"}

    monkeypatch.setattr(do_backend, "merge_provider_settings", fake_merge)
    resolved = _resolved("cloud-smoke")
    dispatch.estimate_cost(resolved, config_dir=Path("/fake/config"))

    assert seen["config_dir"] == Path("/fake/config")


def test_estimate_cost_with_config_dir_reflects_provider_size(
    tmp_path: Path,
) -> None:
    """If provider config sets size=s-2vcpu-4gb and lab omits it, the
    cost estimate from estimate_cost(config_dir=...) must use s-2vcpu-4gb."""
    from unittest.mock import patch

    from playground.backend.cloud_digitalocean.settings import merge_provider_settings

    resolved = _resolved("cloud-smoke")

    # Monkeypatch merge_provider_settings to return a size the lab didn't set.
    def patched_merge(r, *, config_dir=None, loaded=None):
        base = merge_provider_settings(r, config_dir=config_dir, loaded=loaded)
        base["size"] = "s-2vcpu-4gb"
        return base

    with patch(
        "playground.backend.cloud_digitalocean.merge_provider_settings",
        side_effect=patched_merge,
    ):
        result = dispatch.estimate_cost(resolved, config_dir=CONFIG_DIR)

    assert result is not None
    # s-2vcpu-4gb costs $24/mo per Droplet
    assert result.monthly_usd == 24.0


# ---------------------------------------------------------------------------
# plan_provider_summary (Fix 1)
# ---------------------------------------------------------------------------


def test_plan_provider_summary_returns_none_for_libvirt() -> None:
    resolved = _resolved("generic-infra")
    assert dispatch.plan_provider_summary(resolved) is None


def test_plan_provider_summary_returns_none_for_vbox() -> None:
    resolved = _resolved("vbox-smoke")
    assert dispatch.plan_provider_summary(resolved) is None


def test_plan_provider_summary_returns_dict_for_cloud_smoke() -> None:
    resolved = _resolved("cloud-smoke")
    summary = dispatch.plan_provider_summary(resolved, config_dir=CONFIG_DIR)
    assert summary is not None
    assert isinstance(summary, dict)


def test_plan_provider_summary_has_required_keys() -> None:
    resolved = _resolved("cloud-smoke")
    summary = dispatch.plan_provider_summary(resolved, config_dir=CONFIG_DIR)
    assert summary is not None
    for key in ("region", "size", "image", "ssh_exposure"):
        assert key in summary, f"missing key {key!r} in provider summary"


def test_plan_provider_summary_region_from_config() -> None:
    resolved = _resolved("cloud-smoke")
    summary = dispatch.plan_provider_summary(resolved, config_dir=CONFIG_DIR)
    assert summary is not None
    assert summary["region"] == "nyc3"


def test_plan_provider_summary_size_from_config() -> None:
    resolved = _resolved("cloud-smoke")
    summary = dispatch.plan_provider_summary(resolved, config_dir=CONFIG_DIR)
    assert summary is not None
    assert summary["size"] == "s-1vcpu-1gb"


def test_plan_provider_summary_image_from_config() -> None:
    resolved = _resolved("cloud-smoke")
    summary = dispatch.plan_provider_summary(resolved, config_dir=CONFIG_DIR)
    assert summary is not None
    assert summary["image"] == "ubuntu-24-04-x64"


def test_plan_provider_summary_ssh_exposure_open_when_no_cidrs() -> None:
    """Empty firewall_ssh_cidrs → SSH exposure shows 'open to all'."""
    resolved = _resolved("cloud-smoke")
    # Ensure lab has no ssh_cidrs override.
    lab = resolved.model_copy(
        update={"providers": {resolved.backend: {"region": "nyc3"}}}
    )
    # The committed provider config has firewall.ssh_cidrs: [] so this is
    # also empty.
    summary = dispatch.plan_provider_summary(lab, config_dir=CONFIG_DIR)
    assert summary is not None
    assert "open to all" in summary["ssh_exposure"]
    assert "0.0.0.0/0" in summary["ssh_exposure"]


def test_plan_provider_summary_ssh_exposure_lists_cidrs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When firewall_ssh_cidrs is set, they appear joined in ssh_exposure."""
    resolved = _resolved("cloud-smoke")

    def fake_merge(r, *, config_dir=None, loaded=None):
        return {
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "image": "ubuntu-24-04-x64",
            "firewall": {"ssh_cidrs": ["203.0.113.0/24", "198.51.100.0/24"]},
        }

    monkeypatch.setattr(do_backend, "merge_provider_settings", fake_merge)
    summary = dispatch.plan_provider_summary(resolved, config_dir=CONFIG_DIR)
    assert summary is not None
    assert "203.0.113.0/24" in summary["ssh_exposure"]
    assert "198.51.100.0/24" in summary["ssh_exposure"]
