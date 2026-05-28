"""Gate apply's tofu→ansible handoff on VM readiness.

Without this gate, ``execute_apply`` invoked ``ansible-playbook`` the
instant ``tofu apply`` returned — but on Ubuntu Noble (and most cloud
images) sshd doesn't accept connections until cloud-init has run the
networking + ssh stages (typically 30-90 s after libvirt finishes
creating the domain). Ansible's first connection failed with
"Connection refused" and the whole apply errored out as if a real
provisioning step had failed.

This module probes each VM in two phases, in parallel:

1. **TCP :22 reachable** — a tight loop calling
   :func:`socket.create_connection` with exponential backoff until
   ``ssh_timeout`` elapses. Cheap, fast, and a strict prerequisite
   for phase 2.
2. **cloud-init done** — SSH into the VM and run
   ``cloud-init status --wait``. That command blocks on the *VM*
   side until every cloud-init stage (including
   ``package_upgrade``) is finished, which is the second race we
   need to gate on: cloud-init holds the apt lock while ansible
   would otherwise be trying to ``apt install``.

Both phases run concurrently across VMs via a thread pool — total
wall time is roughly ``max(per-VM time)`` rather than the sum.
"""

from __future__ import annotations

import concurrent.futures
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from playground.events import EventBus
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.runs.operation import StepResult

DEFAULT_SSH_TIMEOUT_SECONDS = 300.0
"""Per-VM wait for sshd to accept TCP connections."""

DEFAULT_CLOUD_INIT_TIMEOUT_SECONDS = 600.0
"""Per-VM wait for ``cloud-init status --wait`` to return; covers
package_update + package_upgrade time."""

SSH_PORT = 22


@dataclass(frozen=True)
class VmTarget:
    """One VM to wait on.

    ``ip`` + ``ssh_port`` together name the SSH endpoint. For
    local-libvirt that's the DHCP IP on port 22. For local-vbox it's
    ``127.0.0.1`` on a per-VM NAT port-forward, so ``ssh_port`` varies.
    ``console_hint`` lets a backend override the libvirt-flavored
    "console into the VM" suggestion in timeout diagnostics.
    """

    name: str
    ip: str
    ssh_user: str
    ssh_port: int = SSH_PORT
    console_hint: str | None = None


@dataclass
class _Outcome:
    """Per-VM result the orchestrator stitches together."""

    name: str
    log_lines: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def wait_for_vms_ready(
    *,
    targets: list[VmTarget],
    log_path: Path,
    bus: EventBus,
    run_id: str,
    ssh_timeout: float = DEFAULT_SSH_TIMEOUT_SECONDS,
    cloud_init_timeout: float = DEFAULT_CLOUD_INIT_TIMEOUT_SECONDS,
) -> tuple[StepResult, list[Diagnostic]]:
    """Block until every target has SSH listening AND cloud-init done.

    Returns the standard ``(StepResult, diagnostics)`` shape used by
    other apply steps so the runner can plug it into the existing
    success/failure plumbing. ``StepResult.exit_code`` is 0 when
    every target passed both phases; non-zero (1) on any timeout or
    cloud-init failure.

    The ``ssh`` binary is checked up front; if missing the entire
    step fails with ``runtime.apply.ssh_binary_missing``. No
    network probes are attempted in that case.
    """
    started_at = _iso_now()
    command = ["wait-for-vms-ready", *(_endpoint_label(t) for t in targets)]

    log_lines: list[str] = [
        f"# wait-for-vms-ready: {len(targets)} VM(s)",
        f"# ssh_timeout={ssh_timeout}s cloud_init_timeout={cloud_init_timeout}s",
    ]

    if not targets:
        # Defensive: apply normally has at least one VM by the time we
        # get here. An empty target list means a zero-VM lab; nothing
        # to wait on.
        log_lines.append("no targets — skipping")
        _write_log(log_path, log_lines)
        return _step(command, exit_code=0, log_path=log_path, started_at=started_at), []

    if shutil.which("ssh") is None:
        diagnostic = Diagnostic(
            id="runtime.apply.ssh_binary_missing",
            severity="error",
            message=(
                "`ssh` binary not found on PATH; cannot probe VM readiness "
                "before handing off to ansible"
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

    outcomes: list[_Outcome] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {
            pool.submit(
                _wait_one,
                target=t,
                bus=bus,
                run_id=run_id,
                ssh_timeout=ssh_timeout,
                cloud_init_timeout=cloud_init_timeout,
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
                                id="runtime.apply.wait_unexpected",
                                severity="error",
                                message=f"unexpected error waiting on {target.name!r}: {exc}",
                            )
                        ],
                    )
                )

    # Stable per-VM order in the log: target declaration order, not
    # finish order. Operator-friendly when troubleshooting.
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


# ---------------------------------------------------------------------------
# Per-VM probe
# ---------------------------------------------------------------------------


