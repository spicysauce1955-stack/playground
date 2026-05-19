"""Subprocess invokers for the local-libvirt apply path.

This module is the I/O edge: it shells out to ``tofu`` and
``ansible-playbook`` with captured logs, returns a structured
:class:`StepResult` per invocation, and (optionally) publishes one
``log_line`` event per line of streamed stdout/stderr to a
:class:`EventBus` so TUIs and other live consumers can render
progress before the subprocess exits.

Wrappers are deliberately thin — they do not interpret tofu/ansible
output and do not retry. Live streaming uses a foreground
line-by-line read loop on the merged stdout/stderr pipe; we capture
every line to the run's log file regardless, so the JSONL event log
and the on-disk log file always agree.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from playground.events import EventBus
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.runs.operation import StepResult


def run_tofu_apply(
    tofu_dir: Path,
    var_file: Path,
    log_path: Path,
    *,
    bus: EventBus | None = None,
    run_id: str | None = None,
) -> tuple[StepResult, list[Diagnostic]]:
    """Invoke ``tofu apply -auto-approve -var-file=<var_file>`` in ``tofu_dir``.

    Captures combined stdout+stderr to ``log_path``. When ``bus`` is
    supplied, each output line is also published as a
    ``log_line`` event with the step name and the line text in
    ``payload``.
    """
    return _run_step(
        name="tofu-apply",
        command=[
            "tofu",
            "apply",
            "-auto-approve",
            "-input=false",
            f"-var-file={var_file}",
        ],
        cwd=tofu_dir,
        log_path=log_path,
        missing_binary_id="runtime.apply.tofu_binary_missing",
        bus=bus,
        run_id=run_id,
    )


def run_tofu_destroy(
    tofu_dir: Path,
    var_file: Path,
    log_path: Path,
    *,
    bus: EventBus | None = None,
    run_id: str | None = None,
) -> tuple[StepResult, list[Diagnostic]]:
    """Invoke ``tofu destroy -auto-approve -var-file=<var_file>`` in ``tofu_dir``."""
    return _run_step(
        name="tofu-destroy",
        command=[
            "tofu",
            "destroy",
            "-auto-approve",
            "-input=false",
            f"-var-file={var_file}",
        ],
        cwd=tofu_dir,
        log_path=log_path,
        missing_binary_id="runtime.apply.tofu_binary_missing",
        bus=bus,
        run_id=run_id,
    )


def run_ansible_playbook(
    playbook: Path,
    inventory: Path,
    log_path: Path,
    *,
    cwd: Path,
    bus: EventBus | None = None,
    run_id: str | None = None,
) -> tuple[StepResult, list[Diagnostic]]:
    """Invoke ``ansible-playbook -i <inventory> <playbook>`` from ``cwd``."""
    return _run_step(
        name="ansible-playbook",
        command=["ansible-playbook", "-i", str(inventory), str(playbook)],
        cwd=cwd,
        log_path=log_path,
        missing_binary_id="runtime.apply.ansible_binary_missing",
        bus=bus,
        run_id=run_id,
    )


def _run_step(
    name: str,
    command: list[str],
    cwd: Path | None,
    log_path: Path,
    missing_binary_id: str,
    bus: EventBus | None,
    run_id: str | None,
) -> tuple[StepResult, list[Diagnostic]]:
    binary = command[0]
    if shutil.which(binary) is None:
        return (
            _failed_step(name, command, exit_code=127, log_path=log_path),
            [
                Diagnostic(
                    id=missing_binary_id,
                    severity="error",
                    message=f"`{binary}` binary not found on PATH",
                    source=SourceLocation(path=str(cwd) if cwd else binary),
                    suggestion=f"install {binary} and retry",
                )
            ],
        )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = _now_iso()
    exit_code = _spawn_and_stream(
        command, cwd, log_path, bus=bus, run_id=run_id, step_name=name,
    )
    finished = _now_iso()
    return (
        StepResult(
            name=name,
            command=command,
            exit_code=exit_code,
            log_path=str(log_path),
            started_at=started,
            finished_at=finished,
        ),
        [],
    )


def _spawn_and_stream(
    command: list[str],
    cwd: Path | None,
    log_path: Path,
    *,
    bus: EventBus | None,
    run_id: str | None,
    step_name: str,
) -> int:
    """Run ``command``, tee each line to ``log_path`` and (optionally) the bus.

    Reads stdout line-by-line (stderr merged) in the foreground.
    Returns the subprocess exit code. If the subprocess writes a
    partial last line without a newline, it's still captured.
    """
    with (
        log_path.open("w") as log,
        subprocess.Popen(  # noqa: S603 — explicit args, no shell
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
        ) as proc,
    ):
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            log.write(raw)
            log.flush()
            if bus is not None and run_id is not None:
                bus.publish(
                    run_id,
                    "log_line",
                    {"step": step_name, "line": line},
                )
        proc.wait()
        return proc.returncode


def _failed_step(
    name: str, command: list[str], exit_code: int, log_path: Path
) -> StepResult:
    """A StepResult for a step that never actually ran (binary missing)."""
    now = _now_iso()
    return StepResult(
        name=name,
        command=command,
        exit_code=exit_code,
        log_path=str(log_path),
        started_at=now,
        finished_at=now,
    )


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def tail_log(log_path: Path, lines: int = 20) -> str:
    """Return the last ``lines`` lines of ``log_path`` (empty string if absent)."""
    if not log_path.exists():
        return ""
    text = log_path.read_text(errors="replace")
    return "\n".join(text.splitlines()[-lines:])


__all__ = [
    "run_ansible_playbook",
    "run_tofu_apply",
    "run_tofu_destroy",
    "tail_log",
]
