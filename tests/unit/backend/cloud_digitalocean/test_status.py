"""Tests for cloud_digitalocean.status.query_status."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.cloud_digitalocean import status as status_module
from playground.backend.cloud_digitalocean.status import query_status
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
# No token → all missing + token_missing warning
# ---------------------------------------------------------------------------


def test_query_status_no_token_returns_all_missing(
    resolved_cloud_smoke, monkeypatch
):
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    lab_status, diags = query_status(resolved_cloud_smoke)
    assert all(v.state == "missing" for v in lab_status.vms)
    assert lab_status.provisioned_vms == 0


def test_query_status_no_token_returns_token_missing_diagnostic(
    resolved_cloud_smoke, monkeypatch
):
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    _, diags = query_status(resolved_cloud_smoke)
    ids = [d.id for d in diags]
    assert "runtime.status.token_missing" in ids


def test_query_status_no_token_warning_contains_env_var_name(
    resolved_cloud_smoke, monkeypatch
):
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    _, diags = query_status(resolved_cloud_smoke)
    token_missing = next(d for d in diags if d.id == "runtime.status.token_missing")
    # Message must contain the env-var NAME, not a value.
    assert "DIGITALOCEAN_TOKEN" in token_missing.message


def test_query_status_no_token_does_not_call_api(
    resolved_cloud_smoke, monkeypatch
):
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    called = []

    def fake_list(token, tag):
        called.append((token, tag))
        return [], [], True

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list)
    query_status(resolved_cloud_smoke)
    assert called == [], "API should not be called when token is absent"


# ---------------------------------------------------------------------------
# Active droplet → state=running + ip + provider_id
# ---------------------------------------------------------------------------


def test_query_status_active_droplet_is_running(
    resolved_cloud_smoke, monkeypatch
):
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")
    vm_name = resolved_cloud_smoke.vms[0].name
    lab = resolved_cloud_smoke.lab_name

    def fake_list(token, tag):
        return [
            {
                "id": 99,
                "name": f"{lab}-{vm_name}",
                "status": "active",
                "networks": {
                    "v4": [{"type": "public", "ip_address": "1.2.3.4"}]
                },
            }
        ], [], True

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list)
    lab_status, diags = query_status(resolved_cloud_smoke)
    vm_status = next(v for v in lab_status.vms if v.name == vm_name)
    assert vm_status.state == "running"
    assert vm_status.ip == "1.2.3.4"
    assert vm_status.provider_id == "99"


# ---------------------------------------------------------------------------
# Off (non-active) droplet → state=provisioned
# ---------------------------------------------------------------------------


def test_query_status_off_droplet_is_provisioned(
    resolved_cloud_smoke, monkeypatch
):
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")
    vm_name = resolved_cloud_smoke.vms[0].name
    lab = resolved_cloud_smoke.lab_name

    def fake_list(token, tag):
        return [
            {
                "id": 100,
                "name": f"{lab}-{vm_name}",
                "status": "off",
                "networks": {"v4": []},
            }
        ], [], True

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list)
    lab_status, _ = query_status(resolved_cloud_smoke)
    vm_status = next(v for v in lab_status.vms if v.name == vm_name)
    assert vm_status.state == "provisioned"


# ---------------------------------------------------------------------------
# Missing droplet → state=missing
# ---------------------------------------------------------------------------


def test_query_status_missing_droplet_is_missing(
    resolved_cloud_smoke, monkeypatch
):
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")

    def fake_list(token, tag):
        return [], [], True

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list)
    lab_status, _ = query_status(resolved_cloud_smoke)
    assert all(v.state == "missing" for v in lab_status.vms)


# ---------------------------------------------------------------------------
# Extra tagged droplet → appears in unknown_vms
# ---------------------------------------------------------------------------


def test_query_status_unknown_droplet_in_unknown_vms(
    resolved_cloud_smoke, monkeypatch
):
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")
    lab = resolved_cloud_smoke.lab_name

    def fake_list(token, tag):
        return [
            {
                "id": 200,
                "name": f"{lab}-orphan-vm",
                "status": "active",
                "networks": {"v4": []},
            }
        ], [], True

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list)
    lab_status, _ = query_status(resolved_cloud_smoke)
    assert "orphan-vm" in lab_status.unknown_vms


def test_query_status_declared_vm_not_in_unknown_vms(
    resolved_cloud_smoke, monkeypatch
):
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")
    lab = resolved_cloud_smoke.lab_name
    vm_name = resolved_cloud_smoke.vms[0].name

    def fake_list(token, tag):
        return [
            {
                "id": 201,
                "name": f"{lab}-{vm_name}",
                "status": "active",
                "networks": {"v4": [{"type": "public", "ip_address": "5.5.5.5"}]},
            }
        ], [], True

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list)
    lab_status, _ = query_status(resolved_cloud_smoke)
    assert vm_name not in lab_status.unknown_vms


# ---------------------------------------------------------------------------
# provisioned_vms count
# ---------------------------------------------------------------------------


def test_query_status_provisioned_vms_count(resolved_cloud_smoke, monkeypatch):
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")
    lab = resolved_cloud_smoke.lab_name
    vm_name = resolved_cloud_smoke.vms[0].name

    def fake_list(token, tag):
        return [
            {
                "id": 300,
                "name": f"{lab}-{vm_name}",
                "status": "active",
                "networks": {"v4": [{"type": "public", "ip_address": "9.9.9.9"}]},
            }
        ], [], True

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list)
    lab_status, _ = query_status(resolved_cloud_smoke)
    assert lab_status.provisioned_vms == 1


# ---------------------------------------------------------------------------
# LabStatus shape — expected_vms always matches lab declaration
# ---------------------------------------------------------------------------


def test_query_status_expected_vms_matches_lab(resolved_cloud_smoke, monkeypatch):
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")

    monkeypatch.setattr(
        status_module, "list_droplets_by_tag",
        lambda token, tag: ([], [], True),
    )
    lab_status, _ = query_status(resolved_cloud_smoke)
    assert lab_status.expected_vms == len(resolved_cloud_smoke.vms)


# ---------------------------------------------------------------------------
# Fix 4 — non-conforming droplet name (no <lab>- prefix) in unknown_vms
# ---------------------------------------------------------------------------


def test_query_status_non_conforming_name_in_unknown_vms(
    resolved_cloud_smoke, monkeypatch
):
    """A tagged droplet whose name does NOT start with '<lab>-' must still
    appear in unknown_vms (the raw droplet name), not be silently dropped."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")

    # This droplet name has no <lab>- prefix — old code would skip it.
    non_conforming_name = "orphan-without-prefix"

    def fake_list(token, tag):
        return [
            {
                "id": 999,
                "name": non_conforming_name,
                "status": "active",
                "networks": {"v4": []},
            }
        ], [], True

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list)
    lab_status, _ = query_status(resolved_cloud_smoke)
    assert non_conforming_name in lab_status.unknown_vms, (
        f"Expected {non_conforming_name!r} in unknown_vms "
        f"(got {lab_status.unknown_vms!r})"
    )


