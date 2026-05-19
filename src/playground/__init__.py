"""playground — lab-operating-system for local infrastructure experiments.

Current package areas:

- :mod:`playground.config` — YAML discovery, loader, merge.
- :mod:`playground.models` — typed config and resolved-lab models.
- :mod:`playground.validation` — schema/reference/budget validators.
- :mod:`playground.state` — :class:`StateStore` over ``.playground/``.
- :mod:`playground.events` — in-process :class:`EventBus`.
- :mod:`playground.runs` — :class:`OperationRun` creation and finalize.
- :mod:`playground.logging` — JSONL/human/summary/status subscribers.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("playground")
except PackageNotFoundError:
    __version__ = "0.0.0+uninstalled"

__all__ = ["__version__"]
