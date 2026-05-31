"""End-to-end runner tests for the cloud-digitalocean backend.

All external I/O is shimmed — no real tofu, SSH, or httpx calls.
Monkeypatches target the names imported INTO the runner's module
namespace (e.g. `playground.backend.cloud_digitalocean.runner.<name>`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from playground.backend.cloud_digitalocean.runner import (
    execute_apply,
    execute_destroy,
    execute_reset,
    execute_resume,
    execute_suspend,
)
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus
from playground.runs import StepResult

# ---------------------------------------------------------------------------
# Repo-level constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"
TOFU_DO_DIR = REPO_ROOT / "tofu"
ANSIBLE_DIR = REPO_ROOT / "ansible"

FAKE_TOKEN = "dop_v1_faketoken0000000000000000000000000000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _ok_step(name: str, log_path: Path) -> StepResult:
    """Build a successful StepResult for shimmed steps."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch()
    now = _now()
    return StepResult(
        name=name,
        command=[name, "--shimmed"],
        exit_code=0,
        log_path=str(log_path),
        started_at=now,
        finished_at=now,
    )


def _fail_step(name: str, log_path: Path) -> StepResult:
    """Build a failed StepResult for shimmed steps."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch()
    now = _now()
    return StepResult(
        name=name,
        command=[name, "--shimmed"],
        exit_code=1,
        log_path=str(log_path),
        started_at=now,
        finished_at=now,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def resolved_cloud_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Load + resolve the committed cloud-smoke lab with a fake SSH key."""
    # Provide a fake SSH public key file so _read_ssh_public_key succeeds.
    fake_key_path = tmp_path / "id_rsa.pub"
    fake_key_path.write_text("ssh-rsa AAAA fakekey\n")

    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    resolved = resolve_lab(loaded, "cloud-smoke")
    # Override the provider ssh_public_key_path to our temp file.
    providers = {
        resolved.backend: {
            **dict(resolved.providers.get(resolved.backend, {})),
            "ssh_public_key_path": str(fake_key_path),
        }
    }
    return resolved.model_copy(update={"providers": providers})


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    """Minimal fake tofu/cloud_digitalocean source tree."""
    src = tmp_path / "tofu" / "cloud_digitalocean"
    src.mkdir(parents=True)
    (src / "main.tf").write_text('terraform { required_version = ">= 1.6" }\n')
    (src / "variables.tf").write_text("variable \"name_prefix\" {}\n")
    (src / "cloud_init.cfg").write_text("#cloud-config\nhostname: test\n")
    return tmp_path / "tofu"


@pytest.fixture
def ansible_dir(tmp_path: Path) -> Path:
    adir = tmp_path / "ansible"
    adir.mkdir()
    (adir / "site.yml").write_text("---\n- hosts: playground\n  gather_facts: false\n")
    return adir