def test_query_status_unknown_vms_uses_unprefixed_name_for_conforming(
    resolved_cloud_smoke, monkeypatch
):
    """A tagged droplet with the <lab>-<name> pattern has its prefix stripped
    in unknown_vms so the name matches the VM name from the lab YAML."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")
    lab = resolved_cloud_smoke.lab_name

    def fake_list(token, tag):
        return [
            {
                "id": 888,
                "name": f"{lab}-extra-node",
                "status": "active",
                "networks": {"v4": []},
            }
        ], [], True

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list)
    lab_status, _ = query_status(resolved_cloud_smoke)
    # The prefix should be stripped, so "extra-node" appears, not
    # "<lab>-extra-node".
    assert "extra-node" in lab_status.unknown_vms
    assert f"{lab}-extra-node" not in lab_status.unknown_vms


# ---------------------------------------------------------------------------
# Bug 3 — API failure must surface as error, not silent all-missing
# ---------------------------------------------------------------------------


def test_query_status_api_failure_returns_provider_unreachable_error(
    resolved_cloud_smoke, monkeypatch
):
    """When list_droplets_by_tag returns ok=False (API error), query_status
    must emit a runtime.status.provider_unreachable error diagnostic rather
    than silently reporting all VMs as 'missing' (which looks like confirmed
    teardown to scripts)."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")

    def fake_list_fail(token, tag):
        from playground.models.diagnostic import Diagnostic, SourceLocation
        return [], [
            Diagnostic(
                id="runtime.cloud.api_error",
                severity="warning",
                message="API returned 503",
                source=SourceLocation(path="DigitalOcean API"),
            )
        ], False

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list_fail)
    lab_status, diags = query_status(resolved_cloud_smoke)

    # The provider_unreachable diagnostic must be present at error severity.
    ids = [d.id for d in diags]
    assert "runtime.status.provider_unreachable" in ids, (
        f"Expected provider_unreachable diagnostic; got ids: {ids}"
    )
    unreachable = next(d for d in diags if d.id == "runtime.status.provider_unreachable")
    assert unreachable.severity == "error", (
        "provider_unreachable must be error, not warning"
    )


def test_query_status_api_failure_escalates_api_error_to_error(
    resolved_cloud_smoke, monkeypatch
):
    """The original api_error warning should also be escalated to error
    so callers checking severity see no warnings that look like soft issues."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")

    def fake_list_fail(token, tag):
        from playground.models.diagnostic import Diagnostic, SourceLocation
        return [], [
            Diagnostic(
                id="runtime.cloud.api_error",
                severity="warning",
                message="transport error",
                source=SourceLocation(path="DigitalOcean API"),
            )
        ], False

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list_fail)
    _, diags = query_status(resolved_cloud_smoke)

    api_error_diags = [d for d in diags if d.id == "runtime.cloud.api_error"]
    for d in api_error_diags:
        assert d.severity == "error", (
            f"api_error diagnostic should be escalated to error on failure; "
            f"got severity={d.severity!r}"
        )


def test_query_status_api_failure_not_silently_missing(
    resolved_cloud_smoke, monkeypatch
):
    """An API failure must not produce the same output as 'all VMs torn down'.
    The returned LabStatus may still have state='missing' (VmState has no
    'unknown') but the diagnostics list MUST be non-empty with error severity
    so callers/scripts that check diagnostics see a clear error."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "tok")

    def fake_list_fail(token, tag):
        from playground.models.diagnostic import Diagnostic, SourceLocation
        return [], [
            Diagnostic(
                id="runtime.cloud.api_error",
                severity="warning",
                message="connection refused",
                source=SourceLocation(path="DigitalOcean API"),
            )
        ], False

    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list_fail)

    # Genuine all-torn-down also returns all missing — but with empty diags.
    def fake_list_empty(token, tag):
        return [], [], True

    lab_status_fail, diags_fail = query_status(resolved_cloud_smoke)
    # Reset the monkeypatch to test genuine empty.
    monkeypatch.setattr(status_module, "list_droplets_by_tag", fake_list_empty)
    lab_status_ok, diags_ok = query_status(resolved_cloud_smoke)

    # Both may have all-missing states but the failure case has error diagnostics.
    assert any(d.severity == "error" for d in diags_fail), (
        "API failure must produce at least one error diagnostic"
    )
    assert not any(d.severity == "error" for d in diags_ok), (
        "Genuine empty should not produce error diagnostics"
    )
