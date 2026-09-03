"""Session records under .playground/state/capture/<lab>/<vm>.json."""

from __future__ import annotations

from pathlib import Path

from playground.capture.state import (
    CaptureSession,
    clear_session,
    read_session,
    session_path,
    write_session,
)


def _session() -> CaptureSession:
    return CaptureSession(
        vm="droid1",
        unit="playground-capture@droid1.service",
        started_at="2026-09-03T12:00:00+00:00",
        max_file_mb=100,
        max_files=10,
        snaplen=0,
    )


def test_session_path_follows_the_per_lab_directory_precedent(tmp_path: Path) -> None:
    path = session_path(tmp_path, "redroid-cloud", "droid1")
    assert path == tmp_path / "state" / "capture" / "redroid-cloud" / "droid1.json"


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    write_session(tmp_path, "redroid-cloud", _session())
    loaded = read_session(tmp_path, "redroid-cloud", "droid1")
    assert loaded == _session()


def test_write_creates_missing_parents(tmp_path: Path) -> None:
    written = write_session(tmp_path, "redroid-cloud", _session())
    assert written.is_file()


def test_read_of_an_absent_session_is_none_not_an_error(tmp_path: Path) -> None:
    assert read_session(tmp_path, "redroid-cloud", "droid1") is None


def test_read_of_a_corrupt_record_is_none(tmp_path: Path) -> None:
    """A truncated write must not wedge `capture start` forever -- the
    operator can always start a new session over a broken record."""
    path = session_path(tmp_path, "redroid-cloud", "droid1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert read_session(tmp_path, "redroid-cloud", "droid1") is None


def test_clear_is_idempotent(tmp_path: Path) -> None:
    write_session(tmp_path, "redroid-cloud", _session())
    clear_session(tmp_path, "redroid-cloud", "droid1")
    clear_session(tmp_path, "redroid-cloud", "droid1")
    assert read_session(tmp_path, "redroid-cloud", "droid1") is None


def test_read_of_valid_json_array_is_none(tmp_path: Path) -> None:
    """Valid JSON that is not an object must return None, not raise."""
    path = session_path(tmp_path, "redroid-cloud", "droid1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]")
    assert read_session(tmp_path, "redroid-cloud", "droid1") is None


def test_read_of_valid_json_null_is_none(tmp_path: Path) -> None:
    """Valid JSON null must return None, not raise."""
    path = session_path(tmp_path, "redroid-cloud", "droid1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("null")
    assert read_session(tmp_path, "redroid-cloud", "droid1") is None


def test_read_of_an_undecodable_record_is_none(tmp_path: Path) -> None:
    """A non-atomic write can truncate mid multi-byte character, and
    `read_text` raises UnicodeDecodeError for that -- a ValueError, not an
    OSError. If that escapes, `capture start` crashes on a damaged record
    instead of starting a fresh session.
    """
    path = session_path(tmp_path, "redroid-cloud", "droid1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"vm": "dr\xc3')  # bare UTF-8 lead byte

    assert read_session(tmp_path, "redroid-cloud", "droid1") is None
