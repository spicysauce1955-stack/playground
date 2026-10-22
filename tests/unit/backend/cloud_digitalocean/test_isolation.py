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
    grants_egress_beyond,
    outbound_is_droplets_only,
    outbound_is_open,
)
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus
from playground.runs import StepResult

CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"
FAKE_TOKEN = "dop_v1_faketoken0000000000000000000000000000"
CIDR = "10.60.0.0/24"
LAB = "cloud-smoke"


LAB_IDS = {"111"}
FW_ID = "fw-own-1"


def _fw(isolated: bool) -> dict[str, Any]:
    """Our firewall as the DO API returns it: droplet-id destinations when isolated."""
    dest: dict[str, Any] = (
        {"droplet_ids": [111]} if isolated else {"addresses": ["0.0.0.0/0", "::/0"]}
    )
    return {
        "id": FW_ID,
        "name": f"{LAB}-fw",
        "status": "succeeded",
        "pending_changes": [],
        "droplet_ids": [111],
        "outbound_rules": [
            {"protocol": p, "ports": "0" if p == "icmp" else "all",
             "destinations": dict(dest)}
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
        "resources": [
            {"mode": "managed", "type": "digitalocean_firewall", "name": "lab",
             "instances": [{"attributes": {"id": FW_ID}}]},
            {"mode": "managed", "type": "digitalocean_droplet", "name": "node",
             "instances": [{"attributes": {"id": "111"}}]},
        ],
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
        self.plan_ok = True
        self.plan_json: dict[str, Any] | None = {
            "resource_changes": [
                {"address": "digitalocean_firewall.lab", "change": {"actions": ["update"]}},
                {"address": "digitalocean_droplet.node[0]", "change": {"actions": ["no-op"]}},
            ]
        }
        self.firewall_queue: list[dict[str, Any]] = []  # served first, one per poll
        self.now = 0.0


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    f = Fakes()
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)

    def run_init(tofu_dir, log_path, *, bus, run_id):
        f.calls.append("init")
        return _step("tofu-init", Path(log_path), 0 if f.init_ok else 1), []

    def plan(per_lab_dir, var_file, plan_file, log_path, *, bus, run_id):
        f.calls.append("plan")
        f.var_file = Path(var_file)
        f.plan_file = Path(plan_file)
        f.var_at_apply = json.loads(Path(var_file).read_text())["egress_isolated"]
        return _step("tofu-plan", Path(log_path), 0 if f.plan_ok else 1), []

    def show(per_lab_dir, plan_file):
        f.calls.append("show")
        return f.plan_json

    def apply_plan(per_lab_dir, plan_file, log_path, *, bus, run_id):
        f.calls.append("apply")
        f.applied_plan = Path(plan_file)
        return _step("tofu-apply", Path(log_path), 0 if f.apply_ok else 1), []

    def get_fw(token, fw_id):
        f.calls.append("get_firewall")
        assert token == FAKE_TOKEN
        assert fw_id == FW_ID
        if f.firewall_queue:
            return f.firewall_queue.pop(0), [], True
        if not f.api_ok:
            return None, [], False
        # Mimic the real thing: the firewall reflects what apply just set.
        return (_fw(bool(f.var_at_apply)) if f.api is None else f.api[0]), [], True

    def fake_sleep(secs):
        f.now += secs

    def list_droplets(token, tag):
        assert tag == f"lab:{LAB}"
        return list(f.droplets), [], f.listing_ok

    def list_fws(token):
        return list(f.firewalls), [], f.listing_ok

    monkeypatch.setattr(iso, "list_droplets_by_tag", list_droplets)
    monkeypatch.setattr(iso, "list_firewalls", list_fws)
    monkeypatch.setattr(iso, "run_tofu_init", run_init)
    monkeypatch.setattr(iso, "_tofu_plan", plan)
    monkeypatch.setattr(iso, "_tofu_show_plan", show)
    monkeypatch.setattr(iso, "_tofu_apply_plan", apply_plan)
    monkeypatch.setattr(iso, "get_firewall", get_fw)
    monkeypatch.setattr(iso, "_sleep", fake_sleep)
    monkeypatch.setattr(iso, "_clock", lambda: f.now)
    return f


def _run(fn, resolved, state_dir):
    return fn(resolved=resolved, state_dir=state_dir, bus=EventBus())


# ---- pure predicates -------------------------------------------------------


def test_predicates_on_open_and_isolated_firewalls() -> None:
    assert outbound_is_droplets_only(_fw(True), LAB_IDS)
    assert not outbound_is_droplets_only(_fw(False), LAB_IDS)
    assert outbound_is_open(_fw(False))
    assert not outbound_is_open(_fw(True))


@pytest.mark.parametrize("extra", [
    {"addresses": ["1.1.1.1"]},
    {"addresses": [CIDR]},  # the VPC CIDR reaches the VPC resolver
    {"tags": ["something"]},
    {"load_balancer_uids": ["lb"]},
    {"kubernetes_ids": ["k8s"]},
])
def test_droplets_only_rejects_any_extra_destination(extra: dict[str, Any]) -> None:
    fw = _fw(True)
    fw["outbound_rules"][1]["destinations"].update(extra)
    assert not outbound_is_droplets_only(fw, LAB_IDS)


def test_droplets_only_rejects_cidr_shape() -> None:
    fw = _fw(True)
    for rule in fw["outbound_rules"]:
        rule["destinations"] = {"addresses": [CIDR]}
    assert not outbound_is_droplets_only(fw, LAB_IDS)


def test_droplets_only_requires_all_three_protocols() -> None:
    fw = _fw(True)
    fw["outbound_rules"].pop()
    assert not outbound_is_droplets_only(fw, LAB_IDS)
    assert not outbound_is_droplets_only({"outbound_rules": []}, LAB_IDS)


def test_droplets_only_requires_the_exact_lab_droplet_set() -> None:
    assert not outbound_is_droplets_only(_fw(True), {"111", "222"})
    assert not outbound_is_droplets_only(_fw(True), {"999"})
    assert not outbound_is_droplets_only(_fw(True), set())
    fw = _fw(True)
    fw["outbound_rules"][0]["destinations"]["droplet_ids"] = [111, 222]
    assert not outbound_is_droplets_only(fw, LAB_IDS)
    fw = _fw(True)
    fw["droplet_ids"] = [111, 222]  # attached to a droplet outside the lab
    assert not outbound_is_droplets_only(fw, LAB_IDS)


def test_grants_egress_beyond_counts_vpc_cidr_addresses() -> None:
    fw = _foreign("vpc-cidr")
    for rule in fw["outbound_rules"]:
        rule["destinations"] = {"addresses": [CIDR]}
    assert grants_egress_beyond(fw, LAB_IDS)
    assert not grants_egress_beyond(_fw(True), LAB_IDS)


# ---- isolate ---------------------------------------------------------------


def test_isolate_flips_var_applies_and_verifies(resolved, state_dir, fakes) -> None:
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded"
    assert run.operation == "isolate"
    assert diags == []
    assert fakes.var_at_apply is True  # the file was flipped BEFORE apply
    assert _tfvars(state_dir)["egress_isolated"] is True
    assert fakes.calls == ["plan", "show", "apply", "get_firewall"]  # no init
    assert [s.name for s in run.steps] == [
        "preflight", "set-egress-isolated", "tofu-plan", "tofu-apply", "verify-firewall",
    ]
    assert "own Droplets" in run.summary


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
    assert "get_firewall" not in fakes.calls
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
        {**_fw(True), "id": FW_ID},  # our own firewall (by id) is exempt
    ]
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


