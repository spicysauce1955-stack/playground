"""Orphaned custom-image cleanup after teardown, plus the image API wrappers.

Mirrors test_vpc_sweep.py: best-effort, warnings only, never fails teardown.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from playground.backend.cloud_digitalocean import do as do_module
from playground.backend.cloud_digitalocean import runner
from playground.backend.cloud_digitalocean.do import delete_image, list_private_images
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus

CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"
FAKE_TOKEN = "dop_v1_faketoken0000000000000000000000000000"
LAB = "cloud-smoke"


@pytest.fixture
def resolved():
    loaded, diags = load_config(CONFIG_DIR)
    assert diags == []
    return resolve_lab(loaded, LAB)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", slept.append)
    return slept


def _img(iid: int, name: str, *, tags: list[str] | None = None, type_: str = "custom"):
    return {"id": iid, "name": name, "tags": tags or [], "type": type_}


def _sweep(resolved, tmp_path, operation="destroy"):
    return runner._image_sweep(
        operation=operation, resolved=resolved, log_path=tmp_path / "i.log",
    )


def _deleting(monkeypatch, result=None) -> list[str]:
    deleted: list[str] = []

    def fake(token, image_id):
        deleted.append(str(image_id))
        return list(result or [])

    monkeypatch.setattr(runner, "delete_image", fake)
    return deleted


# ---- _image_sweep ----------------------------------------------------------


def test_no_token_skips(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "list_private_images", lambda t: pytest.fail("no API"))
    step, diags = _sweep(resolved, tmp_path)
    assert step.name == "image-sweep" and step.exit_code == 0 and diags == []


def test_nothing_left_is_clean(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(runner, "list_private_images", lambda t: ([], [], True))
    monkeypatch.setattr(runner, "delete_image", lambda *a: pytest.fail("nothing to delete"))
    step, diags = _sweep(resolved, tmp_path)
    assert step.exit_code == 0 and diags == []
    assert "no custom image present" in Path(step.log_path).read_text()


def test_deletes_only_this_labs_custom_images(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    images = [
        _img(1, f"{LAB}-img-0123456789"),  # ours by name
        _img(2, "renamed", tags=[f"lab:{LAB}"]),  # ours by tag
        _img(3, f"{LAB}-img-0123456789-img-abcdefabcd"),  # lab "<LAB>-img-0123456789"
        _img(4, f"{LAB}-img-XYZ"),  # not a key
        _img(5, f"other-{LAB}-img-0123456789"),
        _img(6, f"{LAB}-img-0123456789", type_="snapshot"),  # never a snapshot
        _img(7, "x", tags=[f"lab:{LAB}"], type_="backup"),
        _img(8, "y", tags=[f"lab:{LAB}-2"]),
    ]
    monkeypatch.setattr(runner, "list_private_images", lambda t: (images, [], True))
    deleted = _deleting(monkeypatch)
    step, diags = _sweep(resolved, tmp_path)
    assert deleted == ["1", "2"]
    assert step.exit_code == 0 and diags == []


def test_delete_is_retried_then_succeeds(resolved, tmp_path, monkeypatch, no_sleep) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(
        runner, "list_private_images", lambda t: ([_img(1, f"{LAB}-img-0123456789")], [], True),
    )
    results = iter([["refused"], ["refused"], []])
    attempts: list[int] = []
    monkeypatch.setattr(runner, "delete_image", lambda t, i: attempts.append(1) or next(results))
    step, diags = _sweep(resolved, tmp_path)
    assert len(attempts) == 3 and no_sleep == [5.0, 5.0]
    assert diags == [] and step.exit_code == 0


@pytest.mark.parametrize("operation", ["destroy", "suspend", "reset"])
def test_persistent_failure_warns_but_never_fails(
    resolved, tmp_path, monkeypatch, operation,
) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(
        runner, "list_private_images",
        lambda t: ([_img(1, f"{LAB}-img-0123456789"), _img(2, f"{LAB}-img-abcdefabcd")], [], True),
    )
    refusal = runner.Diagnostic(id="runtime.cloud.api_error", severity="warning", message="409")
    deleted = _deleting(monkeypatch, [refusal])
    step, diags = _sweep(resolved, tmp_path, operation=operation)
    assert step.exit_code == 0
    assert deleted == ["1", "1", "1", "2", "2", "2"]
    assert {d.severity for d in diags} == {"warning"}
    orphans = [d for d in diags if d.id == f"runtime.{operation}.orphaned_image"]
    assert len(orphans) == 2 and "custom_images" in (orphans[0].suggestion or "")


def test_listing_failure_is_a_warning(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    api_err = runner.Diagnostic(id="runtime.cloud.api_error", severity="warning", message="x")
    monkeypatch.setattr(runner, "list_private_images", lambda t: ([], [api_err], False))
    step, diags = _sweep(resolved, tmp_path)
    assert step.exit_code == 0
    assert [d.id for d in diags] == [
        "runtime.cloud.api_error", "runtime.destroy.image_sweep_inconclusive",
    ]
    assert {d.severity for d in diags} == {"warning"}


# ---- wired into teardown -----------------------------------------------------


def _teardown_shims(monkeypatch, images_ok: bool = False) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(runner, "list_droplets_by_tag", lambda t, tag: ([], [], True))
    monkeypatch.setattr(runner, "find_vpc", lambda t, n: (None, [], True))
    monkeypatch.setattr(runner, "list_private_images", lambda t: ([], [], images_ok))


@pytest.mark.parametrize("fn", [runner.execute_destroy, runner.execute_suspend])
def test_destroy_and_suspend_run_image_sweep_last_and_survive_it(
    resolved, tmp_path, monkeypatch, fn,
) -> None:
    _teardown_shims(monkeypatch)
    run, diags = fn(
        resolved=resolved, state_dir=tmp_path / ".playground",
        tofu_dir=tmp_path / "tofu", bus=EventBus(), config_dir=CONFIG_DIR,
    )
    assert [s.name for s in run.steps][-3:] == ["tag-sweep", "vpc-sweep", "image-sweep"]
    assert run.status == "succeeded"
    op = run.operation
    assert f"runtime.{op}.image_sweep_inconclusive" in {d.id for d in diags}


def test_reset_runs_image_sweep_before_clean_state_files(resolved, tmp_path, monkeypatch) -> None:
    _teardown_shims(monkeypatch, images_ok=True)
    run, _ = runner.execute_reset(
        resolved=resolved, state_dir=tmp_path / ".playground",
        tofu_dir=tmp_path / "tofu", bus=EventBus(), config_dir=CONFIG_DIR,
    )
    assert [s.name for s in run.steps][-4:] == [
        "tag-sweep", "vpc-sweep", "image-sweep", "clean-state-files",
    ]
    assert run.status == "succeeded"


def test_image_sweep_skipped_when_droplets_survive(resolved, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    survivor = {"id": 1, "name": "x", "status": "active"}
    monkeypatch.setattr(runner, "list_droplets_by_tag", lambda t, tag: ([survivor], [], True))
    monkeypatch.setattr(runner, "delete_droplet", lambda t, i: [])
    monkeypatch.setattr(runner, "list_private_images", lambda t: pytest.fail("in use"))
    run, _ = runner.execute_destroy(
        resolved=resolved, state_dir=tmp_path / ".playground",
        tofu_dir=tmp_path / "tofu", bus=EventBus(), config_dir=CONFIG_DIR,
    )
    assert run.status == "failed"
    assert "image-sweep" not in [s.name for s in run.steps]


# ---- API wrappers (monkeypatched HTTP seam) ---------------------------------


def _recording(monkeypatch, pages: dict[str, tuple[int, dict[str, Any]]]):
    seen: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake(method, path, token, *, params=None, timeout=15, json=None):
        assert token == FAKE_TOKEN
        seen.append((method, path, params))
        return pages[path]

    monkeypatch.setattr(do_module, "_request", fake)
    return seen


def test_list_private_images_asks_for_private_and_paginates(monkeypatch) -> None:
    nxt = "https://api.digitalocean.com/v2/images?page=2&per_page=200&private=true"
    seen = _recording(monkeypatch, {
        "/v2/images": (200, {"images": [{"id": 1}], "links": {"pages": {"next": nxt}}}),
        nxt: (200, {"images": [{"id": 2}]}),
    })
    images, diags, ok = list_private_images(FAKE_TOKEN)
    assert ok and diags == [] and [i["id"] for i in images] == [1, 2]
    assert seen[0] == ("GET", "/v2/images", {"per_page": 200, "private": "true"})
    assert seen[1] == ("GET", nxt, None)


def test_list_private_images_pins_pagination_to_the_api_host(monkeypatch) -> None:
    evil = "https://evil.example/v2/images?page=2"
    seen = _recording(monkeypatch, {
        "/v2/images": (200, {"images": [{"id": 1}], "links": {"pages": {"next": evil}}}),
    })
    images, diags, ok = list_private_images(FAKE_TOKEN)
    assert not ok and images == [] and diags[0].severity == "warning"
    assert [p for _, p, _ in seen] == ["/v2/images"]


@pytest.mark.parametrize(
    "status,clean", [(204, True), (404, True), (403, False), (422, False), (0, False)],
)
def test_delete_image_statuses(monkeypatch, status: int, clean: bool) -> None:
    _recording(monkeypatch, {"/v2/images/42": (status, {})})
    diags = delete_image(FAKE_TOKEN, 42)
    assert (diags == []) is clean
    if diags:
        assert diags[0].severity == "warning" and FAKE_TOKEN not in diags[0].message
