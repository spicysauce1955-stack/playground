"""`playground app` — install, launch, and inspect Android apps on Redroid VMs.

Every verb drives `adb` ON the guest over SSH (never on the operator's
machine, never over an exposed 5555) so one mechanism serves both the
declarative `android_app` workload and this interactive group. Targeting
(`--on` / `--role` / `--all`) and command strings are delegated to
`playground.android.targets` / `playground.android.commands`; this module
is the thin Typer + fan-out shell around them.

Lives in its own module rather than growing `main.py` (already ~1900
lines) further. `main.py` imports `app_app` from here to register it with
`app.add_typer(app_app, name="app")`, so importing `main.py`'s config/
diagnostic helpers (`OutputFormat`, `_exit_with_diagnostic`, ...) back at
THIS module's top level would be circular: `main` would not yet have
defined them at the point it imports `app_app`. `_main()` below defers
that import to call time instead, once `main.py` has fully imported.
"""

from __future__ import annotations

import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn
from uuid import uuid4

import typer

from playground.android.commands import (
    adb_shell,
    clear_cmd,
    input_cmd,
    install_cmd,
    install_multiple_cmd,
    launch_cmd,
    list_packages_cmd,
    logcat_cmd,
    pull_cmd,
    push_cmd,
    screenshot_cmd,
    stop_cmd,
    ui_dump_cmd,
    uninstall_cmd,
)
from playground.android.runner import TargetResult, run_guest_shell, run_on_targets, wait_booted
from playground.android.splits import find_base_apk
from playground.android.targets import AndroidTarget, resolve_targets
from playground.android.transport import (
    AdbInvocation,
    LocalAdb,
    render_guest_command,
    require_guest,
)
from playground.backend.dispatch import query_status
from playground.cli.tolerant_validation import validate_tolerant
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.ssh.argv import build_scp_argv, build_ssh_argv


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


app_app = typer.Typer(
    no_args_is_help=True, help="Install and drive Android apps on Redroid VMs."
)

_DEFAULT_TIMEOUT = 30.0
# Raised from 120.0/60.0: both were sized for a small APK on a local
# libvirt guest and are tight for a large (100+MB) split install or a
# slow cloud-VM upload link — see BUG note in the audit that introduced
# this comment. `install` now also pays for a boot-wait round trip before
# either of these ever runs (see _BOOT_WAIT_SECONDS below).
_INSTALL_TIMEOUT = 300.0
_SCP_TIMEOUT = 180.0
_CAPTURE_TIMEOUT = 30.0
_PUSH_PULL_TIMEOUT = 60.0
_BOOT_WAIT_SECONDS = 120
"""Passed to `wait_booted` as `attempts`: one getprop round trip per
second, so this doubles as the wait's budget in seconds."""

