"""Orphaned-VPC cleanup after teardown, plus the VPC/firewall API wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from playground.backend.cloud_digitalocean import do as do_module
from playground.backend.cloud_digitalocean import runner
from playground.backend.cloud_digitalocean.do import delete_vpc, find_vpc
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus

CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"
FAKE_TOKEN = "dop_v1_faketoken0000000000000000000000000000"


@pytest.fixture
def resolved():
    loaded, diags = load_config(CONFIG_DIR)
    assert diags == []
    return resolve_lab(loaded, "cloud-smoke")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", slept.append)
    return slept


def _sweep(resolved, tmp_path):
    return runner._vpc_sweep(operation="destroy", resolved=resolved, log_path=tmp_path / "v.log")


# ---- _vpc_sweep ------------------------------------------------------------


def test_no_token_skips(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    monkeypatch.setattr(runner, "find_vpc", lambda *a: pytest.fail("no API without token"))
    step, diags = _sweep(resolved, tmp_path)
    assert step.exit_code == 0 and diags == []


def test_vpc_already_gone_is_clean(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(runner, "find_vpc", lambda t, n: (None, [], True))
    monkeypatch.setattr(runner, "delete_vpc", lambda *a: pytest.fail("nothing to delete"))
    step, diags = _sweep(resolved, tmp_path)
    assert step.exit_code == 0 and diags == []


def test_orphaned_vpc_is_deleted(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    deleted: list[str] = []
    monkeypatch.setattr(
        runner, "find_vpc",
        lambda t, n: ({"id": "vpc-1", "name": n}, [], True),
    )
    monkeypatch.setattr(runner, "delete_vpc", lambda t, i: deleted.append(i) or [])
    step, diags = _sweep(resolved, tmp_path)
    assert deleted == ["vpc-1"]
    assert step.exit_code == 0 and diags == []


def test_delete_is_retried_then_succeeds(resolved, tmp_path, monkeypatch, no_sleep) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(runner, "find_vpc", lambda t, n: ({"id": "v"}, [], True))
    results = iter([["refused"], ["refused"], []])
    attempts: list[int] = []
    monkeypatch.setattr(runner, "delete_vpc", lambda t, i: attempts.append(1) or next(results))
    step, diags = _sweep(resolved, tmp_path)
    assert len(attempts) == 3 and len(no_sleep) == 2
    assert diags == [] and step.exit_code == 0


def test_persistent_failure_warns_but_never_fails(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(runner, "find_vpc", lambda t, n: ({"id": "v9"}, [], True))
    refusal = runner.Diagnostic(
        id="runtime.cloud.api_error", severity="warning", message="status 409",
    )
    monkeypatch.setattr(runner, "delete_vpc", lambda t, i: [refusal])
    step, diags = _sweep(resolved, tmp_path)
    assert step.exit_code == 0
    assert {d.severity for d in diags} == {"warning"}
    assert "runtime.destroy.orphaned_vpc" in {d.id for d in diags}


def test_listing_failure_is_a_warning(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(runner, "find_vpc", lambda t, n: (None, [], False))
    step, diags = _sweep(resolved, tmp_path)
    assert step.exit_code == 0
    assert [d.id for d in diags] == ["runtime.destroy.vpc_sweep_inconclusive"]
    assert diags[0].severity == "warning"


# ---- wired into teardown -----------------------------------------------------


def test_destroy_runs_vpc_sweep_after_tag_sweep_and_survives_its_failure(
    resolved, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(runner, "list_droplets_by_tag", lambda t, tag: ([], [], True))
    monkeypatch.setattr(runner, "find_vpc", lambda t, n: (None, [], False))
    monkeypatch.setattr(runner, "list_private_images", lambda t: ([], [], True))
    run, diags = runner.execute_destroy(
        resolved=resolved, state_dir=tmp_path / ".playground",
        tofu_dir=tmp_path / "tofu", bus=EventBus(), config_dir=CONFIG_DIR,
    )
    names = [s.name for s in run.steps]
    assert names[-3:] == ["tag-sweep", "vpc-sweep", "image-sweep"]
    assert run.status == "succeeded"
    assert "runtime.destroy.vpc_sweep_inconclusive" in {d.id for d in diags}


def test_vpc_sweep_skipped_when_droplets_survive(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    survivor = {"id": 1, "name": "x", "status": "active"}
    monkeypatch.setattr(runner, "list_droplets_by_tag", lambda t, tag: ([survivor], [], True))
    monkeypatch.setattr(runner, "delete_droplet", lambda t, i: [])
    monkeypatch.setattr(runner, "find_vpc", lambda *a: pytest.fail("VPC is in use"))
    run, _ = runner.execute_destroy(
        resolved=resolved, state_dir=tmp_path / ".playground",
        tofu_dir=tmp_path / "tofu", bus=EventBus(), config_dir=CONFIG_DIR,
    )
    assert run.status == "failed"
    assert "vpc-sweep" not in [s.name for s in run.steps]


# ---- API wrappers (monkeypatched HTTP seam) ---------------------------------


def _patch_request(monkeypatch, responses: dict[tuple[str, str], tuple[int, dict[str, Any]]]):
    seen: list[tuple[str, str]] = []

    def fake(method, path, token, *, params=None, timeout=15):
        assert token == FAKE_TOKEN
        seen.append((method, path))
        return responses[(method, path)]

    monkeypatch.setattr(do_module, "_request", fake)
    return seen


def test_find_vpc_matches_by_name(monkeypatch) -> None:
    body = {"vpcs": [{"name": "l-vpc", "id": "v"}]}
    _patch_request(monkeypatch, {("GET", "/v2/vpcs"): (200, body)})
    vpc, _, ok = find_vpc(FAKE_TOKEN, "l-vpc")
    assert ok and vpc == {"name": "l-vpc", "id": "v"}


def test_find_vpc_malformed_body_is_not_ok(monkeypatch) -> None:
    _patch_request(monkeypatch, {("GET", "/v2/vpcs"): (200, {"vpcs": "nope"})})
    assert find_vpc(FAKE_TOKEN, "x")[2] is False


@pytest.mark.parametrize(
    "status,clean",
    [(204, True), (404, True), (403, False), (409, False), (0, False)],
)
def test_delete_vpc_statuses(monkeypatch, status: int, clean: bool) -> None:
    _patch_request(monkeypatch, {("DELETE", "/v2/vpcs/v1"): (status, {})})
    diags = delete_vpc(FAKE_TOKEN, "v1")
    assert (diags == []) is clean
    if diags:
        assert diags[0].severity == "warning" and FAKE_TOKEN not in diags[0].message


def test_persistent_failure_on_default_vpc_mentions_it(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    refusal = runner.Diagnostic(
        id="runtime.cloud.api_error", severity="warning", message="status 403",
    )
    monkeypatch.setattr(runner, "delete_vpc", lambda t, i: [refusal])
    monkeypatch.setattr(runner, "find_vpc", lambda t, n: ({"id": "v9", "default": True}, [], True))
    _, diags = _sweep(resolved, tmp_path)
    orphan = next(d for d in diags if d.id == "runtime.destroy.orphaned_vpc")
    assert "it may be the region's default VPC" in orphan.message
    monkeypatch.setattr(runner, "find_vpc", lambda t, n: ({"id": "v9", "default": False}, [], True))
    _, diags = _sweep(resolved, tmp_path)
    orphan = next(d for d in diags if d.id == "runtime.destroy.orphaned_vpc")
    assert "default VPC" not in orphan.message


def test_check_vpc_would_be_default() -> None:
    from playground.backend.cloud_digitalocean.do import check_vpc_would_be_default

    other = [{"region": "ams3", "name": "x"}]
    [d] = check_vpc_would_be_default(other, lab_name="l", region="nyc3")
    assert d.id == "runtime.cloud.vpc_would_be_default" and d.severity == "warning"
    assert "create any resource in 'nyc3' first" in (d.suggestion or "")
    assert check_vpc_would_be_default([], lab_name="l", region="nyc3")
    assert not check_vpc_would_be_default(
        [{"region": "nyc3"}], lab_name="l", region="nyc3",
    )


def test_check_vpc_conflicts_own_id_only() -> None:
    from playground.backend.cloud_digitalocean.do import check_vpc_conflicts

    vpcs = [{"id": "v1", "name": "l-vpc", "ip_range": "10.1.0.0/24", "region": "nyc3"}]
    kw = {"lab_name": "l", "vpc_name": "l-vpc", "vpc_cidr": "10.1.0.0/24"}
    assert check_vpc_conflicts(vpcs, own_vpc_id="v1", **kw) == []
    for own in (None, "v2"):
        [d] = check_vpc_conflicts(vpcs, own_vpc_id=own, **kw)
        assert "likely an orphan from a failed destroy" in d.message
        assert "v1" in d.message and "networking/vpc" in (d.suggestion or "")
    # Own id but the range changed: tofu would have to create a second VPC.
    changed = [{**vpcs[0], "ip_range": "10.9.0.0/24"}]
    assert check_vpc_conflicts(changed, own_vpc_id="v1", **kw)


def test_put_firewall_sends_json_body_and_reports_failure(monkeypatch) -> None:
    sent: list[tuple[str, str, Any]] = []

    def fake(method, path, token, *, params=None, timeout=15, json=None):
        sent.append((method, path, json))
        return (200, {"firewall": {"id": "f"}}) if path.endswith("/ok") else (422, {})

    monkeypatch.setattr(do_module, "_request", fake)
    body = {"name": "n", "tags": []}
    assert do_module.put_firewall(FAKE_TOKEN, "ok", body) == (True, [])
    assert sent == [("PUT", "/v2/firewalls/ok", body)]
    ok, diags = do_module.put_firewall(FAKE_TOKEN, "bad", body)
    assert not ok and diags[0].severity == "warning" and FAKE_TOKEN not in diags[0].message


def test_size_check_uses_a_supplied_catalogue_without_fetching(monkeypatch) -> None:
    def boom(token):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(do_module, "_fetch_all_sizes", boom)
    sizes = [{"slug": "s", "available": True, "regions": ["nyc3"]}]
    ok = do_module.check_size_region_availability(
        FAKE_TOKEN, lab_name="l", size="s", region="nyc3",
        catalogue=(sizes, True, None, 200),
    )
    assert ok == []
    bad = do_module.check_size_region_availability(
        FAKE_TOKEN, lab_name="l", size="s", region="sfo3",
        catalogue=(sizes, True, None, 200),
    )
    assert bad[0].id == "runtime.cloud.size_unavailable_in_region"


def test_tfstate_helpers(tmp_path) -> None:
    import json

    path = tmp_path / "terraform.tfstate"
    assert runner.tfstate_resources(path, "digitalocean_vpc") == []
    path.write_text("{not json")
    assert runner.tfstate_resources(path, "digitalocean_vpc") == []
    path.write_text(json.dumps({"resources": [
        {"mode": "managed", "type": "digitalocean_vpc", "name": "lab",
         "instances": [{"attributes": {"id": "v1"}}]},
        {"mode": "data", "type": "digitalocean_vpc", "name": "d",
         "instances": [{"attributes": {"id": "ignored"}}]},
        {"type": "digitalocean_droplet", "name": "node",
         "instances": [{"attributes": {"id": 5}}, {"attributes": {}}]},
    ]}))
    assert len(runner.tfstate_resources(path, "digitalocean_vpc")) == 1
    assert runner.tfstate_instance_ids(path, "digitalocean_vpc") == ["v1"]
    assert runner.tfstate_instance_ids(path, "digitalocean_vpc", name="x") == []
    assert runner.tfstate_instance_ids(path, "digitalocean_droplet") == ["5"]
