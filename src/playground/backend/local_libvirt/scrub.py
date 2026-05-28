"""Scrub libvirt resources by lab name, ignoring tofu state.

The cleanup path of last resort. ``execute_reset`` calls this when an
operator runs ``playground reset <lab>``, typically because tofu state
got out of sync with reality (corrupt state, manual ``virsh undefine``,
lab YAML renamed without a prior destroy).

The contract is strict: every libvirt resource the lab's tofu module
would produce is removed if it exists, and we never touch anything
the lab didn't name. Specifically we **do not** delete the shared
``ubuntu-noble.qcow2`` base image — multiple labs share it, and
re-downloading on the next apply is expensive.

Each individual ``virsh`` call is best-effort against "already gone"
errors because the goal is idempotence: a second ``reset`` on the
same lab succeeds with no work. Fatal errors are reported as
diagnostics so the caller can decide whether to fail the run.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from playground.events import EventBus
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.runs import StepResult

_DEFAULT_POOL = "default"
_BASE_IMAGE_NAME = "ubuntu-noble.qcow2"  # shared; never touch.

# Substrings that mark "resource doesn't exist" in virsh stderr so we
# can tolerate them even on the rare path where we skip the listing
# pre-check. The listing path is preferred — this is the safety net.
_NOT_FOUND_NEEDLES = (
    "failed to get domain",
    "failed to get network",
    "Storage volume not found",
    "no storage vol with matching name",
    "Domain not found",
    "Network not found",
)


@dataclass
class _ScrubLog:
    """Captures every virsh invocation for the step log."""

    lines: list[str]
    fatal: list[Diagnostic]

    def record(self, args: list[str], result: subprocess.CompletedProcess[str]) -> None:
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        self.lines.append(f"[{stamp}] virsh {' '.join(args)}  (exit {result.returncode})")
        if result.stdout.strip():
            self.lines.append(f"  stdout: {result.stdout.strip()}")
        if result.stderr.strip():
            self.lines.append(f"  stderr: {result.stderr.strip()}")


def scrub_lab(
    *,
    resolved: ResolvedLab,
    log_path: Path,
    bus: EventBus,
    run_id: str,
    pool: str = _DEFAULT_POOL,
) -> tuple[StepResult, list[Diagnostic]]:
    """Force-remove the lab's libvirt resources by name.

    Returns ``(StepResult, diagnostics)`` analogous to other apply/destroy
    step helpers. ``StepResult.exit_code`` is 0 when every resource that
    existed was successfully removed (including the case where nothing
    existed). Non-zero when virsh itself is missing/unreachable or a
    real (non-"not found") error landed.
    """
    started_at = _isoformat()
    command = ["virsh", "--scrub-by-name", resolved.lab_name]
    log = _ScrubLog(lines=[f"# scrub-by-name for lab {resolved.lab_name!r}"], fatal=[])

    if shutil.which("virsh") is None:
        diagnostic = Diagnostic(
            id="runtime.reset.virsh_missing",
            severity="error",
            message="`virsh` is not on PATH; cannot scrub libvirt resources by name",
            source=SourceLocation(path="host"),
            suggestion="sudo apt install -y libvirt-clients",
        )
        log.fatal.append(diagnostic)
        log.lines.append("FATAL: virsh missing")
        _write_log(log_path, log)
        return _step(command, exit_code=127, log_path=log_path, started_at=started_at), [diagnostic]

    # Enumerate existing resources up front. We tolerate "not found" by
    # *only operating on what we see in the listing*, so each virsh
    # destroy/undefine call is unconditional on the resource existing.
    domains, dom_diag = _list_domains(log)
    if dom_diag is not None:
        log.fatal.append(dom_diag)
        _write_log(log_path, log)
        return _step(command, exit_code=1, log_path=log_path, started_at=started_at), [dom_diag]

    networks, net_diag = _list_networks(log)
    if net_diag is not None:
        log.fatal.append(net_diag)
        _write_log(log_path, log)
        return _step(command, exit_code=1, log_path=log_path, started_at=started_at), [net_diag]

    removed: list[str] = []
    soft_failures: list[Diagnostic] = []

    volumes, vol_diag = _list_volumes(pool, log)
    if vol_diag is not None:
        # Non-fatal: an absent storage pool just means "no per-VM disks
        # to clean up". Surface the warning but continue with domain +
        # network cleanup.
        log.lines.append(f"WARN: vol-list {pool!r} failed — skipping per-VM disk cleanup")
        soft_failures.append(vol_diag)
        volumes = set()

    # Domains first — undefine after stopping. Disk volumes get removed
    # separately so we keep precise control over the base image.
    for vm in resolved.vms:
        if vm.name in domains:
            bus.publish(
                run_id, "log_line", {"step": "scrub-libvirt", "line": f"domain {vm.name!r}"}
            )
            stop = _run_virsh(["destroy", vm.name], log)
            if stop.returncode != 0 and not _is_not_found_or_inactive(stop):
                soft_failures.append(_scrub_failed(vm.name, "destroy", stop))
            undef = _run_virsh(
                [
                    "undefine",
                    "--nvram",
                    "--managed-save",
                    "--snapshots-metadata",
                    vm.name,
                ],
                log,
            )
            if undef.returncode != 0 and not _is_not_found(undef):
                soft_failures.append(_scrub_failed(vm.name, "undefine", undef))
            else:
                removed.append(f"domain/{vm.name}")

        for vol_name in (f"{vm.name}.qcow2", f"commoninit-{vm.name}.iso"):
            if vol_name == _BASE_IMAGE_NAME:
                continue  # belt-and-braces; lab VMs never use this name
            if vol_name in volumes:
                delete = _run_virsh(
                    ["vol-delete", "--pool", pool, vol_name], log
                )
                if delete.returncode != 0 and not _is_not_found(delete):
                    soft_failures.append(_scrub_failed(vol_name, "vol-delete", delete))
                else:
                    removed.append(f"volume/{vol_name}")

    # Networks last so any VM still referencing them is gone first.
    for net in resolved.networks:
        if net.name in networks:
            bus.publish(
                run_id, "log_line", {"step": "scrub-libvirt", "line": f"network {net.name!r}"}
            )
            stop = _run_virsh(["net-destroy", net.name], log)
            if stop.returncode != 0 and not _is_not_found_or_inactive(stop):
                soft_failures.append(_scrub_failed(net.name, "net-destroy", stop))
            undef = _run_virsh(["net-undefine", net.name], log)
            if undef.returncode != 0 and not _is_not_found(undef):
                soft_failures.append(_scrub_failed(net.name, "net-undefine", undef))
            else:
                removed.append(f"network/{net.name}")

    if removed:
        log.lines.append(f"removed: {', '.join(removed)}")
    else:
        log.lines.append("nothing to remove (already clean)")

    _write_log(log_path, log)

    # Step exit code reflects whether any ERROR-severity diagnostic
    # fired. Warnings (e.g. missing storage pool) don't bump exit_code
    # because the step still did its primary job.
    has_error = any(d.severity == "error" for d in soft_failures)
    exit_code = 1 if has_error else 0
    return (
        _step(command, exit_code=exit_code, log_path=log_path, started_at=started_at),
        soft_failures,
    )


# ---------------------------------------------------------------------------
# virsh helpers
# ---------------------------------------------------------------------------


def _run_virsh(
    args: list[str], log: _ScrubLog, *, timeout: float = 15.0
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["virsh", "--quiet", "--connect", "qemu:///system", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.lines.append(f"virsh {' '.join(args)}: {exc}")
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr=str(exc)
        )
    log.record(args, result)
    return result


def _list_domains(log: _ScrubLog) -> tuple[set[str], Diagnostic | None]:
    result = _run_virsh(["list", "--all", "--name"], log)
    if result.returncode != 0:
        return set(), Diagnostic(
            id="runtime.reset.virsh_unreachable",
            severity="error",
            message=(
                f"`virsh list` failed (exit {result.returncode}): "
                f"{result.stderr.strip() or '(no stderr)'}"
            ),
            source=SourceLocation(path="host"),
            suggestion=(
                "verify libvirtd is running and your user is in the libvirt "
                "group: `sudo systemctl status libvirtd && groups`"
            ),
        )
    return _parse_names(result.stdout), None


def _list_networks(log: _ScrubLog) -> tuple[set[str], Diagnostic | None]:
    result = _run_virsh(["net-list", "--all", "--name"], log)
    if result.returncode != 0:
        return set(), Diagnostic(
            id="runtime.reset.virsh_unreachable",
            severity="error",
            message=(
                f"`virsh net-list` failed (exit {result.returncode}): "
                f"{result.stderr.strip() or '(no stderr)'}"
            ),
            source=SourceLocation(path="host"),
            suggestion="verify libvirtd is running",
        )
    return _parse_names(result.stdout), None


def _list_volumes(
    pool: str, log: _ScrubLog
) -> tuple[set[str], Diagnostic | None]:
    result = _run_virsh(["vol-list", "--pool", pool], log)
    if result.returncode != 0:
        return set(), Diagnostic(
            id="runtime.reset.pool_unreachable",
            severity="warning",
            message=(
                f"`virsh vol-list --pool {pool}` failed (exit "
                f"{result.returncode}): {result.stderr.strip() or '(no stderr)'}"
            ),
            source=SourceLocation(path="host"),
            suggestion=(
                "create the pool or pass a different `--pool` flag; "
                "running `playground doctor` will diagnose."
            ),
        )
    # vol-list (without --details) prints `<name>  <path>` per line. The
    # first column is the volume name.
    names: set[str] = set()
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("---") or line.startswith("Name "):
            continue
        names.add(line.split()[0])
    return names, None


def _parse_names(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if line.strip()}


def _is_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    needle = result.stderr.lower()
    return any(n.lower() in needle for n in _NOT_FOUND_NEEDLES)


def _is_not_found_or_inactive(result: subprocess.CompletedProcess[str]) -> bool:
    return _is_not_found(result) or "not active" in result.stderr.lower() or (
        "is not running" in result.stderr.lower()
    )


def _scrub_failed(
    target: str, action: str, result: subprocess.CompletedProcess[str]
) -> Diagnostic:
    return Diagnostic(
        id="runtime.reset.scrub_failed",
        severity="error",
        message=(
            f"virsh {action} {target!r} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or '(no stderr)'}"
        ),
        source=SourceLocation(path="host"),
        suggestion=(
            f"investigate manually: `virsh {action} {target}`. The lab may "
            "still have residual resources after this reset."
        ),
    )


# ---------------------------------------------------------------------------
# Log + StepResult helpers
# ---------------------------------------------------------------------------


def _write_log(path: Path, log: _ScrubLog) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(log.lines) + "\n")


def _step(
    command: list[str],
    *,
    exit_code: int,
    log_path: Path,
    started_at: str,
) -> StepResult:
    return StepResult(
        name="scrub-libvirt",
        command=command,
        exit_code=exit_code,
        log_path=str(log_path),
        started_at=started_at,
        finished_at=_isoformat(),
    )


def _isoformat() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = ["scrub_lab"]
