"""Unit tests for the libvirt scrub-by-name path used by ``playground reset``.

Every test stubs ``subprocess.run`` so the suite never touches a real
libvirtd. The integration smoke (apply → manual virsh undefine →
reset) lives in the live-infra test suite gated on
``PLAYGROUND_LIVE_INFRA=1``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from playground.backend.local_libvirt import scrub
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def resolved_generic_infra():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


def _completed(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["virsh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# Pre-flight: virsh missing
# ---------------------------------------------------------------------------


def test_scrub_lab_fails_when_virsh_missing(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(scrub.shutil, "which", lambda _name: None)
    step, diagnostics = scrub.scrub_lab(
        resolved=resolved_generic_infra,
        log_path=tmp_path / "scrub.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 127
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.reset.virsh_missing"
    assert (tmp_path / "scrub.log").exists()


# ---------------------------------------------------------------------------
# Pre-flight: virsh unreachable (list fails)
# ---------------------------------------------------------------------------


def test_scrub_lab_fails_when_virsh_list_fails(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(scrub.shutil, "which", lambda _name: "/usr/bin/virsh")

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return _completed(returncode=1, stderr="failed to connect to socket")

    monkeypatch.setattr(scrub.subprocess, "run", _stub)
    step, diagnostics = scrub.scrub_lab(
        resolved=resolved_generic_infra,
        log_path=tmp_path / "scrub.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 1
    assert any(d.id == "runtime.reset.virsh_unreachable" for d in diagnostics)


# ---------------------------------------------------------------------------
# Happy path: lab resources present and removed cleanly
# ---------------------------------------------------------------------------


def test_scrub_lab_removes_all_matching_resources(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(scrub.shutil, "which", lambda _name: "/usr/bin/virsh")
    calls: list[list[str]] = []
    # generic-infra has node1, docker1, router1 and edge, lab-private, routed-a.
    domain_listing = "node1\ndocker1\nrouter1\nother-vm\n"
    network_listing = "edge\nlab-private\nrouted-a\ndefault\n"
    volume_listing = (
        " Name                Path\n"
        "----------------------------------------\n"
        " node1.qcow2         /var/lib/libvirt/images/node1.qcow2\n"
        " docker1.qcow2       /var/lib/libvirt/images/docker1.qcow2\n"
        " router1.qcow2       /var/lib/libvirt/images/router1.qcow2\n"
        " commoninit-node1.iso   /var/lib/libvirt/images/commoninit-node1.iso\n"
        " commoninit-docker1.iso /var/lib/libvirt/images/commoninit-docker1.iso\n"
        " commoninit-router1.iso /var/lib/libvirt/images/commoninit-router1.iso\n"
        " ubuntu-noble.qcow2     /var/lib/libvirt/images/ubuntu-noble.qcow2\n"
    )

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        # Strip the leading prefix flags `--quiet --connect qemu:///system`.
        rest = [a for a in args if a not in ("virsh", "--quiet", "--connect", "qemu:///system")]
        calls.append(rest)
        if rest[:1] == ["list"]:
            return _completed(stdout=domain_listing)
        if rest[:1] == ["net-list"]:
            return _completed(stdout=network_listing)
        if rest[:1] == ["vol-list"]:
            return _completed(stdout=volume_listing)
        return _completed()  # destroy / undefine / vol-delete all succeed

    monkeypatch.setattr(scrub.subprocess, "run", _stub)
    step, diagnostics = scrub.scrub_lab(
        resolved=resolved_generic_infra,
        log_path=tmp_path / "scrub.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert diagnostics == []
    assert step.exit_code == 0

    # Every lab domain got destroy + undefine. The undefine carries the
    # safety flags so qcow2-backed VMs with snapshots/NVRAM are cleaned.
    for vm in ("node1", "docker1", "router1"):
        assert ["destroy", vm] in calls
        assert [
            "undefine", "--nvram", "--managed-save", "--snapshots-metadata", vm
        ] in calls
        assert ["vol-delete", "--pool", "default", f"{vm}.qcow2"] in calls
        assert ["vol-delete", "--pool", "default", f"commoninit-{vm}.iso"] in calls

    for net in ("edge", "lab-private", "routed-a"):
        assert ["net-destroy", net] in calls
        assert ["net-undefine", net] in calls

    # We never touch the shared base image even though it appears in the listing.
    assert ["vol-delete", "--pool", "default", "ubuntu-noble.qcow2"] not in calls
    # We never touch unrelated other-vm / default network.
    assert ["destroy", "other-vm"] not in calls
    assert ["net-destroy", "default"] not in calls


# ---------------------------------------------------------------------------
# Already-clean: nothing to remove
# ---------------------------------------------------------------------------


def test_scrub_lab_is_noop_when_nothing_exists(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(scrub.shutil, "which", lambda _name: "/usr/bin/virsh")
    calls: list[list[str]] = []

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        rest = [a for a in args if a not in ("virsh", "--quiet", "--connect", "qemu:///system")]
        calls.append(rest)
        # All listings empty.
        return _completed(stdout="")

    monkeypatch.setattr(scrub.subprocess, "run", _stub)
    step, diagnostics = scrub.scrub_lab(
        resolved=resolved_generic_infra,
        log_path=tmp_path / "scrub.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert diagnostics == []
    assert step.exit_code == 0
    # Only the three listing calls — never any destroy/undefine/vol-delete.
    assert {tuple(c[:1]) for c in calls} == {("list",), ("net-list",), ("vol-list",)}


# ---------------------------------------------------------------------------
# Tolerance: virsh destroy on stopped domain returns non-zero but is OK
# ---------------------------------------------------------------------------


def test_scrub_lab_tolerates_already_stopped_domain(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(scrub.shutil, "which", lambda _name: "/usr/bin/virsh")

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        rest = [a for a in args if a not in ("virsh", "--quiet", "--connect", "qemu:///system")]
        if rest[:1] == ["list"]:
            return _completed(stdout="node1\ndocker1\nrouter1\n")
        if rest[:1] == ["net-list"]:
            return _completed(stdout="edge\nlab-private\nrouted-a\n")
        if rest[:1] == ["vol-list"]:
            return _completed(stdout="")
        if rest[:1] == ["destroy"]:
            return _completed(returncode=1, stderr="error: Requested operation is not valid: domain is not running")
        return _completed()

    monkeypatch.setattr(scrub.subprocess, "run", _stub)
    step, diagnostics = scrub.scrub_lab(
        resolved=resolved_generic_infra,
        log_path=tmp_path / "scrub.log",
        bus=EventBus(),
        run_id="r1",
    )
    # "not running" is tolerated; undefine still succeeded.
    assert step.exit_code == 0
    assert diagnostics == []


# ---------------------------------------------------------------------------
# Tolerance: pool absent → warning, but domains still cleaned
# ---------------------------------------------------------------------------


def test_scrub_lab_continues_when_pool_listing_fails(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(scrub.shutil, "which", lambda _name: "/usr/bin/virsh")
    domain_calls: list[list[str]] = []

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        rest = [a for a in args if a not in ("virsh", "--quiet", "--connect", "qemu:///system")]
        if rest[:1] in (["list"], ["net-list"]):
            return _completed(stdout="node1\n" if rest[0] == "list" else "edge\n")
        if rest[:1] == ["vol-list"]:
            return _completed(returncode=1, stderr="error: pool 'default' not found")
        domain_calls.append(rest)
        return _completed()

    monkeypatch.setattr(scrub.subprocess, "run", _stub)
    step, diagnostics = scrub.scrub_lab(
        resolved=resolved_generic_infra,
        log_path=tmp_path / "scrub.log",
        bus=EventBus(),
        run_id="r1",
    )
    # Surface the pool warning but still finish the run.
    assert any(d.id == "runtime.reset.pool_unreachable" for d in diagnostics)
    # node1 still got destroyed + undefined.
    assert ["destroy", "node1"] in domain_calls
    # No vol-delete calls because volumes was empty.
    assert not any(c[:1] == ["vol-delete"] for c in domain_calls)


# ---------------------------------------------------------------------------
# Real (non-tolerable) virsh failure surfaces a diagnostic
# ---------------------------------------------------------------------------


def test_scrub_lab_surfaces_unexpected_undefine_failure(
    resolved_generic_infra, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(scrub.shutil, "which", lambda _name: "/usr/bin/virsh")

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        rest = [a for a in args if a not in ("virsh", "--quiet", "--connect", "qemu:///system")]
        if rest[:1] == ["list"]:
            return _completed(stdout="node1\n")
        if rest[:1] == ["net-list"]:
            return _completed(stdout="")
        if rest[:1] == ["vol-list"]:
            return _completed(stdout="")
        if rest[:1] == ["destroy"]:
            return _completed()
        if rest[:1] == ["undefine"]:
            return _completed(returncode=1, stderr="error: internal error: something exploded")
        return _completed()

    monkeypatch.setattr(scrub.subprocess, "run", _stub)
    step, diagnostics = scrub.scrub_lab(
        resolved=resolved_generic_infra,
        log_path=tmp_path / "scrub.log",
        bus=EventBus(),
        run_id="r1",
    )
    assert step.exit_code == 1
    assert any(d.id == "runtime.reset.scrub_failed" for d in diagnostics)
    assert any("node1" in (d.message or "") for d in diagnostics)


# ---------------------------------------------------------------------------
# Capture state cleanup
# ---------------------------------------------------------------------------


def test_reset_removes_the_labs_capture_session_records(tmp_path) -> None:
    """A stale record makes `capture start` refuse forever after a lab is
    destroyed and re-applied: the record lives on the operator's machine,
    but the unit it names went away with the guest."""
    from playground.backend.local_libvirt.runner import _clean_state_files

    capture_dir = tmp_path / "state" / "capture" / "redroid-cloud"
    capture_dir.mkdir(parents=True)
    (capture_dir / "droid1.json").write_text("{}")
    other = tmp_path / "state" / "capture" / "other-lab"
    other.mkdir(parents=True)
    (other / "droid1.json").write_text("{}")

    step, diagnostics = _clean_state_files(
        lab="redroid-cloud",
        targets=[capture_dir],
        log_path=tmp_path / "clean.log",
    )

    assert step.exit_code == 0
    assert diagnostics == []
    assert not capture_dir.exists()
    # Never another lab's state.
    assert (other / "droid1.json").is_file()


def test_all_three_backends_wire_capture_into_clean_state_files() -> None:
    """The test above calls `_clean_state_files` directly, so it passes
    with or without this task's change — it documents the per-lab
    isolation invariant, it does not gate the wiring. THIS is the gate:
    each backend's `execute_reset` must both build the capture path and
    hand it to the cleaner. Building it without passing it is a silent
    no-op, which is the exact failure mode worth a test.

    Source-text assertions follow the established house pattern in
    `tests/unit/ansible/`.
    """
    from pathlib import Path as _Path

    backend_root = (
        _Path(__file__).resolve().parents[4] / "src" / "playground" / "backend"
    )
    for backend in ("local_libvirt", "local_vbox", "cloud_digitalocean"):
        text = (backend_root / backend / "runner.py").read_text()
        assert 'state_dir / "state" / "capture" / lab' in text, (
            f"{backend}: never builds the per-lab capture state path"
        )
        target_lines = [ln for ln in text.splitlines() if "targets=[" in ln]
        assert target_lines, f"{backend}: no _clean_state_files call found"
        assert any("capture_dir" in ln for ln in target_lines), (
            f"{backend}: capture_dir is built but never passed to "
            "_clean_state_files, so reset would silently not scrub it"
        )
