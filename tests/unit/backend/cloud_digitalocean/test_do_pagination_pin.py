"""`links.pages.next` is followed with the bearer token: pin it to the API host."""

from __future__ import annotations

from typing import Any

import pytest

from playground.backend.cloud_digitalocean import do as do_module
from playground.backend.cloud_digitalocean.do import _is_api_url, get_firewall, list_firewalls

TOKEN = "dop_v1_faketoken0000000000000000000000000000"


@pytest.mark.parametrize("url,ok", [
    ("https://api.digitalocean.com/v2/firewalls?page=2", True),
    ("https://api.digitalocean.com:443/v2/firewalls?page=2", True),
    ("http://api.digitalocean.com/v2/firewalls?page=2", False),
    ("https://evil.example.com/v2/firewalls?page=2", False),
    ("https://api.digitalocean.com.evil.example/v2/x", False),
    ("https://api.digitalocean.com@evil.example/v2/x", False),
    ("https://api.digitalocean.com:8443/v2/x", False),
    ("//api.digitalocean.com/v2/x", False),
    ("/v2/firewalls?page=2", False),
])
def test_is_api_url(url: str, ok: bool) -> None:
    assert _is_api_url(url) is ok


def _recording(monkeypatch, pages: dict[str, tuple[int, dict[str, Any]]]) -> list[str]:
    seen: list[str] = []

    def fake(method, path, token, *, params=None, timeout=15):
        seen.append(path)
        return pages[path]

    monkeypatch.setattr(do_module, "_request", fake)
    return seen


def test_foreign_next_link_is_not_followed(monkeypatch) -> None:
    evil = "https://evil.example.com/v2/firewalls?page=2"
    seen = _recording(monkeypatch, {"/v2/firewalls": (200, {
        "firewalls": [{"name": "a"}], "links": {"pages": {"next": evil}},
    })})
    fws, diags, ok = list_firewalls(TOKEN)
    assert not ok and fws == []
    assert diags and diags[0].severity == "warning"
    assert seen == ["/v2/firewalls"]  # the token never left for the other host


def test_sizes_pagination_is_pinned_too(monkeypatch) -> None:
    evil = "https://evil.example.com/v2/sizes?page=2"
    seen = _recording(monkeypatch, {"/v2/sizes": (200, {
        "sizes": [{"slug": "x"}], "links": {"pages": {"next": evil}},
    })})
    sizes, complete, reason, _ = do_module._fetch_all_sizes(TOKEN)
    assert not complete and reason is not None
    assert seen == ["/v2/sizes"]


def test_get_firewall_by_id(monkeypatch) -> None:
    seen = _recording(monkeypatch, {
        "/v2/firewalls/abc": (200, {"firewall": {"id": "abc", "status": "succeeded"}}),
        "/v2/firewalls/gone": (404, {}),
        "/v2/firewalls/boom": (500, {}),
    })
    assert get_firewall(TOKEN, "abc")[0] == {"id": "abc", "status": "succeeded"}
    assert get_firewall(TOKEN, "gone") == (None, [], True)
    fw, diags, ok = get_firewall(TOKEN, "boom")
    assert fw is None and not ok and TOKEN not in diags[0].message
    assert seen == ["/v2/firewalls/abc", "/v2/firewalls/gone", "/v2/firewalls/boom"]
