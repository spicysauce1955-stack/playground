"""Textual-based operator TUI.

Per ``docs/product/requirements.md`` §5.8 the TUI consumes the same
config/state/operation/event/diagnostic models as the CLI — it adds a
human-friendly browsing layer, not parallel business logic.

The Textual dependency is optional: ``pip install -e .[tui]``. The
:func:`run_app` entry point lazy-imports the implementation so the rest
of the CLI doesn't require Textual.
"""

from playground.tui.app import run_app

__all__ = ["run_app"]
