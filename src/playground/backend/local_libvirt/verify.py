"""Post-apply sanity battery — the `verify-lab` lifecycle phase.

Borrowed from Molecule's create → prepare → converge → **verify**
lifecycle pattern. Today's `execute_apply` stops at "ansible
exited 0," which means a role can succeed (ansible reports
``ok=N changed=M``) while the resulting lab is non-functional —
the most obvious historical example being the hardcoded redroid
role failing silently on Android-incapable kernels even though
the role technically aborted with an Ansible failure.

This module runs a minimal sanity battery against the live lab
after ansible finishes:

1. **systemd healthy** on every VM. `systemctl is-system-running`
   must report `running` or `degraded`, not `failed`. A `failed`
   state means a unit didn't start.
2. **docker reachable** on every VM in `[needs_docker]`.
   `docker ps` must exit 0 — proves dockerd is up and the
   ansible user can talk to it via the docker socket.
3. **`commands.enabled` smoke pass** for any preset that targets
   `any` VM. Runs each one and asserts exit 0.

**Severity: warning-only.** Failures attach
``runtime.apply.verify_failed`` diagnostics to the run but the
run's overall status stays ``succeeded``. Rationale: this is the
first iteration of the verify phase; operators are used to
"apply succeeded" meaning "ansible exited 0." Promoting verify
failures to hard-fail can come later if the warning signal
proves too quiet.

The step's ``StepResult.exit_code`` still reflects what actually
happened (non-zero if any sub-check failed), so the diagnostic
surfaces in ``playground runs show`` even though the run says
succeeded.
"""

from __future__ import annotations

import concurrent.futures
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from playground.events import EventBus
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedCommand, ResolvedLab, ResolvedVm
from playground.runs.operation import StepResult

DEFAULT_PER_CHECK_TIMEOUT_SECONDS = 30.0
"""Per-VM, per-check SSH timeout. Each sub-check is a single
short command (`systemctl is-system-running`, `docker ps`, or a
`commands.enabled` body). 30s is generous but bounded."""


@dataclass(frozen=True)
class VmTarget:
    """One VM to verify."""

    name: str
    ip: str
    ssh_user: str
    has_docker: bool
    """Whether the VM's VmRole provisions docker. Drives whether
    we run the docker-ps sub-check on it."""


@dataclass
class _Outcome:
    """Per-VM result the orchestrator stitches together."""

    name: str
    log_lines: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def verify_lab(
    *,
    resolved: ResolvedLab,
    vm_ips: dict[str, str],
    log_path: Path,
    bus: EventBus,
    run_id: str,
    per_check_timeout: float = DEFAULT_PER_CHECK_TIMEOUT_SECONDS,
) -> tuple[StepResult, list[Diagnostic]]:
    """Run the verify battery against the live lab.

    Returns ``(StepResult, diagnostics)`` like every other step.
    The runner converts errors into warnings before adding them
    to the run's diagnostics list — failure here doesn't fail
    the apply (warning-only semantics).
    """
    started_at = _iso_now()
    command = ["verify-lab", *(f"{name}={ip}" for name, ip in vm_ips.items())]

    log_lines: list[str] = [
        f"# verify-lab: {len(vm_ips)} VM(s)",
        f"# per-check timeout: {per_check_timeout:.0f}s",
    ]

    if not vm_ips:
        log_lines.append("no VMs in scope — skipping")
        _write_log(log_path, log_lines)
        return _step(command, exit_code=0, log_path=log_path, started_at=started_at), []

    if shutil.which("ssh") is None:
        diagnostic = Diagnostic(
            id="runtime.apply.verify_ssh_missing",
            severity="error",
            message=(
                "`ssh` is not on PATH; cannot run the post-apply verify "
                "battery"
            ),
            source=SourceLocation(path="host"),
            suggestion="install openssh-client and retry",
        )
        log_lines.append("FATAL: ssh missing")
        _write_log(log_path, log_lines)
        return (
            _step(command, exit_code=127, log_path=log_path, started_at=started_at),
            [diagnostic],
        )

    targets = _build_targets(resolved, vm_ips)
    any_commands = _commands_targeting_any(resolved)

    outcomes: list[_Outcome] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {
            pool.submit(
                _verify_one,
                target=t,
                any_commands=any_commands,
                bus=bus,
                run_id=run_id,
                timeout=per_check_timeout,
            ): t
            for t in targets
        }
        for future in concurrent.futures.as_completed(futures):
            target = futures[future]
            try:
                outcomes.append(future.result())
            except Exception as exc:  # noqa: BLE001 — last-resort handler
                outcomes.append(
                    _Outcome(
                        name=target.name,
                        log_lines=[f"{target.name}: unexpected error: {exc}"],
                        diagnostics=[
                            Diagnostic(
                                id="runtime.apply.verify_failed",
                                severity="error",
                                message=(
                                    f"unexpected error verifying "
                                    f"{target.name!r}: {exc}"
                                ),
                            )
                        ],
                    )
                )

    # Stable per-VM order — declaration order in the lab YAML.
    by_name = {o.name: o for o in outcomes}
    for target in targets:
        outcome = by_name[target.name]
        log_lines.extend(outcome.log_lines)

    all_diagnostics: list[Diagnostic] = []
    for target in targets:
        all_diagnostics.extend(by_name[target.name].diagnostics)

    has_error = any(d.severity == "error" for d in all_diagnostics)
    _write_log(log_path, log_lines)
    return (
        _step(
            command,
            exit_code=1 if has_error else 0,
            log_path=log_path,
            started_at=started_at,
        ),
        all_diagnostics,
    )


