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
