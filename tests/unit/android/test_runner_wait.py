from __future__ import annotations

from playground.android.runner import TargetResult, wait_booted
from playground.android.transport import AndroidTarget, LocalAdb


def _target(name: str = "pixel7") -> AndroidTarget:
    return AndroidTarget(name=name, transport=LocalAdb("S1"))


def test_wait_booted_returns_empty_when_boot_completes(monkeypatch):
    t = _target()

    def fake_run(targets, inv, *, timeout):
        return [TargetResult(targets[0], 0, "1\n", "")]

    monkeypatch.setattr("playground.android.runner.run_on_targets", fake_run)
    assert wait_booted([t], attempts=3, interval=0) == []


def test_wait_booted_names_targets_that_never_boot(monkeypatch):
    t = _target()

    def fake_run(targets, inv, *, timeout):
        return [TargetResult(targets[0], 0, "0\n", "")]

    monkeypatch.setattr("playground.android.runner.run_on_targets", fake_run)
    assert wait_booted([t], attempts=2, interval=0) == ["pixel7"]


def test_wait_booted_tolerates_a_carriage_return(monkeypatch):
    """getprop over adb returns CRLF on some devices."""
    t = _target()

    def fake_run(targets, inv, *, timeout):
        return [TargetResult(targets[0], 0, "1\r\n", "")]

    monkeypatch.setattr("playground.android.runner.run_on_targets", fake_run)
    assert wait_booted([t], attempts=1, interval=0) == []
