"""Tests for the `playground adb` SSH-tunnel verb.

``_write_apply_shims`` and ``_write_ssh_shim`` live in ``tests/cli/conftest.py``
and are imported here as plain module functions (``tests/cli/`` has no
``__init__.py``, so `from .test_cli import ...` fails under pytest's
collection — but pytest adds the rootless test directory to ``sys.path``,
so a bare `from conftest import ...` works).
"""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
from textwrap import dedent

import pytest
from conftest import _write_apply_shims, _write_ssh_shim
from typer.testing import CliRunner

import playground.cli.main as cli_main
from playground.cli.main import app
from playground.models.status import DeviceStatus, LabStatus, VmStatus

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


def _device_lab_config(tmp_path: Path) -> Path:
    """`generic-infra`, plus a `pixel7` device slot.

    Synthesized rather than committed, same rationale
    `test_capture.py::_two_device_config` documents: a permanent
    device-bearing lab in `config/` would be walked by
    `test_every_committed_yaml_parses` and counted by diagnostic-total
    tests elsewhere.
    """
    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config_dir)
    (config_dir / "labs" / "device-lab.yaml").write_text(
        dedent(
            """
            apiVersion: playground/v1
            kind: Lab
            metadata:
              name: device-lab
            spec:
              backend: local-libvirt
              networks:
                - name: lab-net
                  profile: nat
                  cidr: 10.77.0.0/24
              vms:
                - name: node1
                  role: generic-node
                  networks: [lab-net]
              devices:
                - name: pixel7
                  kind: android
            """
        ).lstrip("\n")
    )
    return config_dir


def _invoke_device_lab(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    devices: list[DeviceStatus],
    *args: str,
):
    config_dir = _device_lab_config(tmp_path)
    bin_dir = _write_apply_shims(tmp_path)
    ssh_bin = _write_ssh_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )

    def _stub_status(resolved, tofu_dir, state_dir=None):
        return (
            LabStatus(
                lab=resolved.lab_name,
                backend=resolved.backend,
                expected_vms=len(resolved.vms),
                provisioned_vms=len(resolved.vms),
                vms=[
                    VmStatus(name=vm.name, role=vm.role, state="provisioned")
                    for vm in resolved.vms
                ],
                devices=devices,
            ),
            [],
        )

    monkeypatch.setattr(cli_main, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    return CliRunner().invoke(
        app,
        [
            "adb",
            "--lab", "device-lab",
            "--config-dir", str(config_dir),
            "--tofu-dir", str(tofu_dir),
            *args,
        ],
    )


def test_adb_on_a_connected_device_prints_the_local_serial_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ssh tunnel for a handset -- adb already reaches it directly."""
    result = _invoke_device_lab(
        tmp_path, monkeypatch,
        [DeviceStatus(name="pixel7", state="connected", serial="S1")],
        "--on", "pixel7",
    )

    assert result.exit_code == 0, result.stderr
    assert "adb -s S1" in result.output
    assert not (tmp_path / "ssh.log").exists()


def test_adb_on_an_unauthorized_device_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke_device_lab(
        tmp_path, monkeypatch,
        [DeviceStatus(name="pixel7", state="unauthorized", serial="S1")],
        "--on", "pixel7",
    )

    assert result.exit_code == 1
    assert "device.unauthorized" in result.output + result.stderr


def test_adb_on_a_disconnected_device_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke_device_lab(tmp_path, monkeypatch, [], "--on", "pixel7")

    assert result.exit_code == 1
    assert "device.not_connected" in result.output + result.stderr


def test_adb_unknown_on_names_devices_when_the_lab_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lab with a device slot must not tell the operator to go looking
    for a VM -- the unknown-name error names both populations."""
    result = _invoke_device_lab(tmp_path, monkeypatch, [], "--on", "nope")

    assert result.exit_code == 1
    output = result.output + result.stderr
    assert "config.device.unknown_device" in output
    assert "pixel7" in output
    assert "node1" in output


def _invoke_device_lab_live_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attached: list,
    *args: str,
    adb_diagnostics: list | None = None,
):
    """Like `_invoke_device_lab`, but WITHOUT stubbing `query_status` --
    only the backend's VM half and `adb devices -l` are stubbed, so the
    real backend-neutral device layer (`dispatch._device_statuses`) runs
    and `--state-dir` threading is actually exercised. No real `adb` is
    ever launched: `dispatch.list_attached` is monkeypatched."""
    from playground.backend import dispatch

    config_dir = _device_lab_config(tmp_path)
    bin_dir = _write_apply_shims(tmp_path)
    ssh_bin = _write_ssh_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )

    def _stub_vm_status(resolved, tofu_dir):
        return (
            LabStatus(
                lab=resolved.lab_name,
                backend=resolved.backend,
                expected_vms=len(resolved.vms),
                provisioned_vms=len(resolved.vms),
                vms=[
                    VmStatus(name=vm.name, role=vm.role, state="provisioned")
                    for vm in resolved.vms
                ],
            ),
            [],
        )

    monkeypatch.setattr(dispatch.local_libvirt, "query_status", _stub_vm_status)
    monkeypatch.setattr(
        dispatch, "list_attached", lambda: (attached, adb_diagnostics or [])
    )
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    return CliRunner().invoke(
        app,
        [
            "adb",
            "--lab", "device-lab",
            "--config-dir", str(config_dir),
            "--tofu-dir", str(tofu_dir),
            *args,
        ],
    )


