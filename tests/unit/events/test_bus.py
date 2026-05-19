"""Tests for the in-process event bus and JSONL writer."""

from __future__ import annotations

import json
from pathlib import Path

from playground.events import (
    EventBus,
    JsonlWriter,
    OperationEvent,
    operation_events,
)


def test_publish_invokes_each_subscriber_in_order() -> None:
    bus = EventBus()
    received: list[str] = []
    bus.subscribe(lambda e: received.append(f"a:{e.type}"))
    bus.subscribe(lambda e: received.append(f"b:{e.type}"))

    bus.publish("run-1", "operation_started", {"operation": "apply"})

    assert received == ["a:operation_started", "b:operation_started"]


def test_subscriber_exception_does_not_block_others() -> None:
    bus = EventBus()

    def angry(_event: OperationEvent) -> None:
        raise RuntimeError("kaboom")

    received: list[OperationEvent] = []
    bus.subscribe(angry)
    bus.subscribe(received.append)

    bus.publish("run-1", "operation_started")

    assert len(received) == 1
    assert len(bus.errors) == 1
    assert isinstance(bus.errors[0], RuntimeError)


def test_jsonl_writer_appends_one_event_per_line(tmp_path: Path) -> None:
    writer = JsonlWriter(tmp_path)
    bus = EventBus()
    bus.subscribe(writer)

    bus.publish("run-1", "operation_started", {"operation": "apply"})
    bus.publish("run-1", "step_started", {"step": "tofu-apply"})
    bus.publish("run-1", "step_finished", {"step": "tofu-apply", "exit_code": 0})
    bus.publish("run-1", "operation_finished", {"status": "succeeded"})

    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 4
    events = [json.loads(line) for line in lines]
    assert [e["type"] for e in events] == [
        "operation_started",
        "step_started",
        "step_finished",
        "operation_finished",
    ]
    assert all(e["run_id"] == "run-1" for e in events)
    assert events[2]["payload"]["exit_code"] == 0


def test_operation_events_brackets_lifecycle(tmp_path: Path) -> None:
    bus = EventBus()
    writer = JsonlWriter(tmp_path)
    bus.subscribe(writer)

    with operation_events(bus, "run-1", "apply", "demo") as set_status:
        bus.publish("run-1", "step_started", {"step": "tofu-apply"})
        set_status("failed")

    events = [
        json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert [e["type"] for e in events] == [
        "operation_started",
        "step_started",
        "operation_finished",
    ]
    assert events[0]["payload"] == {"operation": "apply", "lab": "demo"}
    assert events[-1]["payload"] == {"status": "failed"}


def test_operation_events_default_status_is_succeeded(tmp_path: Path) -> None:
    bus = EventBus()
    bus.subscribe(JsonlWriter(tmp_path))

    with operation_events(bus, "run-1", "apply", "demo"):
        pass

    events = [
        json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["payload"] == {"status": "succeeded"}