# Shared targeting/connection flags, reused verbatim across every verb.
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
OnOpt = Annotated[
    str | None, typer.Option("--on", help="Target one VM or device by name.")
]
RoleOpt = Annotated[
    str | None,
    typer.Option(
        "--role",
        help="Target every Android VM that has this role (VMs only; devices have no role).",
    ),
]
AllOpt = Annotated[
    bool, typer.Option("--all", help="Target every Android VM and device in the lab.")
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
        help=(
            "Where generated state lives. Defaults to `.playground/`. Must "
            "match the `apply` that adopted a device — a handset's binding "
            "is read from here, so a mismatched state dir reports a "
            "plugged-in device as not connected."
        ),
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
    state_dir: Path = Path(".playground"),
) -> list[AndroidTarget]:
    """Shared preamble: load, validate, default the lab, and pick devices.

    Mirrors the load/validate/default-lab/resolve sequence already
    duplicated across `exec`, `adb`, and `cp` in `main.py`, then layers
    `resolve_targets` on top to turn the resolved lab + live status into
    concrete Android devices. Exits (never returns) on any diagnostic
    error along the way.
    """
    cli_main = _main()
    loaded, diagnostics = cli_main._load_config_or_exit(
        config_dir, cli_main.OutputFormat.human
    )
    if not cli_main._has_errors(diagnostics):
        diagnostics.extend(validate_tolerant(loaded, lab=lab))
    cli_main._exit_on_errors(diagnostics, cli_main.OutputFormat.human, json_errors=False)
    cli_main._print_warnings(diagnostics)

    if lab is None:
        if len(loaded.labs) == 1:
            lab = next(iter(loaded.labs))
        else:
            _fail(
                Diagnostic(
                    id="config.app.lab_required",
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

    # Backend-neutral live status, same call `exec`/`adb`/`cp` use — vbox is
    # a 127.0.0.1 NAT port-forward, cloud a public IP, libvirt a guest IP,
    # all uniform via VmStatus.ssh_host / ssh_port.
    #
    # `state_dir` is threaded rather than left to `query_status`'s own
    # default: a device slot's binding is read from under it, so an
    # `apply --state-dir /srv/pg` followed by a default-state-dir `app`
    # verb would report an attached handset as `device.not_connected`.
    status, query_diagnostics = query_status(resolved, tofu_dir, state_dir)
    cli_main._exit_on_errors(
        query_diagnostics, cli_main.OutputFormat.human, json_errors=False
    )
    # `_exit_on_errors` only raises on errors; the WARNINGS it lets
    # through are the whole point here. `_device_statuses` downgrades
    # `device.adb_unavailable` to a warning so one missing `adb` cannot
    # hard-fail every verb that queries status — dropping it instead
    # would make `app list` say "device 'pixel7' is not attached" on a
    # machine that simply has no adb installed.
    cli_main._print_warnings(query_diagnostics)

    targets, target_diagnostics = resolve_targets(
        resolved, status, on=on, role=role, all_devices=all_devices, user=user,
    )
    cli_main._exit_on_errors(
        target_diagnostics, cli_main.OutputFormat.human, json_errors=False
    )
    return targets


def _echo_target_output(result: TargetResult) -> None:
    prefix = f"[{result.target.name}]"
    for line in result.stdout.splitlines():
        typer.echo(f"{prefix} {line}")
    for line in result.stderr.splitlines():
        typer.echo(f"{prefix} {line}", err=True)
    if not result.ok:
        typer.echo(f"{prefix} exited {result.returncode}", err=True)


def _exit_for_failures(failed: list[str], total: int) -> None:
    """Fan-out exit rule: 0 only if every device succeeded."""
    if failed:
        typer.echo(
            f"failed on {len(failed)}/{total} device(s): {', '.join(failed)}",
            err=True,
        )
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


def _dispatch(
    targets: list[AndroidTarget], inv: AdbInvocation, *, timeout: float = _DEFAULT_TIMEOUT
) -> None:
    """Run ``inv`` on every target and exit according to the results."""
    results = run_on_targets(targets, inv, timeout=timeout)
    for result in results:
        _echo_target_output(result)
    _exit_for_failures([r.target.name for r in results if not r.ok], len(results))


def _stream(target: AndroidTarget, inv: AdbInvocation) -> None:
    """Run ``inv`` with inherited stdio for `logcat --follow`.

    A single live device only — see `logcat_command`'s guard. Inherits
    stdio (rather than `run_on_targets`' capture) so output streams as it
    is produced instead of waiting for the process to exit.
    """
    guest = require_guest(target)
    argv = build_ssh_argv(
        guest.ssh_host,
        user=guest.ssh_user,
        port=guest.ssh_port,
        command=render_guest_command(inv),
    )
    typer.echo(f"[{target.name}] streaming — Ctrl-C to stop")
    try:
        completed = subprocess.run(argv, check=False)  # noqa: S603
    except FileNotFoundError as exc:
        _fail(
            Diagnostic(
                id="runtime.app.ssh_binary_missing",
                severity="error",
                message=f"failed to launch ssh: {exc}",
                source=SourceLocation(path="ssh"),
                suggestion="install openssh-client",
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None
    raise typer.Exit(code=completed.returncode)


def _scp_one(
    target: AndroidTarget, local: Path, remote_path: str, *, timeout: float = _SCP_TIMEOUT
) -> bool:
    """scp ``local`` to ``remote_path`` on ``target``. Prints and returns False on failure.

    ``target`` leads (not ``local``) so this matches the argument order
    every other guest-facing helper in this module uses (``target`` first)
    -- `stage_for` calls this positionally.
    """
    # scp's `host:path` operand IS shell-interpreted on the remote side
    # (legacy scp protocol execs a remote shell; even the SFTP-based
    # default still tokenizes it for some servers/older clients). Without
    # quoting, a filename with a space is "ambiguous target" and one with
    # a backtick or `$(...)` runs on the guest. Only the path half is
    # quoted -- the user@host half must stay bare.
    guest = require_guest(target)
    dst = f"{guest.ssh_user}@{guest.ssh_host}:{shlex.quote(remote_path)}"
    argv = build_scp_argv(str(local), dst, port=guest.ssh_port)
    try:
        completed = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        typer.echo(f"[{target.name}] scp failed: {exc}", err=True)
        return False
    if completed.returncode != 0:
        typer.echo(f"[{target.name}] scp failed: {completed.stderr.strip()}", err=True)
        return False
    return True


class _StagingFailed(Exception):
    """Raised by `stage_for` when scp to a guest target fails.

    `_scp_one` is declared `-> bool` and documents "returns False on
    failure" for exactly this reason: staging is a precondition for the
    adb command that follows, not a detail that command can be trusted to
    surface on its own. The `uuid4()`-suffixed staging path (see
    `stage_for`) already stops a failed scp from leaving a STALE file at
    the destination for that next adb command to silently pick up -- but
    relying on that as the failure signal makes correctness at the call
    site depend on a decision (the path being fresh, not deterministic)
    made somewhere else entirely. Raising here, like `_RetrievalFailed`
    does for the symmetric retrieval path, keeps the failure signal local
    and explicit instead of resting on a non-local invariant.
    """


def stage_for(target: AndroidTarget, local_path: Path) -> str:
    """Return the path adb should be handed for ``local_path``, raising
    `_StagingFailed` if a guest target's scp fails.

    A guest target runs adb on the VM, so the file has to travel there
    first (scp'd to a fresh, uuid-suffixed name under `/tmp` -- collision-
    safe the same way every other guest staging path in this module
    already is, and the reason a failed scp here can never leave a STALE
    file for the next call to mistake for a fresh one). A local target
    runs adb on this machine, where the file already is -- copying it
    would be a no-op at best and a wrong path at worst.
    """
    if isinstance(target.transport, LocalAdb):
        return str(local_path)
    remote = f"/tmp/playground-stage-{uuid4().hex}-{local_path.name}"
    if not _scp_one(target, local_path, remote):
        raise _StagingFailed(target.name)
    return remote


def _guest_cleanup(target: AndroidTarget, remote_path: str) -> None:
    """Delete a guest-side staging file, best-effort.

    A local target never has one -- adb ran directly on this machine, so
    there is nothing on any guest to remove. This used to ride along
    inside the adb command itself as `; rm -f <tmp>; exit $status`, which
    only worked because a guest shell was there to parse that trailing
    `;` -- see `push_command`'s history.

    Goes through `run_guest_shell` (not a bare `subprocess.run`) so a
    missing `ssh` binary or a timeout is swallowed the same way every
    other guest round trip in this module handles it, rather than raising
    out of what is meant to be a best-effort cleanup.
    """
    if isinstance(target.transport, LocalAdb):
        return
    run_guest_shell([target], f"rm -f {shlex.quote(remote_path)}", timeout=_DEFAULT_TIMEOUT)


def _capture_to_local(
    target: AndroidTarget,
    capture_cmd: AdbInvocation | None,
    device_path: str,
    local_path: Path,
    *,
    timeout: float = _CAPTURE_TIMEOUT,
    clean_device_path_first: bool = True,
) -> bool:
    """Run ``capture_cmd``, `adb pull` its output onto the guest, then stream
    it back to the controller over the SAME ssh session.

    A screenshot or ui-dump lands on the ANDROID device, `adb` runs on the
    GUEST, and the CLI runs on the controller — three hops. Piping the
    guest-local file through this ssh call's stdout covers the third hop
    without a second network round trip or a dependency on a separately
    invoked `scp` for what is, from the guest's point of view, a `cat`.

    ``guest_prefix()`` is NOT a single command — it is
    ``adb connect ... || true; adb -s ...`` — so interpolating it after a
    bare ``&&`` splits the chain at that embedded ``;``: bash treats
    everything from there on as a NEW, unconditional statement list, and
    ``capture_cmd``'s exit status stops gating anything. Because
    ``device_path`` is a fixed, reused filename for screenshot/ui-dump,
    that let a failed capture silently `adb pull` (and `cat` back) a
    STALE file from a previous run while reporting success. Every step
    that is itself built from ``guest_prefix()`` is therefore wrapped in a
    `{ ...; }` group before being used as an `&&` operand, so its own
    internal `;` cannot escape the group and its exit status is what the
    surrounding `&&` actually sees.

    ``clean_device_path_first`` deletes ``device_path`` on the device
    before running ``capture_cmd`` so a capture that fails partway can
    never leave a stale file to be mistaken for a fresh one. It defaults
    to True for screenshot/ui-dump (synthetic, CLI-owned paths that get
    overwritten every call) and must be False for the generic `pull`
    verb, where ``device_path`` is an arbitrary, pre-existing file the
    caller wants to read — deleting it before "capturing" would destroy
    the very thing being pulled.

    ``capture_cmd`` is ``None`` for `pull_command`: the file already
    exists on the device, so there is no capture step to run, only the
    pull below. `"true"` (a real, guaranteed-successful no-op) stands in
    for it in the chain -- a sentinel value, not a command, which is why
    it is expressed as `None` here rather than the literal string.
    """
    guest = require_guest(target)
    guest_tmp = f"/tmp/playground-pull-{uuid4().hex}"
    capture_cmd_str = (
        render_guest_command(capture_cmd) if capture_cmd is not None else "true"
    )
    capture_group = f"{{ {capture_cmd_str}; }}"
    # `render_guest_command(pull_cmd(...))`, not a hand-built `{prefix}
    # pull ...` string -- every adb invocation in this module goes
    # through a real builder now. Byte-identical to the old
    # string: neither `device_path` nor `guest_tmp` has shell
    # metacharacters in practice, but even if one did, this is the exact
    # quoting `render_guest_command` already applies everywhere else.
    pull_group = f"{{ {render_guest_command(pull_cmd(device_path, guest_tmp))} >/dev/null 2>&1; }}"
    chain = f"{capture_group} && {pull_group}"
    if clean_device_path_first:
        # `adb_shell`, not a hand-rolled `{prefix} shell rm -f ...`: it
        # quotes the whole device-side command as ONE token so `adb shell`
        # (which re-joins multi-word argv with spaces before the device's
        # own sh -c reparses it) cannot split or mis-parse it -- the same
        # double-hop hazard every other device-shell command in this
        # module is built to avoid.
        clean_cmd_str = render_guest_command(
            adb_shell(f"rm -f {shlex.quote(device_path)}")
        )
        clean_group = f"{{ {clean_cmd_str}; }}"
        chain = f"{clean_group} && {chain}"
    # `[ -s guest_tmp ]` rejects a capture that exited 0 but wrote zero
    # bytes (observed live: `screencap` can do this). The trailing
    # `status=$?; rm -f guest_tmp; exit $status` runs unconditionally
    # (outside the `&&` chain, after a plain `;`) so the guest-side temp
    # file is always removed — success or failure — without disturbing
    # the exit code the chain produced. See BUG D: nothing else in this
    # verb previously cleaned up its `/tmp` files.
    remote_cmd = (
        f"{chain} && [ -s {shlex.quote(guest_tmp)} ] && cat {shlex.quote(guest_tmp)}; "
        f"status=$?; rm -f {shlex.quote(guest_tmp)}; exit $status"
    )
    argv = build_ssh_argv(
        guest.ssh_host,
        user=guest.ssh_user,
        port=guest.ssh_port,
        command=remote_cmd,
        batch=True,
    )
    try:
        completed = subprocess.run(  # noqa: S603
            argv, capture_output=True, check=False, timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        typer.echo(f"[{target.name}] capture failed: {exc}", err=True)
        return False
    if completed.returncode != 0:
        stderr = completed.stderr.decode(errors="replace").strip()
        typer.echo(f"[{target.name}] capture failed: {stderr}", err=True)
        return False
    if not completed.stdout:
        # Belt-and-suspenders on top of the remote `[ -s ... ]` check --
        # never write a zero-byte file and call it a screenshot/dump.
        typer.echo(f"[{target.name}] capture failed: no data received", err=True)
        return False
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        typer.echo(f"[{target.name}] cannot create {local_path.parent}: {exc}", err=True)
        return False
    if local_path.is_dir():
        typer.echo(
            f"[{target.name}] {local_path} is a directory, expected a file path",
            err=True,
        )
        return False
    try:
        local_path.write_bytes(completed.stdout)
    except OSError as exc:
        typer.echo(f"[{target.name}] failed to write {local_path}: {exc}", err=True)
        return False
    typer.echo(f"[{target.name}] wrote {local_path}")
    return True


class _RetrievalFailed(Exception):
    """Raised by `retrieve_from` when bringing a device file back to the
    controller fails.

    The operator-facing message has already been echoed by the time this
    is raised (mirroring `_capture_to_local`'s echo-then-return-False
    pattern); this only carries enough for a caller's fan-out loop to add
    the target to its failed list, the same way a `False` return used to.
    """


def retrieve_from(target: AndroidTarget, remote_path: str, dest: Path) -> None:
    """Bring a device-side file at ``remote_path`` back to ``dest`` on this
    machine, raising `_RetrievalFailed` if it cannot.

    Guest: `adb pull` runs on the VM, so the file lands there first --
    `_capture_to_local` (with no capture step of its own, and
    `clean_device_path_first=False` since ``remote_path`` is an existing
    file this call did not create, not a synthetic path to pre-clean)
    streams it the rest of the way back over the same ssh session. Local:
    `adb pull` already runs on this machine, so ``dest`` is the direct
    destination and there is no second hop.
    """
    if isinstance(target.transport, LocalAdb):
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            typer.echo(f"[{target.name}] cannot create {dest.parent}: {exc}", err=True)
            raise _RetrievalFailed(target.name) from exc
        if dest.is_dir():
            typer.echo(
                f"[{target.name}] {dest} is a directory, expected a file path", err=True
            )
            raise _RetrievalFailed(target.name)
        result = run_on_targets(
            [target], pull_cmd(remote_path, str(dest)), timeout=_PUSH_PULL_TIMEOUT
        )[0]
        if not result.ok:
            _echo_target_output(result)
            raise _RetrievalFailed(target.name)
        typer.echo(f"[{target.name}] wrote {dest}")
        return
    if not _capture_to_local(
        target, None, remote_path, dest, timeout=_PUSH_PULL_TIMEOUT, clean_device_path_first=False
    ):
        raise _RetrievalFailed(target.name)


def _capture_and_retrieve(
    target: AndroidTarget,
    capture_cmd: AdbInvocation,
    device_path: str,
    local_path: Path,
    *,
    timeout: float = _CAPTURE_TIMEOUT,
) -> bool:
    """Run ``capture_cmd`` on the device, then bring ``device_path`` back to
    ``local_path``. Used by `screenshot_command`/`ui_dump_command`, which
    both need "run a capture, then fetch its output" rather than a plain
    pull of a pre-existing file.

    Guest transport keeps `_capture_to_local`'s single atomic ssh chain
    (clean-before-capture, capture, pull, cat-back, all gated by one `&&`
    chain -- see that function's BUG A docstring) untouched: it is already
    correct and its exact command shape is exercised by real-bash
    regression tests. Local transport has no shell chain to defend --
    adb runs right here, so a failed capture is just a nonzero
    `TargetResult` this function can check in Python before ever calling
    `retrieve_from`, which is what keeps a failed local capture from
    handing back a stale file the same way the guest chain does.
    """
    if isinstance(target.transport, LocalAdb):
        cap_result = run_on_targets([target], capture_cmd, timeout=timeout)[0]
        if not cap_result.ok:
            _echo_target_output(cap_result)
            return False
        try:
            retrieve_from(target, device_path, local_path)
        except _RetrievalFailed:
            return False
        return True
    return _capture_to_local(target, capture_cmd, device_path, local_path, timeout=timeout)


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _ensure_out_directory(out: Path, *, why: str) -> None:
    """``out.mkdir(parents=True, exist_ok=True)``, but exit with a clean
    Diagnostic instead of a bare traceback when ``--out`` already exists as
    a FILE.

    ``exist_ok=True`` only tolerates a pre-existing leaf that is itself a
    directory — Python still raises ``FileExistsError`` when the leaf
    exists as a plain file (e.g. ``playground app screenshot --out
    shot.png``, where the caller meant `ui-dump`'s single-file meaning of
    ``--out`` but this verb's ``--out`` names a DIRECTORY). This is the
    mirror image of the ``local_path.is_dir()`` check in
    ``_capture_to_local`` above, which already handles the flag pointing
    at an existing DIRECTORY when the verb wants a FILE.
    """
    if out.exists() and not out.is_dir():
        _fail(
            Diagnostic(
                id="config.app.out_is_a_file",
                severity="error",
                message=f"--out {str(out)!r} is a file, but {why} needs a directory",
                source=SourceLocation(path=str(out)),
                suggestion="pass a directory path, or remove the existing file first",
            )
        )
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _fail(
            Diagnostic(
                id="config.app.out_dir_uncreatable",
                severity="error",
                message=f"cannot create --out directory {str(out)!r}: {exc}",
                source=SourceLocation(path=str(out)),
            )
        )


@app_app.command(
    "install",
    help="Install an APK. A directory installs every *.apk inside as one split install.",
)
def install_command(
    path: Annotated[
        Path, typer.Argument(help="Local .apk file, or a directory of split APKs.")
    ],
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    if not path.exists():
        _fail(
            Diagnostic(
                id="config.app.apk_not_found",
                severity="error",
                message=f"APK path {str(path)!r} does not exist",
                source=SourceLocation(path=str(path)),
            )
        )

    if path.suffix in (".aab", ".apks"):
        # Same rule `stage_workload_files` enforces in planner/scheduling.py
        # for the declarative `android_app` workload — this is not a
        # directory, so it would otherwise skip the split-set gate below
        # entirely, scp a 100MB+ bundle to the guest, and fail with
        # `INSTALL_FAILED_INVALID_APK`: the exact "let the device answer"
        # outcome the split-set comment above names, just reached from the
        # opposite direction. Match the declarative side's wording so the
        # two are recognisably the same rule.
        _fail(
            Diagnostic(
                id="config.app.bundle_unsupported",
                severity="error",
                message=(
                    f"{str(path)!r} is an Android App Bundle — Redroid "
                    "installs APKs, not bundles"
                ),
                source=SourceLocation(path=str(path)),
                suggestion=(
                    "convert the bundle to device-specific APKs with "
                    "`bundletool build-apks` / `install-apks` (or ship the "
                    "split APKs directly); see "
                    "docs/research/android-emulation/app-install-launch.md"
                ),
            )
        )

    apks = sorted(path.glob("*.apk")) if path.is_dir() else [path]
    if not apks:
        _fail(
            Diagnostic(
                id="config.app.apk_not_found",
                severity="error",
                message=f"directory {str(path)!r} has no *.apk files",
                source=SourceLocation(path=str(path)),
            )
        )
    # Same split-set rule the declarative path enforces in
    # planner/scheduling.py. Without it the two halves of this feature
    # disagree about what a valid split set is: `playground apply` rejects
    # a base-less directory at validate time, while `playground app
    # install` happily ran install-multiple on it and let the device
    # answer -- which it does with `INSTALL_FAILED_INVALID_APK`, a much
    # worse place to find out.
    if path.is_dir():
        _, reason = find_base_apk(apks)
        if reason is not None:
            _fail(
                Diagnostic(
                    id="config.app.split_apk_invalid",
                    severity="error",
                    message=f"directory {str(path)!r} is not a valid split set: {reason}",
                    source=SourceLocation(path=str(path)),
                    suggestion=(
                        "a split set is one base APK (base.apk, or "
                        "<package>.apk from an .xapk) plus its "
                        "split_config.*/config.* siblings; or pass a single "
                        ".apk file directly"
                    ),
                )
            )

    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )

    failed: list[str] = []
    for target in targets:
        # Right after `apply` the Redroid container is up but Android is
        # still booting; installing against it intermittently fails with
        # `device offline` / `INSTALL_FAILED_PACKAGE_MANAGER_NOT_READY` /
        # `Can't find service: package`. Gate on a real boot signal instead
        # of racing it, and report plainly rather than surfacing whatever
        # raw adb error the race happened to produce.
        stragglers = wait_booted([target], attempts=_BOOT_WAIT_SECONDS, interval=1.0)
        if stragglers:
            typer.echo(
                f"[{target.name}] Android did not finish booting within "
                f"{_BOOT_WAIT_SECONDS}s; not installing",
                err=True,
            )
            failed.append(target.name)
            continue

        # `stage_for` is a no-op path rewrite on a local target (adb
        # already runs where the APK is) and an scp-to-`/tmp` on a guest
        # target -- NOT `/data/local/tmp`, which is an ANDROID path, while
        # scp's destination is the Ubuntu guest. `adb install` reads the
        # APK from wherever adb itself is running and pushes it to the
        # device. Found live -- the ssh/scp test shim accepts any path, so
        # this passed every unit test while failing on a real VM with
        # `scp: dest open "/data/local/tmp/...": No such file or directory`.
        remote_paths: list[str] = []
        try:
            for apk in apks:
                remote_paths.append(stage_for(target, apk))
        except _StagingFailed:
            # Best-effort: earlier APKs in this split set may have already
            # landed on the guest even though staging did not finish (BUG
            # D — otherwise never removed, filling the disk).
            for remote_path in remote_paths:
                _guest_cleanup(target, remote_path)
            failed.append(target.name)
            continue
        install_inv = (
            install_multiple_cmd(remote_paths)
            if len(remote_paths) > 1
            else install_cmd(remote_paths[0])
        )
        result = run_on_targets([target], install_inv, timeout=_INSTALL_TIMEOUT)[0]
        _echo_target_output(result)
        # Best-effort, after the install attempt either way (BUG D — a
        # staged APK left on the guest is otherwise never removed and
        # fills the disk); a no-op for a local target, which never staged
        # anything to begin with.
        for remote_path in remote_paths:
            _guest_cleanup(target, remote_path)
        if not result.ok:
            failed.append(target.name)

    _exit_for_failures(failed, len(targets))


@app_app.command("uninstall", help="Remove a package.")
def uninstall_command(
    package: Annotated[str, typer.Argument(help="Package name to remove.")],
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    _dispatch(targets, uninstall_cmd(package))


@app_app.command("launch", help="Start an app: deep link, explicit component, or monkey.")
def launch_command(
    package: Annotated[str, typer.Argument(help="Package name to launch.")],
    activity: Annotated[
        str | None,
        typer.Option("--activity", help="Explicit component, e.g. .MainActivity."),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option("--url", help="Deep link to open via ACTION_VIEW instead."),
    ] = None,
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    if url is not None and activity is not None:
        _fail(
            Diagnostic(
                id="config.app.conflicting_launch",
                severity="error",
                message="--url and --activity are mutually exclusive",
                source=SourceLocation(path="<argv>"),
            )
        )
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    _dispatch(targets, launch_cmd(package, activity=activity, url=url))


@app_app.command("stop", help="Force-stop a running app.")
def stop_command(
    package: Annotated[str, typer.Argument(help="Package name to stop.")],
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    _dispatch(targets, stop_cmd(package))


@app_app.command("clear", help="Reset an app's data so its next run starts fresh.")
def clear_command(
    package: Annotated[str, typer.Argument(help="Package name to clear.")],
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    _dispatch(targets, clear_cmd(package))


@app_app.command("list", help="List installed packages.")
def list_command(
    pattern: Annotated[
        str | None, typer.Option("--pattern", help="Substring filter on the package name.")
    ] = None,
    third_party: Annotated[
        bool, typer.Option("--third-party", help="Only list non-system packages (-3).")
    ] = False,
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    _dispatch(targets, list_packages_cmd(pattern=pattern, third_party=third_party))


@app_app.command("screenshot", help="Capture the screen and pull it to the controller.")
def screenshot_command(
    out: Annotated[Path, typer.Option("--out", help="Directory to write PNG(s) to.")] = Path("."),
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    _ensure_out_directory(out, why="screenshot (one PNG per matched device)")
    device_path = "/sdcard/playground-screenshot.png"
    failed: list[str] = []
    for target in targets:
        # Fan-out writes <out>/<vm>-<timestamp>.png so concurrent devices
        # cannot collide on one filename.
        local_path = out / f"{target.name}-{_timestamp()}.png"
        if not _capture_and_retrieve(target, screenshot_cmd(device_path), device_path, local_path):
            failed.append(target.name)
    _exit_for_failures(failed, len(targets))


@app_app.command("ui-dump", help="Dump the current view hierarchy as XML.")
def ui_dump_command(
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            help="File (single device) or directory (fan-out) to write XML to.",
        ),
    ] = Path("ui-dump.xml"),
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    device_path = "/sdcard/playground-ui-dump.xml"
    multi = len(targets) > 1
    if multi:
        _ensure_out_directory(
            out, why="ui-dump with more than one matched device (fan-out)"
        )
    failed: list[str] = []
    for target in targets:
        local_path = out / f"{target.name}-{_timestamp()}.xml" if multi else out
        if not _capture_and_retrieve(target, ui_dump_cmd(device_path), device_path, local_path):
            failed.append(target.name)
    _exit_for_failures(failed, len(targets))


_VALID_LOG_PRIORITIES = ("V", "D", "I", "W", "E", "F")
"""adb logcat's own priority letters (Verbose/Debug/Info/Warn/Error/Fatal).

`S` (silent) is deliberately excluded: it is not a *minimum* priority
(the meaning `--priority` documents) — it hides everything, which reads
as a bug report ("nothing shows up") rather than a filter."""


@app_app.command("logcat", help="Dump, follow, filter, or clear the Android log.")
def logcat_command(
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Stream instead of dumping the buffer.")
    ] = False,
    lines: Annotated[
        int | None, typer.Option("--lines", "-n", help="Limit the dump to the last N lines.")
    ] = None,
    package: Annotated[
        str | None,
        typer.Option(
            "--package",
            help=(
                "Only show this package's output. Resolves its pid ON THE "
                "DEVICE via `pidof`; if the app is not running, fails with "
                "a clear message instead of an empty filter."
            ),
        ),
    ] = None,
    tag: Annotated[
        str | None,
        typer.Option(
            "--tag", help="Only show this log tag (`logcat -s TAG:LEVEL`; silences the rest)."
        ),
    ] = None,
    priority: Annotated[
        str | None,
        typer.Option(
            "--priority", help="Minimum priority to show: V, D, I, W, E, or F (case-insensitive)."
        ),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option(
            "--clear",
            help=(
                "Clear the log buffer and exit without dumping anything. "
                "Cannot be combined with any other logcat flag."
            ),
        ),
    ] = False,
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    # `--clear` is a distinct operation (wipe, don't read) that ignores
    # every other logcat flag by construction (`logcat_cmd` short-circuits
    # on it) — combining it with a filter or --follow/--lines reads as the
    # caller expecting BOTH a clear AND a dump/stream in one call, which
    # this single adb invocation cannot do. Reject up front rather than
    # silently discarding whichever flag `logcat_cmd` would have ignored.
    if clear and (
        follow or lines is not None or package is not None or tag is not None
        or priority is not None
    ):
        _fail(
            Diagnostic(
                id="config.app.logcat_clear_exclusive",
                severity="error",
                message=(
                    "--clear cannot be combined with --follow, --lines, "
                    "--package, --tag, or --priority"
                ),
                source=SourceLocation(path="<argv>"),
                suggestion=(
                    "run `playground app logcat --clear` on its own, then a "
                    "separate `playground app logcat ...` call to read fresh output"
                ),
            )
        )
    if priority is not None:
        normalized = priority.upper()
        if normalized not in _VALID_LOG_PRIORITIES:
            _fail(
                Diagnostic(
                    id="config.app.invalid_priority",
                    severity="error",
                    message=(
                        f"--priority {priority!r} is not valid; choose one of "
                        + ", ".join(_VALID_LOG_PRIORITIES)
                    ),
                    source=SourceLocation(path="<argv>"),
                    suggestion="pass one of V, D, I, W, E, F (case-insensitive)",
                )
            )
        priority = normalized
    # `--all` means "every device" regardless of how many exist today, and
    # a live stream cannot multiplex — reject the combination up front
    # rather than only when a second device happens to be applied later.
    if follow and all_devices:
        _fail(
            Diagnostic(
                id="config.app.follow_needs_one_device",
                severity="error",
                message="--follow needs exactly one device; pass --on instead of --all",
                source=SourceLocation(path="<argv>"),
                suggestion="pass --on <vm>",
            )
        )
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    if follow and len(targets) > 1:
        _fail(
            Diagnostic(
                id="config.app.follow_needs_one_device",
                severity="error",
                message=(
                    f"--follow needs exactly one device; {len(targets)} matched "
                    "— pass --on to pick one"
                ),
                source=SourceLocation(path="<argv>"),
                suggestion="pass --on <vm>",
            )
        )
    command = logcat_cmd(
        follow=follow, lines=lines, package=package, tag=tag, priority=priority, clear=clear
    )
    if follow:
        _stream(targets[0], command)
    else:
        _dispatch(targets, command)


@app_app.command("input", help="Send an input event: text, tap, swipe, key, ...")
def input_command(
    kind: Annotated[str, typer.Argument(help="text | tap | swipe | key | ...")],
    args: Annotated[
        list[str] | None, typer.Argument(help="Arguments for the input subcommand.")
    ] = None,
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    _dispatch(targets, input_cmd(kind, args or []))


@app_app.command("push", help="Push a local file onto the device.")
def push_command(
    src: Annotated[Path, typer.Argument(help="Local file to push onto the device.")],
    dest: Annotated[str, typer.Argument(help="Destination path on the device.")],
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    if not src.is_file():
        _fail(
            Diagnostic(
                id="config.app.local_path_missing",
                severity="error",
                message=f"local file {str(src)!r} does not exist",
                source=SourceLocation(path=str(src)),
            )
        )
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    failed: list[str] = []
    for target in targets:
        # `stage_for` scps to a guest target's `/tmp` (adb runs there) and
        # is a no-op path rewrite on a local target (adb runs right here,
        # where `src` already is).
        try:
            staged = stage_for(target, src)
        except _StagingFailed:
            failed.append(target.name)
            continue
        result = run_on_targets(
            [target], push_cmd(staged, dest), timeout=_PUSH_PULL_TIMEOUT
        )[0]
        _echo_target_output(result)
        # BUG D: a staged file was never removed. Best-effort, after the
        # push attempt either way; a no-op for a local target, which never
        # staged anything to begin with.
        _guest_cleanup(target, staged)
        if not result.ok:
            failed.append(target.name)
    _exit_for_failures(failed, len(targets))


@app_app.command("pull", help="Pull a file off the device onto the controller.")
def pull_command(
    src: Annotated[str, typer.Argument(help="Source path on the device.")],
    dest: Annotated[Path, typer.Argument(help="Local destination file.")],
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    if len(targets) > 1:
        _fail(
            Diagnostic(
                id="config.app.pull_needs_one_device",
                severity="error",
                message=(
                    f"pull writes one local file; {len(targets)} devices matched "
                    "— pass --on to pick one"
                ),
                source=SourceLocation(path="<argv>"),
                suggestion="pass --on <vm>",
            )
        )
    target = targets[0]
    # `retrieve_from` never deletes `src` first: it is an arbitrary,
    # pre-existing file the caller wants to read, not a synthetic path
    # this CLI owns and recreates every call (unlike screenshot/ui-dump).
    try:
        retrieve_from(target, src, dest)
    except _RetrievalFailed:
        raise typer.Exit(code=1) from None


@app_app.command(
    "shell",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    help=(
        "Run a raw `adb shell` command — the escape hatch for anything not "
        "covered above. Each word after `--` is quoted individually (same as "
        "`playground exec`), so it reaches the device as ONE argv, not "
        "something a shell re-splits on your behalf: a pipeline or "
        "redirection written directly (`... -- ps | grep x`) is passed "
        "through literally and fails with 'inaccessible or not found', "
        "because there is no shell on the device to interpret `|`. Invoke "
        "one explicitly instead: `playground app shell -- sh -c 'ps | grep x'`."
    ),
)
def shell_command(
    ctx: typer.Context,
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    command_args = list(ctx.args)
    if not command_args:
        _fail(
            Diagnostic(
                id="config.app.no_command",
                severity="error",
                message="no command given after `shell`; e.g. `playground app shell -- getprop`",
                source=SourceLocation(path="<argv>"),
            )
        )
    remote_command = " ".join(shlex.quote(arg) for arg in command_args)
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir, state_dir=state_dir,
    )
    _dispatch(targets, adb_shell(remote_command))
