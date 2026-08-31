"""Tests for the `playground adb` SSH-tunnel verb.

``_write_apply_shims`` and ``_write_ssh_shim`` live in ``tests/cli/conftest.py``
and are imported here as plain module functions (``tests/cli/`` has no
``__init__.py``, so `from .test_cli import ...` fails under pytest's
collection — but pytest adds the rootless test directory to ``sys.path``,
so a bare `from conftest import ...` works).
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from conftest import _write_apply_shims, _write_ssh_shim
from typer.testing import CliRunner

from playground.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _invoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    bin_dir = _write_apply_shims(tmp_path)
    ssh_bin = _write_ssh_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    return CliRunner().invoke(
        app,
        [
            "adb",
            "--lab", "generic-infra",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tofu_dir),
            *args,
        ],
    )


def test_adb_builds_a_local_forward_to_5555(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(tmp_path, monkeypatch, "--on", "docker1")

    assert result.exit_code == 0, result.stderr
    ssh_log = (tmp_path / "ssh.log").read_text()
    assert "-L" in ssh_log
    assert "5555:127.0.0.1:5555" in ssh_log
    assert "ubuntu@10.0.10.43" in ssh_log  # docker1's IP from the tofu shim
    assert "-N" in ssh_log  # no remote command; forwarding only


def test_adb_prints_the_connect_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(tmp_path, monkeypatch, "--on", "docker1")

    assert result.exit_code == 0
    assert "adb connect 127.0.0.1:5555" in result.output


def test_adb_honors_an_explicit_local_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(tmp_path, monkeypatch, "--on", "docker1", "--local-port", "5599")

    assert result.exit_code == 0
    ssh_log = (tmp_path / "ssh.log").read_text()
    assert "5599:127.0.0.1:5555" in ssh_log
    assert "adb connect 127.0.0.1:5599" in result.output


def test_adb_picks_the_next_free_port_when_the_default_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 5555))
    holder.listen(1)
    try:
        result = _invoke(tmp_path, monkeypatch, "--on", "docker1")
    finally:
        holder.close()

    assert result.exit_code == 0, result.stderr
    ssh_log = (tmp_path / "ssh.log").read_text()
    assert "5555:127.0.0.1:5555" not in ssh_log
    assert ":127.0.0.1:5555" in ssh_log  # some other local port was chosen
    assert "adb connect 127.0.0.1:" in result.output


def test_adb_unknown_vm_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(tmp_path, monkeypatch, "--on", "nope")

    assert result.exit_code == 1
    assert "config.adb.unknown_vm" in result.output + result.stderr
