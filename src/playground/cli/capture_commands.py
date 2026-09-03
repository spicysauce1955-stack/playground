"""`playground capture` — record a Redroid device's traffic to a pcap.

Sessions are systemd units on the guest, not processes this CLI owns: a
capture has to outlive the ssh call that started it, and has to survive
the operator closing their laptop. Every verb here is therefore a thin
`systemctl` driver plus a session record on this machine.

Lives in its own module rather than growing `main.py` (2000+ lines)
further, following `app_commands.py`. `main.py` imports `capture_app`
from here to register it, so importing `main.py`'s config/diagnostic
helpers back at THIS module's top level would be circular: `main` would
not yet have defined them at the point it imports `capture_app`.
`_main()` below defers that import to call time instead.
"""

from __future__ import annotations

import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from playground.android.runner import TargetResult, run_on_targets
from playground.android.targets import AndroidTarget
from playground.backend.dispatch import query_status
from playground.capture.commands import (
    clean_cmd,
    is_active_cmd,
    remote_capture_dir,
    start_cmd,
    status_cmd,
    stop_cmd,
    unit_name,
)
from playground.capture.state import (
    CaptureSession,
    clear_session,
    read_session,
    write_session,
)
from playground.capture.targets import resolve_capture_targets
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.runs.operation import StepResult, finish_run, start_run
from playground.ssh.argv import build_scp_argv
from playground.validation import validate as validate_loaded_config


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
    CLI command's error gate."""
    _main()._exit_with_diagnostic(
        diagnostic, _main().OutputFormat.human, json_errors=False
    )
    raise typer.Exit(code=1)  # unreachable: _exit_with_diagnostic always raises


capture_app = typer.Typer(
    no_args_is_help=True,
    help="Capture the network traffic of Redroid devices to pcap files.",
)

_DEFAULT_TIMEOUT = 30.0
_START_TIMEOUT = 60.0
"""`start_cmd` now sleeps 2s on the guest (see capture/commands.py) before
confirming the unit is active, on top of the usual sudo/systemctl round
trip -- the plain 30s default leaves too little margin on a slow link."""

LabOpt = Annotated[
    str | None,
    typer.Option(
        "--lab",
        help=(
            "Lab name. Defaults to the only configured lab; required when "
            "multiple labs are configured."
        ),
    ),
]
OnOpt = Annotated[str | None, typer.Option("--on", help="Target one VM by name.")]
RoleOpt = Annotated[
    str | None,
    typer.Option("--role", help="Target every capturable VM that has this role."),
]
AllOpt = Annotated[
    bool, typer.Option("--all", help="Target every capturable VM in the lab.")
]
UserOpt = Annotated[str, typer.Option("--user", help="SSH user (default: ubuntu).")]
ConfigDirOpt = Annotated[
    Path, typer.Option("--config-dir", "-c", help="Config directory to load.")
]
TofuDirOpt = Annotated[
    Path, typer.Option("--tofu-dir", help="OpenTofu working directory.")
]
StateDirOpt = Annotated[
    Path,
    typer.Option(
        "--state-dir",
        help="Where generated state lives. Defaults to `.playground/`.",
    ),
]


def _resolve(
    *,
    lab: str | None,
    on: str | None,
    role: str | None,
    all_devices: bool,
    user: str,
    config_dir: Path,
    tofu_dir: Path,
) -> tuple[str, ResolvedLab, list[AndroidTarget]]:
    """Load, validate, default the lab, and pick capturable devices.

    Returns the lab NAME alongside the resolved lab and targets because
    every verb needs it for the session-record path, and `--lab` may have
    been defaulted here rather than passed.
    """
    cli_main = _main()
    loaded, diagnostics = cli_main._load_config_or_exit(
        config_dir, cli_main.OutputFormat.human
    )
    if not cli_main._has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded, lab=lab))
    cli_main._exit_on_errors(diagnostics, cli_main.OutputFormat.human, json_errors=False)
    cli_main._print_warnings(diagnostics)

    if lab is None:
        if len(loaded.labs) == 1:
            lab = next(iter(loaded.labs))
        else:
            _fail(
                Diagnostic(
                    id="config.capture.lab_required",
                    severity="error",
                    message=(
                        f"--lab required when {len(loaded.labs)} labs are "
                        "configured; pass --lab <name>"
                    ),
                    source=SourceLocation(path=str(config_dir / "labs")),
                    suggestion="run `playground lab list` and pass --lab <name>",
                )
            )

    resolved = cli_main._resolve_lab_or_exit(
        loaded, lab, config_dir, cli_main.OutputFormat.human
    )
    status, query_diagnostics = query_status(resolved, tofu_dir)
    cli_main._exit_on_errors(
        query_diagnostics, cli_main.OutputFormat.human, json_errors=False
    )
    targets, target_diagnostics = resolve_capture_targets(
        resolved, status, on=on, role=role, all_devices=all_devices, user=user,
    )
    cli_main._exit_on_errors(
        target_diagnostics, cli_main.OutputFormat.human, json_errors=False
    )
    return lab, resolved, targets


