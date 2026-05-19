"""Operation run records — the minimum §5.10 requires from mutating ops.

An :class:`OperationRun` is a JSON-serializable record of one mutating
command's lifecycle (start, end, status, summary, per-step exit codes).
It lives at ``.playground/runs/<run-id>/run.json`` next to per-step log
files captured by the backend adapter.

Today's surface is intentionally small:

- :func:`allocate_run_id` produces a sortable, human-readable id
- :func:`start_run` / :func:`finish_run` write the lifecycle markers
- :class:`StepResult` captures one subprocess's exit code + log path

A future slice will add the in-process event bus and JSONL event log
described in ``docs/system_design.md`` §"Operation Runner And Events".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from playground.models.base import StrictModel

RunStatus = Literal["running", "succeeded", "failed"]


class StepResult(StrictModel):
    """One subprocess invocation within an operation run."""

    name: str
    command: list[str]
    exit_code: int
    log_path: str
    started_at: str
    finished_at: str


class OperationRun(StrictModel):
    """JSON-serialized lifecycle marker for one mutating command."""

    run_id: str
    operation: Literal["apply", "destroy", "stop"]
    lab: str
    status: RunStatus
    started_at: str
    finished_at: str | None = None
    steps: list[StepResult] = Field(default_factory=list)
    summary: str | None = None


def allocate_run_id(operation: str, lab: str, now: datetime | None = None) -> str:
    """Return a sortable timestamp-based id.

    Format: ``YYYYMMDDTHHmmssZ-<operation>-<lab>``. Sortable
    lexically, readable at a glance, collision-resistant within
    one-second granularity for a single lab+operation.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{operation}-{lab}"


def start_run(
    runs_dir: Path,
    operation: Literal["apply", "destroy", "stop"],
    lab: str,
    run_id: str | None = None,
) -> tuple[OperationRun, Path]:
    """Allocate (or reuse) a run id, create its directory, return the run record.

    The on-disk layout under ``runs_dir / <run-id>/`` is created here:
    ``run.json`` plus a ``logs/`` directory the caller writes into.
    """
    now = datetime.now(UTC)
    run_id = run_id or allocate_run_id(operation, lab, now=now)
    run_dir = runs_dir / run_id
    # exist_ok=False on the run-id directory itself surfaces a
    # second-granularity collision loudly instead of silently merging
    # two runs' logs into one record.
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(exist_ok=False)
    (run_dir / "logs").mkdir()
    run = OperationRun(
        run_id=run_id,
        operation=operation,
        lab=lab,
        status="running",
        started_at=_isoformat(now),
    )
    _write(run_dir / "run.json", run)
    return run, run_dir


def finish_run(
    run: OperationRun,
    run_dir: Path,
    *,
    status: RunStatus,
    steps: list[StepResult],
    summary: str | None = None,
) -> OperationRun:
    """Stamp ``finished_at``, attach step results, persist, return the record."""
    finished = run.model_copy(
        update={
            "status": status,
            "finished_at": _isoformat(datetime.now(UTC)),
            "steps": list(steps),
            "summary": summary,
        }
    )
    _write(run_dir / "run.json", finished)
    return finished


def _isoformat(now: datetime) -> str:
    return now.replace(microsecond=0).isoformat()


def _write(path: Path, run: OperationRun) -> None:
    path.write_text(run.model_dump_json(indent=2, exclude_none=True) + "\n")


__all__ = [
    "OperationRun",
    "RunStatus",
    "StepResult",
    "allocate_run_id",
    "finish_run",
    "start_run",
]
