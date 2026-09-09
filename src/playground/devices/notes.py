"""Operator notes about a handset, on two surfaces.

`shared` (the default) is `config/devices/<name>.md` -- committed, keyed
by profile name, portable to whoever else works the lab.

`private` is `<state_dir>/state/devices/<serial>/notes.md` -- gitignored,
keyed by serial, needing no profile at all.

NEITHER IS A SECRET STORE. Both are plaintext on disk; `private` only
keeps the file out of git and off a teammate's machine. Nothing here may
be documented as protecting note contents.

The credential heuristic is advisory. It warns on the committed surface
and never blocks: the feature's motivating example ("this phone has
Telegram with account X and password Y") is itself credential-shaped, so
a refusing default would reject the use case the feature exists to
serve. Substring matching does not reliably detect secrets and must
never be presented as a safety guarantee.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from playground.config.loader import shared_note_path
from playground.devices.registry import devices_dir
from playground.models.diagnostic import Diagnostic, SourceLocation

__all__ = [
    "NoteScope",
    "looks_like_credentials",
    "note_path",
    "read_note",
    "write_note",
]

NoteScope = Literal["shared", "private"]

_CREDENTIAL_HINTS = re.compile(
    r"(?<![A-Za-z0-9])(pass(word|phrase)|secret|token|api[_-]?key|credential)s?"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
"""Lookaround rather than `\\b`: `\\b` treats `_` as a word character, so
it would miss `API_TOKEN` (no boundary between `_` and `T`) -- a real
example from this module's own tests. Still advisory; see the module
docstring.

The trailing `s?` catches the plural ("passwords for the test
accounts", "tokens for the api") -- without it, the same lookahead that
correctly rejects `tokenizer` also rejected every plural, and `shared`
being the default scope means this warning is the only guard between a
credential and a commit. This does mean a sentence like "this phone is
for the credentials team" now warns too; that is an accepted false
positive in exchange for catching real plural credentials, and the
detector only ever warns, never blocks."""


def looks_like_credentials(text: str) -> bool:
    """Advisory only. See the module docstring."""
    return _CREDENTIAL_HINTS.search(text) is not None


def note_path(
    scope: NoteScope,
    *,
    config_dir: Path,
    state_dir: Path,
    name: str | None,
    serial: str | None,
) -> Path:
    """Where a note for this scope lives. Raises `ValueError` if the key
    the scope needs (`name` for `shared`, `serial` for `private`) is
    missing."""
    if scope == "private":
        if serial is None:
            raise ValueError("the private note surface is keyed by serial")
        return devices_dir(state_dir) / serial / "notes.md"
    if name is None:
        raise ValueError("the shared note surface is keyed by profile name")
    return shared_note_path(config_dir, name)


def read_note(
    scope: NoteScope,
    *,
    config_dir: Path,
    state_dir: Path,
    name: str | None,
    serial: str | None,
) -> str | None:
    """The note body, or `None` when no note has been written yet."""
    path = note_path(
        scope, config_dir=config_dir, state_dir=state_dir, name=name, serial=serial
    )
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def write_note(
    body: str,
    *,
    scope: NoteScope,
    config_dir: Path,
    state_dir: Path,
    name: str | None,
    serial: str | None,
) -> tuple[Path, list[Diagnostic]]:
    """Write `body` verbatim to the given scope's note file, creating
    parent directories as needed. On `shared`, a credential-shaped body
    is still written -- the write always succeeds -- but is accompanied
    by a `device.note_credential_warning` diagnostic suggesting
    `--private`. `private` writes never warn."""
    path = note_path(
        scope, config_dir=config_dir, state_dir=state_dir, name=name, serial=serial
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")

    diagnostics: list[Diagnostic] = []
    if scope == "shared" and looks_like_credentials(body):
        diagnostics.append(
            Diagnostic(
                id="device.note_credential_warning",
                severity="warning",
                message=(
                    f"note written to {path} looks like it contains a "
                    "credential, and that file is committed to git"
                ),
                source=SourceLocation(path=str(path)),
                suggestion=(
                    "use `--private` to keep it in .playground/ instead; note "
                    "that neither surface encrypts anything"
                ),
            )
        )
    return path, diagnostics
