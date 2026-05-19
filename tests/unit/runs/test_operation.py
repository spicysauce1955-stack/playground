"""Tests for the operation run record helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from playground.runs import (
    OperationRun,
    StepResult,
    allocate_run_id,
    finish_run,
    start_run,
)


def test_allocate_run_id_is_sortable_and_descriptive() -> None:
    now = datetime(2026, 5, 19, 12, 34, 56, tzinfo=UTC)
    rid = allocate_run_id("apply", "generic-infra", now=now)

    assert rid == "20260519T123456Z-apply-generic-infra"
    # Sortable lexically — two ids one second apart sort in real-time order.
    later = allocate_run_id(
        "apply",
        "generic-infra",
        now=datetime(2026, 5, 19, 12, 34, 57, tzinfo=UTC),
    )
    assert rid < later


def test_start_run_creates_directory_and_running_record(tmp_path: Path) -> None:
    run, run_dir = start_run(tmp_path, "apply", "generic-infra")

    assert run.status == "running"
    assert run.operation == "apply"
    assert run.lab == "generic-infra"
    assert run.finished_at is None
    assert run_dir.is_dir()
    assert (run_dir / "logs").is_dir()
    record = json.loads((run_dir / "run.json").read_text())
    assert record["run_id"] == run.run_id
    assert record["status"] == "running"


def test_finish_run_succeeded(tmp_path: Path) -> None:
    run, run_dir = start_run(tmp_path, "apply", "generic-infra")
    step = StepResult(
        name="tofu-apply",
        command=["tofu", "apply"],
        exit_code=0,
        log_path=str(run_dir / "logs" / "tofu-apply.log"),
        started_at="2026-05-19T12:34:56+00:00",
        finished_at="2026-05-19T12:34:57+00:00",
    )

    finished = finish_run(run, run_dir, status="succeeded", steps=[step], summary="ok")

    assert finished.status == "succeeded"
    assert finished.finished_at is not None
    assert finished.steps == [step]
    assert finished.summary == "ok"
    on_disk = json.loads((run_dir / "run.json").read_text())
    assert on_disk["status"] == "succeeded"
    assert on_disk["steps"][0]["name"] == "tofu-apply"


def test_finish_run_failed_records_status(tmp_path: Path) -> None:
    run, run_dir = start_run(tmp_path, "apply", "generic-infra")
    step = StepResult(
        name="tofu-apply",
        command=["tofu", "apply"],
        exit_code=2,
        log_path=str(run_dir / "logs" / "tofu-apply.log"),
        started_at="2026-05-19T12:34:56+00:00",
        finished_at="2026-05-19T12:34:58+00:00",
    )

    finished = finish_run(
        run, run_dir, status="failed", steps=[step], summary="tofu apply failed"
    )

    assert finished.status == "failed"
    assert finished.summary == "tofu apply failed"


def test_operation_run_round_trips_through_json(tmp_path: Path) -> None:
    run, run_dir = start_run(tmp_path, "apply", "generic-infra")
    finished = finish_run(run, run_dir, status="succeeded", steps=[], summary=None)

    rebuilt = OperationRun.model_validate_json((run_dir / "run.json").read_text())

    assert rebuilt == finished
