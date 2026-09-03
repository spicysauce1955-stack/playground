"""Tests for cloud_digitalocean.do.check_size_region_availability.

DigitalOcean size/region availability is not static — it moves over time
(the same size can be pulled from one region and added to another within
hours), so the preflight check queries the live ``GET /v2/sizes`` endpoint.
All tests here stub the HTTP layer (``do_module._request``); none make a
real network call even though ``DIGITALOCEAN_TOKEN`` may be set in the
environment.
"""

from __future__ import annotations

import pytest

from playground.backend.cloud_digitalocean import do as do_module
from playground.backend.cloud_digitalocean.do import check_size_region_availability

_SIZES_BODY = {
    "sizes": [
        {
            "slug": "s-1vcpu-1gb",
            "available": True,
            "regions": ["nyc1", "nyc3", "sfo3"],
        },
        {
            "slug": "s-4vcpu-8gb",
            "available": True,
            "regions": ["nyc1", "ams3"],
        },
        {
            "slug": "s-8vcpu-16gb",
            "available": False,
            "regions": [],
        },
    ]
}


def _stub(monkeypatch: pytest.MonkeyPatch, status: int, body: dict) -> None:
    monkeypatch.setattr(
        do_module,
        "_request",
        lambda method, path, token, *, params=None, timeout=15: (status, body),
    )


# ---------------------------------------------------------------------------
# Case 1: size available in the requested region -> no diagnostic
# ---------------------------------------------------------------------------


def test_size_available_in_region_returns_no_diagnostics(monkeypatch):
    _stub(monkeypatch, 200, _SIZES_BODY)
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-1vcpu-1gb", region="nyc3",
    )
    assert diags == []


# ---------------------------------------------------------------------------
# Case 2: size exists but region not in its list -> error diagnostic naming
# the regions where it IS available
# ---------------------------------------------------------------------------


def test_size_in_wrong_region_returns_error_naming_available_regions(monkeypatch):
    _stub(monkeypatch, 200, _SIZES_BODY)
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-4vcpu-8gb", region="nyc3",
    )
    assert len(diags) == 1
    diag = diags[0]
    assert diag.severity == "error"
    assert diag.id == "runtime.cloud.size_unavailable_in_region"
    # Must name the lab, the size, and the requested region.
    assert "my-lab" in diag.message
    assert "s-4vcpu-8gb" in diag.message
    assert "nyc3" in diag.message
    # Must name the regions where the size IS available so the operator can
    # fix the config in one edit.
    assert "nyc1" in diag.message
    assert "ams3" in diag.message
    assert diag.suggestion is not None
    assert "nyc1" in diag.suggestion
    assert "ams3" in diag.suggestion


def test_size_marked_unavailable_everywhere_returns_error(monkeypatch):
    """``available: false`` with an empty regions list must still error,
    even though the region-membership check alone would not catch it."""
    _stub(monkeypatch, 200, _SIZES_BODY)
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-8vcpu-16gb", region="nyc3",
    )
    assert len(diags) == 1
    assert diags[0].severity == "error"
    assert diags[0].id == "runtime.cloud.size_unavailable_in_region"


# ---------------------------------------------------------------------------
# Case 3: unknown size slug -> error diagnostic
# ---------------------------------------------------------------------------


def test_unknown_size_slug_returns_error_diagnostic(monkeypatch):
    _stub(monkeypatch, 200, _SIZES_BODY)
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-does-not-exist", region="nyc3",
    )
    assert len(diags) == 1
    diag = diags[0]
    assert diag.severity == "error"
    assert diag.id == "runtime.cloud.size_unavailable_in_region"
    assert "s-does-not-exist" in diag.message
    assert "my-lab" in diag.message


# ---------------------------------------------------------------------------
# Case 4: API unreachable / malformed response -> WARNING only, apply proceeds
# ---------------------------------------------------------------------------


def test_transport_error_returns_warning_only(monkeypatch):
    _stub(monkeypatch, 0, {})
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-4vcpu-8gb", region="nyc3",
    )
    assert len(diags) == 1
    assert diags[0].severity == "warning"
    assert diags[0].id == "runtime.cloud.size_check_unavailable"


def test_http_error_status_returns_warning_only(monkeypatch):
    _stub(monkeypatch, 500, {})
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-4vcpu-8gb", region="nyc3",
    )
    assert len(diags) == 1
    assert diags[0].severity == "warning"
    assert diags[0].id == "runtime.cloud.size_check_unavailable"


def test_malformed_response_missing_sizes_key_returns_warning_only(monkeypatch):
    _stub(monkeypatch, 200, {"unexpected": "shape"})
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-4vcpu-8gb", region="nyc3",
    )
    assert len(diags) == 1
    assert diags[0].severity == "warning"
    assert diags[0].id == "runtime.cloud.size_check_unavailable"


def test_malformed_response_sizes_not_a_list_returns_warning_only(monkeypatch):
    _stub(monkeypatch, 200, {"sizes": "not-a-list"})
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-4vcpu-8gb", region="nyc3",
    )
    assert len(diags) == 1
    assert diags[0].severity == "warning"


# ---------------------------------------------------------------------------
# Token-leak guard
# ---------------------------------------------------------------------------


def test_token_never_appears_in_any_diagnostic(monkeypatch):
    """The token value must never appear in a message or suggestion, across
    every branch (success, region-mismatch, unknown-slug, transport error,
    malformed response)."""
    secret = "dop_v1_" + "s" * 64

    scenarios = [
        (200, _SIZES_BODY, "s-1vcpu-1gb", "nyc3"),  # no diagnostics
        (200, _SIZES_BODY, "s-4vcpu-8gb", "nyc3"),  # region mismatch
        (200, _SIZES_BODY, "s-unknown", "nyc3"),  # unknown slug
        (0, {}, "s-4vcpu-8gb", "nyc3"),  # transport error
        (200, {"sizes": "nope"}, "s-4vcpu-8gb", "nyc3"),  # malformed
    ]
    for status, body, size, region in scenarios:
        _stub(monkeypatch, status, body)
        diags = check_size_region_availability(
            secret, lab_name="my-lab", size=size, region=region,
        )
        for d in diags:
            assert secret not in (d.message or ""), (
                f"token leaked in diagnostic message: {d.message!r}"
            )
            assert secret not in (d.suggestion or ""), (
                f"token leaked in diagnostic suggestion: {d.suggestion!r}"
            )
