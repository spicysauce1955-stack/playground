"""Dispatch routing, apply guard and record lifecycle for isolate / connect."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from playground.backend import cloud_digitalocean, dispatch, local_libvirt
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.runs import OperationRun
from playground.runs.operation import StepResult
from playground.state.isolation import (
    IsolationRecord,
    read_isolation,
    write_isolation,
)

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

LIBVIRT_LAB = "generic-infra"
VBOX_LAB = "vbox-smoke"
DO_LAB = "cloud-smoke"


def _resolved(name: str):
    loaded, _ = load_config(CONFIG_DIR)
    return resolve_lab(loaded, name)


def _run(op: Any, lab: str, status: Any = "succeeded") -> OperationRun:
    return OperationRun(
        run_id=f"20260101T000000Z-{op}-{lab}", operation=op, lab=lab,
        status=status, started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
    )


def _record(lab: str, backend: str) -> IsolationRecord:
    return IsolationRecord(
        lab=lab, backend=backend, isolated_at="2026-01-01T00:00:00+00:00",
        lab_cidrs=["10.0.0.0/24"], run_id="r0",
    )


def _warn(id_: str) -> Diagnostic:
    return Diagnostic(
        id=id_, severity="warning", message="w", source=SourceLocation(path="x"),
    )


@dataclass
class Calls:
    """What the faked backend entry points were called with."""

    log: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    status: str = "succeeded"

    def verbs(self) -> list[tuple[str, str]]:
        return [(b, v) for b, v, _ in self.log]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Fake every backend entry point; record (backend, verb, kwargs)."""
    rec = Calls()

    def make(backend: str, verb: str):
        def fake(**kwargs: Any):
            rec.log.append((backend, verb, kwargs))
            run = _run(verb, kwargs["resolved"].lab_name, rec.status)
            if verb == "isolate" and rec.status == "failed":
                run = _with_steps(run, "attach-filter", "tofu-apply")
            return run, []
        return fake

    for verb in ("apply", "isolate", "connect", "destroy", "reset"):
        monkeypatch.setattr(local_libvirt, f"execute_{verb}", make("libvirt", verb))
    for verb in (
        "apply", "isolate", "connect", "destroy", "reset", "suspend", "resume",
    ):
        monkeypatch.setattr(cloud_digitalocean, f"execute_{verb}", make("do", verb))

    def fake_remove(lab: str) -> list[Diagnostic]:
        rec.log.append(("libvirt", "remove_filter", {"lab": lab}))
        return [_warn("w.filter")]

    monkeypatch.setattr(local_libvirt, "remove_isolation_filter", fake_remove)
    return rec


# -- isolate / connect routing ------------------------------------------------


@pytest.mark.parametrize(
    ("lab", "backend"), [(LIBVIRT_LAB, "libvirt"), (DO_LAB, "do")],
)
def test_isolate_routes_and_writes_record(
    tmp_path: Path, calls: Calls, lab: str, backend: str,
) -> None:
    resolved = _resolved(lab)
    run, _ = dispatch.execute_isolate(
        resolved=resolved, state_dir=tmp_path, bus=EventBus(),
        tofu_dir=Path("t"), config_dir=Path("c"),
    )
    assert run is not None and run.status == "succeeded"
    assert calls.verbs() == [(backend, "isolate")]
    assert set(calls.log[-1][2]) == {"resolved", "state_dir", "bus"}
    rec = read_isolation(tmp_path, lab)
    assert rec is not None
    assert rec.backend == resolved.backend
    assert rec.run_id == run.run_id
    assert rec.lab_cidrs == [n.cidr for n in resolved.networks]
    assert rec.isolated_at.endswith("+00:00")


def test_isolate_failed_run_keeps_record_fail_closed(tmp_path: Path, calls: Calls) -> None:
    calls.status = "failed"
    run, diags = dispatch.execute_isolate(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path, bus=EventBus(),
    )
    assert run is not None and run.status == "failed"
    rec = read_isolation(tmp_path, LIBVIRT_LAB)
    assert rec is not None and rec.state == "isolating"
    warn = [d for d in diags if d.id == "runtime.isolate.partially_isolated"]
    assert len(warn) == 1 and warn[0].severity == "warning"
    assert "partially isolated" in warn[0].message
    assert f"playground connect {LIBVIRT_LAB}" in (warn[0].suggestion or "")
    assert f"playground isolate {LIBVIRT_LAB}" in (warn[0].suggestion or "")