def test_foreign_firewall_with_vpc_cidr_addresses_fails_isolate(resolved, state_dir, fakes) -> None:
    fw = _foreign("cidr-fw", tags=["playground"])
    for rule in fw["outbound_rules"]:
        rule["destinations"] = {"addresses": [CIDR]}
    fakes.firewalls = [fw]
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.foreign_firewall"


def test_own_firewall_with_addresses_fails_verify(resolved, state_dir, fakes) -> None:
    bad = _fw(True)
    bad["outbound_rules"][0]["destinations"]["addresses"] = [CIDR]
    fakes.api = [bad]
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.verify_failed"


def test_own_firewall_outbound_with_different_droplet_set_fails_verify(
    resolved, state_dir, fakes,
) -> None:
    fakes.droplets.append({"id": 222, "name": "n2", "tags": ["playground", f"lab:{LAB}"]})
    bad = _fw(True)
    bad["droplet_ids"] = [111, 222]  # attached to both...
    # ...but the outbound rules still only name 111
    fakes.api = [bad]
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.verify_failed"


def test_verify_inconclusive_when_lab_droplets_cannot_be_listed(resolved, state_dir, fakes) -> None:
    fakes.listing_ok = False
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed" and diags[0].id == "runtime.isolate.verify_failed"


# ---- identity by id (names are not unique) ------------------------------------


