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
    from playground.capture.state import lab_capture_dir

    capture_dir = lab_capture_dir(tmp_path, "redroid-cloud")
    capture_dir.mkdir(parents=True)
    (capture_dir / "droid1.json").write_text("{}")
    other = lab_capture_dir(tmp_path, "other-lab")
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
    each backend's `execute_reset` must both build the capture path
    through the SAME function `session_path` uses (`lab_capture_dir` in
    `playground.capture.state`) and hand its result to the cleaner.
    Building it without passing it is a silent no-op, which is the exact
    failure mode worth a test.

    This used to assert on the literal source text
    `'state_dir / "state" / "capture" / lab'` -- if `session_path` (or
    any runner) had ever moved off that literal, `reset` would scrub the
    wrong directory with a green suite. Parsing the AST for an actual
    call to `lab_capture_dir(state_dir, lab)` whose result feeds
    `targets=[...]` ties this test to the same function every path is
    now built from, rather than to a string every writer must
    independently keep in sync.
    """
    import ast
    from pathlib import Path as _Path

    from playground.capture.state import lab_capture_dir as _lab_capture_dir

    assert _lab_capture_dir.__module__ == "playground.capture.state"

    backend_root = (
        _Path(__file__).resolve().parents[4] / "src" / "playground" / "backend"
    )
    for backend in ("local_libvirt", "local_vbox", "cloud_digitalocean"):
        path = backend_root / backend / "runner.py"
        tree = ast.parse(path.read_text(), filename=str(path))

        reset_fn = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "execute_reset"
            ),
            None,
        )
        assert reset_fn is not None, f"{backend}: no execute_reset() found"

        # Find `<name> = lab_capture_dir(state_dir, lab)` inside execute_reset.
        assigned_name = None
        for node in ast.walk(reset_fn):
            if not isinstance(node, ast.Assign):
                continue
            call = node.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "lab_capture_dir"
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                assigned_name = node.targets[0].id
                break
        assert assigned_name is not None, (
            f"{backend}: execute_reset never calls lab_capture_dir(...) to "
            "build the per-lab capture path"
        )

        # Find the `_clean_state_files(..., targets=[...])` call and require
        # the assigned name to be one of its elements.
        targets_call = None
        for node in ast.walk(reset_fn):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "targets" and isinstance(kw.value, ast.List):
                    targets_call = kw.value
                    break
            if targets_call is not None:
                break
        assert targets_call is not None, (
            f"{backend}: no _clean_state_files(targets=[...]) call found"
        )
        target_names = [
            elt.id for elt in targets_call.elts if isinstance(elt, ast.Name)
        ]
        assert assigned_name in target_names, (
            f"{backend}: {assigned_name} (from lab_capture_dir(...)) is "
            "built but never passed to _clean_state_files, so reset would "
            "silently not scrub it"
        )
