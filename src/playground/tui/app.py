"""Textual TUI over the existing CLI primitives.

Two operator interactions are wired up:

- **Browsing** (read-only): selecting a lab on the left refreshes the
  right pane with the resolved metadata, observed status, planned
  actions, budget totals, and validation diagnostics. Every panel
  delegates to the same primitives the CLI calls
  (:func:`config.resolver.resolve_lab`,
  :func:`planner.render_plan`,
  :func:`backend.local_libvirt.query_status`).
- **Mutating actions** (``a`` / ``d`` keybindings): run apply / destroy
  through :func:`backend.local_libvirt.runner.execute_apply` /
  ``execute_destroy``. Each action runs in a background worker thread
  so the foreground event loop stays responsive. Subprocess output
  arrives via the :class:`EventBus` as ``log_line`` events; the TUI
  subscribes a bridge that calls
  :meth:`textual.app.App.call_from_thread` to append to a live log
  pane on the main thread.

The runs viewer (``v`` keybinding) opens a screen listing recorded
runs and a detail screen rendering ``events.jsonl`` as a timeline —
see :class:`RunsScreen`.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, ListItem, ListView, Static

from playground.backend.local_libvirt import (
    execute_apply,
    execute_destroy,
    query_status,
)
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus, OperationEvent
from playground.planner import render_plan
from playground.runs import OperationRun
from playground.validation import validate as validate_loaded_config


class _ConfirmScreen(ModalScreen[bool]):
    """Tiny yes/no modal — used before mutating actions."""

    BINDINGS = [
        Binding("y", "confirm(True)", "Yes"),
        Binding("n", "confirm(False)", "No"),
        Binding("escape", "confirm(False)", "Cancel"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        yield Static(self.prompt + "\n\n[y] Yes   [n/Esc] No", id="confirm-prompt")

    def action_confirm(self, choice: bool) -> None:
        self.dismiss(choice)


class _LogPane(VerticalScroll):
    """Append-only log pane for live ``log_line`` events."""

    def __init__(self) -> None:
        super().__init__(id="log-pane")
        self._body = Static("", id="log-body")
        self._lines: list[str] = []

    def compose(self) -> ComposeResult:
        yield self._body

    def append(self, line: str) -> None:
        self._lines.append(line)
        # Keep the pane bounded at ~1000 lines so a chatty subprocess
        # doesn't balloon memory. Ample for a live view; full output
        # is on disk in the run's log file.
        if len(self._lines) > 1000:
            self._lines = self._lines[-1000:]
        self._body.update("\n".join(self._lines))
        self.scroll_end(animate=False)

    def clear(self) -> None:
        self._lines = []
        self._body.update("")


class PlaygroundTui(App[None]):
    """Operator console — read-only browsing + apply/destroy with live logs."""

    CSS = """
    #lab-list {
        width: 30%;
        border: solid $primary;
    }
    #detail {
        width: 70%;
        border: solid $primary;
        padding: 0 1;
    }
    #log-pane {
        height: 40%;
        border: solid $accent;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("a", "apply", "Apply"),
        Binding("d", "destroy", "Destroy"),
        Binding("v", "open_runs", "Runs"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config_dir: Path = Path("config"),
        tofu_dir: Path = Path("tofu"),
        ansible_dir: Path = Path("ansible"),
        state_dir: Path = Path(".playground"),
    ) -> None:
        super().__init__()
        self.config_dir = config_dir
        self.tofu_dir = tofu_dir
        self.ansible_dir = ansible_dir
        self.state_dir = state_dir
        self._lab_names: list[str] = []
        self.detail_text = ""
        self.last_run: OperationRun | None = None
        self._busy = False

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield ListView(id="lab-list")
            with VerticalScroll(id="detail"):
                yield Static("Select a lab on the left.", id="detail-text")
        yield _LogPane()
        yield Footer()

    def on_mount(self) -> None:
        self.title = "playground"
        self.sub_title = str(self.config_dir)
        self._reload_lab_list()

    # ------------------------------------------------------------------
    # Read-side actions
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._reload_lab_list()

    def action_open_runs(self) -> None:
        self.push_screen(RunsScreen(self.state_dir))

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

    def _selected_lab(self) -> str | None:
        listview = self.query_one("#lab-list", ListView)
        if listview.index is None or listview.index >= len(self._lab_names):
            return None
        return self._lab_names[listview.index]

    # ------------------------------------------------------------------
    # Mutating actions — apply / destroy in a worker thread
    # ------------------------------------------------------------------

    def action_apply(self) -> None:
        lab = self._selected_lab()
        if lab is None or self._busy:
            return

        def on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._run_apply(lab)

        self.push_screen(_ConfirmScreen(f"Apply lab {lab!r}?"), on_confirm)

    def action_destroy(self) -> None:
        lab = self._selected_lab()
        if lab is None or self._busy:
            return

        def on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._run_destroy(lab)

        self.push_screen(
            _ConfirmScreen(f"Destroy lab {lab!r}? This tears down the VMs."),
            on_confirm,
        )

    def _run_apply(self, lab: str) -> None:
        self._start_operation("apply", lab)

        def worker() -> None:
            loaded, _ = load_config(self.config_dir)
            resolved = resolve_lab(loaded, lab)
            bus = self._build_bus()
            run, _diags = execute_apply(
                resolved=resolved,
                state_dir=self.state_dir,
                tofu_dir=self.tofu_dir,
                ansible_dir=self.ansible_dir,
                config_dir=self.config_dir,
                bus=bus,
            )
            self.call_from_thread(self._finish_operation, run)

        threading.Thread(target=worker, daemon=True).start()

    def _run_destroy(self, lab: str) -> None:
        self._start_operation("destroy", lab)

        def worker() -> None:
            loaded, _ = load_config(self.config_dir)
            resolved = resolve_lab(loaded, lab)
            bus = self._build_bus()
            run, _diags = execute_destroy(
                resolved=resolved,
                state_dir=self.state_dir,
                tofu_dir=self.tofu_dir,
                bus=bus,
            )
            self.call_from_thread(self._finish_operation, run)

        threading.Thread(target=worker, daemon=True).start()

    def _start_operation(self, op: str, lab: str) -> None:
        self._busy = True
        pane = self.query_one("#log-pane", _LogPane)
        pane.clear()
        pane.append(f"--- {op} {lab} ---")

    def _build_bus(self) -> EventBus:
        bus = EventBus()
        pane = self.query_one("#log-pane", _LogPane)

        def bridge(event: OperationEvent) -> None:
            self.call_from_thread(_handle_event, pane, event)

        bus.subscribe(bridge)
        return bus

    def _finish_operation(self, run: OperationRun | None) -> None:
        self._busy = False
        self.last_run = run
        pane = self.query_one("#log-pane", _LogPane)
        if run is None:
            pane.append("--- aborted before run: pre-flight failed ---")
            return
        pane.append(
            f"--- finished: {run.run_id} (status={run.status}) ---"
        )
        # Refresh the detail pane so observed status reflects the new VMs.
        selected = self._selected_lab()
        if selected:
            self._render_lab(selected)