def _echo(result: TargetResult) -> None:
    prefix = f"[{result.target.vm_name}]"
    for line in result.stdout.splitlines():
        typer.echo(f"{prefix} {line}")
    for line in result.stderr.splitlines():
        typer.echo(f"{prefix} {line}", err=True)
    if not result.ok:
        typer.echo(f"{prefix} exited {result.returncode}", err=True)


def _exit_for_failures(failed: list[str], total: int) -> None:
    if failed:
        typer.echo(
            f"failed on {len(failed)}/{total} device(s): {', '.join(failed)}",
            err=True,
        )
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@capture_app.command("start", help="Start capturing a device's traffic.")
def start_command(
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    lab_name, resolved, targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
    )

    failed: list[str] = []
    startable: list[AndroidTarget] = []
    # Refuse BEFORE touching any guest: a second start against a live
    # session produces two overlapping pcap sets that cannot be
    # attributed to either experiment. This is a PER-TARGET skip, not a
    # global abort: one device already mid-capture is no reason to refuse
    # every other device in a --all/--role fan-out. Mirrors the
    # continue/failed.append/_exit_for_failures shape every other
    # fan-out verb in this CLI uses (see `install_command` in
    # app_commands.py).
    for target in targets:
        if read_session(state_dir, lab_name, target.vm_name) is not None:
            _main()._print_diagnostics(
                [
                    Diagnostic(
                        id="runtime.capture.already_running",
                        severity="error",
                        message=(
                            f"a capture session is already recorded for "
                            f"{target.vm_name!r} in lab {lab_name!r}"
                        ),
                        source=SourceLocation(
                            path=str(state_dir / "state" / "capture" / lab_name)
                        ),
                        suggestion=(
                            f"run `playground capture stop --lab {lab_name} --on "
                            f"{target.vm_name}` first"
                        ),
                    )
                ],
                err=True,
            )
            failed.append(target.vm_name)
            continue
        startable.append(target)

    # One command per target: `run_on_targets` sends ONE command string to
    # every target, but each device's command names its own unit instance,
    # so capture calls it per-target. Identical for a single device (the
    # common case); with --all it serializes. A parallel variant belongs in
    # `android/runner.py`, and only once something needs it.
    results = [
        run_on_targets([t], start_cmd(t.vm_name), timeout=_START_TIMEOUT)[0]
        for t in startable
    ]
    for result in results:
        _echo(result)
        if result.ok:
            # Only record a session the guest actually started. A record
            # written on failure would make `start` refuse forever while
            # nothing was capturing.
            try:
                write_session(
                    state_dir,
                    lab_name,
                    CaptureSession(
                        vm=result.target.vm_name,
                        unit=unit_name(result.target.vm_name),
                        started_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                        max_file_mb=resolved.capture.max_file_mb,
                        max_files=resolved.capture.max_files,
                        snaplen=resolved.capture.snaplen,
                    ),
                )
            except OSError as exc:
                # The guest IS capturing at this point -- an I/O error
                # writing the record must not be silent (a raw traceback)
                # or leave the operator with no way to find the still-
                # running unit. Name it explicitly so `--on`/`status`
                # still reach it even with no local record.
                _main()._print_diagnostics(
                    [
                        Diagnostic(
                            id="runtime.capture.session_write_failed",
                            severity="error",
                            message=(
                                f"{result.target.vm_name!r} started capturing "
                                f"({unit_name(result.target.vm_name)}) but the "
                                f"session record could not be written: {exc}"
                            ),
                            source=SourceLocation(
                                path=str(state_dir / "state" / "capture" / lab_name)
                            ),
                            suggestion=(
                                "the capture IS running -- stop it with "
                                f"`playground capture stop --lab {lab_name} --on "
                                f"{result.target.vm_name}`, or inspect it with "
                                f"`playground capture status --lab {lab_name} --on "
                                f"{result.target.vm_name}`"
                            ),
                        )
                    ],
                    err=True,
                )
                failed.append(result.target.vm_name)
                continue
            typer.echo(
                f"[{result.target.vm_name}] capturing — "
                f"stop with `playground capture stop --lab {lab_name} "
                f"--on {result.target.vm_name}`"
            )
        else:
            failed.append(result.target.vm_name)
    _exit_for_failures(failed, len(targets))


