"""Read-only Textual TUI over the existing CLI primitives.

First slice scope: lab list on the left, lab detail (resolved metadata +
plan + status) on the right. Selecting a lab in the list refreshes the
detail pane. No mutating actions yet — apply / destroy stay in the CLI
until §7's event stream grows richer per-resource detail.

The detail pane reuses :func:`playground.config.resolver.resolve_lab`,
:func:`playground.planner.render_plan`, and
:func:`playground.backend.local_libvirt.query_status` so there's no
parallel business logic.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, ListItem, ListView, Static

from playground.backend.local_libvirt import query_status
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.planner import render_plan
from playground.validation import validate as validate_loaded_config


class PlaygroundTui(App[None]):
    """Read-only operator console."""

    CSS = """
    #lab-list {
        width: 35%;
        border: solid $primary;
    }
    #detail {
        width: 65%;
        border: solid $primary;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config_dir: Path = Path("config"),
        tofu_dir: Path = Path("tofu"),
    ) -> None:
        super().__init__()
        self.config_dir = config_dir
        self.tofu_dir = tofu_dir
        self._lab_names: list[str] = []
        self.detail_text = ""

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield ListView(id="lab-list")
            with VerticalScroll(id="detail"):
                yield Static("Select a lab on the left.", id="detail-text")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "playground"
        self.sub_title = str(self.config_dir)
        self._reload_lab_list()

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._reload_lab_list()

    def _reload_lab_list(self) -> None:
        loaded, _ = load_config(self.config_dir)
        self._lab_names = sorted(loaded.labs)
        listview = self.query_one("#lab-list", ListView)
        listview.clear()
        for name in self._lab_names:
            listview.append(ListItem(Static(name), id=f"lab-{name}"))
        if self._lab_names:
            listview.index = 0
            self._render_lab(self._lab_names[0])
        else:
            self._set_detail("No labs configured.")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None:
            return
        name = (event.item.id or "lab-").removeprefix("lab-")
        if name:
            self._render_lab(name)

    # ------------------------------------------------------------------
    # Detail rendering
    # ------------------------------------------------------------------

    def _render_lab(self, name: str) -> None:
        loaded, parse_diagnostics = load_config(self.config_dir)
        validation_diagnostics = validate_loaded_config(loaded)
        if name not in loaded.labs:
            self._set_detail(f"Lab {name!r} disappeared from disk.")
            return

        try:
            resolved = resolve_lab(loaded, name)
        except (KeyError, ValueError) as exc:
            self._set_detail(f"Could not resolve {name!r}: {exc}")
            return

        warnings = [d for d in validation_diagnostics if d.severity == "warning"]
        plan = render_plan(resolved, warnings=warnings)
        status, _ = query_status(resolved, self.tofu_dir)

        lines = [
            f"# {resolved.lab_name}",
            f"backend: {resolved.backend}",
            f"offline: {resolved.offline}",
            "",
            "## Status",
            f"  {status.provisioned_vms} of {status.expected_vms} VMs provisioned",
        ]
        for vm in status.vms:
            marker = "+" if vm.state == "provisioned" else "-"
            ip = vm.ip or "—"
            lines.append(f"  {marker} {vm.name}  role={vm.role}  ip={ip}")
        lines += ["", "## Plan"]
        for action in plan.actions:
            lines.append(f"  + {action.resource_type:<8}  {action.name}  {action.summary}")
        lines += [
            "",
            "## Budget",
            (
                f"  totals: {plan.budget.vms} VMs / {plan.budget.vcpu} vCPU / "
                f"{plan.budget.memory_mb} MiB / {plan.budget.disk_gb} GiB / "
                f"{plan.budget.containers} workloads"
            ),
            f"  fits: {'yes' if plan.budget.fits else 'NO'}",
        ]
        all_diag = parse_diagnostics + validation_diagnostics
        if all_diag:
            lines += ["", "## Diagnostics"]
            for d in all_diag:
                lines.append(f"  [{d.severity.upper()}] {d.id}: {d.message}")
        self._set_detail("\n".join(lines))

    def _set_detail(self, text: str) -> None:
        self.detail_text = text
        self.query_one("#detail-text", Static).update(text)


def run_app(
    config_dir: Path = Path("config"),
    tofu_dir: Path = Path("tofu"),
) -> None:
    """Entry point — used by the ``playground tui`` CLI command."""
    PlaygroundTui(config_dir=config_dir, tofu_dir=tofu_dir).run()


__all__ = ["PlaygroundTui", "run_app"]
