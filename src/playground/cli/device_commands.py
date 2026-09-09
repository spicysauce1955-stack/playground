"""`playground device` — list, save, show, and annotate physical handsets.

Everything here runs on the CONTROLLER: `list_attached()` shells out to the
local `adb`, and `read_registry()`/notes read this machine's own state and
config trees. Nothing here binds a device to a lab slot (that's
`devices/adopt.py`, driven by `apply`/`exec`/`app`); this module exists so
an operator can find out what to write into a lab file BEFORE any lab
references a device at all — the chicken-and-egg `device list` solves, and
the profile-authoring `device save`/`device note` provide.

Lives in its own module rather than growing `main.py` further, following
`app_commands.py` and `capture_commands.py`. `main.py` imports `device_app`
from here to register it, so importing `main.py`'s config/diagnostic
helpers back at THIS module's top level would be circular: `main` would
not yet have defined them at the point it imports `device_app`. `_main()`
below defers that import to call time instead.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from ruamel.yaml import YAML

from playground.config.loader import load_config
from playground.devices.adb import AttachedDevice, list_attached
from playground.devices.binding import device_matches
from playground.devices.notes import NoteScope, note_path, read_note, write_note
from playground.devices.registry import RegistryEntry, read_registry
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.kinds import DeviceProfile
from playground.models.status import DeviceState

__all__ = ["device_app"]


def _main() -> Any:  # noqa: ANN401 - deferred cross-module import, see module docstring
    """Deferred import of `playground.cli.main` — see module docstring.

    Typed `Any` rather than importing `main`'s classes/functions for typing
    purposes: doing so would defeat the point (mypy resolves type-only
    imports at check time, same as a runtime import, so a `TYPE_CHECKING`
    import of `main`'s helpers would still make this module's typing
    depend on `main`'s — harmless for mypy, but it invites a future edit to
    "simplify" it into a real top-level import and reintroduce the cycle).
    """
    from playground.cli import main as cli_main

    return cli_main


def _is_interactive() -> bool:
    """Whether this session can be prompted, rather than calling
    `sys.stdin.isatty()` inline -- a thin, monkeypatchable seam so a test
    can drive the interactive `device note` prompt through
    `CliRunner(..., input=...)` without an actual terminal. `CliRunner`'s
    piped stdin is never a tty, so the seam is what makes the branch
    reachable at all under test."""
    return sys.stdin.isatty()


def _fail(diagnostic: Diagnostic) -> NoReturn:
    """Exit with one diagnostic, human-formatted, matching every other
    read-only CLI command's error gate (`main.py`'s `_exit_with_diagnostic`).
    """
    _main()._exit_with_diagnostic(
        diagnostic, _main().OutputFormat.human, json_errors=False
    )
    raise typer.Exit(code=1)  # unreachable: _exit_with_diagnostic always raises


device_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Find, save, and annotate physical Android handsets. `device note` "
        "writes to the SHARED surface by default (config/devices/<name>.md, "
        "tracked by git and committed with the repo); pass --private to keep "
        "a note under .playground/ on this machine only."
    ),
)

ConfigDirOpt = Annotated[
    Path, typer.Option("--config-dir", "-c", help="Config directory to load.")
]
StateDirOpt = Annotated[
    Path,
    typer.Option(
        "--state-dir",
        help="Where generated state lives. Defaults to `.playground/`.",
    ),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]
DeviceArg = Annotated[str, typer.Argument(help="Profile name or raw serial.")]

_UNAUTHORIZED_REMEDY = "tap Allow on the handset's RSA prompt, then re-run"

_REMEDY_TEXT = {"unauthorized": _UNAUTHORIZED_REMEDY}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _device_profiles(config_dir: Path) -> dict[str, DeviceProfile]:
    """Configured `DeviceProfile`s, or an empty mapping when `config_dir`
    does not exist yet.

    `load_config` raises `NotADirectoryError` for a missing directory, but
    every verb in this module must work before any config tree exists at
    all (that is the whole point of `device note --as` being able to
    create the first profile) — so a missing directory reads as "no
    profiles configured" rather than an error.
    """
    if not config_dir.is_dir():
        return {}
    loaded, _diagnostics = load_config(config_dir)
    return loaded.device_profiles


def _matching_profiles(
    profiles: dict[str, DeviceProfile], serial: str, model: str | None
) -> list[str]:
    """Names of every profile whose selector matches this device.

    Uses `devices.binding.device_matches` -- the exact serial/model
    predicate `match_candidates` uses to bind a lab slot -- rather than a
    second, independently-written matcher. Two matchers that disagree
    about what "matches" means is exactly the kind of silent
    inconsistency this module must not introduce: a model-only selector
    binds a lab slot just fine, and had to be able to surface its note
    here too, under the same rule.
    """
    candidate = AttachedDevice(serial=serial, state="missing", model=model)
    return [
        name
        for name, profile in profiles.items()
        if device_matches(profile.spec.match, candidate)
    ]


def _unambiguous_profile_for(
    profiles: dict[str, DeviceProfile], serial: str, model: str | None
) -> str | None:
    """The one profile that selects this device, or `None` when zero or
    more than one profile matches.

    Ambiguity across profiles is real -- two loosely-selected profiles can
    both match one handset -- and must never be resolved by guessing which
    one's note to show; showing no note is the honest answer when the
    match isn't unique.
    """
    matches = _matching_profiles(profiles, serial, model)
    return matches[0] if len(matches) == 1 else None


def _resolve_identity(
    device: str, profiles: dict[str, DeviceProfile], registry: dict[str, RegistryEntry]
) -> tuple[str | None, str | None]:
    """`(profile_name, serial)` for a `device` argument that may be either.

    `device` is checked against known profile names first, then registry
    serials; anything else is treated as a bare serial that simply has no
    registry entry yet (an attached-and-new device, or a typo — this
    module never validates a serial against a live `adb`).
    """
    if device in profiles:
        match = profiles[device].spec.match
        serial = match.serial if match is not None else None
        return device, serial
    if device in registry:
        model = registry[device].facts.model
        return _unambiguous_profile_for(profiles, device, model), device
    return _unambiguous_profile_for(profiles, device, None), device


def _read_scoped_note(
    scope: NoteScope,
    *,
    config_dir: Path,
    state_dir: Path,
    name: str | None,
    serial: str | None,
) -> str | None:
    """`read_note`, tolerating the `ValueError` a scope raises when its
    required key (profile name for `shared`, serial for `private`) is
    absent — a missing key means "nothing to read here", not a crash."""
    if scope == "shared" and name is None:
        return None
    if scope == "private" and serial is None:
        return None
    try:
        return read_note(
            scope, config_dir=config_dir, state_dir=state_dir, name=name, serial=serial
        )
    except ValueError:
        return None


def _note_for(
    config_dir: Path, state_dir: Path, profile_name: str | None, serial: str | None
) -> str | None:
    """The note an operator would most want to see for one device: shared
    (committed, keyed by profile) first, private (keyed by serial) as a
    fallback. Mirrors `devices.adopt._lookup_note`'s precedence."""
    shared = _read_scoped_note(
        "shared", config_dir=config_dir, state_dir=state_dir, name=profile_name, serial=None
    )
    if shared is not None:
        return shared
    return _read_scoped_note(
        "private", config_dir=config_dir, state_dir=state_dir, name=None, serial=serial
    )


