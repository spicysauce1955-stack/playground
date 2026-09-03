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


_PAGE_1_WITH_NEXT = {
    "sizes": _SIZES_BODY["sizes"],
    "links": {
        "pages": {
            "next": "https://api.digitalocean.com/v2/sizes?page=2&per_page=200",
        }
    },
}

_PAGE_2_MATCH = {
    "sizes": [
        {
            "slug": "s-page-2-only",
            "available": True,
            "regions": ["nyc1"],
        },
    ],
    # No `links.pages.next` -> this is the last page; catalogue is complete.
}

_PAGE_2_NO_MATCH_NO_NEXT = {
    "sizes": [
        {
            "slug": "s-something-else",
            "available": True,
            "regions": ["nyc1"],
        },
    ],
}


def _stub_sequence(monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, dict]]) -> None:
    """Stub `_request` to return successive responses on successive calls,
    regardless of the `path`/`params` passed (mirrors how `check_size_region_
    availability` follows an absolute `links.pages.next` URL as `path`)."""
    calls = iter(responses)

    def fake_request(method, path, token, *, params=None, timeout=15):
        try:
            return next(calls)
        except StopIteration:  # pragma: no cover - test bug guard
            raise AssertionError("more pages fetched than stubbed") from None

    monkeypatch.setattr(do_module, "_request", fake_request)


# ---------------------------------------------------------------------------
# Pagination: slug found on page 2 -> definitive result regardless of
# whether the rest of the catalogue was ever fetched
# ---------------------------------------------------------------------------


def test_size_found_on_second_page_returns_no_diagnostics(monkeypatch):
    _stub_sequence(
        monkeypatch,
        [(200, _PAGE_1_WITH_NEXT), (200, _PAGE_2_MATCH)],
    )
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-page-2-only", region="nyc1",
    )
    assert diags == []


def test_size_found_on_second_page_but_wrong_region_returns_error(monkeypatch):
    _stub_sequence(
        monkeypatch,
        [(200, _PAGE_1_WITH_NEXT), (200, _PAGE_2_MATCH)],
    )
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-page-2-only", region="ams3",
    )
    assert len(diags) == 1
    assert diags[0].severity == "error"
    assert diags[0].id == "runtime.cloud.size_unavailable_in_region"


# ---------------------------------------------------------------------------
# Pagination: truncated (page cap hit) + unknown slug -> WARNING, never error
# ---------------------------------------------------------------------------


def test_pagination_truncated_by_page_cap_and_unknown_slug_returns_warning(monkeypatch):
    """Every page keeps advertising a `next` link, so pagination never
    terminates naturally and hits `_MAX_SIZE_PAGES`. The slug is never
    seen. This must degrade to a warning, not assert non-existence."""

    def fake_request(method, path, token, *, params=None, timeout=15):
        return (
            200,
            {
                "sizes": [{"slug": "s-filler", "available": True, "regions": ["nyc1"]}],
                "links": {
                    "pages": {
                        "next": "https://api.digitalocean.com/v2/sizes?page=999&per_page=200",
                    }
                },
            },
        )

    monkeypatch.setattr(do_module, "_request", fake_request)
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-does-not-exist", region="nyc3",
    )
    assert len(diags) == 1
    assert diags[0].severity == "warning"
    assert diags[0].id == "runtime.cloud.size_check_unavailable"
    assert diags[0].id != "runtime.cloud.size_unavailable_in_region"


# ---------------------------------------------------------------------------
# Pagination: fully enumerated (multiple pages, last one has no `next`) +
# unknown slug -> hard error, because absence was actually confirmed
# ---------------------------------------------------------------------------


def test_pagination_fully_enumerated_and_unknown_slug_returns_error(monkeypatch):
    _stub_sequence(
        monkeypatch,
        [(200, _PAGE_1_WITH_NEXT), (200, _PAGE_2_NO_MATCH_NO_NEXT)],
    )
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-does-not-exist", region="nyc3",
    )
    assert len(diags) == 1
    assert diags[0].severity == "error"
    assert diags[0].id == "runtime.cloud.size_unavailable_in_region"


# ---------------------------------------------------------------------------
# Pagination: mid-pagination fetch failure -> WARNING, never error
# ---------------------------------------------------------------------------


def test_mid_pagination_transport_failure_returns_warning(monkeypatch):
    """First page succeeds and points at a next page; the second page fetch
    fails outright (transport error). The slug isn't in what was fetched,
    but the listing was never confirmed complete."""
    _stub_sequence(
        monkeypatch,
        [(200, _PAGE_1_WITH_NEXT), (0, {})],
    )
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-does-not-exist", region="nyc3",
    )
    assert len(diags) == 1
    assert diags[0].severity == "warning"
    assert diags[0].id == "runtime.cloud.size_check_unavailable"


def test_mid_pagination_malformed_shape_returns_warning(monkeypatch):
    """First page succeeds; the second page's body is missing the `sizes`
    key entirely. Inconclusive, so this must warn rather than error."""
    _stub_sequence(
        monkeypatch,
        [(200, _PAGE_1_WITH_NEXT), (200, {"unexpected": "shape"})],
    )
    diags = check_size_region_availability(
        "tok", lab_name="my-lab", size="s-does-not-exist", region="nyc3",
    )
    assert len(diags) == 1
    assert diags[0].severity == "warning"
    assert diags[0].id == "runtime.cloud.size_check_unavailable"


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


def test_token_never_appears_in_any_diagnostic_across_pagination(monkeypatch):
    """Same guarantee as above, but exercising the paginated code paths:
    slug found on page 2, page-cap truncation, and a mid-pagination
    failure."""
    secret = "dop_v1_" + "t" * 64

    def multi_page_ok(method, path, token, *, params=None, timeout=15):
        assert secret not in path
        if params:
            assert secret not in str(params)
        return (200, _PAGE_1_WITH_NEXT) if "page=2" not in path else (200, _PAGE_2_MATCH)

    monkeypatch.setattr(do_module, "_request", multi_page_ok)
    diags = check_size_region_availability(
        secret, lab_name="my-lab", size="s-page-2-only", region="nyc1",
    )
    for d in diags:
        assert secret not in (d.message or "")
        assert secret not in (d.suggestion or "")

    def always_next_page(method, path, token, *, params=None, timeout=15):
        return (
            200,
            {
                "sizes": [{"slug": "s-filler", "available": True, "regions": ["nyc1"]}],
                "links": {"pages": {"next": "https://api.digitalocean.com/v2/sizes?page=999"}},
            },
        )

    monkeypatch.setattr(do_module, "_request", always_next_page)
    diags = check_size_region_availability(
        secret, lab_name="my-lab", size="s-does-not-exist", region="nyc3",
    )
    for d in diags:
        assert secret not in (d.message or "")
        assert secret not in (d.suggestion or "")

    _stub_sequence(monkeypatch, [(200, _PAGE_1_WITH_NEXT), (0, {})])
    diags = check_size_region_availability(
        secret, lab_name="my-lab", size="s-does-not-exist", region="nyc3",
    )
    for d in diags:
        assert secret not in (d.message or "")
        assert secret not in (d.suggestion or "")
