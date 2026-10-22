"""DigitalOcean API credentials and thin HTTP client.

Credential rule is absolute: the token value is **never** logged, returned
in a Diagnostic, put in an exception message, or passed as a subprocess
argument.  The only external surface is ``token_env_name`` (the NAME of the
env-var, not its value) and the boolean ``token_present``.

The HTTP call is isolated behind ``_request`` — a module-level function that
can be monkeypatched in tests.  Tests never need a real network connection.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any
from urllib.parse import urlsplit

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
    json: dict[str, Any] | None = None,
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
                json=json,
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


_API_ORIGIN = ("https", "api.digitalocean.com")


def _is_api_url(url: str) -> bool:
    """True only for an https URL on the DigitalOcean API host.

    ``links.pages.next`` is followed with the bearer token attached, so it
    must never lead anywhere but the API itself.
    """
    try:
        parts = urlsplit(url)
        return (parts.scheme, parts.hostname or "") == _API_ORIGIN and parts.port in (None, 443)
    except ValueError:
        return False


_MAX_LIST_PAGES = 25
"""Page cap for :func:`_list_paged` (bounds a looping ``links.pages.next``)."""


def _list_paged(
    token: str, path: str, key: str, *, extra_params: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """GET a paginated collection, following ``links.pages.next``.

    Returns ``(items, ok)``.  ``ok`` is False on any transport/HTTP failure,
    malformed body, or page-cap hit; callers must not read an empty list as
    "absent" unless ``ok`` is True.  ``extra_params`` go on the first page
    only; DigitalOcean's ``next`` links already carry the full query string.
    """
    items: list[dict[str, Any]] = []
    url = path
    params: dict[str, Any] | None = {"per_page": 200, **(extra_params or {})}
    for _ in range(_MAX_LIST_PAGES):
        status, body = _request("GET", url, token, params=params)
        if status == 0 or status >= 300:
            return items, False
        page = body.get(key)
        if not isinstance(page, list):
            return items, False
        items.extend(i for i in page if isinstance(i, dict))
        next_url: str | None = None
        links = body.get("links")
        if isinstance(links, dict):
            pages = links.get("pages")
            if isinstance(pages, dict):
                candidate = pages.get("next")
                if isinstance(candidate, str) and candidate:
                    next_url = candidate
        if next_url is None:
            return items, True
        if not _is_api_url(next_url):
            return items, False  # never send the token to another origin
        url, params = next_url, None
    return items, False


def _api_error(what: str, suggestion: str) -> Diagnostic:
    """Warning diagnostic for a failed read of ``what``; never carries the token."""
    return Diagnostic(
        id="runtime.cloud.api_error",
        severity="warning",
        message=f"DigitalOcean API request failed while {what}",
        source=SourceLocation(path="DigitalOcean API"),
        suggestion=suggestion,
    )


def list_firewalls(token: str) -> tuple[list[dict[str, Any]], list[Diagnostic], bool]:
    """List every Cloud Firewall in the account (``GET /v2/firewalls``).

    Returns ``(firewalls, diagnostics, ok)``; ``ok`` False means the list is
    incomplete and must not be read as "no firewalls".
    """
    items, ok = _list_paged(token, "/v2/firewalls", "firewalls")
    if not ok:
        return [], [
            _api_error(
                "listing firewalls",
                "check the token and retry; inspect "
                "https://cloud.digitalocean.com/networking/firewalls",
            )
        ], False
    return items, [], True


def get_firewall(
    token: str, firewall_id: str,
) -> tuple[dict[str, Any] | None, list[Diagnostic], bool]:
    """``GET /v2/firewalls/<id>``: look a firewall up BY ID (names are not unique).

    Returns ``(firewall_or_None, diagnostics, ok)``.  404 is ``(None, [], True)``
    (definitively absent); any other failure is ``ok`` False.  The firewall
    dict carries ``status`` and ``pending_changes`` for settle polling.
    """
    status, body = _request("GET", f"/v2/firewalls/{firewall_id}", token)
    if status == 404:
        return None, [], True
    fw = body.get("firewall") if isinstance(body, dict) else None
    if status == 0 or status >= 300 or not isinstance(fw, dict):
        return None, [
            _api_error(
                f"reading firewall {firewall_id}",
                "check the token and retry; inspect "
                "https://cloud.digitalocean.com/networking/firewalls",
            )
        ], False
    return fw, [], True


def put_firewall(
    token: str, firewall_id: str, body: dict[str, Any],
) -> tuple[bool, list[Diagnostic]]:
    """``PUT /v2/firewalls/<id>``: replace a firewall BY ID.

    DigitalOcean's PUT needs the FULL body (``name``, ``inbound_rules``,
    ``outbound_rules``, ``droplet_ids``, ``tags``).  Returns ``(ok, diags)``.
    """
    status, resp = _request("PUT", f"/v2/firewalls/{firewall_id}", token, json=body)
    if status == 0 or status >= 300 or not isinstance(resp.get("firewall"), dict):
        return False, [
            _api_error(
                f"updating firewall {firewall_id} (status {status})",
                "check the token and retry; inspect "
                "https://cloud.digitalocean.com/networking/firewalls",
            )
        ]
    return True, []


def find_vpc(
    token: str, name: str,
) -> tuple[dict[str, Any] | None, list[Diagnostic], bool]:
    """Find the VPC called ``name`` via ``GET /v2/vpcs``.

    Returns ``(vpc_or_None, diagnostics, ok)``.  ``ok`` False means the
    listing failed, so ``None`` does not mean "absent".
    """
    items, ok = _list_paged(token, "/v2/vpcs", "vpcs")
    if not ok:
        return None, [
            _api_error(
                f"listing VPCs (looking for {name!r})",
                "check the token and retry; inspect "
                "https://cloud.digitalocean.com/networking/vpc",
            )
        ], False
    for vpc in items:
        if vpc.get("name") == name:
            return vpc, [], True
    return None, [], True


def list_vpcs(token: str) -> tuple[list[dict[str, Any]], list[Diagnostic], bool]:
    """List every VPC in the account, all regions (``GET /v2/vpcs``).

    Returns ``(vpcs, diagnostics, ok)``; ``ok`` False means the list is
    incomplete and must not be read as "no conflicting VPC".
    """
    items, ok = _list_paged(token, "/v2/vpcs", "vpcs")
    if not ok:
        return [], [
            _api_error(
                "listing VPCs",
                "check the token and retry; inspect "
                "https://cloud.digitalocean.com/networking/vpc",
            )
        ], False
    return items, [], True


VPC_CONSOLE_URL = "https://cloud.digitalocean.com/networking/vpc"


def check_vpc_conflicts(
    vpcs: list[dict[str, Any]],
    *,
    lab_name: str,
    vpc_name: str,
    vpc_cidr: str,
    own_vpc_id: str | None = None,
) -> list[Diagnostic]:
    """Error diagnostics for VPCs that would make the lab's VPC create fail.

    A VPC (any region) conflicts when it has the same name as ``vpc_name`` or
    an ``ip_range`` overlapping ``vpc_cidr`` -- unless it is this lab's own
    VPC: its id equals ``own_vpc_id`` (the ``digitalocean_vpc`` id in the
    per-lab tfstate) and it still has the same name and range, so tofu
    manages it in place.  A same-name, same-range VPC that tofu does not
    manage is an orphan (typically from a failed destroy) and a conflict.
    Tofu can destroy Droplets before hitting the resulting 422, so callers
    run this before any tofu step.
    """
    try:
        want = ipaddress.ip_network(vpc_cidr, strict=False)
    except ValueError:
        return []
    out: list[Diagnostic] = []
    for vpc in vpcs:
        name = vpc.get("name")
        rng = vpc.get("ip_range")
        other: ipaddress.IPv4Network | ipaddress.IPv6Network | None
        try:
            other = (
                ipaddress.ip_network(rng, strict=False)
                if isinstance(rng, str) else None
            )
        except ValueError:
            other = None
        same_name = name == vpc_name
        overlaps = (
            other is not None
            and other.version == want.version
            and other.overlaps(want)
        )
        is_own = (
            own_vpc_id is not None
            and str(vpc.get("id")) == str(own_vpc_id)
            and same_name
            and other == want
        )
        if is_own:
            continue  # tofu manages this one in place
        if not (same_name or overlaps):
            continue
        orphan = same_name and other == want
        if orphan:
            why = (
                f"has the same name {vpc_name!r} and range but is not in this "
                "lab's OpenTofu state; it is likely an orphan from a failed destroy"
            )
        elif same_name:
            why = f"has the same name {vpc_name!r}"
        else:
            why = f"has ip_range {rng} overlapping {vpc_cidr}"
        out.append(
            Diagnostic(
                id="runtime.cloud.vpc_conflict",
                severity="error",
                message=(
                    f"lab {lab_name!r}: existing VPC {name!r} "
                    f"(region {vpc.get('region')!r}, id {vpc.get('id')}) "
                    f"{why}; creating the lab VPC would fail"
                ),
                source=SourceLocation(path="DigitalOcean API"),
                suggestion=(
                    f"delete the unused VPC (id {vpc.get('id')}) at "
                    f"{VPC_CONSOLE_URL}, or change the lab network CIDR "
                    "so it does not overlap any VPC in the account"
                ),
            )
        )
    return out


def check_vpc_would_be_default(
    vpcs: list[dict[str, Any]], *, lab_name: str, region: str,
) -> list[Diagnostic]:
    """Warn when ``region`` has no VPC yet, so the lab's VPC may become its default.

    DigitalOcean may make the first VPC created in a region that region's
    default, and a default VPC cannot be deleted (so ``destroy`` would leave
    it behind).  ``vpcs`` is the full account listing already fetched.
    """
    if any(v.get("region") == region for v in vpcs):
        return []
    return [
        Diagnostic(
            id="runtime.cloud.vpc_would_be_default",
            severity="warning",
            message=(
                f"lab {lab_name!r}: the account has no VPC in region {region!r}; "
                "DigitalOcean may make this lab's VPC the region's default, and "
                "a default VPC cannot be deleted, so destroy would leave it behind"
            ),
            source=SourceLocation(path="DigitalOcean API"),
            suggestion=(
                f"create any resource in {region!r} first, or pick a region "
                "that already has a default VPC"
            ),
        )
    ]


def delete_vpc(token: str, vpc_id: str) -> list[Diagnostic]:
    """DELETE /v2/vpcs/<id>.  204 and 404 are success; anything else warns.

    DigitalOcean refuses to delete a VPC that still has members, so a
    non-2xx right after a Droplet delete can be transient.
    """
    status, _ = _request("DELETE", f"/v2/vpcs/{vpc_id}", token)
    if status in (204, 404):
        return []
    return [
        Diagnostic(
            id="runtime.cloud.api_error",
            severity="warning",
            message=(
                f"DigitalOcean API returned status {status} when deleting "
                f"VPC {vpc_id}; it may still exist"
            ),
            source=SourceLocation(path=f"vpc/{vpc_id}"),
            suggestion=(
                "delete it at https://cloud.digitalocean.com/networking/vpc "
                "(an orphaned VPC is free but blocks re-creating its CIDR)"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Account SSH keys (needed for non-Ubuntu / custom-image Droplets)
# ---------------------------------------------------------------------------

SSH_KEYS_CONSOLE_URL = "https://cloud.digitalocean.com/account/security"


def list_ssh_keys(token: str) -> tuple[list[dict[str, Any]], list[Diagnostic], bool]:
    """List every SSH key registered in the account (``GET /v2/account/keys``).

    Returns ``(keys, diagnostics, ok)``; ``ok`` False means the list is
    incomplete and must not be read as "key not registered".
    """
    items, ok = _list_paged(token, "/v2/account/keys", "ssh_keys")
    if not ok:
        return [], [
            _api_error(
                "listing account SSH keys",
                f"check the token and retry; inspect {SSH_KEYS_CONSOLE_URL}",
            )
        ], False
    return items, [], True


def create_ssh_key(
    token: str, *, name: str, public_key: str,
) -> tuple[int, dict[str, Any] | None]:
    """``POST /v2/account/keys``.  Returns ``(status, key_or_None)``.

    201 with an ``ssh_key`` object is success.  DigitalOcean answers 422 when
    the key is already registered (e.g. a concurrent apply won the race);
    callers re-list on 422.  The public key is not a secret, but nothing
    here logs it either.
    """
    status, body = _request(
        "POST", "/v2/account/keys", token,
        json={"name": name, "public_key": public_key},
    )
    key = body.get("ssh_key") if isinstance(body, dict) else None
    if 200 <= status < 300 and isinstance(key, dict):
        return status, key
    return status, None


def _find_key(keys: list[dict[str, Any]], fingerprint: str) -> dict[str, Any] | None:
    want = fingerprint.lower()
    for key in keys:
        fp = key.get("fingerprint")
        if isinstance(fp, str) and fp.lower() == want:
            return key
    return None


def ensure_ssh_key(
    token: str, *, name: str, public_key: str, fingerprint: str,
) -> tuple[dict[str, Any] | None, bool, str]:
    """Make sure the account has a key with ``fingerprint``.

    Lists ``/v2/account/keys`` and reuses a key with that fingerprint
    (whatever its name); otherwise registers ``public_key`` as ``name``.  A
    422 from the create means someone registered it meanwhile (a concurrent
    apply), so the list is re-read once.

    Returns ``(key_or_None, created, problem)``: ``key`` is the registered
    key dict on success; ``problem`` is a token-free one-line reason on
    failure (``""`` on success).  Never raises.
    """
    keys, _diags, ok = list_ssh_keys(token)
    if not ok:
        return None, False, "listing the account's SSH keys failed (API/transport error)"
    found = _find_key(keys, fingerprint)
    if found is not None:
        return found, False, ""
    status, created = create_ssh_key(token, name=name, public_key=public_key)
    if created is not None:
        return created, True, ""
    if status == 422:
        keys, _diags, ok = list_ssh_keys(token)
        found = _find_key(keys, fingerprint) if ok else None
        if found is not None:
            return found, False, ""
        return None, False, (
            "registering the key was refused (status 422) and it is not in the "
            "account's key list"
            + ("" if ok else " (the re-list failed)")
        )
    return None, False, f"registering the key failed (status {status})"


# ---------------------------------------------------------------------------
# Custom images
# ---------------------------------------------------------------------------

IMAGES_CONSOLE_URL = "https://cloud.digitalocean.com/images/custom_images"


def list_private_images(
    token: str,
) -> tuple[list[dict[str, Any]], list[Diagnostic], bool]:
    """List the account's private images (``GET /v2/images?private=true``).

    Covers custom images (and snapshots/backups, which callers filter out
    by name/tag).  Returns ``(images, diagnostics, ok)``; ``ok`` False means
    the list is incomplete and must not be read as "no images".
    """
    items, ok = _list_paged(
        token, "/v2/images", "images", extra_params={"private": "true"},
    )
    if not ok:
        return [], [
            _api_error(
                "listing private images",
                f"check the token and retry; inspect {IMAGES_CONSOLE_URL}",
            )
        ], False
    return items, [], True


def delete_image(token: str, image_id: int | str) -> list[Diagnostic]:
    """DELETE /v2/images/<id>.  204 and 404 are success; anything else warns."""
    status, _ = _request("DELETE", f"/v2/images/{image_id}", token)
    if status in (204, 404):
        return []
    return [
        Diagnostic(
            id="runtime.cloud.api_error",
            severity="warning",
            message=(
                f"DigitalOcean API returned status {status} when deleting "
                f"image {image_id}; it may still exist"
            ),
            source=SourceLocation(path=f"image/{image_id}"),
            suggestion=(
                f"delete it at {IMAGES_CONSOLE_URL} (a custom image is billed "
                "for its storage)"
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
        if not _is_api_url(next_url):
            return sizes, False, "status_error", status

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
    catalogue: tuple[list[dict[str, Any]], bool, str | None, int] | None = None,
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

    ``catalogue`` is an already-fetched :func:`_fetch_all_sizes` result; pass
    it to check several slugs against ONE paginated fetch.

    The token value is never logged or included in any Diagnostic.
    """
    sizes, complete, incomplete_reason, status = (
        catalogue if catalogue is not None else _fetch_all_sizes(token)
    )

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
    """Extract ``{name, id, status, public_ipv4, private_ipv4}`` from a raw
    droplet dict.

    ``public_ipv4`` / ``private_ipv4`` are the first ``networks.v4`` entries
    whose ``type`` is ``"public"`` / ``"private"`` (the private one is the
    VPC address), or ``None`` when absent.
    """
    networks_v4: list[dict[str, Any]] = (
        (d.get("networks") or {}).get("v4") or []
    )
    public_ipv4: str | None = None
    private_ipv4: str | None = None
    for net in networks_v4:
        if public_ipv4 is None and net.get("type") == "public":
            public_ipv4 = net.get("ip_address")
        elif private_ipv4 is None and net.get("type") == "private":
            private_ipv4 = net.get("ip_address")
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "status": d.get("status"),
        "public_ipv4": public_ipv4,
        "private_ipv4": private_ipv4,
    }


__all__ = [
    "CONSOLE_URL",
    "DEFAULT_TOKEN_ENV",
    "IMAGES_CONSOLE_URL",
    "SSH_KEYS_CONSOLE_URL",
    "VPC_CONSOLE_URL",
    "check_size_region_availability",
    "check_vpc_would_be_default",
    "create_ssh_key",
    "put_firewall",
    "delete_droplet",
    "delete_image",
    "check_vpc_conflicts",
    "delete_vpc",
    "droplet_summary",
    "ensure_ssh_key",
    "get_firewall",
    "find_vpc",
    "list_droplets_by_tag",
    "list_firewalls",
    "list_private_images",
    "list_ssh_keys",
    "list_vpcs",
    "read_token",
    "token_env_name",
    "token_present",
    "verify_token",
]
