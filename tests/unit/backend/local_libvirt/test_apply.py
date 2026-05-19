"""Tests for the local-libvirt apply adapter (subprocess invokers)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from playground.backend.local_libvirt.apply import (
    run_ansible_playbook,
    run_tofu_apply,
    run_tofu_destroy,
    tail_log,
)


def _write_shim(
    tmp_path: Path,
    binary: str,
    exit_code: int = 0,
    stdout: str = "",
) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / binary
    # Use a heredoc so literal newlines in `stdout` survive the round-trip
    # (printf '%s' under bash with a single-quoted Python repr would
    # render `\n` as the literal two chars).
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<'PG_EOF'\n"
        f"{stdout}"
        f"{'' if stdout.endswith(chr(10)) or not stdout else chr(10)}"
        "PG_EOF\n"
        f"exit {exit_code}\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


# ---------------------------------------------------------------------------
# run_tofu_apply
# ---------------------------------------------------------------------------


def test_run_tofu_apply_streams_lines_to_event_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from playground.events import EventBus

    bin_dir = _write_shim(
        tmp_path, "tofu", exit_code=0,
        stdout="planning...\napplying...\ndone.\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    bus = EventBus()
    events = []
    bus.subscribe(events.append)

    step, _ = run_tofu_apply(
        tofu_dir=tmp_path,
        var_file=tmp_path / "vars.tfvars.json",
        log_path=tmp_path / "logs" / "tofu.log",
        bus=bus, run_id="r1",
    )

    assert step.exit_code == 0
    lines = [e.payload["line"] for e in events if e.type == "log_line"]
    assert lines == ["planning...", "applying...", "done."]
    # All events tagged with the run id and step name.
    for e in events:
        assert e.run_id == "r1"
        assert e.payload["step"] == "tofu-apply"
    # Log file still has the same content.
    assert (tmp_path / "logs" / "tofu.log").read_text().splitlines() == [
        "planning...", "applying...", "done."
    ]


def test_run_tofu_apply_works_without_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bus is optional — when None, lines still go to the log file.
    bin_dir = _write_shim(tmp_path, "tofu", exit_code=0, stdout="ok\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    step, _ = run_tofu_apply(
        tofu_dir=tmp_path,
        var_file=tmp_path / "vars.tfvars.json",
        log_path=tmp_path / "logs" / "tofu.log",
    )

    assert step.exit_code == 0
    assert (tmp_path / "logs" / "tofu.log").read_text() == "ok\n"


def test_run_tofu_apply_succeeds_and_captures_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_shim(tmp_path, "tofu", exit_code=0, stdout="apply complete\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    log_path = tmp_path / "logs" / "tofu.log"
    step, diagnostics = run_tofu_apply(
        tofu_dir=tmp_path,
        var_file=tmp_path / "vars.tfvars.json",
        log_path=log_path,
    )

    assert diagnostics == []
    assert step.exit_code == 0
    assert step.name == "tofu-apply"
    assert step.command[:3] == ["tofu", "apply", "-auto-approve"]
    assert f"-var-file={tmp_path / 'vars.tfvars.json'}" in step.command
    assert "apply complete" in log_path.read_text()


def test_run_tofu_apply_records_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_shim(tmp_path, "tofu", exit_code=2, stdout="boom\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    log_path = tmp_path / "logs" / "tofu.log"
    step, diagnostics = run_tofu_apply(
        tofu_dir=tmp_path,
        var_file=tmp_path / "vars.tfvars.json",
        log_path=log_path,
    )

    assert diagnostics == []
    assert step.exit_code == 2
    assert "boom" in log_path.read_text()


def test_run_tofu_apply_reports_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")

    log_path = tmp_path / "logs" / "tofu.log"
    step, diagnostics = run_tofu_apply(
        tofu_dir=tmp_path,
        var_file=tmp_path / "vars.tfvars.json",
        log_path=log_path,
    )

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.apply.tofu_binary_missing"
    assert step.exit_code == 127  # sentinel: nothing actually ran


# ---------------------------------------------------------------------------
# run_tofu_destroy
# ---------------------------------------------------------------------------


def test_run_tofu_destroy_succeeds_and_captures_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_shim(tmp_path, "tofu", exit_code=0, stdout="destroy complete\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    log_path = tmp_path / "logs" / "tofu-destroy.log"
    step, diagnostics = run_tofu_destroy(
        tofu_dir=tmp_path,
        var_file=tmp_path / "vars.tfvars.json",
        log_path=log_path,
    )

    assert diagnostics == []
    assert step.exit_code == 0
    assert step.name == "tofu-destroy"
    assert step.command[:3] == ["tofu", "destroy", "-auto-approve"]
    assert f"-var-file={tmp_path / 'vars.tfvars.json'}" in step.command
    assert "destroy complete" in log_path.read_text()


def test_run_tofu_destroy_records_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_shim(tmp_path, "tofu", exit_code=1, stdout="cannot destroy\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    log_path = tmp_path / "logs" / "tofu-destroy.log"
    step, diagnostics = run_tofu_destroy(
        tofu_dir=tmp_path,
        var_file=tmp_path / "vars.tfvars.json",
        log_path=log_path,
    )

    assert diagnostics == []
    assert step.exit_code == 1


# ---------------------------------------------------------------------------
# run_ansible_playbook
# ---------------------------------------------------------------------------


def test_run_ansible_playbook_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _write_shim(tmp_path, "ansible-playbook", exit_code=0, stdout="PLAY")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    playbook = tmp_path / "ansible" / "site.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("")
    inventory = tmp_path / "inv.ini"
    inventory.write_text("[playground]\n")
    log_path = tmp_path / "logs" / "ansible.log"

    step, diagnostics = run_ansible_playbook(
        playbook, inventory, log_path, cwd=tmp_path
    )

    assert diagnostics == []
    assert step.exit_code == 0
    assert step.command[:2] == ["ansible-playbook", "-i"]


def test_run_ansible_playbook_reports_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    playbook = tmp_path / "ansible" / "site.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("")
    inventory = tmp_path / "inv.ini"
    inventory.write_text("")

    step, diagnostics = run_ansible_playbook(
        playbook, inventory, tmp_path / "log", cwd=tmp_path
    )

    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.apply.ansible_binary_missing"
    assert step.exit_code == 127


# ---------------------------------------------------------------------------
# tail_log
# ---------------------------------------------------------------------------


def test_tail_log_returns_last_lines(tmp_path: Path) -> None:
    log = tmp_path / "log"
    log.write_text("\n".join(f"line {i}" for i in range(50)) + "\n")

    tail = tail_log(log, lines=5)

    assert tail.splitlines() == [f"line {i}" for i in range(45, 50)]


def test_tail_log_handles_missing_file(tmp_path: Path) -> None:
    assert tail_log(tmp_path / "absent") == ""
