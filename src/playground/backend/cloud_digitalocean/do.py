"""DigitalOcean API credentials and thin HTTP client.

Credential rule is absolute: the token value is **never** logged, returned
in a Diagnostic, put in an exception message, or passed as a subprocess
argument.  The only external surface is ``token_env_name`` (the NAME of the
env-var, not its value) and the boolean ``token_present``.

The HTTP call is isolated behind ``_request`` — a module-level function that
can be monkeypatched in tests.  Tests never need a real network connection.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab

# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

DEFAULT_TOKEN_ENV = "DIGITALOCEAN_TOKEN"

CONSOLE_URL = "https://cloud.digitalocean.com/droplets/{id}"


def token_env_name(resolved: ResolvedLab) -> str:
    """Return the env-var NAME that holds the DigitalOcean API token.

    Reads ``spec.providers.<backend>.token_env`` from the lab if set,
    otherwise falls back to :data:`DEFAULT_TOKEN_ENV`.  Always returns the
    NAME of the variable, never its value.
    """
    return (
        resolved.providers.get(resolved.backend, {}).get("token_env")
        or DEFAULT_TOKEN_ENV
    )


def read_token(resolved: ResolvedLab) -> str | None:
    """Read the API token from the environment.  Never logs or returns the value
    in any error path — only used internally by ``list_droplets_by_tag`` and
    ``delete_droplet``.
    """
    return os.environ.get(token_env_name(resolved))


def token_present(resolved: ResolvedLab) -> bool:
    """Return True if the API token env-var is set and non-empty."""
    return bool(read_token(resolved))


# ---------------------------------------------------------------------------
# HTTP seam (monkeypatchable)
# ---------------------------------------------------------------------------


def _request(
    method: str,
    path: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 15,
) -> tuple[int, dict[str, Any]]:
    """Execute one HTTP request against the DigitalOcean API.

    Returns ``(status_code, parsed_body)``.  On any transport error or JSON
    decode failure returns ``(0, {})``.  ``timeout`` bounds the call so a
    stalled network can never hang a caller indefinitely (a preflight uses
    a short value so ``apply``/``plan``/``doctor`` fail fast — NOTE-6).

    The ``Authorization: Bearer <token>`` header is set here and NEVER
    logged.  Callers must not log the ``token`` argument either.
    """
    try:
        with httpx.Client(
            base_url="https://api.digitalocean.com",
            timeout=timeout,
        ) as client:
            response = client.request(
                method,
                path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                body: dict[str, Any] = response.json()
            except Exception:  # noqa: BLE001
                body = {}
            return response.status_code, body
    except Exception:  # noqa: BLE001 — transport error; caller handles (0, {})
        return 0, {}


# ---------------------------------------------------------------------------
# API wrappers
# ---------------------------------------------------------------------------


def list_droplets_by_tag(
    token: str,
    tag: str,
) -> tuple[list[dict[str, Any]], list[Diagnostic], bool]:
    """GET /v2/droplets?tag_name=<tag>&per_page=200.

    Returns ``(droplets, diagnostics, ok)``.

    - Genuine empty list: ``([], [], True)``.
    - Success with results: ``([...], [], True)``.
    - API or transport failure: ``([], [warning_diagnostic], False)``.

    The ``ok`` flag is the authoritative signal: callers **must not** treat
    a failure ``([], [...], False)`` as "no Droplets found".  The warning
    message deliberately contains no token value.
    """
    status, body = _request(
        "GET",
        "/v2/droplets",
        token,
        params={"tag_name": tag, "per_page": 200},
    )
    if status == 0 or status >= 300:
        return [], [
            Diagnostic(
                id="runtime.cloud.api_error",
                severity="warning",
                message=(
                    f"DigitalOcean API returned status {status} when listing "
                    f"Droplets by tag {tag!r}; state may be stale"
                ),
                source=SourceLocation(path="DigitalOcean API"),
                suggestion=(
                    "check that $DIGITALOCEAN_TOKEN is valid and the account "
                    "has read access; retry or inspect the console at "
                    "https://cloud.digitalocean.com"
                ),
            )
        ], False
    droplets = body.get("droplets", [])
    if not isinstance(droplets, list):
        return [], [
            Diagnostic(
                id="runtime.cloud.api_error",
                severity="warning",
                message=(
                    f"DigitalOcean API response for tag {tag!r} had unexpected "
                    "shape (missing 'droplets' list)"
                ),
                source=SourceLocation(path="DigitalOcean API"),
            )
        ], False
    return droplets, [], True


def delete_droplet(
    token: str,
    droplet_id: int | str,
) -> list[Diagnostic]:
    """DELETE /v2/droplets/<id>.

    Treats 204 (deleted) and 404 (already gone) as success — idempotent.
    Status 0 (transport error) and any other non-2xx status produce a warning
    diagnostic without the token value; the tag-sweep re-list will determine
    whether the Droplet actually persists.
    """
    status, _ = _request("DELETE", f"/v2/droplets/{droplet_id}", token)
    if status in (204, 404):
        # 204 = deleted, 404 = already gone — both are definitive success.
        return []
    # Status 0 is a transport error; all other values are unexpected HTTP
    # responses.  In either case the Droplet's fate is unknown — return a
    # warning so the caller's tag-sweep re-list can determine survivors.
    return [
        Diagnostic(
            id="runtime.cloud.api_error",
            severity="warning",
            message=(
                f"DigitalOcean API returned status {status} when deleting "
                f"Droplet {droplet_id}; resource may still be running"
            ),
            source=SourceLocation(path=f"droplet/{droplet_id}"),
            suggestion=(
                f"remove manually at "
                f"{CONSOLE_URL.format(id=droplet_id)}"
            ),
        )
    ]


_MAX_SIZE_PAGES = 25
"""Page cap for :func:`_fetch_all_sizes`.

