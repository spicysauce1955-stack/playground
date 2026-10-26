"""Suite-wide guards.

No test may reach the real DigitalOcean API, whatever the developer's shell
exports.  Two guards, applied to every test:

1. ``DIGITALOCEAN_TOKEN`` is removed from the environment (subprocesses
   inherit that too), so a test that forgets to stub the API can never use
   a real token.  Tests that need a token set a fake one themselves.
2. ``playground.backend.cloud_digitalocean.do._request`` -- the single HTTP
   seam every API wrapper goes through -- is replaced.  An unstubbed call
   answers like a transport failure (``(0, {})``, so code that degrades
   gracefully does not hide it) and then FAILS the test at teardown, naming
   the request.  Tests that exercise the API monkeypatch ``_request`` (or a
   wrapper above it) themselves, which overrides this.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from playground.backend.cloud_digitalocean import do as _do


@pytest.fixture(autouse=True)
def _no_real_digitalocean_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(_do.DEFAULT_TOKEN_ENV, raising=False)
    reached: list[str] = []

    def refuse(method: str, path: str, token: str, **_: Any) -> tuple[int, dict[str, Any]]:
        reached.append(f"{method} {path}")
        return 0, {}

    monkeypatch.setattr(_do, "_request", refuse)
    yield
    if reached:
        pytest.fail(
            "test reached the real DigitalOcean HTTP seam (stub it): "
            + ", ".join(reached),
            pytrace=False,
        )