def test_adb_reads_the_binding_from_the_given_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--state-dir` must reach `query_status`: the slot's binding lives
    under it, so an `apply --state-dir /srv/pg` followed by a
    default-state-dir `adb` reported a plugged-in handset as
    `device.not_connected` while `status --state-dir /srv/pg` called it
    connected -- two contradictory answers, the wrong one naming no
    cause."""
    from playground.devices.adb import AttachedDevice
    from playground.devices.binding import write_binding

    state_dir = tmp_path / "srv-pg"
    write_binding(state_dir, "device-lab", "pixel7", "S1")
    result = _invoke_device_lab_live_status(
        tmp_path,
        monkeypatch,
        [AttachedDevice(serial="S1", state="connected", model="Pixel 7")],
        "--on", "pixel7", "--state-dir", str(state_dir),
    )

    assert result.exit_code == 0, result.output + result.stderr
    assert "adb -s S1" in result.output


def test_adb_with_the_wrong_state_dir_still_fails_but_is_now_the_operators_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart: the same binding under a DIFFERENT state dir is
    genuinely not this invocation's binding, so `device.not_connected` is
    the honest answer -- what was missing was any way to point `adb` at
    the right one."""
    from playground.devices.adb import AttachedDevice
    from playground.devices.binding import write_binding

    write_binding(tmp_path / "srv-pg", "device-lab", "pixel7", "S1")
    result = _invoke_device_lab_live_status(
        tmp_path,
        monkeypatch,
        [AttachedDevice(serial="S1", state="connected", model="Pixel 7")],
        "--on", "pixel7", "--state-dir", str(tmp_path / "elsewhere"),
    )

    assert result.exit_code == 1
    assert "device.not_connected" in result.output + result.stderr


def test_adb_surfaces_the_rebind_case_instead_of_not_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swapped cable: bound to `S1`, `S2` attached and matching. The old
    answer was `device.not_connected` ("device is not attached") with a
    matching phone plugged in."""
    from playground.devices.adb import AttachedDevice
    from playground.devices.binding import write_binding

    state_dir = tmp_path / "srv-pg"
    write_binding(state_dir, "device-lab", "pixel7", "S1")
    result = _invoke_device_lab_live_status(
        tmp_path,
        monkeypatch,
        [AttachedDevice(serial="S2", state="connected", model="Pixel 7")],
        "--on", "pixel7", "--state-dir", str(state_dir),
    )

    output = result.output + result.stderr
    assert result.exit_code == 1
    assert "device.rebind_required" in output
    assert "S2" in output
    assert "--rebind" in output


def test_adb_prints_the_downgraded_adb_unavailable_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no `adb` on PATH, `_device_statuses` downgrades
    `device.adb_unavailable` to a warning so it cannot hard-fail every
    caller of `query_status`. `adb` (a device-targeting verb) dropped it
    entirely, leaving "device 'pixel7' is not attached" as the only
    visible explanation on a machine that has no adb at all."""
    from playground.models.diagnostic import Diagnostic, SourceLocation

    result = _invoke_device_lab_live_status(
        tmp_path, monkeypatch, [], "--on", "pixel7",
        adb_diagnostics=[
            Diagnostic(
                id="device.adb_unavailable",
                severity="error",
                message="`adb devices -l` failed: adb not found on PATH",
                source=SourceLocation(path="adb"),
            )
        ],
    )

    output = result.output + result.stderr
    assert result.exit_code == 1
    assert "device.adb_unavailable" in output
    assert "not found on PATH" in output