def _wait_one(
    *,
    target: VmTarget,
    bus: EventBus,
    run_id: str,
    ssh_timeout: float,
    cloud_init_timeout: float,
) -> _Outcome:
    """Run both phases for one VM. Always returns; never raises."""
    outcome = _Outcome(name=target.name)

    endpoint = f"{target.ip}:{target.ssh_port}"
    bus.publish(
        run_id, "log_line",
        {"step": "wait-for-vms-ready",
         "line": f"{target.name}: waiting for SSH on {endpoint}"},
    )
    tcp_ok, tcp_elapsed = _wait_tcp(target.ip, target.ssh_port, ssh_timeout)
    if not tcp_ok:
        outcome.log_lines.append(
            f"{target.name}: TIMEOUT waiting for {endpoint} after {ssh_timeout:.0f}s"
        )
        outcome.diagnostics.append(
            Diagnostic(
                id="runtime.apply.wait_ssh_timeout",
                severity="error",
                message=(
                    f"VM {target.name!r} ({target.ip}:{target.ssh_port}) never accepted "
                    f"TCP connections within {ssh_timeout:.0f}s"
                ),
                source=SourceLocation(path=target.ip),
                suggestion=target.console_hint or (
                    f"console into the VM via `virsh console {target.name}` and "
                    "inspect cloud-init: `journalctl -u cloud-init` / "
                    "`systemctl status ssh`"
                ),
            )
        )
        return outcome

    outcome.log_lines.append(
        f"{target.name}: TCP {endpoint} open after {tcp_elapsed:.1f}s"
    )

    # TCP-open does NOT imply sshd is answering. With a VirtualBox NAT
    # port-forward the host port accepts connections the instant the VM
    # is defined — long before the guest's sshd is up — so the TCP probe
    # above returns ~instantly and is a false "ready". Poll an actual SSH
    # auth round-trip (`ssh ... true`) until it succeeds; transport
    # errors (connection refused, exit 255 "banner exchange", our own
    # attempt timeout) just mean "not up yet, retry". This is the real
    # readiness gate, and it's also correct for libvirt (where it
    # succeeds on the first try right after TCP opens).
    auth_ok, auth_elapsed = _wait_ssh_auth(target=target, timeout=ssh_timeout)
    if not auth_ok:
        outcome.log_lines.append(
            f"{target.name}: TIMEOUT waiting for sshd to answer on {endpoint} "
            f"after {ssh_timeout:.0f}s"
        )
        outcome.diagnostics.append(
            Diagnostic(
                id="runtime.apply.wait_sshd_timeout",
                severity="error",
                message=(
                    f"VM {target.name!r} ({endpoint}): TCP was open but sshd "
                    f"did not complete an SSH handshake within {ssh_timeout:.0f}s"
                ),
                source=SourceLocation(path=target.ip),
                suggestion=target.console_hint or (
                    f"console into the VM via `virsh console {target.name}` and "
                    "check `systemctl status ssh` / `journalctl -u cloud-init`"
                ),
            )
        )
        return outcome

    outcome.log_lines.append(
        f"{target.name}: sshd answering after {auth_elapsed:.1f}s"
    )
    bus.publish(
        run_id, "log_line",
        {"step": "wait-for-vms-ready", "line": f"{target.name}: SSH ready"},
    )

    bus.publish(
        run_id, "log_line",
        {"step": "wait-for-vms-ready", "line": f"{target.name}: waiting for cloud-init"},
    )
    ci_result = _wait_cloud_init(
        ip=target.ip,
        user=target.ssh_user,
        timeout=cloud_init_timeout,
        port=target.ssh_port,
    )
    if ci_result.timed_out:
        outcome.log_lines.append(
            f"{target.name}: TIMEOUT waiting for cloud-init after {cloud_init_timeout:.0f}s"
        )
        outcome.diagnostics.append(
            Diagnostic(
                id="runtime.apply.wait_cloud_init_timeout",
                severity="error",
                message=(
                    f"VM {target.name!r}: `cloud-init status --wait` did not "
                    f"complete within {cloud_init_timeout:.0f}s"
                ),
                source=SourceLocation(path=target.ip),
                suggestion=target.console_hint or (
                    f"console into the VM: `virsh console {target.name}` and run "
                    "`cloud-init status --long` to see which stage is hung"
                ),
            )
        )
        return outcome

    if ci_result.exit_code != 0:
        outcome.log_lines.append(
            f"{target.name}: cloud-init failed (exit {ci_result.exit_code}): "
            f"{ci_result.stdout_summary or '(no stdout)'}"
        )
        outcome.diagnostics.append(
            Diagnostic(
                id="runtime.apply.wait_cloud_init_failed",
                severity="error",
                message=(
                    f"VM {target.name!r}: cloud-init reported error/degraded "
                    f"(exit {ci_result.exit_code}): "
                    f"{ci_result.stdout_summary or ci_result.stderr_summary or '(no output)'}"
                ),
                source=SourceLocation(path=target.ip),
                suggestion=(
                    f"`{_ssh_hint(target)} cloud-init status --long` "
                    "for the failed stage; check /var/log/cloud-init-output.log "
                    "on the VM"
                ),
            )
        )
        return outcome

    outcome.log_lines.append(f"{target.name}: cloud-init done")
    bus.publish(
        run_id, "log_line",
        {"step": "wait-for-vms-ready", "line": f"{target.name}: ready"},
    )
    return outcome


