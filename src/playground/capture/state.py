"""Per-VM capture session records under `.playground/state/capture/`.

Layout: ``<state_dir>/state/capture/<lab>/<vm>.json``. `.playground/state/`
holds per-lab FILES as ``<subdir>/<lab>.<ext>`` and per-lab DIRECTORIES as
``<subdir>/<lab>/``; capture needs one record per VM, so it takes the
directory form.

Paths are built here rather than through `playground.state`, whose
module docstring promises a `StateStore` that was never written -- every
other state path in the repo is likewise built at its call site.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from playground.models.base import StrictModel


class CaptureSession(StrictModel):
    """One capture session, as recorded on the operator's machine.

    The limits are copied in rather than re-read from the lab at
    `stop`/`fetch` time: they are `resolved.capture` as of `start`, i.e.
    the lab's CONFIGURED limits at session start -- not necessarily what
    tcpdump actually used. The values tcpdump ran with were baked into
    the wrapper script at APPLY time; editing `spec.capture` without
    re-applying leaves this record and the wrapper disagreeing about
    what the ring's real bounds are. Editing `spec.capture` mid-session
    must not retroactively change what a finished session claims about
    itself.
    """

    vm: str
    unit: str
    started_at: str
    max_file_mb: int
    max_files: int
    snaplen: int


def session_path(state_dir: Path, lab: str, vm: str) -> Path:
    return state_dir / "state" / "capture" / lab / f"{vm}.json"


def write_session(state_dir: Path, lab: str, session: CaptureSession) -> Path:
    path = session_path(state_dir, lab, session.vm)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def read_session(state_dir: Path, lab: str, vm: str) -> CaptureSession | None:
    """The recorded session, or None when there is none to read.

    A corrupt or truncated record reads as None rather than raising: the
    only thing a caller does with "no valid session" is start a fresh
    one, and a half-written file must not wedge that forever.
    """
    path = session_path(state_dir, lab, vm)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, NOT an OSError, so it needs
        # naming explicitly. A file truncated mid multi-byte sequence is
        # precisely what a non-atomic write_text produces, and tolerating
        # that is why this function promises never to raise.
        return None
    try:
        return CaptureSession.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError):
        return None


def clear_session(state_dir: Path, lab: str, vm: str) -> None:
    """Remove the record. Absent is success -- the goal is "be gone"."""
    session_path(state_dir, lab, vm).unlink(missing_ok=True)
