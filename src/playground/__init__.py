"""playground — lab-operating-system for local infrastructure experiments.

The package is split by responsibility, mirroring the team ownership in
``ai/engineering/team_work_plan.md``.

Team A (this branch) owns:

- :mod:`playground.config` — YAML discovery, loader, merge.
- :mod:`playground.models` — typed contract models (see
  ``ai/architecture/shared_contracts.md``).
- :mod:`playground.validation` — schema/reference/budget validators.
- :mod:`playground.state` — :class:`StateStore` over ``.playground/``.
- :mod:`playground.events` — in-process :class:`EventBus`.
- :mod:`playground.runs` — :class:`OperationRun` creation and finalize.
- :mod:`playground.logging` — JSONL/human/summary/status subscribers.

Team B owns ``playground.providers``, ``playground.backends``,
``playground.doctor``, ``playground.runtime``.

Team C owns ``playground.cli``, ``playground.tui``, ``playground.output``,
``playground.commands``.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("playground")
except PackageNotFoundError:
    __version__ = "0.0.0+uninstalled"

__all__ = ["__version__"]
