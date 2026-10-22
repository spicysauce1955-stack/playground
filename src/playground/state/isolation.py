"""Per-lab egress-isolation record (``<state_dir>/isolation/<lab>.json``).

``dispatch.execute_isolate`` writes the record after a succeeded isolate;
``dispatch.execute_connect`` and a non-failed destroy / reset / suspend
remove it. ``dispatch.execute_apply`` / ``execute_resume`` read it to refuse
an isolated lab. Backends never touch it.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from playground.models.base import StrictModel


class IsolationRecord(StrictModel):
    lab: str
    backend: str
    isolated_at: str
    """ISO-8601 UTC timestamp of the succeeded isolate."""
    lab_cidrs: list[str]
    run_id: str


def isolation_path(state_dir: Path, lab: str) -> Path:
    return state_dir / "isolation" / f"{lab}.json"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_isolation(state_dir: Path, record: IsolationRecord) -> Path:
    """Atomically write ``record``; re-isolating overwrites the old one."""
    path = isolation_path(state_dir, record.lab)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record.model_dump(mode="json"), indent=2) + "\n")
    os.replace(tmp, path)
    return path


def read_isolation(state_dir: Path, lab: str) -> IsolationRecord | None:
    """Return the record, or ``None`` if absent.

    A corrupt record is treated as present-but-unreadable and reported as an
    isolated lab with unknown details: failing open would let ``apply``
    silently reopen egress, which is exactly what the guard exists to stop.
    """
    path = isolation_path(state_dir, lab)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    try:
        return IsolationRecord.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return IsolationRecord(
            lab=lab, backend="unknown", isolated_at="unknown",
            lab_cidrs=[], run_id="unknown",
        )


def clear_isolation(state_dir: Path, lab: str) -> bool:
    """Remove the record. Returns True if one existed. Idempotent."""
    try:
        isolation_path(state_dir, lab).unlink()
    except FileNotFoundError:
        return False
    return True


__all__ = [
    "IsolationRecord",
    "clear_isolation",
    "isolation_path",
    "now_iso",
    "read_isolation",
    "write_isolation",
]
