"""The Redroid image tag must be immutable.

`redroid/redroid:11.0.0-latest` is a MOVING tag: upstream repoints it when a
new 11.0.0 build ships. Two applies weeks apart would then produce different
Android builds from identical committed config, which quietly undermines
every reproducibility claim this repo makes about labs -- and makes an
app-testing result impossible to attribute to a specific Android build.

Redroid publishes date-stamped immutable tags (11.0.0-240527) alongside the
moving ones. Pin those.
"""

from __future__ import annotations

import re
from pathlib import Path

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS = REPO_ROOT / "ansible" / "roles" / "redroid" / "defaults" / "main.yml"

_yaml = YAML(typ="safe")


def _image() -> str:
    return str(_yaml.load(DEFAULTS.read_text())["redroid_image"])


def test_image_is_explicitly_tagged() -> None:
    image = _image()
    assert ":" in image.rsplit("/", 1)[-1], (
        f"{image!r} has no tag, so it resolves to :latest at pull time"
    )


def test_image_tag_is_not_a_moving_tag() -> None:
    tag = _image().rsplit(":", 1)[-1]
    assert tag != "latest", "bare :latest is a moving tag"
    assert not tag.endswith("-latest"), (
        f"{tag!r} is a moving tag -- upstream repoints '<version>-latest' on "
        "every new build. Pin the date-stamped tag it currently points at "
        "(e.g. 11.0.0-240527) so a re-apply reproduces the same Android build."
    )


def test_pinned_tag_is_date_stamped() -> None:
    """Redroid's immutable tags look like 11.0.0-240527 or 11.0.0_r221023."""
    tag = _image().rsplit(":", 1)[-1]
    assert re.fullmatch(r"\d+\.\d+\.\d+(_64only)?[-_]r?\d{6}", tag), (
        f"{tag!r} does not look like an immutable date-stamped redroid tag"
    )