def _first_line(body: str | None) -> str | None:
    if not body:
        return None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _age(last_verified: str | None) -> str:
    """How long ago a registry entry was last confirmed against a live
    handset, derived from `last_verified`.

    This is the mitigation for the one failure playground cannot detect: a
    factory reset keeps the serial and keeps matching the profile, while
    the operator's note quietly stops describing what's actually on the
    handset. Nothing here can spot that -- showing the age is the only
    honest thing to do instead of pretending the note is still accurate.
    """
    if last_verified is None:
        return "never verified"
    try:
        then = datetime.fromisoformat(last_verified)
    except ValueError:
        return "verified at an unparseable time"
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = max((datetime.now(UTC) - then).total_seconds(), 0.0)
    days = seconds / 86400
    if days >= 1:
        return f"verified {int(days)}d ago"
    hours = seconds / 3600
    if hours >= 1:
        return f"verified {int(hours)}h ago"
    minutes = seconds / 60
    if minutes >= 1:
        return f"verified {int(minutes)}m ago"
    return "verified just now"


def _dump_profile_yaml(name: str, serial: str) -> str:
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    buf = io.StringIO()
    yaml.dump(
        {
            "apiVersion": "playground/v1",
            "kind": "DeviceProfile",
            "metadata": {"name": name},
            "spec": {"match": {"serial": serial}},
        },
        buf,
    )
    return buf.getvalue()