@capture_app.command("stop", help="Stop capturing and seal the session.")
def stop_command(
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    lab_name, _resolved, targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
    )

    failed: list[str] = []
    stoppable: list[AndroidTarget] = []
    # PER-TARGET skip, not a global abort: one device with no recorded
    # session must not block stopping the others -- that would let one
    # operator's out-of-band start (or a record that was never written,
    # see FIX 2 above) prevent stopping a genuinely running capture
    # elsewhere in a --all/--role fan-out.
    for target in targets:
        if read_session(state_dir, lab_name, target.vm_name) is None:
            _main()._print_diagnostics(
                [
                    Diagnostic(
                        id="runtime.capture.not_running",
                        severity="error",
                        message=(
                            f"no capture session recorded for {target.vm_name!r} "
                            f"in lab {lab_name!r}"
                        ),
                        source=SourceLocation(
                            path=str(state_dir / "state" / "capture" / lab_name)
                        ),
                        suggestion=(
                            f"run `playground capture status --lab {lab_name}` to "
                            "see what the guest reports"
                        ),
                    )
                ],
                err=True,
            )
            failed.append(target.vm_name)
            continue
        stoppable.append(target)

    for target in stoppable:
        result = run_on_targets(
            [target], stop_cmd(target.vm_name), timeout=_DEFAULT_TIMEOUT
        )[0]
        _echo(result)
        if result.ok:
            # Clear only on success: leaving the record in place after a
            # failed stop keeps `fetch` pointed at a session that may
            # still be writing.
            clear_session(state_dir, lab_name, target.vm_name)
            typer.echo(f"[{target.vm_name}] stopped")
        else:
            failed.append(target.vm_name)
    _exit_for_failures(failed, len(targets))


@capture_app.command("status", help="Report capture state, file count, and bytes.")
def status_command(
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    """Ask the GUEST, not the session record.

    The record says what this machine started; the guest says what is
    actually running. They can disagree — another operator's stop, a
    reboot, a container restart — and the guest is the truth.
    """
    lab_name, _resolved, targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
    )
    failed: list[str] = []
    for target in targets:
        result = run_on_targets(
            [target], status_cmd(target.vm_name), timeout=_DEFAULT_TIMEOUT
        )[0]
        _echo(result)
        recorded = read_session(state_dir, lab_name, target.vm_name)
        if recorded is not None:
            typer.echo(
                f"[{target.vm_name}] session recorded here since "
                f"{recorded.started_at}"
            )
        if not result.ok:
            failed.append(target.vm_name)
    _exit_for_failures(failed, len(targets))


_FETCH_TIMEOUT = 300.0
"""A 1 GB ring over a cloud VM's uplink is not a 30-second transfer."""


