"""Guards on the committed DigitalOcean OpenTofu root assets.

Regression: a non-ASCII em-dash in a comment in ``cloud_init.cfg`` made
DigitalOcean's ConfigDrive datasource reject the whole cloud-config
("empty cloud config"), so the ``users:`` block never ran, the ``ubuntu``
user was never created, and SSH key auth failed. A live apply caught it.
Keep the user-data ASCII-only so the YAML loader never chokes on a byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DO_TOFU_DIR = _REPO_ROOT / "tofu" / "cloud_digitalocean"


def test_cloud_init_cfg_is_ascii_only() -> None:
    cfg = _DO_TOFU_DIR / "cloud_init.cfg"
    raw = cfg.read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:  # pragma: no cover - failure path
        bad = raw[exc.start : exc.end]
        line = raw[: exc.start].count(b"\n") + 1
        pytest.fail(
            f"{cfg} contains non-ASCII byte(s) {bad!r} at line {line}; "
            "DigitalOcean's ConfigDrive YAML loader discards the entire "
            "cloud-config on a non-ASCII byte. Keep this file ASCII-only."
        )
