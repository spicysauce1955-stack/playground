"""`playground capture` — start/stop/status against a shimmed guest.

`redroid-cloud` is a cloud-digitalocean lab, so `query_status` is
monkeypatched to a canned LabStatus exactly as tests/cli/test_app.py
does; these tests are about the commands sent and the exit codes, not
about reaching DigitalOcean.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
from pathlib import Path
from textwrap import dedent

from typer.testing import CliRunner

import playground.cli.capture_commands as capture_commands
from playground.capture.state import CaptureSession, read_session, write_session
from playground.cli.main import app
from playground.models.status import LabStatus, VmStatus

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _stub_status(resolved, tofu_dir):
    return (
        LabStatus(
            lab=resolved.lab_name,
            backend=resolved.backend,
            expected_vms=len(resolved.vms),
            provisioned_vms=len(resolved.vms),
            vms=[
                VmStatus(
                    name=vm.name, role=vm.role, state="running",
                    ssh_host="127.0.0.1", ssh_port=22,
                )
                for vm in resolved.vms
            ],
        ),
        [],
    )


def _run(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, *args,
    ssh_exit=0, config_dir=None, lab="redroid-cloud",
):
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=ssh_exit)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    monkeypatch.setattr(capture_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    return CliRunner().invoke(
        app,
        ["capture", *args, "--lab", lab,
         "--config-dir", str(config_dir if config_dir is not None else CONFIG_DIR),
         "--tofu-dir", str(tofu_dir),
         "--state-dir", str(tmp_path / ".playground")],
    )


def _two_device_config(tmp_path: Path) -> Path:
    """A lab with TWO capturable VMs, synthesized rather than committed.

    The committed `redroid-cloud` lab has exactly one capturable VM
    (`droid1`), so nothing in the repo's committed config can exercise
    the CLI's per-target fan-out loops (a single target can never tell
    "skip this one, keep going" apart from "abort the whole batch"). A
    permanent two-VM lab in config/ would be walked by
    `test_every_committed_yaml_parses` and counted by tests that assert
    exact diagnostic totals (see `_committed_config_dir` in
    `test_cli.py`), so it is synthesized into a copy of the committed
    config instead -- the same `shutil.copytree` + mutate pattern
    `test_cli.py` already uses at lines ~1304 and ~1337.

    Backend is `local-libvirt` (not `redroid-cloud`'s `cloud-digitalocean`)
    purely so this fixture doesn't need a second provider block; capture
    supports both backends identically (see `_CAPTURE_BACKENDS`).
    `query_status` is monkeypatched regardless, so no real backend is ever
    touched. Two `redroid-host` VMs cost 8 vCPU / 16384 MB / 120 GB
    against the platform defaults of 12 / 24576 / 250 / 8 VMs
    (`config/defaults.yaml`), so no budget diagnostic fires and no
    explicit `spec.budget` override is needed here.
    """
    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config_dir)
    (config_dir / "labs" / "two-droids.yaml").write_text(
        dedent(
            """
            apiVersion: playground/v1
            kind: Lab
            metadata:
              name: two-droids
            spec:
              backend: local-libvirt
              networks:
                - name: lab-net
                  profile: nat
                  cidr: 10.76.0.0/24
              vms:
                - name: droid1
                  role: redroid-host
                  networks: [lab-net]
                - name: droid2
                  role: redroid-host
                  networks: [lab-net]
            """
        ).lstrip("\n")
    )
    return config_dir


def test_start_starts_the_instanced_unit(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start")
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    assert "sudo -n systemctl start" in log
    assert "playground-capture@droid1.service" in log


def test_start_writes_a_session_record(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start")
    assert result.exit_code == 0
    session = read_session(tmp_path / ".playground", "redroid-cloud", "droid1")
    assert session is not None
    assert session.unit == "playground-capture@droid1.service"
    # Limits are recorded from the lab's resolved spec.capture.
    assert session.max_file_mb == 100
    assert session.max_files == 10


def test_start_twice_is_refused(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """"Already capturing" and "just started capturing" produce
    differently attributable pcaps, so this is an error, not a no-op."""
    first = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start")
    assert first.exit_code == 0
    second = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start")
    assert second.exit_code == 1
    assert "runtime.capture.already_running" in second.output + str(second.stderr)


def test_start_does_not_record_a_session_when_the_guest_fails(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """A recorded session that never started would make `start` refuse
    forever while nothing was capturing."""
    result = _run(
        tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start", ssh_exit=1
    )
    assert result.exit_code == 1
    assert read_session(tmp_path / ".playground", "redroid-cloud", "droid1") is None


def test_start_reports_a_running_capture_when_the_session_write_fails(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """The guest is ALREADY capturing by the time `write_session` runs. An
    I/O error there must not surface as a raw traceback, and must not
    pretend nothing happened -- it has to name the still-running unit so
    the operator can reach it with `--on`/`status` even with no local
    record."""
    def _raise(state_dir, lab, session):
        raise OSError("disk full")

    monkeypatch.setattr(capture_commands, "write_session", _raise)
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start")
    assert result.exit_code == 1
    output = result.output + str(result.stderr)
    assert "runtime.capture.session_write_failed" in output
    assert "playground-capture@droid1.service" in output
    assert read_session(tmp_path / ".playground", "redroid-cloud", "droid1") is None


def test_stop_stops_the_unit_and_clears_the_record(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start")
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "stop")
    assert result.exit_code == 0, result.output
    assert "sudo -n systemctl stop" in (tmp_path / "ssh.log").read_text()
    assert read_session(tmp_path / ".playground", "redroid-cloud", "droid1") is None


def test_stop_without_a_session_is_a_clean_error(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "stop")
    assert result.exit_code == 1
    assert "runtime.capture.not_running" in result.output + str(result.stderr)


def test_status_needs_no_session_record(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """status asks the guest, so it must work on a lab this machine has
    never started a session for."""
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "status")
    assert result.exit_code == 0, result.output
    assert "is-active" in (tmp_path / "ssh.log").read_text()


def test_unknown_vm_is_rejected_before_any_ssh(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(
        tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start", "--on", "nope"
    )
    assert result.exit_code == 1
    assert "config.capture.unknown_vm" in result.output + str(result.stderr)
    assert not (tmp_path / "ssh.log").exists()


def test_start_all_skips_a_running_device_but_starts_the_rest(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """FIX 1 regression test: one device already mid-capture must not
    abort the whole `--all` batch. Proven three ways, because each is a
    different symptom the old batch-abort `_fail()` produced: the run
    still fails overall (1), but `droid2` -- which has nothing wrong with
    it -- gets started anyway (2), and `droid1`'s pre-existing record is
    left untouched rather than clobbered (3).
    """
    config_dir = _two_device_config(tmp_path)
    state_dir = tmp_path / ".playground"
    write_session(
        state_dir,
        "two-droids",
        CaptureSession(
            vm="droid1",
            unit="playground-capture@droid1.service",
            started_at="2026-01-01T00:00:00+00:00",
            max_file_mb=100,
            max_files=10,
            snaplen=0,
        ),
    )

    result = _run(
        tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start", "--all",
        config_dir=config_dir, lab="two-droids",
    )

    # (1) something failed overall.
    assert result.exit_code == 1
    output = result.output + str(result.stderr)
    assert "runtime.capture.already_running" in output

    # (2) droid2 was still started, despite droid1 being skipped.
    log = (tmp_path / "ssh.log").read_text()
    assert "playground-capture@droid2.service" in log
    droid2_session = read_session(state_dir, "two-droids", "droid2")
    assert droid2_session is not None
    assert droid2_session.unit == "playground-capture@droid2.service"

    # (3) droid1's pre-existing record is untouched, not clobbered by a
    # second start racing the first.
    droid1_session = read_session(state_dir, "two-droids", "droid1")
    assert droid1_session is not None
    assert droid1_session.started_at == "2026-01-01T00:00:00+00:00"


def test_stop_all_skips_a_device_with_no_record_but_stops_the_rest(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """Mirror of the start test: one device with no recorded session must
    not block stopping another device that IS genuinely capturing."""
    config_dir = _two_device_config(tmp_path)
    state_dir = tmp_path / ".playground"
    write_session(
        state_dir,
        "two-droids",
        CaptureSession(
            vm="droid1",
            unit="playground-capture@droid1.service",
            started_at="2026-01-01T00:00:00+00:00",
            max_file_mb=100,
            max_files=10,
            snaplen=0,
        ),
    )

    result = _run(
        tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "stop", "--all",
        config_dir=config_dir, lab="two-droids",
    )

    assert result.exit_code == 1
    output = result.output + str(result.stderr)
    assert "runtime.capture.not_running" in output

    # droid1 WAS stopped on the guest and its record cleared, despite
    # droid2 having nothing to stop.
    log = (tmp_path / "ssh.log").read_text()
    assert "sudo -n systemctl stop" in log
    assert "playground-capture@droid1.service" in log
    assert read_session(state_dir, "two-droids", "droid1") is None


def test_fetch_scps_the_guest_capture_directory(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, write_scp_shim
) -> None:
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)
    scp_bin = write_scp_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH",
        f"{scp_bin}{os.pathsep}{ssh_bin}{os.pathsep}{bin_dir}"
        f"{os.pathsep}{os.environ['PATH']}",
    )
    monkeypatch.setattr(capture_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    result = CliRunner().invoke(
        app,
        ["capture", "fetch", "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir),
         "--state-dir", str(tmp_path / ".playground")],
    )
    assert result.exit_code == 0, result.output
    scp_log = (tmp_path / "scp.log").read_text()
    assert "-r" in scp_log, "the guest capture directory is a directory"
    assert "/var/lib/playground/capture/droid1" in scp_log
    # The pcaps land as a run artifact, so `runs list` can find them.
    runs = list((tmp_path / ".playground" / "runs").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "run.json").is_file()
    assert (runs[0] / "artifacts" / "capture" / "droid1").is_dir()


def test_fetch_leaves_the_guest_copy_in_place_by_default(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, write_scp_shim
) -> None:
    """A failed transfer must not be able to destroy the only copy, so
    removal is opt-in via --clean."""
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)
    scp_bin = write_scp_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH",
        f"{scp_bin}{os.pathsep}{ssh_bin}{os.pathsep}{bin_dir}"
        f"{os.pathsep}{os.environ['PATH']}",
    )
    monkeypatch.setattr(capture_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    CliRunner().invoke(
        app,
        ["capture", "fetch", "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir),
         "--state-dir", str(tmp_path / ".playground")],
    )
    assert not (tmp_path / "ssh.log").exists(), (
        "fetch must not touch the guest's shell when --clean was not passed"
    )


def test_fetch_all_reports_the_working_device_when_one_fails(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """One device's scp failing must not swallow the other's success.

    This exact fan-out shape (per-target failure among several targets)
    was the site of a real defect in the previous task -- the whole
    batch used to abort on one bad target. It is correct here (traced,
    not just asserted), but shipping the highest-risk path with no
    regression coverage is how that defect comes back.
    """
    config_dir = _two_device_config(tmp_path)
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)

    # A per-target scp shim: the destination path carries the VM name
    # (`artifacts/<vm>`), so branching on argv content picks out droid2
    # without touching droid1's transfer. `write_scp_shim` (conftest)
    # can only fail uniformly for every invocation, so this shim is
    # local to this test rather than a new conftest factory.
    scp_dir = tmp_path / "scpbin"
    scp_dir.mkdir()
    scp = scp_dir / "scp"
    scp.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> {shlex.quote(str(tmp_path / "scp.log"))}\n'
        'case "$*" in\n'
        "  *droid2*) exit 1 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    scp.chmod(scp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv(
        "PATH",
        f"{scp_dir}{os.pathsep}{ssh_bin}{os.pathsep}{bin_dir}"
        f"{os.pathsep}{os.environ['PATH']}",
    )
    monkeypatch.setattr(capture_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)

    result = CliRunner().invoke(
        app,
        ["capture", "fetch", "--all", "--lab", "two-droids",
         "--config-dir", str(config_dir), "--tofu-dir", str(tofu_dir),
         "--state-dir", str(tmp_path / ".playground")],
    )

    assert result.exit_code == 1
    output = result.output + str(result.stderr)
    # droid1's success was reported despite droid2 failing alongside it.
    assert "[droid1] pcaps →" in output

    runs = list((tmp_path / ".playground" / "runs").iterdir())
    assert len(runs) == 1
    run_record = json.loads((runs[0] / "run.json").read_text())
    assert run_record["status"] == "failed"
    assert (runs[0] / "artifacts" / "capture" / "droid1").is_dir()
    # The summary is what `playground runs show` prints back later: a
    # transfer failure must be counted there, so it says 1/2, not 2/2.
    assert "1/2" in run_record["summary"]


def test_fetch_clean_failure_is_reported_as_a_failure(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, write_scp_shim
) -> None:
    """The operator explicitly asked for cleanup and did not get it. The
    pcaps are safely on disk either way (fetch itself succeeded), but a
    guest-side `rm` failure (misconfigured sudoers, permissions) must
    not be swallowed behind an exit-0 run recorded `succeeded` -- that
    would tell the operator cleanup happened when it did not.
    """
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=1)
    scp_bin = write_scp_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH",
        f"{scp_bin}{os.pathsep}{ssh_bin}{os.pathsep}{bin_dir}"
        f"{os.pathsep}{os.environ['PATH']}",
    )
    monkeypatch.setattr(capture_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)

    result = CliRunner().invoke(
        app,
        ["capture", "fetch", "--lab", "redroid-cloud", "--clean",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir),
         "--state-dir", str(tmp_path / ".playground")],
    )

    assert result.exit_code == 1
    output = result.output + str(result.stderr)
    assert "guest-side cleanup failed" in output

    # The pcaps ARE on disk -- only the guest-side cleanup failed, so
    # nothing about the transfer itself should look like a failure.
    runs = list((tmp_path / ".playground" / "runs").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "artifacts" / "capture" / "droid1").is_dir()
    run_record = json.loads((runs[0] / "run.json").read_text())
    assert run_record["status"] == "failed"
    # The transfer succeeded -- only cleanup failed -- so the summary
    # must say the pcaps WERE fetched (1/1), never 0/1, and must call
    # out the cleanup failure so `playground runs show` doesn't read as
    # a lost capture.
    summary = run_record["summary"]
    assert "1/1" in summary
    assert "0/1" not in summary
    assert "cleanup" in summary


def test_fetch_writes_a_log_file_for_runs_show_on_failure(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, write_scp_shim
) -> None:
    """`log_path` must point at a real log FILE under the run's `logs/`
    directory, not the artifact directory -- `runs_show_command` prints
    `log_path` expecting a diagnostic file, and the failure case (a
    dead scp) is exactly the one an operator will go looking for via
    `playground runs show`.
    """
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)
    scp_bin = write_scp_shim(tmp_path, exit_code=1)
    monkeypatch.setenv(
        "PATH",
        f"{scp_bin}{os.pathsep}{ssh_bin}{os.pathsep}{bin_dir}"
        f"{os.pathsep}{os.environ['PATH']}",
    )
    monkeypatch.setattr(capture_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)

    result = CliRunner().invoke(
        app,
        ["capture", "fetch", "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir),
         "--state-dir", str(tmp_path / ".playground")],
    )

    assert result.exit_code == 1
    runs = list((tmp_path / ".playground" / "runs").iterdir())
    assert len(runs) == 1
    run_dir = runs[0]
    run_record = json.loads((run_dir / "run.json").read_text())
    log_path = Path(run_record["steps"][0]["log_path"])
    # Under logs/, not artifacts/ -- and not the artifact directory itself.
    assert log_path.parent == run_dir / "logs"
    assert log_path.is_file()
    assert "exit 1" in log_path.read_text()