@capture_app.command("fetch", help="Copy captured pcaps into a run artifact directory.")
def fetch_command(
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    clean: Annotated[
        bool,
        typer.Option(
            "--clean",
            help="Remove the guest-side pcaps after a successful transfer.",
        ),
    ] = False,
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    """Pull each device's pcap directory into this run's artifacts.

    Recorded as an operation run so the pcaps are addressable later
    (`playground runs list`) rather than landing in whatever directory
    the operator happened to be standing in.

    The guest copy is left in place unless `--clean`: a failed scp must
    never be able to destroy the only copy of a capture.
    """
    lab_name, _resolved, targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
    )

    run, run_dir = start_run(state_dir / "runs", "capture", lab_name)
    artifacts = run_dir / "artifacts" / "capture"
    logs_dir = run_dir / "logs"
    steps: list[StepResult] = []
    # Two different questions, two different lists: "did the operator get
    # everything they asked for?" (the exit code, `failed` = both lists
    # combined) vs. "how many devices' pcaps were actually fetched?" (the
    # persisted summary, which must count only transfer failures --
    # counting a cleanup failure there would print "fetched capture from
    # 0/1" for a run whose pcaps DID arrive and only cleanup afterward
    # failed).
    fetch_failed: list[str] = []
    clean_failed: list[str] = []

    for target in targets:
        destination = artifacts / target.vm_name
        destination.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        # scp's `host:path` operand IS shell-interpreted on the remote
        # side (legacy scp protocol execs a remote shell; even the
        # SFTP-based default still tokenizes it for some servers/older
        # clients). Without quoting, a VM name with a space is
        # "ambiguous target" and one with a backtick or `$(...)` runs on
        # the guest. Only the path half is quoted -- the user@host half
        # must stay bare. Mirrors `_scp_one` in `app_commands.py`.
        source = (
            f"{target.ssh_user}@{target.ssh_host}:"
            f"{shlex.quote(f'{remote_capture_dir(target.vm_name)}/.')}"
        )
        argv = build_scp_argv(
            source, str(destination), port=target.ssh_port, recursive=True
        )
        try:
            completed = subprocess.run(  # noqa: S603
                argv, capture_output=True, text=True, check=False,
                timeout=_FETCH_TIMEOUT,
            )
            code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            code, stdout, stderr = 1, "", str(exc)

        # A `.log` file under the run's `logs/` directory, not the
        # artifact directory itself: `runs_show_command` prints
        # `log_path` expecting a diagnostic FILE (every other StepResult
        # producer in this repo follows that convention), and a failed
        # scp's stderr is exactly what `playground runs show` exists to
        # surface later. Written on failure too -- that is the case
        # someone will actually go looking for.
        log_path = logs_dir / f"fetch-{target.vm_name}.log"
        log_path.write_text(
            f"$ {' '.join(argv)}\n"
            f"exit {code}\n"
            f"--- stdout ---\n{stdout}"
            f"--- stderr ---\n{stderr}"
        )

        steps.append(
            StepResult(
                name=f"capture-fetch:{target.vm_name}",
                command=list(argv),
                exit_code=code,
                log_path=str(log_path),
                started_at=started_at,
                finished_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            )
        )
        if code != 0:
            typer.echo(f"[{target.vm_name}] fetch failed: {stderr.strip()}", err=True)
            fetch_failed.append(target.vm_name)
            continue
        typer.echo(f"[{target.vm_name}] pcaps → {destination}")
        if clean:
            # Gate on the GUEST, not on the local session record: the
            # record can legitimately be absent while a capture is
            # running (a second operator, or a `playground reset` that
            # scrubbed it) -- see `status_command`'s docstring. Skipping
            # `stop` before `fetch --clean` is the natural operator
            # error the example lab's own header teaches (start ->
            # install -> stop -> fetch), and `rm -f` against a file
            # tcpdump still has open unlinks the inode without stopping
            # the write: data lost, disk still consumed, nothing
            # reported until the next `-C` rotation. The transfer above
            # has already succeeded either way, so the operator's data
            # is safe -- only the guest-side copy is at stake here.
            probe = run_on_targets(
                [target], is_active_cmd(target.vm_name), timeout=_DEFAULT_TIMEOUT
            )[0]
            if probe.stdout.strip() == "active":
                _main()._print_diagnostics(
                    [
                        Diagnostic(
                            id="runtime.capture.still_running",
                            severity="error",
                            message=(
                                f"{target.vm_name!r} is still capturing -- the "
                                f"pcaps WERE fetched, but the guest copy was "
                                f"deliberately left in place rather than "
                                f"deleted out from under a live tcpdump"
                            ),
                            source=SourceLocation(
                                path=str(remote_capture_dir(target.vm_name))
                            ),
                            suggestion=(
                                f"run `playground capture stop --lab {lab_name} "
                                f"--on {target.vm_name}` before cleaning"
                            ),
                        )
                    ],
                    err=True,
                )
                clean_failed.append(target.vm_name)
                continue
            removal = run_on_targets(
                [target], clean_cmd(target.vm_name), timeout=_DEFAULT_TIMEOUT
            )[0]
            _echo(removal)
            if not removal.ok:
                typer.echo(
                    f"[{target.vm_name}] pcaps were fetched successfully, but the "
                    f"guest-side cleanup failed — the capture directory on the "
                    f"guest still holds them",
                    err=True,
                )
                clean_failed.append(target.vm_name)

    failed = fetch_failed + clean_failed
    fetched = len(targets) - len(fetch_failed)
    summary = f"fetched capture from {fetched}/{len(targets)} device(s)"
    if clean_failed:
        summary += (
            f"; guest-side cleanup failed on {len(clean_failed)} "
            "(pcaps remain on the guest)"
        )

    finish_run(
        run,
        run_dir,
        status="failed" if failed else "succeeded",
        steps=steps,
        summary=summary,
    )
    typer.echo(f"  run: {run.run_id}")
    _exit_for_failures(failed, len(targets))
