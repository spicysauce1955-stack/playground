"""isolate / connect for cloud-digitalocean: all external I/O is faked."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from playground.backend.cloud_digitalocean import isolation as iso
from playground.backend.cloud_digitalocean.isolation import (
    execute_connect,
    execute_isolate,
    outbound_is_open,
    outbound_is_vpc_only,
)
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus
from playground.runs import StepResult

CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"
FAKE_TOKEN = "dop_v1_faketoken0000000000000000000000000000"
CIDR = "10.60.0.0/24"
LAB = "cloud-smoke"


def _fw(isolated: bool, cidr: str = CIDR) -> dict[str, Any]:
    dests = [cidr] if isolated else ["0.0.0.0/0", "::/0"]
    return {
        "name": f"{LAB}-fw",
        "outbound_rules": [
            {"protocol": p, "ports": "0" if p == "icmp" else "all",
             "destinations": {"addresses": list(dests)}}
            for p in ("tcp", "udp", "icmp")
        ],
    }


def _step(name: str, log_path: Path, code: int = 0) -> StepResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch()
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    return StepResult(name=name, command=[name], exit_code=code,
                      log_path=str(log_path), started_at=now, finished_at=now)


@pytest.fixture
def resolved():
    loaded, diags = load_config(CONFIG_DIR)
    assert diags == []
    return resolve_lab(loaded, LAB)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """A provisioned lab: per-lab dir with tfvars, state, variables.tf, .terraform."""
    d = tmp_path / ".playground"
    per_lab = d / "state" / "cloud-digitalocean" / LAB
    (per_lab / ".terraform").mkdir(parents=True)
    (per_lab / "variables.tf").write_text('variable "egress_isolated" {}\n')
    (per_lab / f"{LAB}.tfvars.json").write_text(json.dumps({
        "name_prefix": LAB, "vpc_cidr": CIDR, "egress_isolated": False,
    }))
    (per_lab / "terraform.tfstate").write_text(json.dumps({
        "resources": [{"type": "digitalocean_firewall", "name": "lab"}],
    }))
    return d


def _tfvars(state_dir: Path) -> dict[str, Any]:
    return json.loads(
        (state_dir / "state" / "cloud-digitalocean" / LAB / f"{LAB}.tfvars.json").read_text()
    )


class Fakes:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.apply_ok = True
        self.init_ok = True
        self.api: list[dict[str, Any] | None] | None = None  # firewall per call
        self.api_ok = True
        self.var_at_apply: bool | None = None
        self.var_file: Path | None = None
        self.droplets: list[dict[str, Any]] = [
            {"id": 111, "name": f"{LAB}-node1", "tags": ["playground", f"lab:{LAB}"]},
        ]
        self.firewalls: list[dict[str, Any]] = []
        self.listing_ok = True


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    f = Fakes()
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)

    def run_init(tofu_dir, log_path, *, bus, run_id):
        f.calls.append("init")
        return _step("tofu-init", Path(log_path), 0 if f.init_ok else 1), []

    def run_apply(tofu_dir, var_file, log_path, *, bus, run_id):
        f.calls.append("apply")
        f.var_file = Path(var_file)
        f.var_at_apply = json.loads(Path(var_file).read_text())["egress_isolated"]
        return _step("tofu-apply", Path(log_path), 0 if f.apply_ok else 1), []

    def find_firewall(token, name):
        f.calls.append("find_firewall")
        assert token == FAKE_TOKEN
        assert name == f"{LAB}-fw"
        if not f.api_ok:
            return None, [], False
        # Mimic the real thing: the firewall reflects what apply just set.
        return _fw(bool(f.var_at_apply)) if f.api is None else f.api[0], [], True

    def list_droplets(token, tag):
        assert tag == f"lab:{LAB}"
        return list(f.droplets), [], f.listing_ok

    def list_fws(token):
        return list(f.firewalls), [], f.listing_ok

    monkeypatch.setattr(iso, "list_droplets_by_tag", list_droplets)
    monkeypatch.setattr(iso, "list_firewalls", list_fws)
    monkeypatch.setattr(iso, "run_tofu_init", run_init)
    monkeypatch.setattr(iso, "run_tofu_apply", run_apply)
    monkeypatch.setattr(iso, "find_firewall", find_firewall)
    return f


def _run(fn, resolved, state_dir):
    return fn(resolved=resolved, state_dir=state_dir, bus=EventBus())


# ---- pure predicates -------------------------------------------------------


def test_predicates_on_open_and_isolated_firewalls() -> None:
    assert outbound_is_vpc_only(_fw(True), CIDR)
    assert not outbound_is_vpc_only(_fw(False), CIDR)
    assert outbound_is_open(_fw(False))
    assert not outbound_is_open(_fw(True))


def test_vpc_only_rejects_extra_destinations() -> None:
    fw = _fw(True)
    fw["outbound_rules"][0]["destinations"]["addresses"].append("1.1.1.1")
    assert not outbound_is_vpc_only(fw, CIDR)
    fw = _fw(True)
    fw["outbound_rules"][1]["destinations"]["tags"] = ["something"]
    assert not outbound_is_vpc_only(fw, CIDR)


def test_vpc_only_requires_all_three_protocols_and_the_exact_cidr() -> None:
    fw = _fw(True)
    fw["outbound_rules"].pop()
    assert not outbound_is_vpc_only(fw, CIDR)
    assert not outbound_is_vpc_only(_fw(True, "10.99.0.0/24"), CIDR)
    assert not outbound_is_vpc_only({"outbound_rules": []}, CIDR)


# ---- isolate ---------------------------------------------------------------


def test_isolate_flips_var_applies_and_verifies(resolved, state_dir, fakes) -> None:
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded"
    assert run.operation == "isolate"
    assert diags == []
    assert fakes.var_at_apply is True  # the file was flipped BEFORE apply
    assert _tfvars(state_dir)["egress_isolated"] is True
    assert fakes.calls == ["apply", "find_firewall"]  # .terraform exists: no init
    assert [s.name for s in run.steps] == [
        "preflight", "set-egress-isolated", "tofu-apply", "verify-firewall",
    ]
    assert str(CIDR) in run.summary


def test_isolate_preserves_every_other_tfvar(resolved, state_dir, fakes) -> None:
    execute_isolate(resolved=resolved, state_dir=state_dir, bus=EventBus())
    data = _tfvars(state_dir)
    assert data["name_prefix"] == LAB and data["vpc_cidr"] == CIDR


def test_isolate_is_idempotent(resolved, state_dir, fakes) -> None:
    assert _run(execute_isolate, resolved, state_dir)[0].status == "succeeded"
    run, _ = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded"
    assert _tfvars(state_dir)["egress_isolated"] is True


def test_connect_reopens_and_is_idempotent(resolved, state_dir, fakes) -> None:
    _run(execute_isolate, resolved, state_dir)
    run, diags = _run(execute_connect, resolved, state_dir)
    assert run.status == "succeeded" and run.operation == "connect"
    assert diags == []
    assert fakes.var_at_apply is False
    assert _tfvars(state_dir)["egress_isolated"] is False
    assert _run(execute_connect, resolved, state_dir)[0].status == "succeeded"


def test_connect_on_never_isolated_lab_succeeds(resolved, state_dir, fakes) -> None:
    run, _ = _run(execute_connect, resolved, state_dir)
    assert run.status == "succeeded"


def test_isolate_runs_init_when_terraform_dir_missing(resolved, state_dir, fakes) -> None:
    (state_dir / "state" / "cloud-digitalocean" / LAB / ".terraform").rmdir()
    run, _ = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded"
    assert fakes.calls[0] == "init"


def test_init_failure_fails_and_restores_tfvars(resolved, state_dir, fakes) -> None:
    (state_dir / "state" / "cloud-digitalocean" / LAB / ".terraform").rmdir()
    fakes.init_ok = False
    run, _ = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert "apply" not in fakes.calls
    assert _tfvars(state_dir)["egress_isolated"] is False


# ---- failure modes ---------------------------------------------------------


def test_isolate_without_state_is_not_provisioned(resolved, tmp_path, fakes) -> None:
    run, diags = _run(execute_isolate, resolved, tmp_path / ".playground")
    assert run.status == "failed"
    assert [d.id for d in diags] == ["runtime.isolate.not_provisioned"]
    assert fakes.calls == []


def test_isolate_after_destroy_does_not_create_a_lab(resolved, state_dir, fakes) -> None:
    """Destroy leaves tfvars + an empty tfstate behind; applying would rebuild."""
    per_lab = state_dir / "state" / "cloud-digitalocean" / LAB
    (per_lab / "terraform.tfstate").write_text(json.dumps({"resources": []}))
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.not_provisioned"
    assert "apply" not in fakes.calls
    assert _tfvars(state_dir)["egress_isolated"] is False


def test_connect_on_unprovisioned_lab_is_a_noop_success(resolved, tmp_path, fakes) -> None:
    run, diags = _run(execute_connect, resolved, tmp_path / ".playground")
    assert run.status == "succeeded"
    assert all(d.severity == "info" for d in diags)
    assert fakes.calls == []


def test_stale_per_lab_root_asks_for_reapply(resolved, state_dir, fakes) -> None:
    per_lab = state_dir / "state" / "cloud-digitalocean" / LAB
    (per_lab / "variables.tf").write_text('variable "region" {}\n')
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert [d.id for d in diags] == ["runtime.isolate.needs_reapply"]
    assert "apply" in diags[0].suggestion
    assert fakes.calls == []
    assert _tfvars(state_dir)["egress_isolated"] is False


def test_stale_root_without_vpc_cidr_in_tfvars_asks_for_reapply(
    resolved, state_dir, fakes,
) -> None:
    per_lab = state_dir / "state" / "cloud-digitalocean" / LAB
    (per_lab / f"{LAB}.tfvars.json").write_text(json.dumps({"name_prefix": LAB}))
    _, diags = _run(execute_isolate, resolved, state_dir)
    assert diags[0].id == "runtime.isolate.needs_reapply"


def test_missing_token_fails_without_touching_anything(
    resolved, state_dir, fakes, monkeypatch,
) -> None:
    monkeypatch.delenv("DIGITALOCEAN_TOKEN")
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.cloud.token_missing"
    assert FAKE_TOKEN not in diags[0].message
    assert fakes.calls == []


def test_unreadable_tfvars_fails(resolved, state_dir, fakes) -> None:
    per_lab = state_dir / "state" / "cloud-digitalocean" / LAB
    (per_lab / f"{LAB}.tfvars.json").write_text("{not json")
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.state_unreadable"


def test_apply_failure_fails_reverts_tfvars_and_skips_verify(
    resolved, state_dir, fakes,
) -> None:
    fakes.apply_ok = False
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.apply_failed"
    assert "find_firewall" not in fakes.calls
    assert _tfvars(state_dir)["egress_isolated"] is False


def test_verification_failure_when_firewall_still_open(resolved, state_dir, fakes) -> None:
    fakes.api = [_fw(False)]  # apply "succeeded" but the API still shows it open
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert [d.id for d in diags] == ["runtime.isolate.verify_failed"]


def test_verification_failure_when_connect_leaves_it_isolated(
    resolved, state_dir, fakes,
) -> None:
    fakes.api = [_fw(True)]
    run, diags = _run(execute_connect, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.connect.verify_failed"


def test_verification_is_inconclusive_when_api_unreachable(
    resolved, state_dir, fakes,
) -> None:
    fakes.api_ok = False
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"  # never claim isolation we could not confirm
    assert diags[0].id == "runtime.isolate.verify_failed"


def test_missing_firewall_fails_verification(resolved, state_dir, fakes) -> None:
    fakes.api = [None]
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.verify_failed"


# ---- hygiene ---------------------------------------------------------------


def test_token_never_reaches_run_record_or_logs(resolved, state_dir, fakes) -> None:
    run, diags = _run(execute_isolate, resolved, state_dir)
    for path in state_dir.rglob("*"):
        if path.is_file():
            assert FAKE_TOKEN not in path.read_text(errors="ignore"), path
    assert FAKE_TOKEN not in repr(diags) + run.model_dump_json()


def test_apply_uses_the_per_lab_var_file_as_an_absolute_path(
    resolved, state_dir, fakes,
) -> None:
    _run(execute_isolate, resolved, state_dir)
    assert fakes.var_file is not None and fakes.var_file.is_absolute()


# ---- foreign firewalls (DO unions allow rules across firewalls) -------------


def _foreign(name: str = "sibling-fw", *, isolated: bool = False, **target: Any) -> dict[str, Any]:
    fw = _fw(isolated)
    fw.update({"name": name, "id": f"id-{name}", "droplet_ids": [], "tags": []})
    fw.update(target)
    return fw


def test_sibling_lab_firewall_tagged_playground_defeats_isolate(resolved, state_dir, fakes) -> None:
    fakes.firewalls = [_foreign("sibling-fw", tags=["playground"])]
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert [d.id for d in diags] == ["runtime.isolate.foreign_firewall"]
    assert "sibling-fw" in diags[0].message and "id-sibling-fw" in diags[0].message
    assert diags[0].suggestion


def test_foreign_firewall_attached_by_droplet_id_fails(resolved, state_dir, fakes) -> None:
    fakes.firewalls = [_foreign("by-id", droplet_ids=[111])]
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.foreign_firewall"


def test_unrelated_firewall_is_ignored(resolved, state_dir, fakes) -> None:
    fakes.firewalls = [
        _foreign("elsewhere", droplet_ids=[999], tags=["other-project"]),
        _foreign("us", tags=["playground"]),  # own name is skipped below
    ]
    fakes.firewalls[1]["name"] = f"{LAB}-fw"
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded" and diags == []


def test_foreign_firewall_that_is_also_vpc_only_is_fine(resolved, state_dir, fakes) -> None:
    fakes.firewalls = [_foreign("narrow", isolated=True, tags=["playground"])]
    run, _ = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded"


def test_foreign_firewall_with_no_outbound_rules_is_fine(resolved, state_dir, fakes) -> None:
    fakes.firewalls = [_foreign("deny", tags=["playground"], outbound_rules=[])]
    run, _ = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded"


def test_foreign_check_inconclusive_when_listing_fails(resolved, state_dir, fakes) -> None:
    fakes.listing_ok = False
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.verify_failed"


def test_connect_does_not_check_foreign_firewalls(resolved, state_dir, fakes) -> None:
    fakes.firewalls = [_foreign("sibling-fw", tags=["playground"])]
    fakes.listing_ok = False
    run, _ = _run(execute_connect, resolved, state_dir)
    assert run.status == "succeeded"