def _build_targets(
    resolved: ResolvedLab, vm_ips: dict[str, str]
) -> list[VmTarget]:
    """Pair lab VMs with their IPs + flag docker-needing ones.

    Skips VMs without an IP — they would have failed the earlier
    wait-for-vms-ready phase too, no point double-reporting.
    """
    by_name: dict[str, ResolvedVm] = {vm.name: vm for vm in resolved.vms}
    targets: list[VmTarget] = []
    for vm in resolved.vms:
        ip = vm_ips.get(vm.name)
        if ip is None:
            continue
        has_docker = any(
            provisioner.get("ansible_role") == "docker"
            for provisioner in vm.provisioners
        )
        targets.append(
            VmTarget(
                name=vm.name,
                ip=ip,
                ssh_user=vm.ssh.user,
                has_docker=has_docker,
            )
        )
    _ = by_name  # quiet unused-locals lint; kept for future per-name lookups
    return targets


def _commands_targeting_any(resolved: ResolvedLab) -> list[ResolvedCommand]:
    return [
        cmd for cmd in resolved.commands if cmd.target.any is True
    ]


# ---------------------------------------------------------------------------
# Per-VM verification
# ---------------------------------------------------------------------------


def _verify_one(
    *,
    target: VmTarget,
    any_commands: list[ResolvedCommand],
    bus: EventBus,
    run_id: str,
    timeout: float,
) -> _Outcome:
    """Three sub-checks per VM. Always returns; never raises."""
    outcome = _Outcome(name=target.name)

    bus.publish(
        run_id, "log_line",
        {"step": "verify-lab", "line": f"{target.name}: verifying"},
    )

    # 1. systemd healthy
    sysd = _ssh(
        target, "systemctl is-system-running", timeout=timeout
    )
    sysd_state = sysd.stdout.strip() or "<unknown>"
    if sysd.returncode != 0 and sysd_state not in ("running", "degraded"):
        outcome.log_lines.append(
            f"{target.name}: systemd state={sysd_state!r} (exit {sysd.returncode})"
        )
        outcome.diagnostics.append(
            Diagnostic(
                id="runtime.apply.verify_failed",
                severity="error",
                message=(
                    f"VM {target.name!r}: systemctl is-system-running "
                    f"reports {sysd_state!r} (expected `running` or "
                    "`degraded`)"
                ),
                source=SourceLocation(path=target.ip),
                suggestion=(
                    f"`ssh {target.ssh_user}@{target.ip} systemctl --failed`"
                    " to see which units didn't start"
                ),
            )
        )
    else:
        outcome.log_lines.append(
            f"{target.name}: systemd state={sysd_state!r} OK"
        )

    # 2. docker reachable (only on VMs that provision docker)
    if target.has_docker:
        dock = _ssh(target, "docker ps", timeout=timeout)
        if dock.returncode != 0:
            outcome.log_lines.append(
                f"{target.name}: docker ps failed (exit {dock.returncode})"
            )
            outcome.diagnostics.append(
                Diagnostic(
                    id="runtime.apply.verify_failed",
                    severity="error",
                    message=(
                        f"VM {target.name!r}: `docker ps` failed "
                        f"(exit {dock.returncode}): "
                        f"{dock.stderr.strip() or '(no stderr)'}"
                    ),
                    source=SourceLocation(path=target.ip),
                    suggestion=(
                        f"check the docker role ran: "
                        f"`ssh {target.ssh_user}@{target.ip} systemctl "
                        "status docker`"
                    ),
                )
            )
        else:
            outcome.log_lines.append(f"{target.name}: docker ps OK")

    # 3. commands.enabled with target: any
    for cmd in any_commands:
        result = _ssh(target, cmd.shell, timeout=cmd.timeout_seconds or timeout)
        if result.returncode != 0:
            outcome.log_lines.append(
                f"{target.name}: command {cmd.name!r} failed "
                f"(exit {result.returncode})"
            )
            outcome.diagnostics.append(
                Diagnostic(
                    id="runtime.apply.verify_failed",
                    severity="error",
                    message=(
                        f"VM {target.name!r}: enabled command {cmd.name!r} "
                        f"failed (exit {result.returncode}): "
                        f"{result.stderr.strip()[:200] or '(no stderr)'}"
                    ),
                    source=SourceLocation(path=target.ip),
                    suggestion=(
                        f"reproduce: `playground exec --on {target.name} -- "
                        f"{cmd.shell}`"
                    ),
                )
            )
        else:
            outcome.log_lines.append(
                f"{target.name}: command {cmd.name!r} OK"
            )

    return outcome


def _ssh(
    target: VmTarget, command: str, *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a single shell command on the VM via SSH.

    Matches the SSH option set used by wait-for-vms-ready so first-
    boot host-key prompts never block the run.
    """
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"{target.ssh_user}@{target.ip}",
        command,
    ]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=124, stdout="", stderr=f"timeout after {timeout}s",
        )


# ---------------------------------------------------------------------------
# StepResult + log plumbing
# ---------------------------------------------------------------------------


def _write_log(log_path: Path, lines: list[str]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n")


def _step(
    command: list[str], *, exit_code: int, log_path: Path, started_at: str,
) -> StepResult:
    return StepResult(
        name="verify-lab",
        command=command,
        exit_code=exit_code,
        log_path=str(log_path),
        started_at=started_at,
        finished_at=_iso_now(),
    )


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = [
    "DEFAULT_PER_CHECK_TIMEOUT_SECONDS",
    "VmTarget",
    "verify_lab",
]