def test_same_named_firewall_with_other_id_is_foreign(resolved, state_dir, fakes) -> None:
    orphan = _foreign(f"{LAB}-fw", tags=["playground"])  # name collision, open egress
    orphan["id"] = "orphan-id"
    fakes.firewalls = [{**_fw(True)}, orphan]
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert [d.id for d in diags] == ["runtime.isolate.foreign_firewall"]
    assert "orphan-id" in diags[0].message


def test_our_firewall_is_read_by_id_from_state(resolved, state_dir, fakes) -> None:
    run, _ = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded"  # fake get_firewall asserts the id == FW_ID


def test_state_without_firewall_id_is_unreadable(resolved, state_dir, fakes) -> None:
    per_lab = state_dir / "state" / "cloud-digitalocean" / LAB
    # Firewall type present (passes preflight) but no id recorded.
    (per_lab / "terraform.tfstate").write_text(json.dumps({"resources": [
        {"mode": "managed", "type": "digitalocean_firewall", "name": "lab",
         "instances": [{"attributes": {}}]},
    ]}))
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.state_unreadable"


def test_read_state_ids_handles_both_id_shapes(tmp_path: Path) -> None:
    path = tmp_path / "terraform.tfstate"
    path.write_text(json.dumps({"resources": [
        {"mode": "managed", "type": "digitalocean_firewall", "name": "lab",
         "instances": [{"attributes": {"id": "f1"}}]},
        {"mode": "managed", "type": "digitalocean_droplet", "name": "node",
         "instances": [{"attributes": {"id": 5}}, {"attributes": {"id": "6"}}]},
        {"mode": "data", "type": "digitalocean_droplet", "name": "x",
         "instances": [{"attributes": {"id": "7"}}]},
    ]}))
    assert iso._read_state_ids(path) == ("f1", {"5", "6"})
    assert iso._read_state_ids(tmp_path / "missing") == (None, set())


# ---- coverage of the whole lab set ------------------------------------------------


def test_tag_listed_orphan_droplet_not_covered_fails_by_name(resolved, state_dir, fakes) -> None:
    fakes.droplets.append({"id": 222, "name": "orphan-vm", "tags": [f"lab:{LAB}"]})
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert [d.id for d in diags] == ["runtime.isolate.uncovered_droplet"]
    assert "orphan-vm" in diags[0].message and "222" in diags[0].message


def test_empty_tag_listing_with_state_droplets_fails(resolved, state_dir, fakes) -> None:
    fakes.droplets = []
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.verify_failed"


def test_tag_listing_missing_a_state_droplet_fails(resolved, state_dir, fakes) -> None:
    per_lab = state_dir / "state" / "cloud-digitalocean" / LAB
    state = json.loads((per_lab / "terraform.tfstate").read_text())
    state["resources"][1]["instances"].append({"attributes": {"id": "333"}})
    (per_lab / "terraform.tfstate").write_text(json.dumps(state))
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.verify_failed"
    assert "333" in diags[0].message


def test_state_with_no_droplets_never_passes(resolved, state_dir, fakes) -> None:
    per_lab = state_dir / "state" / "cloud-digitalocean" / LAB
    state = json.loads((per_lab / "terraform.tfstate").read_text())
    state["resources"].pop()  # drop the droplets
    (per_lab / "terraform.tfstate").write_text(json.dumps(state))
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed" and diags[0].id == "runtime.isolate.verify_failed"


def test_string_and_int_droplet_ids_compare_equal(resolved, state_dir, fakes) -> None:
    """tfstate ids are strings, tag-listing and firewall ids may be ints."""
    fw = _fw(True)
    fw["droplet_ids"] = ["111"]
    for rule in fw["outbound_rules"]:
        rule["destinations"] = {"droplet_ids": ["111"]}
    fakes.api = [fw]
    fakes.droplets = [{"id": "111", "name": "n", "tags": [f"lab:{LAB}"]}]
    run, _ = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded"


# ---- settled state ---------------------------------------------------------------


def _pending(isolated: bool, **over: Any) -> dict[str, Any]:
    return {**_fw(isolated), **over}


def test_waits_until_firewall_settles(resolved, state_dir, fakes) -> None:
    fakes.firewall_queue = [
        _pending(True, status="waiting", pending_changes=[{"droplet_id": 111}]),
        _pending(True, status="succeeded", pending_changes=[{"droplet_id": 111}]),
    ]
    run, _ = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded"
    assert fakes.calls.count("get_firewall") == 3
    assert fakes.now == 2 * iso._SETTLE_INTERVAL


