"""`playground apply`/`destroy`/`status` -- physical device lifecycle wiring.

Task 16's job: after Task 15 taught `app`/`adb`/`shell` to target a
handset via `status.devices`, nothing actually adopted a device (`apply`),
released it (`destroy`), or populated it (`status`) -- these tests cover
that wiring.

`execute_apply`/`execute_destroy` are stubbed to a synthetic successful
`OperationRun` rather than driven through real tofu/ansible/ssh shims:
device adoption/release only runs AFTER the backend step succeeds, so
these tests need a fast, deterministic "backend succeeded" without
actually reaching (flaky, network-timing-dependent)
`wait_for_vms_ready`'s live TCP/SSH probing of fabricated VM IPs --
that pipeline is already covered by `tests/cli/test_cli.py`'s apply
tests. Every test here also monkeypatches `list_attached` (and, where
relevant, `adopt`/`probe_facts`) so nothing ever shells out to a real
`adb`; none block on stdin (the discovery-prompt tests rely on
`CliRunner`'s piped stdin never being a tty, exactly as
`device_commands._is_interactive` documents).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

import playground.cli.main as cli_main
from playground.cli.main import app
from playground.devices.adb import AttachedDevice, DeviceFacts
from playground.devices.binding import read_binding, write_binding
from playground.models.status import DeviceStatus, LabStatus, VmStatus
from playground.runs.operation import OperationRun, StepResult

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _device_lab_config(tmp_path: Path, *, required: bool = True) -> Path:
    """`generic-infra`, plus one `pixel7` device slot -- synthesized
    rather than committed, same rationale as `test_adb.py::_device_lab_config`
    and `test_capture.py::_two_device_config`: a permanent device-bearing
    lab in `config/` would be walked by `test_every_committed_yaml_parses`
    and counted by diagnostic-total tests elsewhere.
    """
    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config_dir)
    (config_dir / "labs" / "device-lab.yaml").write_text(
        dedent(
            f"""
            apiVersion: playground/v1
            kind: Lab
            metadata:
              name: device-lab
            spec:
              backend: local-libvirt
              networks:
                - name: lab-net
                  profile: nat
                  cidr: 10.79.0.0/24
              vms:
                - name: node1
                  role: generic-node
                  networks: [lab-net]
              devices:
                - name: pixel7
                  kind: android
                  required: {"true" if required else "false"}
            """
        ).lstrip("\n")
    )
    return config_dir


def _fake_run(operation: str, lab: str, *, status: str = "succeeded") -> OperationRun:
    """A synthetic, already-finished `OperationRun` -- stands in for a
    real tofu/ansible apply or destroy so these tests exercise ONLY the
    device wiring layered on top, not the (flaky, network-dependent in
    a sandbox with no real VM) backend pipeline itself."""
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    return OperationRun(
        run_id=f"20260101T000000Z-{operation}-{lab}",
        operation=operation,  # type: ignore[arg-type]
        lab=lab,
        status=status,  # type: ignore[arg-type]
        started_at=now,
        finished_at=now,
        steps=[
            StepResult(
                name=f"{operation}-stub", command=["true"], exit_code=0,
                log_path="/dev/null", started_at=now, finished_at=now,
            )
        ],
        summary=f"stub {operation}",
    )


def _stub_execute_apply(monkeypatch: pytest.MonkeyPatch, lab: str) -> None:
    monkeypatch.setattr(
        "playground.cli.main.execute_apply",
        lambda **kwargs: (_fake_run("apply", lab), []),
    )


def _stub_execute_destroy(monkeypatch: pytest.MonkeyPatch, lab: str) -> None:
    monkeypatch.setattr(
        "playground.cli.main.execute_destroy",
        lambda **kwargs: (_fake_run("destroy", lab), []),
    )


def _invoke_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: Path, *extra: str
):
    _stub_execute_apply(monkeypatch, "device-lab")
    return CliRunner().invoke(
        app,
        [
            "apply", "device-lab",
            "--config-dir", str(config_dir),
            "--tofu-dir", str(tmp_path / "tofu"),
            "--ansible-dir", str(tmp_path / "ansible"),
            "--state-dir", str(tmp_path / ".playground"),
            *extra,
        ],
    )


# ---------------------------------------------------------------------------
# apply: adoption
# ---------------------------------------------------------------------------


def test_apply_adopts_declared_devices(tmp_path, monkeypatch):
    """A lab with a device slot runs adoption through `apply`."""
    calls: list[str] = []
    monkeypatch.setattr(
        "playground.cli.main.adopt",
        lambda *a, **k: (calls.append("adopt"), ([], []))[1],
    )
    monkeypatch.setattr("playground.cli.main.list_attached", lambda: ([], []))

    config_dir = _device_lab_config(tmp_path, required=True)
    result = _invoke_apply(tmp_path, monkeypatch, config_dir)

    assert result.exit_code == 0, result.stderr
    assert calls == ["adopt"]


def test_apply_a_device_less_lab_never_calls_adopt(tmp_path, monkeypatch):
    """The no-regression guarantee: a lab with no `spec.devices` must not
    even call `adopt`/`list_attached`."""
    calls: list[str] = []
    monkeypatch.setattr(
        "playground.cli.main.adopt", lambda *a, **k: calls.append("adopt") or ([], [])
    )
    monkeypatch.setattr(
        "playground.cli.main.list_attached", lambda: calls.append("list_attached") or ([], [])
    )
    _stub_execute_apply(monkeypatch, "generic-infra")

    result = CliRunner().invoke(
        app,
        [
            "apply", "generic-infra",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tmp_path / "tofu"),
            "--ansible-dir", str(tmp_path / "ansible"),
            "--state-dir", str(tmp_path / ".playground"),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert calls == []


def test_apply_fails_when_a_required_device_is_absent(tmp_path, monkeypatch):
    """Real `adopt`, no handset attached, slot is `required: true` ->
    the apply must fail even though the backend step succeeded."""
    monkeypatch.setattr("playground.cli.main.list_attached", lambda: ([], []))

    config_dir = _device_lab_config(tmp_path, required=True)
    result = _invoke_apply(tmp_path, monkeypatch, config_dir)

    assert result.exit_code == 1
    assert "config.device.no_match" in result.output + result.stderr


def test_apply_succeeds_with_a_warning_when_an_optional_device_is_absent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("playground.cli.main.list_attached", lambda: ([], []))

    config_dir = _device_lab_config(tmp_path, required=False)
    result = _invoke_apply(tmp_path, monkeypatch, config_dir)

    assert result.exit_code == 0, result.stderr
    assert "config.device.no_match" in result.output + result.stderr


def test_apply_adopted_device_is_bound_for_a_later_status_query(tmp_path, monkeypatch):
    """End-to-end (adoption side): after `apply`, the binding persists so
    a later `status`/`app`/`adb` call can find it without re-adopting.

    `probe_facts` (real `adopt`'s follow-up `getprop`/`su -c id` round
    trips once a candidate matches) is stubbed out here -- it is `adopt`'s
    own concern, already covered by its unit tests, and the real function
    would shell out to a real, non-existent `S1`.
    """
    monkeypatch.setattr(
        "playground.cli.main.list_attached",
        lambda: ([AttachedDevice(serial="S1", state="connected", model="Pixel 7")], []),
    )
    monkeypatch.setattr(
        "playground.devices.adopt.probe_facts",
        lambda serial: DeviceFacts(serial=serial, model="Pixel 7"),
    )

    config_dir = _device_lab_config(tmp_path, required=True)
    result = _invoke_apply(tmp_path, monkeypatch, config_dir)

    assert result.exit_code == 0, result.stderr
    binding = read_binding(tmp_path / ".playground", "device-lab", "pixel7")
    assert binding is not None
    assert binding.serial == "S1"


# ---------------------------------------------------------------------------
# apply: discovery prompt (offer_unknown wiring)
# ---------------------------------------------------------------------------


def test_apply_discovery_never_prompts_off_a_tty(tmp_path, monkeypatch):
    """`CliRunner`'s piped stdin is never a tty, so `effective_mode`
    resolves to `silent` regardless of the lab's configured
    `device_discovery` -- an attached-but-unclaimed handset must not
    cause a config/devices/*.yaml write, and the command must not hang.

    `adopt` is stubbed to a no-op success: this test is about the
    discovery half only, and a real `adopt` matching >1 attached
    candidate against one unfiltered slot would otherwise fail on
    `config.device.ambiguous_match` before discovery ever runs.
    """
    monkeypatch.setattr("playground.cli.main.adopt", lambda *a, **k: ([], []))
    monkeypatch.setattr(
        "playground.cli.main.list_attached",
        lambda: (
            [
                AttachedDevice(serial="S1", state="connected", model="Pixel 7"),
                AttachedDevice(serial="S9", state="connected", model="Unclaimed"),
            ],
            [],
        ),
    )

    config_dir = _device_lab_config(tmp_path, required=False)
    result = _invoke_apply(tmp_path, monkeypatch, config_dir)

    assert result.exit_code == 0, result.stderr
    assert not (config_dir / "devices").exists() or not any(
        (config_dir / "devices").glob("*.yaml")
    )


def test_apply_writes_a_profile_for_what_discovery_returns(tmp_path, monkeypatch):
    """Wiring test for the `offer_unknown` -> `_write_profile` half:
    stub `offer_unknown` itself (its own prompt/ignore semantics are
    Task 12's, already covered by `tests/unit/devices/test_discovery.py`)
    and verify `apply` persists what it returns as a real DeviceProfile."""
    monkeypatch.setattr("playground.cli.main.list_attached", lambda: ([], []))
    monkeypatch.setattr(
        "playground.cli.main.offer_unknown", lambda *a, **k: {"S9": "new-phone"}
    )

    config_dir = _device_lab_config(tmp_path, required=False)
    result = _invoke_apply(tmp_path, monkeypatch, config_dir)

    assert result.exit_code == 0, result.stderr
    written = config_dir / "devices" / "new-phone.yaml"
    assert written.is_file()
    assert "S9" in written.read_text(encoding="utf-8")


def test_apply_output_json_never_prompts_even_on_a_tty(tmp_path, monkeypatch):
    """`--output json` must force silent discovery even if `_is_interactive`
    happens to say True -- a scripted caller must get clean JSON, never an
    interleaved prompt or a stray `saved device profile ...` line.

    `adopt` is stubbed to a no-op success (this test is about the
    discovery/`--output json` interaction, not adoption itself, and a
    real `adopt` would otherwise auto-bind `S9` and shell out to a real,
    non-existent handset via `probe_facts`).
    """
    monkeypatch.setattr("playground.cli.main.adopt", lambda *a, **k: ([], []))
    monkeypatch.setattr("playground.cli.main._is_interactive", lambda: True)
    monkeypatch.setattr(
        "playground.cli.main.list_attached",
        lambda: ([AttachedDevice(serial="S9", state="connected")], []),
    )
    seen_modes: list[str] = []
    monkeypatch.setattr(
        "playground.cli.main.offer_unknown",
        lambda *a, mode, **k: seen_modes.append(mode) or {},
    )

    config_dir = _device_lab_config(tmp_path, required=False)
    result = _invoke_apply(tmp_path, monkeypatch, config_dir, "--output", "json")

    assert result.exit_code == 0, result.stderr
    assert seen_modes == ["silent"]
    json.loads(result.stdout)  # must be clean, parseable JSON on stdout


# ---------------------------------------------------------------------------
# destroy: release
# ---------------------------------------------------------------------------


def test_destroy_releases_the_lab_devices_binding(tmp_path, monkeypatch):
    _stub_execute_destroy(monkeypatch, "device-lab")
    config_dir = _device_lab_config(tmp_path, required=False)
    state_dir = tmp_path / ".playground"
    write_binding(state_dir, "device-lab", "pixel7", "S1")

    result = CliRunner().invoke(
        app,
        [
            "destroy", "device-lab",
            "--config-dir", str(config_dir),
            "--tofu-dir", str(tmp_path / "tofu"),
            "--state-dir", str(state_dir),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert read_binding(state_dir, "device-lab", "pixel7") is None


def test_destroy_a_device_less_lab_never_calls_release(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "playground.cli.main.release", lambda *a, **k: calls.append("release") or []
    )
    _stub_execute_destroy(monkeypatch, "generic-infra")

    result = CliRunner().invoke(
        app,
        [
            "destroy", "generic-infra",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tmp_path / "tofu"),
            "--state-dir", str(tmp_path / ".playground"),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert calls == []


# ---------------------------------------------------------------------------
# status: device section
# ---------------------------------------------------------------------------


def _stub_status_with_devices(devices: list[DeviceStatus]):
    def _stub(resolved, tofu_dir, state_dir=Path(".playground")):
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

    return _stub


def test_status_human_output_renders_a_device_section(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli_main,
        "query_status",
        _stub_status_with_devices(
            [DeviceStatus(name="pixel7", state="connected", serial="S1", model="Pixel 7")]
        ),
    )
    config_dir = _device_lab_config(tmp_path, required=False)

    result = CliRunner().invoke(
        app,
        [
            "status", "device-lab",
            "--config-dir", str(config_dir),
            "--tofu-dir", str(tmp_path / "tofu"),
            "--state-dir", str(tmp_path / ".playground"),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert "pixel7" in result.output
    assert "S1" in result.output
    assert "connected" in result.output


def test_status_human_output_omits_device_section_when_there_are_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "query_status", _stub_status_with_devices([]))

    result = CliRunner().invoke(
        app,
        [
            "status", "generic-infra",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tmp_path / "tofu"),
            "--state-dir", str(tmp_path / ".playground"),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert "devices connected" not in result.output


def test_status_json_output_includes_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli_main,
        "query_status",
        _stub_status_with_devices(
            [DeviceStatus(name="pixel7", state="connected", serial="S1")]
        ),
    )
    config_dir = _device_lab_config(tmp_path, required=False)

    result = CliRunner().invoke(
        app,
        [
            "status", "device-lab",
            "--config-dir", str(config_dir),
            "--tofu-dir", str(tmp_path / "tofu"),
            "--state-dir", str(tmp_path / ".playground"),
            "--output", "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["devices"] == [
        {"name": "pixel7", "state": "connected", "serial": "S1"}
    ]


def _stub_status_with_diagnostics(diagnostics: list):
    def _stub(resolved, tofu_dir, state_dir=Path(".playground")):
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
                devices=[DeviceStatus(name="pixel7", state="missing")],
            ),
            diagnostics,
        )

    return _stub


def test_status_single_lab_human_output_surfaces_a_warning_diagnostic(tmp_path, monkeypatch):
    """Regression test for the fix-round-2 finding: the single-lab
    `status <lab>` branch used to call `_exit_on_errors` (a no-op for a
    WARNING) and then silently drop `query_diagnostics` -- so `adb`
    missing/hung on the controller rendered as a bare `state=missing`
    with no visible cause. It must now print the warning."""
    from playground.models.diagnostic import Diagnostic, SourceLocation

    warning = Diagnostic(
        id="device.adb_unavailable",
        severity="warning",
        message="`adb devices -l` failed: adb not found on PATH",
        source=SourceLocation(path="adb"),
        suggestion="install android-platform-tools and re-run `playground doctor`",
    )
    monkeypatch.setattr(cli_main, "query_status", _stub_status_with_diagnostics([warning]))
    config_dir = _device_lab_config(tmp_path, required=False)

    result = CliRunner().invoke(
        app,
        [
            "status", "device-lab",
            "--config-dir", str(config_dir),
            "--tofu-dir", str(tmp_path / "tofu"),
            "--state-dir", str(tmp_path / ".playground"),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert "device.adb_unavailable" in result.output + result.stderr
    assert "adb not found on PATH" in result.output + result.stderr


def test_status_single_lab_json_output_includes_the_warning_diagnostic(tmp_path, monkeypatch):
    from playground.models.diagnostic import Diagnostic, SourceLocation

    warning = Diagnostic(
        id="device.adb_unavailable",
        severity="warning",
        message="`adb devices -l` failed: adb not found on PATH",
        source=SourceLocation(path="adb"),
    )
    monkeypatch.setattr(cli_main, "query_status", _stub_status_with_diagnostics([warning]))
    config_dir = _device_lab_config(tmp_path, required=False)

    result = CliRunner().invoke(
        app,
        [
            "status", "device-lab",
            "--config-dir", str(config_dir),
            "--tofu-dir", str(tmp_path / "tofu"),
            "--state-dir", str(tmp_path / ".playground"),
            "--output", "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload["diagnostics"]) == 1
    diag = payload["diagnostics"][0]
    assert diag["id"] == "device.adb_unavailable"
    assert diag["severity"] == "warning"
    assert diag["message"] == "`adb devices -l` failed: adb not found on PATH"
