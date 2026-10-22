"""Orphaned-VPC cleanup after teardown, plus the VPC/firewall API wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from playground.backend.cloud_digitalocean import do as do_module
from playground.backend.cloud_digitalocean import runner
from playground.backend.cloud_digitalocean.do import delete_vpc, find_firewall, find_vpc
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
    run, diags = runner.execute_destroy(
        resolved=resolved, state_dir=tmp_path / ".playground",
        tofu_dir=tmp_path / "tofu", bus=EventBus(), config_dir=CONFIG_DIR,
    )
    names = [s.name for s in run.steps]
    assert names[-2:] == ["tag-sweep", "vpc-sweep"]
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


def test_find_firewall_matches_by_name(monkeypatch) -> None:
    _patch_request(monkeypatch, {("GET", "/v2/firewalls"): (200, {
        "firewalls": [{"name": "other"}, {"name": "lab-fw", "id": "f1"}],
    })})
    fw, diags, ok = find_firewall(FAKE_TOKEN, "lab-fw")
    assert ok and diags == [] and fw == {"name": "lab-fw", "id": "f1"}


def test_find_firewall_follows_pagination(monkeypatch) -> None:
    page2 = "https://api.digitalocean.com/v2/firewalls?page=2"
    _patch_request(monkeypatch, {
        ("GET", "/v2/firewalls"): (200, {
            "firewalls": [{"name": "a"}], "links": {"pages": {"next": page2}},
        }),
        ("GET", page2): (200, {"firewalls": [{"name": "lab-fw"}]}),
    })
    fw, _, ok = find_firewall(FAKE_TOKEN, "lab-fw")
    assert ok and fw is not None


def test_find_firewall_absent_vs_failed(monkeypatch) -> None:
    _patch_request(monkeypatch, {("GET", "/v2/firewalls"): (200, {"firewalls": []})})
    assert find_firewall(FAKE_TOKEN, "x") == (None, [], True)
    _patch_request(monkeypatch, {("GET", "/v2/firewalls"): (500, {})})
    fw, diags, ok = find_firewall(FAKE_TOKEN, "x")
    assert fw is None and not ok and diags[0].severity == "warning"
    assert FAKE_TOKEN not in diags[0].message


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