# ---------------------------------------------------------------------------
# TCP probe
# ---------------------------------------------------------------------------


def _wait_tcp(ip: str, port: int, timeout: float) -> tuple[bool, float]:
    """Return ``(success, elapsed_seconds)``. Uses exponential backoff."""
    start = time.monotonic()
    deadline = start + timeout
    delay = 1.0
    while True:
        try:
            with socket.create_connection((ip, port), timeout=5.0):
                return True, time.monotonic() - start
        except (TimeoutError, OSError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, time.monotonic() - start
        time.sleep(min(delay, max(0.5, remaining)))
        delay = min(delay * 1.5, 5.0)


def _wait_ssh_auth(*, target: VmTarget, timeout: float) -> tuple[bool, float]:
    """Poll a real SSH auth round-trip until it succeeds or times out.

    Returns ``(success, elapsed_seconds)``. Each attempt runs
    ``ssh ... true``; a zero exit means sshd is up and the key is
    accepted. Any non-zero exit (transport failure, banner-exchange
    timeout, our per-attempt timeout) is treated as "not ready yet" and
    retried with exponential backoff until ``timeout`` elapses.
    """
    start = time.monotonic()
    deadline = start + timeout
    delay = 2.0
    while True:
        if _ssh_probe(target) == 0:
            return True, time.monotonic() - start
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, time.monotonic() - start
        time.sleep(min(delay, max(0.5, remaining)))
        delay = min(delay * 1.5, 10.0)


def _ssh_probe(target: VmTarget, *, attempt_timeout: float = 15.0) -> int:
    """One ``ssh ... true`` attempt. Returns the process exit code
    (255 on transport failure; 124 if our per-attempt timeout fires)."""
    cmd = [
        "ssh",
        *(["-p", str(target.ssh_port)] if target.ssh_port != SSH_PORT else []),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"{target.ssh_user}@{target.ip}",
        "true",
    ]
    try:
        return subprocess.run(  # noqa: S603 — explicit args, no shell
            cmd, capture_output=True, text=True, check=False,
            timeout=attempt_timeout,
        ).returncode
    except subprocess.TimeoutExpired:
        return 124


# ---------------------------------------------------------------------------
# cloud-init probe (over SSH)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CloudInitResult:
    exit_code: int
    stdout_summary: str
    stderr_summary: str
    timed_out: bool = False


def _wait_cloud_init(
    *, ip: str, user: str, timeout: float, port: int = SSH_PORT,
) -> _CloudInitResult:
    """SSH in and block on ``cloud-init status --wait``.

    The remote command itself blocks until cloud-init is done, so our
    subprocess timeout is the effective overall budget for this phase.
    ``-p <port>`` is added only for non-default ports (vbox NAT
    forwards) so the libvirt command line is byte-for-byte unchanged.
    """
    cmd = [
        "ssh",
        *(["-p", str(port)] if port != SSH_PORT else []),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"{user}@{ip}",
        "cloud-init status --wait",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _CloudInitResult(
            exit_code=-1, stdout_summary="", stderr_summary="", timed_out=True,
        )
    return _CloudInitResult(
        exit_code=result.returncode,
        stdout_summary=result.stdout.strip()[-500:],
        stderr_summary=result.stderr.strip()[-500:],
    )


# ---------------------------------------------------------------------------
# Log + StepResult plumbing
# ---------------------------------------------------------------------------


def _endpoint_label(target: VmTarget) -> str:
    """Stable per-VM label for the step command summary. Includes the
    port only for non-default ports so libvirt's summary is unchanged."""
    if target.ssh_port == SSH_PORT:
        return f"{target.name}={target.ip}"
    return f"{target.name}={target.ip}:{target.ssh_port}"


def _ssh_hint(target: VmTarget) -> str:
    """An ``ssh ...`` prefix an operator can paste, port-aware."""
    if target.ssh_port == SSH_PORT:
        return f"ssh {target.ssh_user}@{target.ip}"
    return f"ssh -p {target.ssh_port} {target.ssh_user}@{target.ip}"


def _write_log(log_path: Path, lines: list[str]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n")


def _step(
    command: list[str], *, exit_code: int, log_path: Path, started_at: str,
) -> StepResult:
    return StepResult(
        name="wait-for-vms-ready",
        command=command,
        exit_code=exit_code,
        log_path=str(log_path),
        started_at=started_at,
        finished_at=_iso_now(),
    )


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = [
    "DEFAULT_CLOUD_INIT_TIMEOUT_SECONDS",
    "DEFAULT_SSH_TIMEOUT_SECONDS",
    "VmTarget",
    "wait_for_vms_ready",
]