def test_isolate_records_isolating_before_backend_and_isolated_after(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake(**kwargs: Any):
        rec = read_isolation(tmp_path, LIBVIRT_LAB)
        seen.append(rec.state if rec else "absent")
        return _run("isolate", LIBVIRT_LAB), []

    monkeypatch.setattr(local_libvirt, "execute_isolate", fake)
    run, _ = dispatch.execute_isolate(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path, bus=EventBus(),
    )
    assert seen == ["isolating"]
    final = read_isolation(tmp_path, LIBVIRT_LAB)
    assert final is not None and final.state == "isolated"
    assert run is not None and final.run_id == run.run_id


def test_isolate_prewrite_failure_aborts_without_calling_backend(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(dispatch, "write_isolation", boom)
    run, diags = dispatch.execute_isolate(
        resolved=_resolved(DO_LAB), state_dir=tmp_path, bus=EventBus(),
    )
    assert run is None
    assert [d.id for d in diags] == ["runtime.isolate.record_write_failed"]
    assert diags[0].severity == "error"
    assert calls.verbs() == []


def _failed(lab: str, *steps: str) -> OperationRun:
    return _with_steps(_run("isolate", lab, "failed"), *steps)


def _with_steps(run: OperationRun, *steps: str) -> OperationRun:
    return run.model_copy(update={"steps": [
        StepResult(
            name=n, command=[n], exit_code=1, log_path="x",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
        )
        for n in steps
    ]})


@pytest.mark.parametrize(
    ("lab", "backend_mod", "step"),
    [
        (LIBVIRT_LAB, local_libvirt, "attach-filter"),
        (DO_LAB, cloud_digitalocean, "tofu-apply"),
    ],
)
def test_isolate_failure_without_mutating_step_clears_new_record(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch,
    lab: str, backend_mod: Any, step: str,
) -> None:
    monkeypatch.setattr(
        backend_mod, "execute_isolate",
        lambda **kw: (_failed(lab, "preflight", "tofu-plan"), [_warn("x.fail")]),
    )
    run, diags = dispatch.execute_isolate(
        resolved=_resolved(lab), state_dir=tmp_path, bus=EventBus(),
    )
    assert run is not None and run.status == "failed"
    assert read_isolation(tmp_path, lab) is None
    assert all(d.id != "runtime.isolate.partially_isolated" for d in diags)


@pytest.mark.parametrize(
    ("lab", "backend_mod", "step"),
    [
        (LIBVIRT_LAB, local_libvirt, "attach-filter"),
        (DO_LAB, cloud_digitalocean, "tofu-apply"),
    ],
)
def test_isolate_failure_after_mutating_step_keeps_isolating(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch,
    lab: str, backend_mod: Any, step: str,
) -> None:
    monkeypatch.setattr(
        backend_mod, "execute_isolate",
        lambda **kw: (_failed(lab, "preflight", step), []),
    )
    _, diags = dispatch.execute_isolate(
        resolved=_resolved(lab), state_dir=tmp_path, bus=EventBus(),
    )
    rec = read_isolation(tmp_path, lab)
    assert rec is not None and rec.state == "isolating"
    assert "runtime.isolate.partially_isolated" in [d.id for d in diags]


@pytest.mark.parametrize("steps", [[], ["attach-filter"]])
def test_reisolate_failure_restores_prior_isolated_record(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch,
    steps: list[str],
) -> None:
    prior = _record(LIBVIRT_LAB, "local-libvirt")
    write_isolation(tmp_path, prior)
    monkeypatch.setattr(
        local_libvirt, "execute_isolate",
        lambda **kw: (_failed(LIBVIRT_LAB, *steps), []),
    )
    _, diags = dispatch.execute_isolate(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path, bus=EventBus(),
    )
    assert read_isolation(tmp_path, LIBVIRT_LAB) == prior
    partial = "runtime.isolate.partially_isolated" in [d.id for d in diags]
    assert partial == bool(steps)


def test_teardown_of_never_isolated_lab_skips_filter_removal(
    tmp_path: Path, calls: Calls,
) -> None:
    run, diags = dispatch.execute_destroy(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path,
        tofu_dir=Path("t"), bus=EventBus(),
    )
    assert run is not None and run.status == "succeeded"
    assert ("libvirt", "remove_filter") not in calls.verbs()
    assert diags == []


def test_isolate_success_record_update_failure_keeps_guard(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = dispatch.write_isolation
    n = {"c": 0}

    def flaky(sd: Path, rec: IsolationRecord) -> Path:
        n["c"] += 1
        if n["c"] == 2:
            raise OSError("disk full")
        return real(sd, rec)

    monkeypatch.setattr(dispatch, "write_isolation", flaky)
    run, diags = dispatch.execute_isolate(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path, bus=EventBus(),
    )
    assert run is not None and run.status == "succeeded"
    assert [d.id for d in diags] == ["runtime.isolate.record_update_failed"]
    rec = read_isolation(tmp_path, LIBVIRT_LAB)
    assert rec is not None and rec.state == "isolating"


def test_guard_mentions_isolating_state(tmp_path: Path, calls: Calls) -> None:
    rec = _record(LIBVIRT_LAB, "local-libvirt").model_copy(update={"state": "isolating"})
    write_isolation(tmp_path, rec)
    run, diags = dispatch.execute_apply(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path, tofu_dir=Path("t"),
        ansible_dir=Path("a"), config_dir=CONFIG_DIR, bus=EventBus(),
    )
    assert run is None
    assert "partially isolated" in diags[0].message
    assert calls.verbs() == []


def test_guard_fails_closed_on_unreadable_record(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(dispatch, "read_isolation", boom)
    run, diags = dispatch.execute_apply(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path, tofu_dir=Path("t"),
        ansible_dir=Path("a"), config_dir=CONFIG_DIR, bus=EventBus(),
    )
    assert run is None
    assert [d.id for d in diags] == ["runtime.apply.lab_isolated"]
    assert "denied" in diags[0].message
    assert calls.verbs() == []


@pytest.mark.parametrize("lab", [LIBVIRT_LAB, DO_LAB])
def test_connect_clears_record(tmp_path: Path, calls: Calls, lab: str) -> None:
    resolved = _resolved(lab)
    write_isolation(tmp_path, _record(lab, resolved.backend))
    run, _ = dispatch.execute_connect(
        resolved=resolved, state_dir=tmp_path, bus=EventBus(),
    )
    assert run is not None and run.status == "succeeded"
    assert read_isolation(tmp_path, lab) is None


def test_connect_failed_run_keeps_record(tmp_path: Path, calls: Calls) -> None:
    write_isolation(tmp_path, _record(LIBVIRT_LAB, "local-libvirt"))
    calls.status = "failed"
    dispatch.execute_connect(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path, bus=EventBus(),
    )
    assert read_isolation(tmp_path, LIBVIRT_LAB) is not None


def test_connect_on_never_isolated_lab_succeeds(tmp_path: Path, calls: Calls) -> None:
    run, _ = dispatch.execute_connect(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path, bus=EventBus(),
    )
    assert run is not None and run.status == "succeeded"


@pytest.mark.parametrize("verb", ["isolate", "connect"])
def test_vbox_not_supported(tmp_path: Path, calls: Calls, verb: str) -> None:
    fn = getattr(dispatch, f"execute_{verb}")
    run, diags = fn(resolved=_resolved(VBOX_LAB), state_dir=tmp_path, bus=EventBus())
    assert run is None
    assert [d.id for d in diags] == ["runtime.backend.verb_not_supported"]
    assert "only cloud backends" not in diags[0].message
    assert verb in diags[0].message
    assert calls.verbs() == []


def test_verb_not_supported_suspend_message_unchanged() -> None:
    diag = dispatch.verb_not_supported_diagnostic("suspend", "local-libvirt")
    assert "only cloud backends do" in diag.message


# -- apply / resume guard -----------------------------------------------------


@pytest.mark.parametrize("lab", [LIBVIRT_LAB, DO_LAB, VBOX_LAB])
def test_apply_refused_when_isolated(tmp_path: Path, calls: Calls, lab: str) -> None:
    resolved = _resolved(lab)
    write_isolation(tmp_path, _record(lab, resolved.backend))
    run, diags = dispatch.execute_apply(
        resolved=resolved, state_dir=tmp_path, tofu_dir=Path("t"),
        ansible_dir=Path("a"), config_dir=CONFIG_DIR, bus=EventBus(),
    )
    assert run is None
    assert [d.id for d in diags] == ["runtime.apply.lab_isolated"]
    assert diags[0].severity == "error"
    assert f"playground connect {lab}" in (diags[0].suggestion or "")
    assert calls.verbs() == []


def test_resume_refused_when_isolated(tmp_path: Path, calls: Calls) -> None:
    write_isolation(tmp_path, _record(DO_LAB, "cloud-digitalocean"))
    run, diags = dispatch.execute_resume(
        resolved=_resolved(DO_LAB), state_dir=tmp_path, tofu_dir=Path("t"),
        ansible_dir=Path("a"), config_dir=CONFIG_DIR, bus=EventBus(),
    )
    assert run is None
    assert [d.id for d in diags] == ["runtime.apply.lab_isolated"]
    assert calls.verbs() == []


def test_apply_not_refused_for_other_lab_record(tmp_path: Path, calls: Calls) -> None:
    write_isolation(tmp_path, _record("some-other-lab", "local-libvirt"))
    run, _ = dispatch.execute_apply(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path, tofu_dir=Path("t"),
        ansible_dir=Path("a"), config_dir=CONFIG_DIR, bus=EventBus(),
    )
    assert run is not None
    assert calls.verbs() == [("libvirt", "apply")]


def test_apply_after_connect_proceeds(tmp_path: Path, calls: Calls) -> None:
    resolved = _resolved(LIBVIRT_LAB)
    write_isolation(tmp_path, _record(LIBVIRT_LAB, "local-libvirt"))
    dispatch.execute_connect(resolved=resolved, state_dir=tmp_path, bus=EventBus())
    run, _ = dispatch.execute_apply(
        resolved=resolved, state_dir=tmp_path, tofu_dir=Path("t"),
        ansible_dir=Path("a"), config_dir=CONFIG_DIR, bus=EventBus(),
    )
    assert run is not None


# -- record clearing on teardown ---------------------------------------------


@pytest.mark.parametrize("verb", ["destroy", "reset"])
def test_libvirt_teardown_clears_record_and_removes_filter(
    tmp_path: Path, calls: Calls, verb: str,
) -> None:
    write_isolation(tmp_path, _record(LIBVIRT_LAB, "local-libvirt"))
    fn = getattr(dispatch, f"execute_{verb}")
    run, diags = fn(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path,
        tofu_dir=Path("t"), bus=EventBus(),
    )
    assert run is not None and run.status == "succeeded"
    assert read_isolation(tmp_path, LIBVIRT_LAB) is None
    assert ("libvirt", "remove_filter") in calls.verbs()
    assert [d.id for d in diags] == ["w.filter"]


@pytest.mark.parametrize("verb", ["destroy", "reset"])
def test_libvirt_failed_teardown_keeps_record_and_filter(
    tmp_path: Path, calls: Calls, verb: str,
) -> None:
    write_isolation(tmp_path, _record(LIBVIRT_LAB, "local-libvirt"))
    calls.status = "failed"
    fn = getattr(dispatch, f"execute_{verb}")
    run, diags = fn(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path,
        tofu_dir=Path("t"), bus=EventBus(),
    )
    assert run is not None and run.status == "failed"
    assert read_isolation(tmp_path, LIBVIRT_LAB) is not None
    assert ("libvirt", "remove_filter") not in calls.verbs()
    assert diags == []


@pytest.mark.parametrize("verb", ["destroy", "reset", "suspend"])
def test_do_teardown_clears_record_without_libvirt_filter(
    tmp_path: Path, calls: Calls, verb: str,
) -> None:
    write_isolation(tmp_path, _record(DO_LAB, "cloud-digitalocean"))
    fn = getattr(dispatch, f"execute_{verb}")
    run, _ = fn(
        resolved=_resolved(DO_LAB), state_dir=tmp_path,
        tofu_dir=Path("t"), bus=EventBus(),
    )
    assert run is not None and run.status == "succeeded"
    assert read_isolation(tmp_path, DO_LAB) is None
    assert ("libvirt", "remove_filter") not in calls.verbs()


def test_filter_cleanup_exception_becomes_warning(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(lab: str) -> list[Diagnostic]:
        raise NotImplementedError

    monkeypatch.setattr(local_libvirt, "remove_isolation_filter", boom)
    write_isolation(tmp_path, _record(LIBVIRT_LAB, "local-libvirt"))
    run, diags = dispatch.execute_destroy(
        resolved=_resolved(LIBVIRT_LAB), state_dir=tmp_path,
        tofu_dir=Path("t"), bus=EventBus(),
    )
    assert run is not None and run.status == "succeeded"
    assert [d.severity for d in diags] == ["warning"]
