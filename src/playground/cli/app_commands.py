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
    adb_prefix,
    adb_shell,
    clear_cmd,
    input_cmd,
    install_cmd,
    install_multiple_cmd,
    launch_cmd,
    list_packages_cmd,
    logcat_cmd,
    screenshot_cmd,
    stop_cmd,
    ui_dump_cmd,
    uninstall_cmd,
    wait_booted_cmd,
)
from playground.android.runner import TargetResult, run_on_targets
from playground.android.targets import AndroidTarget, resolve_targets
from playground.backend.dispatch import query_status
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.ssh.argv import build_scp_argv, build_ssh_argv
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
# either of these ever runs (see _BOOT_WAIT_TIMEOUT below).
_INSTALL_TIMEOUT = 300.0
_SCP_TIMEOUT = 180.0
_CAPTURE_TIMEOUT = 30.0
_PUSH_PULL_TIMEOUT = 60.0
_BOOT_WAIT_SECONDS = 120
"""Passed to `wait_booted_cmd`: how long the GUEST-side poll loop waits."""
_BOOT_WAIT_TIMEOUT = float(_BOOT_WAIT_SECONDS + 30)
"""ssh/subprocess timeout for the boot-wait call itself — must exceed the
guest-side loop's own budget or we would time out the ssh call before the
loop has a chance to give up on its own and report a clean failure."""

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
OnOpt = Annotated[str | None, typer.Option("--on", help="Target one VM by name.")]
RoleOpt = Annotated[
    str | None,
    typer.Option("--role", help="Target every Android VM that has this role."),
]
AllOpt = Annotated[
    bool, typer.Option("--all", help="Target every Android VM in the lab.")
]
UserOpt = Annotated[str, typer.Option("--user", help="SSH user (default: ubuntu).")]
ConfigDirOpt = Annotated[
    Path, typer.Option("--config-dir", "-c", help="Config directory to load.")
]
TofuDirOpt = Annotated[
    Path, typer.Option("--tofu-dir", help="OpenTofu working directory.")
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
        diagnostics.extend(validate_loaded_config(loaded, lab=lab))
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
    status, query_diagnostics = query_status(resolved, tofu_dir)
    cli_main._exit_on_errors(
        query_diagnostics, cli_main.OutputFormat.human, json_errors=False
    )

    targets, target_diagnostics = resolve_targets(
        resolved, status, on=on, role=role, all_devices=all_devices, user=user,
    )
    cli_main._exit_on_errors(
        target_diagnostics, cli_main.OutputFormat.human, json_errors=False
    )
    return targets


def _echo_target_output(result: TargetResult) -> None:
    prefix = f"[{result.target.vm_name}]"
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
    targets: list[AndroidTarget], command: str, *, timeout: float = _DEFAULT_TIMEOUT
) -> None:
    """Run ``command`` on every target and exit according to the results."""
    results = run_on_targets(targets, command, timeout=timeout)
    for result in results:
        _echo_target_output(result)
    _exit_for_failures([r.target.vm_name for r in results if not r.ok], len(results))


def _stream(target: AndroidTarget, command: str) -> None:
    """Run ``command`` with inherited stdio for `logcat --follow`.

    A single live device only — see `logcat_command`'s guard. Inherits
    stdio (rather than `run_on_targets`' capture) so output streams as it
    is produced instead of waiting for the process to exit.
    """
    argv = build_ssh_argv(
        target.ssh_host, user=target.ssh_user, port=target.ssh_port, command=command
    )
    typer.echo(f"[{target.vm_name}] streaming — Ctrl-C to stop")
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
    local: Path, target: AndroidTarget, remote_path: str, *, timeout: float = _SCP_TIMEOUT
) -> bool:
    """scp ``local`` to ``remote_path`` on ``target``. Prints and returns False on failure."""
    # scp's `host:path` operand IS shell-interpreted on the remote side
    # (legacy scp protocol execs a remote shell; even the SFTP-based
    # default still tokenizes it for some servers/older clients). Without
    # quoting, a filename with a space is "ambiguous target" and one with
    # a backtick or `$(...)` runs on the guest. Only the path half is
    # quoted -- the user@host half must stay bare.
    dst = f"{target.ssh_user}@{target.ssh_host}:{shlex.quote(remote_path)}"
    argv = build_scp_argv(str(local), dst, port=target.ssh_port)
    try:
        completed = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        typer.echo(f"[{target.vm_name}] scp failed: {exc}", err=True)
        return False
    if completed.returncode != 0:
        typer.echo(f"[{target.vm_name}] scp failed: {completed.stderr.strip()}", err=True)
        return False
    return True


