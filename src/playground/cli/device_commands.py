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
from playground.devices.adb import list_attached
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
    help="Find, save, and annotate physical Android handsets.",
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


def _profile_for_serial(profiles: dict[str, DeviceProfile], serial: str) -> str | None:
    """The name of the profile that selects `serial`, if any.

    Shared notes are keyed by profile name, not serial, so this is what
    lets `device list`/`device show` find a shared note for a device the
    operator only knows by serial.
    """
    for name, profile in profiles.items():
        match = profile.spec.match
        if match is not None and match.serial == serial:
            return name
    return None


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
        return _profile_for_serial(profiles, device), device
    return _profile_for_serial(profiles, device), device


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
    profile_name = _profile_for_serial(profiles, serial)
    note = _note_for(config_dir, state_dir, profile_name, serial)
    return _DeviceRow(
        serial=serial,
        state=state,
        model=facts.model if facts is not None else attached_model,
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


def _serial_for_private(device: str, profiles: dict[str, DeviceProfile]) -> str:
    """The private surface is keyed by serial. `device` may itself be a
    profile name pinned to a serial -- if so, use that; otherwise `device`
    is taken to be the serial itself, which is what lets `--private` work
    with no profile at all."""
    profile = profiles.get(device)
    if profile is not None and profile.spec.match is not None:
        serial = profile.spec.match.serial
        if serial is not None:
            return serial
    return device


def _append_line(existing: str | None, line: str) -> str:
    line = line.rstrip("\n")
    if not existing:
        return f"{line}\n"
    body = existing if existing.endswith("\n") else f"{existing}\n"
    return f"{body}{line}\n"


@device_app.command("note", help="Read or write an operator note about a handset.")
def note_command(
    device: Annotated[str, typer.Argument(help="Profile name or raw serial.")],
    append: Annotated[str | None, typer.Option("--append")] = None,
    set_: Annotated[bool, typer.Option("--set", help="Read markdown from stdin.")] = False,
    show: Annotated[bool, typer.Option("--show")] = False,
    clear: Annotated[bool, typer.Option("--clear")] = False,
    as_name: Annotated[str | None, typer.Option("--as", help="Profile to create.")] = None,
    private: Annotated[bool, typer.Option("--private")] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
    config_dir: ConfigDirOpt = Path("config"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    scope: NoteScope = "private" if private else "shared"
    profiles = _device_profiles(config_dir)

    name: str | None
    serial: str | None
    if scope == "private":
        name = None
        serial = _serial_for_private(device, profiles)
    else:
        name = device if device in profiles else as_name
        if name is None:
            if sys.stdin.isatty():
                name = typer.prompt("No DeviceProfile named that -- name one to create")
            else:
                _fail(
                    Diagnostic(
                        id="config.device.note_name_required",
                        severity="error",
                        message=(
                            f"{device!r} has no DeviceProfile, so there is no shared "
                            "note to write; name one with --as <name>"
                        ),
                        source=SourceLocation(path=str(config_dir / "devices")),
                        suggestion=(
                            "or use --private, which keys on serial and needs no profile"
                        ),
                    )
                )
        if name not in profiles:
            # Writing a note is the explicit human action that authorises
            # this `config/` write -- the third and last path allowed to
            # create a profile, alongside the discovery prompt and
            # `device save`. See the module + `_write_profile` docstrings.
            _write_profile(config_dir, name, device)
        serial = None

    if clear:
        try:
            path = note_path(
                scope, config_dir=config_dir, state_dir=state_dir, name=name, serial=serial
            )
        except ValueError as exc:
            _fail(_scope_key_diagnostic(exc, config_dir))
        if path.is_file():
            path.unlink()
            typer.echo(f"note cleared: {path}")
        else:
            typer.echo(f"no note to clear at {path}")
        return

    if set_:
        body = sys.stdin.read()
    elif append is not None:
        try:
            existing = read_note(
                scope, config_dir=config_dir, state_dir=state_dir, name=name, serial=serial
            )
        except ValueError as exc:
            _fail(_scope_key_diagnostic(exc, config_dir))
        body = _append_line(existing, append)
    else:
        # --show, or no action flag at all: read-only display. This is an
        # intended note-body display path, same as `device show` -- never
        # echoed as part of a diagnostic or log line.
        try:
            current = read_note(
                scope, config_dir=config_dir, state_dir=state_dir, name=name, serial=serial
            )
        except ValueError as exc:
            _fail(_scope_key_diagnostic(exc, config_dir))
        if json_out:
            _main()._print_json({"scope": scope, "note": current})
        elif current:
            typer.echo(current)
        else:
            typer.echo("(no note recorded yet)")
        return

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
        _main()._print_json({"scope": scope, "path": str(path)})
    else:
        typer.echo(f"note written to {path}")


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
