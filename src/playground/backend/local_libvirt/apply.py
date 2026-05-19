"""Subprocess invokers for the local-libvirt apply path.

This module is the I/O edge: it shells out to ``tofu`` and
``ansible-playbook`` with captured logs and returns a structured
:class:`StepResult` per invocation. The CLI composes the steps into one
operation run.

The wrappers are deliberately thin — they do not interpret tofu/ansible
output, do not retry, do not stream live (capture to file only, dump
tail on failure). Live streaming, retries, and richer parsing live in
the operation runner / event bus slice when it lands.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.runs.operation import StepResult


def run_tofu_apply(
    tofu_dir: Path,
    var_file: Path,
    log_path: Path,
) -> tuple[StepResult, list[Diagnostic]]:
    """Invoke ``tofu apply -auto-approve -var-file=<var_file>`` in ``tofu_dir``.

    Captures combined stdout+stderr to ``log_path``. Returns the
    :class:`StepResult` plus any diagnostics that prevent invocation
    (missing binary).
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
    )


def run_ansible_playbook(
    playbook: Path,
    inventory: Path,
    log_path: Path,
    *,
    cwd: Path,
) -> tuple[StepResult, list[Diagnostic]]:
    """Invoke ``ansible-playbook -i <inventory> <playbook>`` from ``cwd``.

    Captures combined stdout+stderr to ``log_path``. ``cwd`` is the
    directory the subprocess starts in — typically the repo root so
    Ansible's relative ``roles_path`` resolves correctly.
    """
    return _run_step(
        name="ansible-playbook",
        command=["ansible-playbook", "-i", str(inventory), str(playbook)],
        cwd=cwd,
        log_path=log_path,
        missing_binary_id="runtime.apply.ansible_binary_missing",
    )


def _run_step(
    name: str,
    command: list[str],
    cwd: Path | None,
    log_path: Path,
    missing_binary_id: str,
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
    with log_path.open("wb") as log:
        completed = subprocess.run(  # noqa: S603 — explicit args, no shell
            command,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    finished = _now_iso()
    return (
        StepResult(
            name=name,
            command=command,
            exit_code=completed.returncode,
            log_path=str(log_path),
            started_at=started,
            finished_at=finished,
        ),
        [],
    )


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


__all__ = ["run_ansible_playbook", "run_tofu_apply", "tail_log"]