def _capture_to_local(
    target: AndroidTarget,
    capture_cmd: str,
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

    ``adb_prefix()`` is NOT a single command — it is
    ``adb connect ... || true; adb -s ...`` — so interpolating it after a
    bare ``&&`` splits the chain at that embedded ``;``: bash treats
    everything from there on as a NEW, unconditional statement list, and
    ``capture_cmd``'s exit status stops gating anything. Because
    ``device_path`` is a fixed, reused filename for screenshot/ui-dump,
    that let a failed capture silently `adb pull` (and `cat` back) a
    STALE file from a previous run while reporting success. Every step
    that is itself built from ``adb_prefix()`` is therefore wrapped in a
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
    """
    guest_tmp = f"/tmp/playground-pull-{uuid4().hex}"
    prefix = adb_prefix()
    capture_group = f"{{ {capture_cmd}; }}"
    pull_group = (
        f"{{ {prefix} pull {shlex.quote(device_path)} "
        f"{shlex.quote(guest_tmp)} >/dev/null 2>&1; }}"
    )
    chain = f"{capture_group} && {pull_group}"
    if clean_device_path_first:
        # `adb_shell`, not a hand-rolled `{prefix} shell rm -f ...`: it
        # quotes the whole device-side command as ONE token so `adb shell`
        # (which re-joins multi-word argv with spaces before the device's
        # own sh -c reparses it) cannot split or mis-parse it -- the same
        # double-hop hazard every other device-shell command in this
        # module is built to avoid.
        clean_group = f"{{ {adb_shell(f'rm -f {shlex.quote(device_path)}')}; }}"
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
        target.ssh_host,
        user=target.ssh_user,
        port=target.ssh_port,
        command=remote_cmd,
        batch=True,
    )
    try:
        completed = subprocess.run(  # noqa: S603
            argv, capture_output=True, check=False, timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        typer.echo(f"[{target.vm_name}] capture failed: {exc}", err=True)
        return False
    if completed.returncode != 0:
        stderr = completed.stderr.decode(errors="replace").strip()
        typer.echo(f"[{target.vm_name}] capture failed: {stderr}", err=True)
        return False
    if not completed.stdout:
        # Belt-and-suspenders on top of the remote `[ -s ... ]` check --
        # never write a zero-byte file and call it a screenshot/dump.
        typer.echo(f"[{target.vm_name}] capture failed: no data received", err=True)
        return False
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        typer.echo(f"[{target.vm_name}] cannot create {local_path.parent}: {exc}", err=True)
        return False
    if local_path.is_dir():
        typer.echo(
            f"[{target.vm_name}] {local_path} is a directory, expected a file path",
            err=True,
        )
        return False
    try:
        local_path.write_bytes(completed.stdout)
    except OSError as exc:
        typer.echo(f"[{target.vm_name}] failed to write {local_path}: {exc}", err=True)
        return False
    typer.echo(f"[{target.vm_name}] wrote {local_path}")
    return True


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


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
    if path.is_dir() and not any(a.name == "base.apk" for a in apks):
        _fail(
            Diagnostic(
                id="config.app.split_apk_invalid",
                severity="error",
                message=(
                    f"directory {str(path)!r} has no base.apk; a split set "
                    "needs exactly one base.apk alongside its "
                    "split_config.*.apk siblings"
                ),
                source=SourceLocation(path=str(path)),
                suggestion=(
                    "pass the single .apk file directly, or add the base.apk "
                    "the splits belong to"
                ),
            )
        )

    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
    )

    failed: list[str] = []
    for target in targets:
        # Right after `apply` the Redroid container is up but Android is
        # still booting; installing against it intermittently fails with
        # `device offline` / `INSTALL_FAILED_PACKAGE_MANAGER_NOT_READY` /
        # `Can't find service: package`. Gate on a real boot signal instead
        # of racing it, and report plainly rather than surfacing whatever
        # raw adb error the race happened to produce.
        boot = run_on_targets(
            [target], wait_booted_cmd(_BOOT_WAIT_SECONDS), timeout=_BOOT_WAIT_TIMEOUT
        )[0]
        if not boot.ok:
            typer.echo(
                f"[{target.vm_name}] Android did not finish booting within "
                f"{_BOOT_WAIT_SECONDS}s; not installing",
                err=True,
            )
            failed.append(target.vm_name)
            continue

        # Stage on the VM's own filesystem, NOT /data/local/tmp: that is an
        # ANDROID path, and scp's destination is the Ubuntu guest, which has
        # no such directory. `adb install` reads the APK from the machine
        # running adb (the guest) and pushes it to the device itself.
        # Found live -- the ssh/scp test shim accepts any path, so this
        # passed every unit test while failing on a real VM with
        # `scp: dest open "/data/local/tmp/...": No such file or directory`.
        stage_dir = f"/tmp/playground-apk-{uuid4().hex}"
        remote_paths = [f"{stage_dir}/{apk.name}" for apk in apks]
        mkdir = run_on_targets(
            [target], f"mkdir -p {shlex.quote(stage_dir)}", timeout=_DEFAULT_TIMEOUT
        )[0]
        if not mkdir.ok:
            _echo_target_output(mkdir)
            failed.append(target.vm_name)
            continue
        if not all(
            _scp_one(apk, target, remote_path)
            for apk, remote_path in zip(apks, remote_paths, strict=True)
        ):
            # Best-effort: some APKs may have already landed on the guest
            # even though the whole install did not go ahead (BUG D — this
            # stage_dir is otherwise never removed and fills the disk).
            run_on_targets(
                [target], f"rm -rf {shlex.quote(stage_dir)}", timeout=_DEFAULT_TIMEOUT
            )
            failed.append(target.vm_name)
            continue
        install_command_str = (
            install_multiple_cmd(remote_paths)
            if len(remote_paths) > 1
            else install_cmd(remote_paths[0])
        )
        # Fold stage_dir cleanup into the SAME ssh round trip as the
        # install (rather than a separate call) so a failed install still
        # removes the staged APK(s), and the real install exit status is
        # still what decides pass/fail.
        full_command = (
            f"{install_command_str}; status=$?; "
            f"rm -rf {shlex.quote(stage_dir)}; exit $status"
        )
        result = run_on_targets([target], full_command, timeout=_INSTALL_TIMEOUT)[0]
        _echo_target_output(result)
        if not result.ok:
            failed.append(target.vm_name)

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
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
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
        config_dir=config_dir, tofu_dir=tofu_dir,
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
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
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
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
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
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
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
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
    )
    out.mkdir(parents=True, exist_ok=True)
    device_path = "/sdcard/playground-screenshot.png"
    failed: list[str] = []
    for target in targets:
        # Fan-out writes <out>/<vm>-<timestamp>.png so concurrent devices
        # cannot collide on one filename.
        local_path = out / f"{target.vm_name}-{_timestamp()}.png"
        if not _capture_to_local(target, screenshot_cmd(device_path), device_path, local_path):
            failed.append(target.vm_name)
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
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
    )
    device_path = "/sdcard/playground-ui-dump.xml"
    multi = len(targets) > 1
    if multi:
        out.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    for target in targets:
        local_path = out / f"{target.vm_name}-{_timestamp()}.xml" if multi else out
        if not _capture_to_local(target, ui_dump_cmd(device_path), device_path, local_path):
            failed.append(target.vm_name)
    _exit_for_failures(failed, len(targets))


@app_app.command("logcat", help="Dump or follow the Android log.")
def logcat_command(
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Stream instead of dumping the buffer.")
    ] = False,
    lines: Annotated[
        int | None, typer.Option("--lines", "-n", help="Limit the dump to the last N lines.")
    ] = None,
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
) -> None:
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
        config_dir=config_dir, tofu_dir=tofu_dir,
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
    command = logcat_cmd(follow=follow, lines=lines)
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
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
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
        config_dir=config_dir, tofu_dir=tofu_dir,
    )
    failed: list[str] = []
    for target in targets:
        guest_tmp = f"/tmp/playground-push-{uuid4().hex}-{src.name}"
        if not _scp_one(src, target, guest_tmp):
            failed.append(target.vm_name)
            continue
        # BUG D: guest_tmp was never removed. Clean it up in the same ssh
        # round trip, after the push attempt, regardless of outcome
        # (`; status=$?; rm -f ...; exit $status`, plain sequencing after
        # a `;` — not `&&` — is exactly what makes the cleanup unconditional).
        push_cmd = f"{adb_prefix()} push {shlex.quote(guest_tmp)} {shlex.quote(dest)}"
        command = f"{push_cmd}; status=$?; rm -f {shlex.quote(guest_tmp)}; exit $status"
        result = run_on_targets([target], command, timeout=_PUSH_PULL_TIMEOUT)[0]
        _echo_target_output(result)
        if not result.ok:
            failed.append(target.vm_name)
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
) -> None:
    targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
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
    # `pull`'s device_path is an arbitrary, pre-existing file the caller
    # wants to read, not a synthetic path this CLI owns and recreates
    # every call (unlike screenshot/ui-dump) — deleting it first would
    # destroy the very file being pulled.
    if not _capture_to_local(
        target, "true", src, dest, timeout=_PUSH_PULL_TIMEOUT, clean_device_path_first=False
    ):
        raise typer.Exit(code=1)


@app_app.command(
    "shell",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    help="Run a raw `adb shell` command — the escape hatch for anything not covered above.",
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
        config_dir=config_dir, tofu_dir=tofu_dir,
    )
    _dispatch(targets, adb_shell(remote_command))