def test_never_settling_is_inconclusive_and_bounded(resolved, state_dir, fakes) -> None:
    fakes.firewall_queue = [_pending(True, status="waiting")] * 1000
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.verify_failed"
    assert "did not finish" in diags[0].message
    assert iso._SETTLE_TIMEOUT <= fakes.now <= iso._SETTLE_TIMEOUT + iso._SETTLE_INTERVAL


def test_connect_waits_for_settle_too(resolved, state_dir, fakes) -> None:
    fakes.firewall_queue = [_pending(False, status="waiting")]
    run, _ = _run(execute_connect, resolved, state_dir)
    assert run.status == "succeeded"
    assert fakes.calls.count("get_firewall") == 2


def test_connect_never_settling_fails(resolved, state_dir, fakes) -> None:
    fakes.firewall_queue = [_pending(False, pending_changes=[{"x": 1}])] * 1000
    run, diags = _run(execute_connect, resolved, state_dir)
    assert run.status == "failed" and diags[0].id == "runtime.connect.verify_failed"


def test_firewall_missing_by_id_fails(resolved, state_dir, fakes, monkeypatch) -> None:
    monkeypatch.setattr(iso, "get_firewall", lambda t, i: (None, [], True))
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed" and "not found" in diags[0].message


# ---- scoped plan -------------------------------------------------------------------


def _plan(*changes: tuple[str, list[str]]) -> dict[str, Any]:
    return {"resource_changes": [
        {"address": a, "change": {"actions": acts}} for a, acts in changes
    ]}


@pytest.mark.parametrize("change", [
    ("digitalocean_droplet.node[0]", ["delete", "create"]),
    ("digitalocean_droplet.node[1]", ["create"]),
    ("digitalocean_vpc.lab", ["update"]),
    ("digitalocean_firewall.lab", ["delete", "create"]),
])
def test_plan_with_other_changes_is_refused(resolved, state_dir, fakes, change) -> None:
    fakes.plan_json = _plan(("digitalocean_firewall.lab", ["update"]), change)
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert [d.id for d in diags] == ["runtime.isolate.unexpected_changes"]
    assert change[0] in diags[0].message
    assert "apply" not in fakes.calls
    assert _tfvars(state_dir)["egress_isolated"] is False


def test_connect_refuses_unexpected_changes_with_its_own_id(resolved, state_dir, fakes) -> None:
    fakes.plan_json = _plan(("digitalocean_droplet.node[0]", ["create"]))
    run, diags = _run(execute_connect, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.connect.unexpected_changes"


def test_unreadable_plan_is_refused(resolved, state_dir, fakes) -> None:
    fakes.plan_json = None
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed" and diags[0].id == "runtime.isolate.unexpected_changes"
    assert "apply" not in fakes.calls


def test_noop_plan_is_accepted_for_an_idempotent_repeat(resolved, state_dir, fakes) -> None:
    fakes.plan_json = _plan(("digitalocean_firewall.lab", ["no-op"]),
                            ("digitalocean_droplet.node[0]", ["read"]))
    run, _ = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded"


def test_missing_resource_changes_key_means_no_changes(resolved, state_dir, fakes) -> None:
    fakes.plan_json = {}
    run, _ = _run(execute_isolate, resolved, state_dir)
    assert run.status == "succeeded"


def test_plan_failure_fails_and_restores_tfvars(resolved, state_dir, fakes) -> None:
    fakes.plan_ok = False
    run, diags = _run(execute_isolate, resolved, state_dir)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.plan_failed"
    assert "show" not in fakes.calls and "apply" not in fakes.calls
    assert _tfvars(state_dir)["egress_isolated"] is False


def test_the_inspected_plan_file_is_the_one_applied(resolved, state_dir, fakes) -> None:
    run, _ = _run(execute_isolate, resolved, state_dir)
    assert fakes.plan_file == fakes.applied_plan
    assert fakes.plan_file.name == "egress.tfplan"
    assert fakes.plan_file.is_absolute()
    assert fakes.plan_file.parent.name == "logs"
    assert run.run_id in str(fakes.plan_file)


def test_unexpected_changes_helper() -> None:
    assert iso._unexpected_changes(_plan(("digitalocean_firewall.lab", ["update"]))) == []
    assert iso._unexpected_changes(_plan(("a.b", ["delete", "create"]))) == ["a.b (delete+create)"]
