"""Merge DigitalOcean provider settings from ProviderConfig + lab overrides.

This is the single public function that every caller should use to build
the resolved ``provider_settings`` dict passed to :func:`build_do_plan`.
It lives here rather than in ``plan.py`` to keep the plan module I/O-free.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from playground.config.loader import load_config
from playground.models.resolved import ResolvedLab

if TYPE_CHECKING:
    from playground.config.loader import LoadedConfig


def merge_provider_settings(
    resolved: ResolvedLab,
    *,
    config_dir: Path | None = None,
    loaded: LoadedConfig | None = None,
) -> dict[str, Any]:
    """Return the merged provider settings for a cloud-digitalocean lab.

    Loads the ProviderConfig spec defaults from *config_dir* (if given)
    and overlays the lab-level ``spec.providers[backend]`` dict on top.
    Falls back to just the lab overrides on any error (missing config_dir,
    load error, provider not found in config).

    ``driver`` and ``token_env`` are stripped from the result because they
    are not OpenTofu variable inputs.

    :param resolved: Fully resolved, backend-neutral lab model.
    :param config_dir: Path to the config directory (e.g. ``config/``).
        ``None`` means skip loading provider defaults and use lab overrides
        only.  Ignored when *loaded* is provided.
    :param loaded: Already-loaded :class:`~playground.config.loader.LoadedConfig`.
        When provided, *config_dir* is ignored and the config is not re-loaded,
        avoiding redundant I/O when the caller already holds a loaded config.
    :returns: Merged dict suitable for passing to :func:`build_do_plan` as
        ``provider_settings``.
    """
    lab_overrides: dict[str, Any] = dict(
        resolved.providers.get(resolved.backend, {})
    )
    base: dict[str, Any] = {}

    if loaded is not None:
        # Caller supplied an already-loaded config — use it directly.
        try:
            provider_cfg = loaded.providers.get(resolved.backend)
            if provider_cfg is not None:
                try:
                    base = dict(provider_cfg.spec.model_dump())
                except Exception:  # noqa: BLE001
                    base = {}
        except Exception:  # noqa: BLE001
            base = {}
    elif config_dir is not None:
        try:
            _loaded, _diags = load_config(config_dir)
            provider_cfg = _loaded.providers.get(resolved.backend)
            if provider_cfg is not None:
                try:
                    base = dict(provider_cfg.spec.model_dump())
                except Exception:  # noqa: BLE001
                    base = {}
        except Exception:  # noqa: BLE001
            base = {}

    base.update(lab_overrides)
    # Belt-and-braces: strip keys that are not tofu inputs.
    base.pop("driver", None)
    base.pop("token_env", None)
    return base


__all__ = ["merge_provider_settings"]
