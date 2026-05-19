"""In-process event bus + JSONL writer for operation runs.

System design's §"Reactive Operation Events" anchors what this should
grow into: many producers (adapters, ansible runs, docker ops, doctor,
cache), many consumers (log writers, run summary builders, CLI stream,
TUI views, future websockets). The minimum useful slice is a synchronous
publish-subscribe loop within one process plus a JSONL writer that
persists events under ``.playground/runs/<run-id>/events.jsonl`` so the
run record is reconstructable after process exit.

External brokers (Redis, NATS, etc.) are explicitly out of scope per
``docs/product/requirements.md`` §5.11.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from playground.models.base import StrictModel

EventType = Literal[
    "operation_started",
    "step_started",
    "step_finished",
    "operation_finished",
    "log_line",
]
"""Event types emitted by the platform.

- ``operation_started`` / ``operation_finished`` bracket every run.
- ``step_started`` / ``step_finished`` bracket every subprocess
  invocation; ``step_finished.payload['exit_code']`` carries the
  process exit code.
- ``log_line`` carries one line of streamed subprocess stdout/stderr
  in ``payload['line']``. Producers (see
  :func:`playground.backend.local_libvirt.apply.run_tofu_apply` etc.)
  emit one ``log_line`` per line of captured output so consumers
  (TUI live panes, future websocket bridges) can render progress
  without waiting for the subprocess to exit.
"""


class OperationEvent(StrictModel):
    """One observable moment in an operation run's lifecycle."""

    run_id: str
    timestamp: str
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


Subscriber = Callable[[OperationEvent], None]


class EventBus:
    """Synchronous in-process publish-subscribe.

    Subscribers are called in registration order on the publishing
    thread. Exceptions in one subscriber do not stop other subscribers
    from receiving the event — they are collected on the bus and the
    caller can inspect them via :attr:`errors`.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self.errors: list[BaseException] = []

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def publish(
        self,
        run_id: str,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
    ) -> OperationEvent:
        event = OperationEvent(
            run_id=run_id,
            timestamp=datetime.now(UTC).replace(microsecond=0).isoformat(),
            type=event_type,
            payload=dict(payload or {}),
        )
        for subscriber in self._subscribers:
            try:
                subscriber(event)
            except BaseException as exc:  # noqa: BLE001 — surface, don't suppress
                self.errors.append(exc)
        return event


class JsonlWriter:
    """Append each event as one JSON line to ``run_dir/events.jsonl``."""

    def __init__(self, run_dir: Path) -> None:
        self._path = run_dir / "events.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: OperationEvent) -> None:
        with self._path.open("a") as fh:
            fh.write(event.model_dump_json(exclude_none=True) + "\n")

    @property
    def path(self) -> Path:
        return self._path


@contextmanager
def operation_events(
    bus: EventBus, run_id: str, operation: str, lab: str
) -> Iterator[Callable[[str], None]]:
    """Bracket a block with ``operation_started`` / ``operation_finished``.

    The ``operation_finished`` event's payload includes ``status``
    (``succeeded`` / ``failed``), which the caller sets by passing it
    to the yielded callable.
    """
    bus.publish(
        run_id, "operation_started", {"operation": operation, "lab": lab}
    )
    finish_status: dict[str, str] = {"status": "succeeded"}

    def set_status(status: str) -> None:
        finish_status["status"] = status

    try:
        yield set_status
    finally:
        bus.publish(run_id, "operation_finished", {**finish_status})


__all__ = [
    "EventBus",
    "EventType",
    "JsonlWriter",
    "OperationEvent",
    "Subscriber",
    "operation_events",
]
