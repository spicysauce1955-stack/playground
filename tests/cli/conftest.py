"""Shared PATH-shim factories for CLI tests.

tests/cli/ has no __init__.py, so these helpers were duplicated verbatim
between test_cli.py and test_adb.py. conftest.py is importable by every
test module in this directory without packaging, so they live here now.
"""

from __future__ import annotations

import shlex
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import pytest


class _ShimFactory(Protocol):
    def __call__(self, tmp_path: Path, **kwargs: object) -> Path: ...


def _write_apply_shims(
    tmp_path: Path,
    *,
    tofu_apply_exit: int = 0,
    tofu_destroy_exit: int = 0,
    ansible_exit: int = 0,
    vm_ips_payload: str | None = None,
) -> Path:
    """Write tofu + ansible-playbook shims handling apply/destroy/output.

    Each `tofu <verb>` returns the corresponding exit code; `tofu output
    -json` returns ``vm_ips_payload``. ansible-playbook exits with
    ``ansible_exit``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    default_ips = (
        '{"vm_ips": {"sensitive": false, "type": ["map","string"], '
        '"value": {"node1":"10.0.10.42","docker1":"10.0.10.43","router1":"10.0.10.44"}}}'
    )
    payload = vm_ips_payload if vm_ips_payload is not None else default_ips
    tofu = bin_dir / "tofu"
    tofu.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f"  apply) echo 'tofu apply ok'; exit {tofu_apply_exit} ;;\n"
        f"  destroy) echo 'tofu destroy ok'; exit {tofu_destroy_exit} ;;\n"
        f"  output) cat <<'PAYLOAD'\n{payload}\nPAYLOAD\n   ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    tofu.chmod(tofu.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    ansible = bin_dir / "ansible-playbook"
    ansible.write_text(
        f"#!/usr/bin/env bash\necho ansible ran\nexit {ansible_exit}\n"
    )
    ansible.chmod(ansible.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _write_ssh_shim(
    tmp_path: Path, *, exit_code: int = 0, stdout: str = ""
) -> Path:
    """PATH-shimmed `ssh` that records its argv to a log file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / "ssh.log"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > {shlex.quote(str(log_path))}\n'
        + (f'echo {shlex.quote(stdout)}\n' if stdout else "")
        + f"exit {exit_code}\n"
    )
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _write_scp_shim(tmp_path: Path, *, exit_code: int = 0) -> Path:
    """PATH-shimmed `scp` that records its argv to a log file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / "scp.log"
    scp = bin_dir / "scp"
    scp.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > {shlex.quote(str(log_path))}\n'
        f"exit {exit_code}\n"
    )
    scp.chmod(scp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


@pytest.fixture
def write_apply_shims() -> Callable[..., Path]:
    """Factory writing `tofu` + `ansible-playbook` PATH shims."""
    return _write_apply_shims


@pytest.fixture
def write_ssh_shim() -> Callable[..., Path]:
    """Factory writing an `ssh` PATH shim that logs its argv."""
    return _write_ssh_shim


@pytest.fixture
def write_scp_shim() -> Callable[..., Path]:
    """Factory writing an `scp` PATH shim that logs its argv."""
    return _write_scp_shim