def _write_profile(config_dir: Path, name: str, serial: str) -> Path:
    """Create `config/devices/<name>.yaml` selecting `serial`.

    This writes under `config/` with no other gate -- callers must only
    reach here on an explicit human action (`device save`, or `device
    note --as` naming a profile that does not yet exist). See the module
    docstring on why `device note` is allowed to do this.
    """
    devices_dir = config_dir / "devices"
    devices_dir.mkdir(parents=True, exist_ok=True)
    path = devices_dir / f"{name}.yaml"
    path.write_text(_dump_profile_yaml(name, serial), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# device list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DeviceRow:
    serial: str
    state: DeviceState
    model: str | None
    sdk: int | None
    rooted: bool | None
    note: str | None
    age: str

    def render(self) -> str:
        model = self.model or "-"
        sdk = str(self.sdk) if self.sdk is not None else "-"
        root = "-" if self.rooted is None else ("yes" if self.rooted else "no")
        note = f'note: "{self.note}"' if self.note else "note: -"
        return (
            f"{self.serial:<20} {model:<16} sdk={sdk:<4} root={root:<4} "
            f"state={self.state:<13} {note} ({self.age})"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "serial": self.serial,
            "state": self.state,
            "model": self.model,
            "sdk": self.sdk,
            "rooted": self.rooted,
            "note": self.note,
            "note_age": self.age,
        }


def _build_row(
    serial: str,
    *,
    state: DeviceState,
    attached_model: str | None,
    entry: RegistryEntry | None,
    profiles: dict[str, DeviceProfile],
    config_dir: Path,
    state_dir: Path,
) -> _DeviceRow:
    facts = entry.facts if entry is not None else None
    model = facts.model if facts is not None else attached_model
    profile_name = _unambiguous_profile_for(profiles, serial, model)
    note = _note_for(config_dir, state_dir, profile_name, serial)
    return _DeviceRow(
        serial=serial,
        state=state,
        model=model,
        sdk=facts.sdk if facts is not None else None,
        rooted=facts.rooted if facts is not None else None,
        note=_first_line(note),
        age=_age(entry.last_verified if entry is not None else None),
    )


@device_app.command("list", help="List attached and known handsets.")
def list_command(
    config_dir: ConfigDirOpt = Path("config"),
    state_dir: StateDirOpt = Path(".playground"),
    json_out: JsonOpt = False,
) -> None:
    """Merge three populations: attached-and-known, attached-and-new, and
    known-but-absent (read from the registry, so an unplugged phone still
    shows up). `list_attached()`'s own diagnostics (e.g. `adb` missing)
    are surfaced but never abort the command -- a dead `adb` still leaves
    the registry half of the picture worth printing."""
    attached, adb_diagnostics = list_attached()
    if adb_diagnostics:
        _main()._print_diagnostics(adb_diagnostics, err=True)

    registry = read_registry(state_dir)
    profiles = _device_profiles(config_dir)

    rows: list[_DeviceRow] = []
    seen: set[str] = set()
    for device in attached:
        rows.append(
            _build_row(
                device.serial,
                state=device.state,
                attached_model=device.model,
                entry=registry.get(device.serial),
                profiles=profiles,
                config_dir=config_dir,
                state_dir=state_dir,
            )
        )
        seen.add(device.serial)
    for serial in sorted(registry):
        if serial in seen:
            continue
        rows.append(
            _build_row(
                serial,
                state="missing",
                attached_model=None,
                entry=registry[serial],
                profiles=profiles,
                config_dir=config_dir,
                state_dir=state_dir,
            )
        )

    if json_out:
        _main()._print_json({"devices": [row.as_dict() for row in rows]})
        return

    if not rows:
        typer.echo("no attached or known devices")
        return

    for row in rows:
        typer.echo(row.render())
        remedy = _REMEDY_TEXT.get(row.state)
        if remedy:
            typer.echo(f"  {remedy}")


# ---------------------------------------------------------------------------
# device save
# ---------------------------------------------------------------------------


@device_app.command("save", help="Save a DeviceProfile selecting a handset by serial.")
def save_command(
    serial: Annotated[str, typer.Argument(help="Handset serial to select.")],
    as_name: Annotated[str, typer.Option("--as", help="Profile name to create.")],
    config_dir: ConfigDirOpt = Path("config"),
) -> None:
    path = config_dir / "devices" / f"{as_name}.yaml"
    if path.is_file():
        _fail(
            Diagnostic(
                id="config.device.profile_exists",
                severity="error",
                message=f"a DeviceProfile named {as_name!r} already exists at {path}",
                source=SourceLocation(path=str(path)),
                suggestion="choose a different --as name, or edit the file directly",
            )
        )
    written = _write_profile(config_dir, as_name, serial)
    typer.echo(f"saved {written}")


# ---------------------------------------------------------------------------
# device show
# ---------------------------------------------------------------------------


def _indent(body: str) -> str:
    lines = body.splitlines() or [""]
    return "\n".join(f"    {line}" for line in lines)


@device_app.command("show", help="Show registry facts and notes for one handset.")
def show_command(
    device: DeviceArg,
    config_dir: ConfigDirOpt = Path("config"),
    state_dir: StateDirOpt = Path(".playground"),
    json_out: JsonOpt = False,
) -> None:
    profiles = _device_profiles(config_dir)
    registry = read_registry(state_dir)
    profile_name, serial = _resolve_identity(device, profiles, registry)
    entry = registry.get(serial) if serial is not None else None

    shared_body = _read_scoped_note(
        "shared", config_dir=config_dir, state_dir=state_dir, name=profile_name, serial=None
    )
    private_body = _read_scoped_note(
        "private", config_dir=config_dir, state_dir=state_dir, name=None, serial=serial
    )

    if json_out:
        _main()._print_json(
            {
                "device": device,
                "profile": profile_name,
                "serial": serial,
                "facts": entry.facts.model_dump(mode="json") if entry is not None else None,
                "first_seen": entry.first_seen if entry is not None else None,
                "last_seen": entry.last_seen if entry is not None else None,
                "last_verified": entry.last_verified if entry is not None else None,
                "notes": {"shared": shared_body, "private": private_body},
            }
        )
        return

    typer.echo(f"device: {device}")
    typer.echo(f"  profile: {profile_name or '-'}")
    typer.echo(f"  serial: {serial or '-'}")
    if entry is not None:
        facts = entry.facts
        typer.echo(f"  model: {facts.model or '-'}")
        typer.echo(f"  manufacturer: {facts.manufacturer or '-'}")
        typer.echo(f"  sdk: {facts.sdk if facts.sdk is not None else '-'}")
        typer.echo(f"  abi: {facts.abi or '-'}")
        typer.echo(f"  rooted: {'yes' if facts.rooted else 'no'}")
        typer.echo(f"  first seen: {entry.first_seen}")
        typer.echo(f"  last seen: {entry.last_seen}")
        typer.echo(f"  last verified: {entry.last_verified} ({_age(entry.last_verified)})")
    else:
        typer.echo("  no registry entry (never probed)")
    typer.echo("  shared note:")
    typer.echo(_indent(shared_body) if shared_body else "    (none)")
    typer.echo("  private note:")
    typer.echo(_indent(private_body) if private_body else "    (none)")


# ---------------------------------------------------------------------------
# device note
# ---------------------------------------------------------------------------


def _serial_for_private(
    device: str,
    profiles: dict[str, DeviceProfile],
    registry: dict[str, RegistryEntry],
) -> str:
    """The serial the private surface is keyed by.

    Three cases, in order:

    1. `device` is not a profile name at all -- it IS the serial. That is
       what lets `--private` work with no profile in existence, which is
       the whole point of that surface.
    2. `device` is a profile whose selector pins a serial -- use it.
    3. `device` is a profile selecting by model alone (or with no `match`
       at all) -- resolve the serial through the registry, which is every
       handset this machine has ever seen.

    Case 3 previously fell through to returning the PROFILE NAME as if it
    were a serial. That wrote `.playground/state/devices/<profile>/notes.md`,
    a path no reader ever looks at: `adopt._lookup_note` and `_note_for`
    both key the private surface on the real serial. The note was written
    successfully and then permanently invisible -- worse than refusing.
    So an unresolvable or ambiguous case raises `ValueError` (turned into
    a diagnostic by the caller) rather than inventing a key.

    The registry, not a live `adb`: no `device note` invocation shells
    out to adb today, and adding a probe here would make writing a note
    depend on the handset being plugged in.
    """
    profile = profiles.get(device)
    if profile is None:
        return device
    match = profile.spec.match
    if match is not None and match.serial is not None:
        return match.serial
    candidates = [
        serial
        for serial, entry in registry.items()
        if device_matches(
            match,
            AttachedDevice(serial=serial, state="missing", model=entry.facts.model),
        )
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(
        f"profile {device!r} pins no serial and {len(candidates)} known "
        "handsets match its selector, so there is no serial to key a "
        "private note by"
    )


def _serial_diagnostic(exc: ValueError, state_dir: Path) -> Diagnostic:
    """`_serial_for_private`'s refusal, as an operator-facing error."""
    return Diagnostic(
        id="config.device.note_serial_required",
        severity="error",
        message=str(exc),
        source=SourceLocation(path=str(state_dir / "state" / "devices")),
        suggestion=(
            "pass the handset's serial directly (`playground device note "
            "<serial> --private`); `playground device list` shows the serials "
            "this machine knows"
        ),
    )


def _name_required_diagnostic(device: str, config_dir: Path) -> Diagnostic:
    return Diagnostic(
        id="config.device.note_name_required",
        severity="error",
        message=(
            f"{device!r} has no DeviceProfile, so there is no shared note to "
            "read or write; name one with --as <name>"
        ),
        source=SourceLocation(path=str(config_dir / "devices")),
        suggestion="or use --private, which keys on serial and needs no profile",
    )


def _updated_at(path: Path) -> str | None:
    """The note file's mtime, ISO-8601 UTC, or `None` when there is no
    file. This is the `updated_at` half of the documented `--json`
    payload; a note that was never written has no timestamp to report,
    and inventing "now" would be a lie."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="seconds")


def _note_payload(
    *,
    device: str,
    serial: str | None,
    scope: NoteScope,
    path: Path,
    markdown: str | None,
) -> dict[str, object]:
    """The `--json` shape the design doc specifies verbatim:
    `{device, serial, scope, path, markdown, updated_at}`. The body key is
    `markdown`, NOT `note` -- a consumer written against the spec reads
    that name."""
    return {
        "device": device,
        "serial": serial,
        "scope": scope,
        "path": str(path),
        "markdown": markdown,
        "updated_at": _updated_at(path),
    }


def _append_line(existing: str | None, line: str) -> str:
    line = line.rstrip("\n")
    if not existing:
        return f"{line}\n"
    body = existing if existing.endswith("\n") else f"{existing}\n"
    return f"{body}{line}\n"


@device_app.command(
    "note",
    help=(
        "Read or write an operator note about a handset. WRITES ARE SHARED "
        "BY DEFAULT: the note goes to config/devices/<name>.md, which is "
        "tracked by git and gets committed with the repo, so whoever clones "
        "it sees the note too. Use --private for a note that stays on this "
        "machine under .playground/ (gitignored). Neither surface encrypts "
        "anything; neither is a secret store."
    ),
)
def note_command(
    device: Annotated[str, typer.Argument(help="Profile name or raw serial.")],
    append: Annotated[
        str | None,
        typer.Option(
            "--append",
            help="Append one line, keeping whatever the note already says.",
        ),
    ] = None,
    set_: Annotated[
        bool,
        typer.Option("--set", help="Replace the note with markdown read from stdin."),
    ] = False,
    show: Annotated[
        bool,
        typer.Option(
            "--show", help="Print the note. Read-only: creates and writes nothing."
        ),
    ] = False,
    clear: Annotated[
        bool,
        typer.Option("--clear", help="Delete the note file. Creates nothing."),
    ] = False,
    as_name: Annotated[
        str | None,
        typer.Option(
            "--as",
            help=(
                "Profile name to create when <device> has no DeviceProfile "
                "yet. Shared notes are keyed by profile name, so naming one "
                "also writes a committed config/devices/<name>.yaml."
            ),
        ),
    ] = None,
    private: Annotated[
        bool,
        typer.Option(
            "--private",
            help=(
                "Use the private surface instead: "
                ".playground/state/devices/<serial>/notes.md -- keyed by "
                "serial, NOT committed to git. Anything sensitive belongs "
                "here rather than in the shared default."
            ),
        ),
    ] = False,
    json_out: Annotated[
        bool,
        typer.Option(
            "--json",
            help=(
                "Emit {device, serial, scope, path, markdown, updated_at} as "
                "JSON instead of human text."
            ),
        ),
    ] = False,
    config_dir: ConfigDirOpt = Path("config"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    """Read or write one handset's note.

    ORDER MATTERS HERE. Every read-only branch (`--show`, `--clear`, and
    the bare no-flag display) runs BEFORE anything may create a profile:
    the spec allows exactly three paths to write under `config/` -- the
    discovery prompt, `device save`, and WRITING a shared note -- so that
    polling adb or reading a note never puts churn in `git status`. An
    earlier version created `config/devices/<name>.yaml` before it had
    even looked at which sub-verb was asked for, so `--show` and
    `--clear` both created a committed file.
    """
    scope: NoteScope = "private" if private else "shared"
    profiles = _device_profiles(config_dir)
    registry = read_registry(state_dir)

    if as_name is not None and not as_name.strip():
        # `--as ""` used to reach `_write_profile` and create files
        # literally named `.yaml` and `.md`.
        _fail(
            Diagnostic(
                id="config.device.note_name_required",
                severity="error",
                message="--as needs a non-empty profile name",
                source=SourceLocation(path=str(config_dir / "devices")),
                suggestion="pass --as <name>, e.g. --as my-pixel",
            )
        )
    as_name = as_name.strip() if as_name is not None else None

    writes_body = set_ or append is not None

    name: str | None = None
    serial: str | None = None
    display_serial: str | None
    if scope == "private":
        try:
            serial = _serial_for_private(device, profiles, registry)
        except ValueError as exc:
            _fail(_serial_diagnostic(exc, state_dir))
        display_serial = serial
    else:
        name = device if device in profiles else as_name
        if name is None and writes_body and _is_interactive():
            # Only a WRITE may prompt: the prompt exists to name a profile
            # that is about to be created, and a read has nothing to
            # create. Whitespace is stripped so the answer cannot become
            # a `.yaml`-named file either.
            name = typer.prompt(
                "No DeviceProfile named that -- name one to create"
            ).strip()
        if not name:
            _fail(_name_required_diagnostic(device, config_dir))
        _, display_serial = _resolve_identity(device, profiles, registry)

    def _path() -> Path:
        try:
            return note_path(
                scope, config_dir=config_dir, state_dir=state_dir, name=name, serial=serial
            )
        except ValueError as exc:
            _fail(_scope_key_diagnostic(exc, config_dir))

    def _read() -> str | None:
        try:
            return read_note(
                scope, config_dir=config_dir, state_dir=state_dir, name=name, serial=serial
            )
        except ValueError as exc:
            _fail(_scope_key_diagnostic(exc, config_dir))

    if clear:
        path = _path()
        existed = path.is_file()
        if existed:
            path.unlink()
        if json_out:
            _main()._print_json(
                _note_payload(
                    device=device,
                    serial=display_serial,
                    scope=scope,
                    path=path,
                    markdown=None,
                )
            )
        elif existed:
            typer.echo(f"note cleared: {path}")
        else:
            typer.echo(f"no note to clear at {path}")
        return

    if not writes_body:
        # --show, or no action flag at all: read-only display. This is an
        # intended note-body display path, same as `device show` -- never
        # echoed as part of a diagnostic or log line. Nothing on this
        # branch touches the filesystem.
        current = _read()
        if json_out:
            _main()._print_json(
                _note_payload(
                    device=device,
                    serial=display_serial,
                    scope=scope,
                    path=_path(),
                    markdown=current,
                )
            )
        elif current:
            typer.echo(current)
        else:
            typer.echo("(no note recorded yet)")
        return

    if scope == "shared" and name not in profiles:
        # Writing a note is the explicit human action that authorises this
        # `config/` write -- the third and last path allowed to create a
        # profile, alongside the discovery prompt and `device save`. See
        # the module + `_write_profile` docstrings. It happens HERE, after
        # every read-only branch has already returned.
        _write_profile(config_dir, name, device)

    body = sys.stdin.read() if set_ else _append_line(_read(), append or "")

    try:
        path, note_diagnostics = write_note(
            body, scope=scope, config_dir=config_dir, state_dir=state_dir,
            name=name, serial=serial,
        )
    except ValueError as exc:
        _fail(_scope_key_diagnostic(exc, config_dir))

    # `write_note` only ever returns warnings (a credential-shaped shared
    # note); the write always succeeds regardless, so these never change
    # the exit code -- print and move on.
    if note_diagnostics:
        _main()._print_diagnostics(note_diagnostics)
    if json_out:
        _main()._print_json(
            _note_payload(
                device=device,
                serial=display_serial,
                scope=scope,
                path=path,
                markdown=body,
            )
        )
    elif scope == "shared":
        # Said on every successful write, not only in --help: with shared
        # as the default, the safe choice is the one behind a flag, and
        # the operator has to know which one they just took.
        typer.echo(f"note written to {path}")
        typer.echo(
            "  this file is SHARED: tracked by git and committed with the "
            "repo -- use --private to keep a note on this machine only"
        )
    else:
        typer.echo(f"note written to {path}")
        typer.echo(
            "  private: stays under .playground/ (gitignored), never committed"
        )


def _scope_key_diagnostic(exc: ValueError, config_dir: Path) -> Diagnostic:
    """Turn the `ValueError` `note_path`/`read_note`/`write_note` raise for
    a missing scope key into an operator-facing `Diagnostic` -- never a
    bare traceback."""
    return Diagnostic(
        id="config.device.note_scope_key_missing",
        severity="error",
        message=str(exc),
        source=SourceLocation(path=str(config_dir / "devices")),
    )