At ``per_page=200`` this is headroom for ~5000 size entries — DigitalOcean's
catalogue today is a few hundred — while guaranteeing a pathological or
looping ``links.pages.next`` response can never hang a preflight forever.
"""


def _fetch_all_sizes(
    token: str,
) -> tuple[list[dict[str, Any]], bool, str | None, int]:
    """Fetch the full ``GET /v2/sizes`` catalogue, following ``links.pages.next``.

    Returns ``(sizes, complete, incomplete_reason, last_status)``:

    - ``sizes`` is every entry parsed from every page fetched so far
      (best-effort — populated even when a later page fails).
    - ``complete`` is True only when pagination reached a page with no
      ``links.pages.next`` without ever hitting a transport/HTTP failure, a
      malformed page body, or the page cap. Only a ``True`` here means the
      absence of a slug is a confirmed fact rather than an artifact of a
      truncated fetch.
    - ``incomplete_reason`` is ``None`` when ``complete`` is True, else one
      of ``"status_error"`` (transport failure or non-2xx status),
      ``"shape_error"`` (a page body was missing/malformed ``sizes``), or
      ``"page_cap"`` (pagination did not terminate within
      :data:`_MAX_SIZE_PAGES` pages).
    - ``last_status`` is the HTTP status of the last request attempted (0 for
      a transport error), used to compose diagnostics.

    The token value is never logged or included in any return value here.
    """
    sizes: list[dict[str, Any]] = []
    path = "/v2/sizes"
    params: dict[str, Any] | None = {"per_page": 200}
    status = 0
    for _ in range(_MAX_SIZE_PAGES):
        status, body = _request("GET", path, token, params=params, timeout=8)
        if status == 0 or status >= 300:
            return sizes, False, "status_error", status

        page_sizes = body.get("sizes")
        if not isinstance(page_sizes, list):
            return sizes, False, "shape_error", status
        sizes.extend(page_sizes)

        next_url: str | None = None
        links = body.get("links")
        if isinstance(links, dict):
            pages = links.get("pages")
            if isinstance(pages, dict):
                candidate = pages.get("next")
                if isinstance(candidate, str) and candidate:
                    next_url = candidate

        if next_url is None:
            return sizes, True, None, status

        # `next_url` is an absolute URL that already carries its own query
        # string (DigitalOcean's pagination convention); passing it as
        # `path` with no extra `params` lets `_request` use it verbatim
        # (httpx.Client overrides base_url when given an absolute URL).
        path = next_url
        params = None

    return sizes, False, "page_cap", status


def check_size_region_availability(
    token: str,
    *,
    lab_name: str,
    size: str,
    region: str,
) -> list[Diagnostic]:
    """Preflight check: is ``size`` currently available in ``region``?

    DigitalOcean size/region availability is **not static** — capacity moves
    over time (a size can be pulled from one region and added to another
    within the same day), so this queries the live ``GET /v2/sizes`` list
    instead of consulting any hardcoded allowlist. The list is paginated
    (``links.pages.next``); :func:`_fetch_all_sizes` follows every page up
    to a bounded cap so a catalogue larger than one page is not mistaken for
    the complete set.

    Returns:
    - ``[]`` when the size exists, is available, and lists ``region``.
    - ``[error Diagnostic]`` (id ``runtime.cloud.size_unavailable_in_region``)
      when the size was found (on any page) and confirmed not available in
      ``region`` or not available at all, OR when the *entire* catalogue was
      successfully enumerated and the slug is genuinely not in it. The
      message names the regions where the size IS currently available so
      the operator can fix the lab in one edit.
    - ``[warning Diagnostic]`` (id ``runtime.cloud.size_check_unavailable``)
      when the API could not be reached, returned an unexpected shape, or
      the catalogue listing could not be established as complete (a page
      fetch failed partway through, or pagination hit the page cap) and the
      slug was not found in what *was* fetched. This check must NEVER
      hard-fail an apply on a transient DigitalOcean blip, transport error,
      or an incomplete listing — a preflight that breaks applies when the
      API has a hiccup, or when the catalogue merely exceeds one page, is
      worse than the 422 it is trying to prevent. Only a fully enumerated
      "not found" is reported as a hard error; any inconclusive result
      degrades to a warning and lets the apply proceed.

    The token value is never logged or included in any Diagnostic.
    """
    sizes, complete, incomplete_reason, status = _fetch_all_sizes(token)

    match: dict[str, Any] | None = None
    for entry in sizes:
        if isinstance(entry, dict) and entry.get("slug") == size:
            match = entry
            break

    if match is None:
        if not complete:
            if incomplete_reason == "shape_error":
                detail = (
                    "a DigitalOcean sizes API page had an unexpected shape "
                    "(missing 'sizes' list)"
                )
            elif incomplete_reason == "page_cap":
                detail = (
                    "the sizes catalogue was not fully enumerated within "
                    f"{_MAX_SIZE_PAGES} pages"
                )
            else:  # "status_error"
                detail = (
                    "a DigitalOcean sizes API page request returned status "
                    f"{status}"
                )
            return [
                Diagnostic(
                    id="runtime.cloud.size_check_unavailable",
                    severity="warning",
                    message=(
                        f"lab {lab_name!r}: could not verify whether size "
                        f"{size!r} is available in region {region!r} "
                        f"({detail}); proceeding without this check"
                    ),
                    source=SourceLocation(path="DigitalOcean API"),
                    suggestion=(
                        "verify manually with `doctl compute size list` or "
                        "at https://cloud.digitalocean.com/droplets/new"
                    ),
                )
            ]

        return [
            Diagnostic(
                id="runtime.cloud.size_unavailable_in_region",
                severity="error",
                message=(
                    f"lab {lab_name!r}: DigitalOcean size {size!r} was not "
                    "found in the live sizes list; it cannot be provisioned "
                    f"in region {region!r}"
                ),
                source=SourceLocation(
                    path="spec.providers.cloud-digitalocean.size"
                ),
                suggestion=(
                    "check for typos, or list current slugs with "
                    "`doctl compute size list`"
                ),
            )
        ]

    available_regions = sorted(
        r for r in (match.get("regions") or []) if isinstance(r, str)
    )
    is_available = bool(match.get("available", False))

    if is_available and region in available_regions:
        return []

    if available_regions:
        region_list = ", ".join(available_regions)
        message = (
            f"lab {lab_name!r}: DigitalOcean size {size!r} is not currently "
            f"available in region {region!r}; it IS currently available in: "
            f"{region_list}"
        )
        suggestion = (
            "set spec.providers.cloud-digitalocean.region to one of: "
            f"{region_list}"
        )
    else:
        message = (
            f"lab {lab_name!r}: DigitalOcean size {size!r} is not currently "
            f"available in any region (requested region was {region!r})"
        )
        suggestion = "choose a different size slug; see `doctl compute size list`"

    return [
        Diagnostic(
            id="runtime.cloud.size_unavailable_in_region",
            severity="error",
            message=message,
            source=SourceLocation(
                path="spec.providers.cloud-digitalocean.region"
            ),
            suggestion=suggestion,
        )
    ]


def verify_token(token: str) -> int:
    """Probe ``GET /v2/account`` to verify the token is accepted by the API.

    Returns the HTTP status code:
    - 200–299 → token is valid.
    - 401 → token is expired or revoked.
    - 403 → token lacks required scope.
    - 0 → transport error (treat as transient; caller decides whether to block).

    The token value is **never** logged or returned — only the status code is.

    Used as a fail-fast preflight, so it bounds the call to 8s: a rejected
    token answers 401/403 in well under that, and a stalled network returns
    0 quickly instead of feeling like a hang (NOTE-6).
    """
    status, _ = _request("GET", "/v2/account", token, timeout=8)
    return status


def droplet_summary(d: dict[str, Any]) -> dict[str, Any]:
    """Extract ``{name, id, status, public_ipv4}`` from a raw droplet dict.

    ``public_ipv4`` is the first ``networks.v4`` entry whose ``type`` is
    ``"public"``, or ``None`` when no public IPv4 is present.
    """
    networks_v4: list[dict[str, Any]] = (
        (d.get("networks") or {}).get("v4") or []
    )
    public_ipv4: str | None = None
    for net in networks_v4:
        if net.get("type") == "public":
            public_ipv4 = net.get("ip_address")
            break
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "status": d.get("status"),
        "public_ipv4": public_ipv4,
    }


__all__ = [
    "CONSOLE_URL",
    "DEFAULT_TOKEN_ENV",
    "check_size_region_availability",
    "delete_droplet",
    "droplet_summary",
    "list_droplets_by_tag",
    "read_token",
    "token_env_name",
    "token_present",
    "verify_token",
]
