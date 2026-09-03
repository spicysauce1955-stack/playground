"""verify-lab checks that declared APKs actually installed."""

from __future__ import annotations

import subprocess

from playground.backend.local_libvirt import verify as verify_mod
from playground.backend.local_libvirt.verify import VmTarget, _verify_one
from playground.events import EventBus


def _target(**kw: object) -> VmTarget:
    base: dict[str, object] = dict(
        name="droid1", ip="10.0.0.5", ssh_user="ubuntu",
        has_docker=False, ssh_port=22, android_packages=("com.example.app",),
    )
    base.update(kw)
    return VmTarget(**base)  # type: ignore[arg-type]


def test_missing_package_produces_a_diagnostic(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        command = argv[-1]
        if "pm list packages" in command:
            return subprocess.CompletedProcess(argv, 0, "package:com.other\n", "")
        return subprocess.CompletedProcess(argv, 0, "running", "")

    monkeypatch.setattr(verify_mod.subprocess, "run", fake_run)
    outcome = _verify_one(
        target=_target(), any_commands=[], bus=EventBus(), run_id="r1", timeout=30.0
    )
    assert any("com.example.app" in d.message for d in outcome.diagnostics)


def test_present_package_produces_no_diagnostic(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        command = argv[-1]
        if "pm list packages" in command:
            return subprocess.CompletedProcess(
                argv, 0, "package:com.example.app\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "running", "")

    monkeypatch.setattr(verify_mod.subprocess, "run", fake_run)
    outcome = _verify_one(
        target=_target(), any_commands=[], bus=EventBus(), run_id="r1", timeout=30.0
    )
    assert outcome.diagnostics == []


def test_prefix_collision_is_not_treated_as_installed(monkeypatch) -> None:
    """`com.foo` must NOT match when only `com.foo.debug` is installed.

    A substring test (`"package:com.foo" not in installed.stdout`) would
    incorrectly pass here because "package:com.foo" is a substring of
    "package:com.foo.debug". Exact-token membership must catch this.
    """

    def fake_run(argv, **kwargs):
        command = argv[-1]
        if "pm list packages" in command:
            return subprocess.CompletedProcess(
                argv, 0, "package:com.foo.debug\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "running", "")

    monkeypatch.setattr(verify_mod.subprocess, "run", fake_run)
    outcome = _verify_one(
        target=_target(android_packages=("com.foo",)),
        any_commands=[],
        bus=EventBus(),
        run_id="r1",
        timeout=30.0,
    )
    assert any("com.foo" in d.message for d in outcome.diagnostics)
    assert any("NOT installed" in line for line in outcome.log_lines)


def test_query_failure_reports_unreachable_device_not_missing_package(
    monkeypatch,
) -> None:
    """A failed adb/ssh call must be diagnosed as "could not query", not
    as "package not installed" — otherwise the reader is sent down the
    wrong debugging path (blaming the install instead of the device)."""

    def fake_run(argv, **kwargs):
        command = argv[-1]
        if "pm list packages" in command:
            return subprocess.CompletedProcess(
                argv, 255, "", "ssh: connect to host 10.0.0.5 port 22: Connection refused"
            )
        return subprocess.CompletedProcess(argv, 0, "running", "")

    monkeypatch.setattr(verify_mod.subprocess, "run", fake_run)
    outcome = _verify_one(
        target=_target(), any_commands=[], bus=EventBus(), run_id="r1", timeout=30.0
    )
    assert len(outcome.diagnostics) == 1
    message = outcome.diagnostics[0].message
    assert "could not query" in message
    assert "not installed" not in message
    assert outcome.diagnostics[0].id == "runtime.apply.verify_failed"
    assert outcome.diagnostics[0].severity == "error"


def test_vm_with_no_declared_packages_skips_the_check(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(argv, **kwargs):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, "running", "")

    monkeypatch.setattr(verify_mod.subprocess, "run", fake_run)
    _verify_one(
        target=_target(android_packages=()),
        any_commands=[],
        bus=EventBus(),
        run_id="r1",
        timeout=30.0,
    )
    assert not any("pm list packages" in c for c in calls)