def _install_shims(
    monkeypatch: pytest.MonkeyPatch,
    *,
    init_ok: bool = True,
    apply_ok: bool = True,
    destroy_ok: bool = True,
    wait_ok: bool = True,
    ansible_ok: bool = True,
    vm_ips: dict[str, str] | None = None,
    list_droplets_return: list[dict[str, Any]] | None = None,
    list_droplets_survivors: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Install shimmed versions of all runner-imported callables.

    Returns a list collecting the step names in call order.
    """
    if vm_ips is None:
        vm_ips = {"node1": "203.0.113.10"}
    if list_droplets_return is None:
        list_droplets_return = []
    # survivors is what the *second* call returns (post-delete)
    _call_count: dict[str, int] = {"list": 0}
    steps_called: list[str] = []

    MOD = "playground.backend.cloud_digitalocean.runner"

    def fake_run_tofu_init(tofu_dir, log_path, *, bus, run_id):
        steps_called.append("tofu-init")
        if init_ok:
            step = _ok_step("tofu-init", Path(log_path))
        else:
            step = _fail_step("tofu-init", Path(log_path))
        return step, []

    def fake_run_tofu_apply(tofu_dir, var_file, log_path, *, bus, run_id):
        steps_called.append("tofu-apply")
        if apply_ok:
            step = _ok_step("tofu-apply", Path(log_path))
        else:
            step = _fail_step("tofu-apply", Path(log_path))
        return step, []

    def fake_run_tofu_destroy(tofu_dir, var_file, log_path, *, bus, run_id):
        steps_called.append("tofu-destroy")
        if destroy_ok:
            step = _ok_step("tofu-destroy", Path(log_path))
        else:
            step = _fail_step("tofu-destroy", Path(log_path))
        return step, []

    def fake_fetch_vm_ips(tofu_dir):
        return vm_ips, []

    def fake_render_inventory(resolved, vips, *, staged_workloads, ssh_ports):
        body = "[playground]\n"
        for name, ip in vips.items():
            body += f"{name} ansible_host={ip}\n"
        return body, []

    def fake_wait_for_vms_ready(*, targets, log_path, bus, run_id):
        steps_called.append("wait-for-vms-ready")
        if wait_ok:
            step = _ok_step("wait-for-vms-ready", Path(log_path))
        else:
            step = _fail_step("wait-for-vms-ready", Path(log_path))
        return step, []

    def fake_run_ansible_playbook(
        playbook, inventory, log_path, *, cwd, bus, run_id, ansible_cfg
    ):
        steps_called.append("ansible-playbook")
        if ansible_ok:
            step = _ok_step("ansible-playbook", Path(log_path))
        else:
            step = _fail_step("ansible-playbook", Path(log_path))
        return step, []

    def fake_verify_lab(resolved, vm_ips, log_path, *, bus, run_id, ssh_ports):
        steps_called.append("verify-lab")
        return _ok_step("verify-lab", Path(log_path)), []

    def fake_list_droplets_by_tag(token, tag):
        _call_count["list"] += 1
        if _call_count["list"] == 1:
            return list(list_droplets_return), []
        # Second call (survivors check)
        if list_droplets_survivors is not None:
            return list(list_droplets_survivors), []
        return [], []

    def fake_schedule_workloads(resolved):
        return {vm.name: [] for vm in resolved.vms}, []

    def fake_stage_workload_files(scheduled, *, source_base, stage_dir):
        return {}, []

    monkeypatch.setattr(f"{MOD}.run_tofu_init", fake_run_tofu_init)
    monkeypatch.setattr(f"{MOD}.run_tofu_apply", fake_run_tofu_apply)
    monkeypatch.setattr(f"{MOD}.run_tofu_destroy", fake_run_tofu_destroy)
    monkeypatch.setattr(f"{MOD}.fetch_vm_ips", fake_fetch_vm_ips)
    monkeypatch.setattr(f"{MOD}.render_inventory", fake_render_inventory)
    monkeypatch.setattr(f"{MOD}.wait_for_vms_ready", fake_wait_for_vms_ready)
    monkeypatch.setattr(f"{MOD}.run_ansible_playbook", fake_run_ansible_playbook)
    monkeypatch.setattr(f"{MOD}.verify_lab", fake_verify_lab)
    monkeypatch.setattr(f"{MOD}.list_droplets_by_tag", fake_list_droplets_by_tag)
    monkeypatch.setattr(f"{MOD}.schedule_workloads", fake_schedule_workloads)
    monkeypatch.setattr(f"{MOD}.stage_workload_files", fake_stage_workload_files)

    return steps_called


# ===========================================================================
# 1. apply happy path
# ===========================================================================


def test_apply_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_cloud_smoke,
    source_root: Path,
    ansible_dir: Path,
) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    steps_called = _install_shims(monkeypatch, vm_ips={"node1": "203.0.113.10"})

    state_dir = tmp_path / ".playground"
    bus = EventBus()

    run, diags = execute_apply(
        resolved=resolved_cloud_smoke,
        state_dir=state_dir,
        tofu_dir=source_root,
        ansible_dir=ansible_dir,
        config_dir=CONFIG_DIR,
        bus=bus,
    )

    # --- operation result ---
    assert run is not None
    assert run.status == "succeeded"
    assert run.operation == "apply"

    # --- step ordering ---
    assert "tofu-init" in steps_called
    assert "tofu-apply" in steps_called
    assert "wait-for-vms-ready" in steps_called
    assert "ansible-playbook" in steps_called
    assert "verify-lab" in steps_called
    step_names = [s.name for s in run.steps]
    assert step_names.index("tofu-init") < step_names.index("tofu-apply")
    assert step_names.index("tofu-apply") < step_names.index("wait-for-vms-ready")
    assert step_names.index("wait-for-vms-ready") < step_names.index("ansible-playbook")
    assert step_names.index("ansible-playbook") < step_names.index("verify-lab")

    # --- per-lab dir got .tf files + cloud_init.cfg ---
    lab = resolved_cloud_smoke.lab_name
    per_lab_dir = state_dir / "state" / "cloud-digitalocean" / lab
    assert per_lab_dir.is_dir()
    assert (per_lab_dir / "main.tf").exists()
    assert (per_lab_dir / "cloud_init.cfg").exists()

    # --- tfvars file written without token ---
    tfvars_path = per_lab_dir / f"{lab}.tfvars.json"
    assert tfvars_path.exists()
    tfvars_data = json.loads(tfvars_path.read_text())
    assert isinstance(tfvars_data, dict)
    # No token in keys or string values
    content_lower = tfvars_path.read_text().lower()
    assert "token" not in content_lower, f"Unexpected 'token' in tfvars: {content_lower!r}"

    # --- inventory written ---
    inventory_path = state_dir / "state" / "inventory" / f"{lab}.ini"
    assert inventory_path.exists()
    assert "203.0.113.10" in inventory_path.read_text()

    # --- run record at state_dir/runs/<run_id>/run.json ---
    run_json_path = state_dir / "runs" / run.run_id / "run.json"
    assert run_json_path.exists()
    run_record = json.loads(run_json_path.read_text())
    assert run_record["status"] == "succeeded"
    assert run_record["operation"] == "apply"

    # --- no token in run record ---
    run_json_text = run_json_path.read_text().lower()
    assert FAKE_TOKEN.lower() not in run_json_text
    assert "dop_v1" not in run_json_text


# ===========================================================================
# 2. apply fails at tofu-apply
# ===========================================================================


def test_apply_fails_at_tofu_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_cloud_smoke,
    source_root: Path,
    ansible_dir: Path,
) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    steps_called = _install_shims(monkeypatch, apply_ok=False)

    state_dir = tmp_path / ".playground"
    bus = EventBus()

    run, diags = execute_apply(
        resolved=resolved_cloud_smoke,
        state_dir=state_dir,
        tofu_dir=source_root,
        ansible_dir=ansible_dir,
        config_dir=CONFIG_DIR,
        bus=bus,
    )

    assert run is not None
    assert run.status == "failed"

    # The failure summary must mention Droplets may have been created
    # and how to clean up.
    assert run.summary is not None
    assert "Droplets may have been created" in run.summary or "tofu-apply failed" in run.summary

    # Later steps did NOT run
    assert "wait-for-vms-ready" not in steps_called
    assert "ansible-playbook" not in steps_called
    assert "verify-lab" not in steps_called

    # tofu-apply step is present and marked failed
    step_map = {s.name: s for s in run.steps}
    assert "tofu-apply" in step_map
    assert step_map["tofu-apply"].exit_code != 0


# ===========================================================================
# 3. resume emits disk-changes warning before mutating
# ===========================================================================


def test_resume_emits_disk_changes_warning_and_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_cloud_smoke,
    source_root: Path,
    ansible_dir: Path,
) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    _install_shims(monkeypatch, vm_ips={"node1": "203.0.113.10"})

    state_dir = tmp_path / ".playground"
    events_received: list[Any] = []
    bus = EventBus()
    bus.subscribe(events_received.append)

    run, diags = execute_resume(
        resolved=resolved_cloud_smoke,
        state_dir=state_dir,
        tofu_dir=source_root,
        ansible_dir=ansible_dir,
        config_dir=CONFIG_DIR,
        bus=bus,
    )

    assert run is not None
    assert run.status == "succeeded"
    assert run.operation == "resume"

    # The warning about disk changes must have been published BEFORE any
    # mutating step (tofu-init is the first mutation).
    log_line_events = [
        e for e in events_received
        if e.type == "log_line" and "disk changes" in e.payload.get("line", "").lower()
    ]
    assert len(log_line_events) >= 1, (
        "Expected a 'disk changes' warning event during resume; "
        f"got events: {[e.payload for e in events_received if e.type == 'log_line']}"
    )

    # The warning must appear before the first tofu-init step_started event.
    tofu_init_events = [
        e for e in events_received
        if e.type == "step_started" and e.payload.get("step") == "tofu-init"
    ]
    assert tofu_init_events, "Expected step_started for tofu-init"
    first_tofu_init_idx = events_received.index(tofu_init_events[0])
    warning_idx = events_received.index(log_line_events[0])
    assert warning_idx < first_tofu_init_idx, (
        "disk-changes warning must be emitted BEFORE the first tofu-init step"
    )

    # events.jsonl should also contain the warning (written by JsonlWriter)
    run_dir = state_dir / "runs" / run.run_id
    events_jsonl = run_dir / "events.jsonl"
    assert events_jsonl.exists()
    events_text = events_jsonl.read_text()
    assert "disk changes" in events_text.lower() or "not preserved" in events_text.lower()


# ===========================================================================
# 4. destroy happy path
# ===========================================================================


def test_destroy_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_cloud_smoke,
    source_root: Path,
) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    steps_called = _install_shims(
        monkeypatch, list_droplets_return=[], list_droplets_survivors=[]
    )

    state_dir = tmp_path / ".playground"
    lab = resolved_cloud_smoke.lab_name
    # Simulate a prior apply: create per-lab dir so tofu-destroy doesn't no-op
    per_lab_dir = state_dir / "state" / "cloud-digitalocean" / lab
    per_lab_dir.mkdir(parents=True)
    (per_lab_dir / "main.tf").write_text("terraform {}")

    bus = EventBus()
    run, diags = execute_destroy(
        resolved=resolved_cloud_smoke,
        state_dir=state_dir,
        tofu_dir=source_root,
        bus=bus,
    )

    assert run is not None
    assert run.status == "succeeded"
    assert run.operation == "destroy"
    assert "tofu-destroy" in steps_called

    # Per-lab state files are NOT deleted by destroy (preserve for resume)
    assert per_lab_dir.exists(), "destroy must NOT delete per-lab state dir"

    # Run record saved
    run_json = state_dir / "runs" / run.run_id / "run.json"
    assert run_json.exists()
    record = json.loads(run_json.read_text())
    assert record["operation"] == "destroy"
    assert record["status"] == "succeeded"

    # No token in run record
    assert FAKE_TOKEN not in run_json.read_text()


# ===========================================================================
# 5. suspend with surviving compute → must fail
# ===========================================================================


def test_suspend_with_surviving_droplet_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_cloud_smoke,
    source_root: Path,
) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)

    survivor = {"id": 12345, "name": "cloud-smoke-node1", "status": "active", "networks": {}}

    _install_shims(
        monkeypatch,
        list_droplets_return=[survivor],   # first pass: droplet to delete
        list_droplets_survivors=[survivor],  # second pass: still alive
    )

    state_dir = tmp_path / ".playground"
    lab = resolved_cloud_smoke.lab_name
    per_lab_dir = state_dir / "state" / "cloud-digitalocean" / lab
    per_lab_dir.mkdir(parents=True)
    (per_lab_dir / "main.tf").write_text("terraform {}")

    events_received: list[Any] = []
    bus = EventBus()
    bus.subscribe(events_received.append)

    run, diags = execute_suspend(
        resolved=resolved_cloud_smoke,
        state_dir=state_dir,
        tofu_dir=source_root,
        bus=bus,
    )

    assert run is not None
    assert run.status == "failed", (
        "suspend must not report success while tagged compute survives"
    )

    # Must have an orphaned-resource diagnostic
    orphan_ids = [d.id for d in diags if "orphaned_resource" in d.id]
    assert len(orphan_ids) >= 1, (
        f"Expected orphaned_resource diagnostic; got: {[d.id for d in diags]}"
    )

    # The diagnostic must reference the Droplet
    orphan_diags = [d for d in diags if "orphaned_resource" in d.id]
    combined_messages = " ".join(d.message for d in orphan_diags)
    assert "cloud-smoke-node1" in combined_messages or "12345" in str(combined_messages)

    # Console URL must appear in suggestion
    for d in orphan_diags:
        assert "digitalocean.com" in (d.suggestion or ""), (
            f"Suggestion should include console URL; got: {d.suggestion!r}"
        )

    # The "powered-off Droplets still bill" warning must have been emitted
    # before teardown steps ran.
    billing_warnings = [
        e for e in events_received
        if e.type == "log_line" and "still bill" in e.payload.get("line", "").lower()
    ]
    log_lines_seen = [
        e.payload.get("line")
        for e in events_received
        if e.type == "log_line"
    ]
    assert len(billing_warnings) >= 1, (
        "Expected powered-off billing warning before teardown; "
        f"log_line events: {log_lines_seen}"
    )

    # Warning must appear before first tofu-destroy step_started
    destroy_events = [
        e for e in events_received
        if e.type == "step_started" and e.payload.get("step") == "tofu-destroy"
    ]
    assert destroy_events
    destroy_idx = events_received.index(destroy_events[0])
    warning_idx = events_received.index(billing_warnings[0])
    assert warning_idx < destroy_idx, "billing warning must precede tofu-destroy step"

    # No token in diagnostics
    for d in diags:
        assert FAKE_TOKEN not in (d.message or "")
        assert FAKE_TOKEN not in (d.suggestion or "")


# ===========================================================================
# 6. suspend idempotent (no per-lab dir, no droplets) → succeeded
# ===========================================================================


def test_suspend_idempotent_no_prior_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_cloud_smoke,
    source_root: Path,
) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    _install_shims(monkeypatch, list_droplets_return=[], list_droplets_survivors=[])

    state_dir = tmp_path / ".playground"
    # Do NOT create the per-lab dir — simulates "never applied"
    bus = EventBus()

    run, diags = execute_suspend(
        resolved=resolved_cloud_smoke,
        state_dir=state_dir,
        tofu_dir=source_root,
        bus=bus,
    )

    assert run is not None
    assert run.status == "succeeded"

    # No orphaned-resource errors
    orphan_diags = [d for d in diags if "orphaned_resource" in d.id]
    assert orphan_diags == []


# ===========================================================================
# 7. reset removes per-lab dir + inventory, preserves run logs
# ===========================================================================


def test_reset_cleans_state_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_cloud_smoke,
    source_root: Path,
) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    _install_shims(monkeypatch, list_droplets_return=[], list_droplets_survivors=[])

    state_dir = tmp_path / ".playground"
    lab = resolved_cloud_smoke.lab_name

    # Pre-create state that should be cleaned
    per_lab_dir = state_dir / "state" / "cloud-digitalocean" / lab
    per_lab_dir.mkdir(parents=True)
    (per_lab_dir / "main.tf").write_text("terraform {}")
    (per_lab_dir / "terraform.tfstate").write_text('{"version":4}')

    inventory_path = state_dir / "state" / "inventory" / f"{lab}.ini"
    inventory_path.parent.mkdir(parents=True)
    inventory_path.write_text("[playground]\nnode1 ansible_host=1.2.3.4\n")

    bus = EventBus()
    run, diags = execute_reset(
        resolved=resolved_cloud_smoke,
        state_dir=state_dir,
        tofu_dir=source_root,
        bus=bus,
    )

    assert run is not None
    assert run.status == "succeeded"

    # The clean-state-files step must have run
    step_names = [s.name for s in run.steps]
    assert "clean-state-files" in step_names

    # Per-lab dir and inventory must be removed
    assert not per_lab_dir.exists(), "per-lab dir must be removed by reset"
    assert not inventory_path.exists(), "inventory must be removed by reset"

    # Run logs must still be present
    run_json = state_dir / "runs" / run.run_id / "run.json"
    assert run_json.exists(), "run.json must be preserved after reset"


# ===========================================================================
# No token in any run record / event / diagnostic
# ===========================================================================


def test_no_token_in_run_record_or_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_cloud_smoke,
    source_root: Path,
    ansible_dir: Path,
) -> None:
    """Belt-and-braces: the token must never appear in run.json or events.jsonl."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    _install_shims(monkeypatch, vm_ips={"node1": "203.0.113.10"})

    state_dir = tmp_path / ".playground"
    bus = EventBus()

    run, _ = execute_apply(
        resolved=resolved_cloud_smoke,
        state_dir=state_dir,
        tofu_dir=source_root,
        ansible_dir=ansible_dir,
        config_dir=CONFIG_DIR,
        bus=bus,
    )
    assert run is not None

    run_dir = state_dir / "runs" / run.run_id
    for fpath in run_dir.rglob("*"):
        if fpath.is_file():
            text = fpath.read_text(errors="replace")
            assert FAKE_TOKEN not in text, (
                f"Token value leaked into {fpath.relative_to(state_dir)}"
            )