def _handle_event(pane: _LogPane, event: OperationEvent) -> None:
    """Render one event on the main thread (called via call_from_thread)."""
    if event.type == "log_line":
        line = event.payload.get("line", "")
        pane.append(line)
    elif event.type == "step_started":
        pane.append(f"--- step: {event.payload.get('step', '?')} ---")
    elif event.type == "step_finished":
        exit_code = event.payload.get("exit_code", "?")
        pane.append(f"--- step done (exit {exit_code}) ---")


# ---------------------------------------------------------------------------
# Runs viewer
# ---------------------------------------------------------------------------


class RunsScreen(Screen[None]):
    """List past runs; pressing Enter opens the run detail screen."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("enter", "open", "Open"),
    ]

    def __init__(self, state_dir: Path) -> None:
        super().__init__()
        self.state_dir = state_dir
        self._run_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield ListView(id="runs-list")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "playground: runs"
        runs_dir = self.state_dir / "runs"
        listview = self.query_one("#runs-list", ListView)
        if not runs_dir.is_dir():
            listview.append(ListItem(Static("No runs recorded yet.")))
            return
        for entry in sorted(runs_dir.iterdir(), reverse=True):
            record = entry / "run.json"
            if not record.is_file():
                continue
            try:
                run = OperationRun.model_validate_json(record.read_text())
            except (ValueError, OSError):
                continue
            self._run_ids.append(run.run_id)
            listview.append(
                ListItem(
                    Static(
                        f"{run.run_id}  {run.operation:<7}  {run.status:<9}  "
                        f"start={run.started_at}  end={run.finished_at or '—'}"
                    ),
                    id=f"run-{run.run_id}",
                )
            )
        if self._run_ids:
            listview.index = 0

    def action_open(self) -> None:
        listview = self.query_one("#runs-list", ListView)
        if listview.index is None or listview.index >= len(self._run_ids):
            return
        run_id = self._run_ids[listview.index]
        self.app.push_screen(RunDetailScreen(self.state_dir, run_id))


class RunDetailScreen(Screen[None]):
    """One run's record + events.jsonl rendered as a timeline."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, state_dir: Path, run_id: str) -> None:
        super().__init__()
        self.state_dir = state_dir
        self.run_id = run_id
        self.detail_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll():
            yield Static("", id="run-detail")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"playground: run {self.run_id}"
        run_dir = self.state_dir / "runs" / self.run_id
        record_path = run_dir / "run.json"
        if not record_path.is_file():
            text = f"Run {self.run_id} not found at {record_path}."
            self._update(text)
            return
        run = OperationRun.model_validate_json(record_path.read_text())
        lines = [
            f"# {run.run_id}",
            f"operation: {run.operation}",
            f"lab:       {run.lab}",
            f"status:    {run.status}",
            f"started:   {run.started_at}",
        ]
        if run.finished_at:
            lines.append(f"finished:  {run.finished_at}")
        if run.summary:
            lines.append(f"summary:   {run.summary}")
        if run.steps:
            lines += ["", "## Steps"]
            for step in run.steps:
                lines.append(
                    f"  - {step.name}: exit {step.exit_code}  (log {step.log_path})"
                )
        events_path = run_dir / "events.jsonl"
        if events_path.exists():
            lines += ["", "## Timeline"]
            for raw in events_path.read_text().splitlines():
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                payload = event.get("payload", {})
                if event["type"] == "log_line":
                    lines.append(
                        f"  {event['timestamp']}  {payload.get('step', '?')}: "
                        f"{payload.get('line', '')}"
                    )
                else:
                    detail = ", ".join(f"{k}={v}" for k, v in payload.items())
                    lines.append(
                        f"  {event['timestamp']}  {event['type']}  {detail}"
                    )
        self._update("\n".join(lines))

    def _update(self, text: str) -> None:
        self.detail_text = text
        self.query_one("#run-detail", Static).update(text)


def run_app(
    config_dir: Path = Path("config"),
    tofu_dir: Path = Path("tofu"),
    ansible_dir: Path = Path("ansible"),
    state_dir: Path = Path(".playground"),
) -> None:
    """Entry point — used by the ``playground tui`` CLI command."""
    PlaygroundTui(
        config_dir=config_dir,
        tofu_dir=tofu_dir,
        ansible_dir=ansible_dir,
        state_dir=state_dir,
    ).run()


__all__ = ["PlaygroundTui", "RunDetailScreen", "RunsScreen", "run_app"]
