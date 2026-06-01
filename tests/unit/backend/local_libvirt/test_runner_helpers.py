"""Unit tests for pure helpers in the local-libvirt runner.

The big ``execute_apply`` integration smoke lives in the live-infra
path; this file targets the small standalone helpers that are easier
to exercise on their own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.backend.local_libvirt import runner as runner_module
from playground.backend.local_libvirt.runner import _wait_timeout_kwargs
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus
from playground.runs import StepResult

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


def _resolved_with_libvirt_overrides(overrides: dict) -> object:
    loaded, _ = load_config(CONFIG_DIR)
    resolved = resolve_lab(loaded, "generic-infra")
    new_providers = {**resolved.providers}
    new_providers["local-libvirt"] = {
        **new_providers.get("local-libvirt", {}), **overrides,
    }
    return resolved.model_copy(update={"providers": new_providers})


def test_wait_timeout_kwargs_empty_when_no_overrides() -> None:
    """Default path: tofu defaults flow through wait.py's
    DEFAULT_SSH_TIMEOUT_SECONDS / DEFAULT_CLOUD_INIT_TIMEOUT_SECONDS."""
    resolved = _resolved_with_libvirt_overrides({})
    assert _wait_timeout_kwargs(resolved) == {}


def test_wait_timeout_kwargs_picks_up_ssh_override() -> None:
    """TCG (`domain_type: qemu`) boots are slow — operators raise the
    SSH gate so apply doesn't fail at wait-for-vms-ready."""
    resolved = _resolved_with_libvirt_overrides(
        {"wait_ssh_timeout_seconds": 1800},
    )
    assert _wait_timeout_kwargs(resolved) == {"ssh_timeout": 1800.0}


def test_wait_timeout_kwargs_picks_up_cloud_init_override() -> None:
    resolved = _resolved_with_libvirt_overrides(
        {"wait_cloud_init_timeout_seconds": 2400.0},
    )
    assert _wait_timeout_kwargs(resolved) == {"cloud_init_timeout": 2400.0}


def test_wait_timeout_kwargs_picks_up_both() -> None:
    resolved = _resolved_with_libvirt_overrides({
        "wait_ssh_timeout_seconds": 1800,
        "wait_cloud_init_timeout_seconds": 2400,
    })
    assert _wait_timeout_kwargs(resolved) == {
        "ssh_timeout": 1800.0,
        "cloud_init_timeout": 2400.0,
    }


def test_wait_timeout_kwargs_rejects_negative() -> None:
    resolved = _resolved_with_libvirt_overrides(
        {"wait_ssh_timeout_seconds": -1},
    )
    with pytest.raises(ValueError, match="wait_ssh_timeout_seconds"):
        _wait_timeout_kwargs(resolved)


def test_wait_timeout_kwargs_rejects_zero() -> None:
    # Zero is non-sensical too — a non-positive deadline would make the
    # wait loop return immediately as a false timeout.
    resolved = _resolved_with_libvirt_overrides(
        {"wait_cloud_init_timeout_seconds": 0},
    )
    with pytest.raises(ValueError, match="wait_cloud_init_timeout_seconds"):
        _wait_timeout_kwargs(resolved)


def test_wait_timeout_kwargs_rejects_non_numeric() -> None:
    resolved = _resolved_with_libvirt_overrides(
        {"wait_ssh_timeout_seconds": "1800"},
    )
    with pytest.raises(ValueError, match="wait_ssh_timeout_seconds"):
        _wait_timeout_kwargs(resolved)


def test_wait_timeout_kwargs_rejects_boolean() -> None:
    # `bool` is a subclass of `int` in Python — guard explicitly so
    # `wait_ssh_timeout_seconds: true` (a YAML mistype) doesn't pass.
    resolved = _resolved_with_libvirt_overrides(
        {"wait_ssh_timeout_seconds": True},
    )
    with pytest.raises(ValueError, match="wait_ssh_timeout_seconds"):
        _wait_timeout_kwargs(resolved)


# ---------------------------------------------------------------------------
# BUG-3b: tofu apply failure summary warns about orphaned/running domains
# ---------------------------------------------------------------------------


def _make_step(name: str, exit_code: int, log_path: Path) -> StepResult:
    """Build a minimal StepResult for shimming."""
    from datetime import UTC, datetime
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch()
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    return StepResult(
        name=name,
        command=[name, "--shimmed"],
        exit_code=exit_code,
        log_path=str(log_path),
        started_at=now,
        finished_at=now,
    )


def test_tofu_apply_failure_summary_mentions_orphaned_domains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-3b: when tofu apply fails without crashing domains, the run
    summary must warn that partially-created domains may still be running
    and direct the operator to use reset/destroy.

    Scope: shims schedule_workloads, stage_workload_files, render_tfvars,
    run_tofu_apply, and check_domains_running in the runner's module
    namespace.  The real run lifecycle (start_run / finish_run / EventBus)
    is exercised with a real tmp_path.
    """
    loaded, _ = load_config(CONFIG_DIR)
    resolved = resolve_lab(loaded, "generic-infra")
    bus = EventBus()

    MOD = "playground.backend.local_libvirt.runner"

    # Shim: schedule/stage succeed with empty workloads.
    monkeypatch.setattr(
        f"{MOD}.schedule_workloads",
        lambda resolved: ([], []),
    )
    monkeypatch.setattr(
        f"{MOD}.stage_workload_files",
        lambda scheduled, source_base, stage_dir: ([], []),
    )
    monkeypatch.setattr(
        f"{MOD}.render_tfvars",
        lambda resolved: {},
    )

    # Shim: tofu apply returns non-zero (no crash diagnostics).
    def fake_tofu_apply(tofu_dir, var_file, log_path, *, bus, run_id):
        step = _make_step("tofu-apply", 1, Path(log_path))
        return step, []

    monkeypatch.setattr(f"{MOD}.run_tofu_apply", fake_tofu_apply)

    # Shim: no domain-crash diagnostics (generic apply failure, not VMX).
    monkeypatch.setattr(
        f"{MOD}.check_domains_running",
        lambda vm_names, lab: [],
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    run, diags = runner_module.execute_apply(
        resolved=resolved,
        state_dir=state_dir,
        tofu_dir=tmp_path / "tofu",
        ansible_dir=tmp_path / "ansible",
        config_dir=tmp_path / "config" / "labs",
        bus=bus,
    )

    assert run is not None
    assert run.status == "failed"
    summary = run.summary or ""
    # The summary must NOT claim "no VMs provisioned" (the old misleading text).
    assert "no VMs provisioned" not in summary, (
        f"Old misleading message still present: {summary!r}"
    )
    # The summary must warn about orphaned/running domains.
    assert any(
        kw in summary.lower()
        for kw in ("orphan", "running", "may have been created", "partially")
    ), f"Summary does not mention orphaned/running domains: {summary!r}"
    # The summary must tell the operator how to clean up.
    assert "reset" in summary or "destroy" in summary, (
        f"Summary does not mention reset/destroy: {summary!r}"
    )
