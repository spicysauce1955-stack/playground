"""Tests for the local-libvirt apply adapter (subprocess invokers)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from playground.backend.local_libvirt.apply import (
    run_ansible_playbook,
    run_tofu_apply,
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
    shim.write_text(
        f"#!/usr/bin/env bash\nprintf %s {stdout!r}\nexit {exit_code}\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


# ---------------------------------------------------------------------------
# run_tofu_apply
# ---------------------------------------------------------------------------


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
