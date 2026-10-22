"""Tests for the per-lab egress-isolation record."""

from __future__ import annotations

from pathlib import Path

from playground.state.isolation import (
    IsolationRecord,
    clear_isolation,
    isolation_path,
    now_iso,
    read_isolation,
    write_isolation,
)


def _record(lab: str = "lab1") -> IsolationRecord:
    return IsolationRecord(
        lab=lab, backend="local-libvirt", isolated_at=now_iso(),
        lab_cidrs=["10.61.0.0/24"], run_id="r1",
    )


def test_read_absent_is_none(tmp_path: Path) -> None:
    assert read_isolation(tmp_path, "lab1") is None


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    rec = _record()
    path = write_isolation(tmp_path, rec)
    assert path == tmp_path / "isolation" / "lab1.json"
    assert read_isolation(tmp_path, "lab1") == rec
    assert not list((tmp_path / "isolation").glob("*.tmp"))


def test_write_overwrites(tmp_path: Path) -> None:
    write_isolation(tmp_path, _record())
    newer = _record().model_copy(update={"run_id": "r2"})
    write_isolation(tmp_path, newer)
    loaded = read_isolation(tmp_path, "lab1")
    assert loaded is not None and loaded.run_id == "r2"


def test_clear_is_idempotent(tmp_path: Path) -> None:
    assert clear_isolation(tmp_path, "lab1") is False
    write_isolation(tmp_path, _record())
    assert clear_isolation(tmp_path, "lab1") is True
    assert read_isolation(tmp_path, "lab1") is None
    assert clear_isolation(tmp_path, "lab1") is False


def test_records_are_per_lab(tmp_path: Path) -> None:
    write_isolation(tmp_path, _record("a"))
    assert read_isolation(tmp_path, "b") is None
    assert isolation_path(tmp_path, "a").exists()


def test_corrupt_record_fails_closed(tmp_path: Path) -> None:
    """An unreadable record still counts as isolated (guard must not fail open)."""
    path = isolation_path(tmp_path, "lab1")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    rec = read_isolation(tmp_path, "lab1")
    assert rec is not None
    assert rec.lab == "lab1"
    assert rec.backend == "unknown"
