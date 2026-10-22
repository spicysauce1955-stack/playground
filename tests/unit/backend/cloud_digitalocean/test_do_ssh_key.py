"""DigitalOcean-registered SSH key for custom / non-Ubuntu images.

DigitalOcean refuses to create such a Droplet without ``ssh_keys`` (HTTP 422
"The image for this droplet does not use root passwords").  Covers the pure
fingerprint helpers, the API wrappers (HTTP seam mocked), and the apply /
resume preflight.  No test reaches the real API (see tests/conftest.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from playground.backend.cloud_digitalocean import do as do_module
from playground.backend.cloud_digitalocean import runner
from playground.backend.cloud_digitalocean.do import create_ssh_key, ensure_ssh_key, list_ssh_keys
from playground.backend.cloud_digitalocean.ssh_keys import (
    do_key_name,
    key_for_upload,
    md5_fingerprint,
)
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"
FAKE_TOKEN = "dop_v1_faketoken0000000000000000000000000000"

# Throwaway keys; fingerprints are `ssh-keygen -l -E md5` of each.
ED25519 = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIyWn2kj8/wYihzET8ROnv+dXUPvLQRWvEmo0QVEvvRm"
    " test@host"
)
ED25519_FP = "d6:42:82:da:42:46:25:16:23:07:30:43:b5:a3:b0:5d"
KEY_NAME = "playground-d64282da"
UPLOAD = ED25519.rsplit(" ", 1)[0]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_md5_fingerprint_matches_ssh_keygen() -> None:
    assert md5_fingerprint(ED25519) == ED25519_FP
    assert md5_fingerprint(UPLOAD) == ED25519_FP  # comment is irrelevant
    assert md5_fingerprint("  " + ED25519 + "\n") == ED25519_FP


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "ssh-ed25519",
        "ssh-rsa AAAA fakekey",  # decodes, but no embedded key type
        "ssh-ed25519 AAAATESTKEY user@host",  # not valid base64
        "ssh-rsa " + ED25519.split()[1],  # embedded type is ssh-ed25519
        "not a key at all",
    ],
)
def test_md5_fingerprint_rejects_malformed_keys(bad: str) -> None:
    assert md5_fingerprint(bad) is None
    assert key_for_upload(bad) is None


def test_key_name_and_upload_form() -> None:
    assert do_key_name(ED25519_FP) == KEY_NAME
    assert key_for_upload(ED25519) == UPLOAD


# ---------------------------------------------------------------------------
# API wrappers over a mocked HTTP seam
# ---------------------------------------------------------------------------


class _Api:
    """Scripted ``_request``: a queue of responses per (method, path)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.responses: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
        monkeypatch.setattr(do_module, "_request", self)

    def on(self, method: str, path: str, *responses: tuple[int, dict[str, Any]]) -> None:
        self.responses.setdefault((method, path), []).extend(responses)

    def __call__(self, method, path, token, *, params=None, timeout=15, json=None):
        assert token == FAKE_TOKEN
        self.calls.append((method, path, json))
        queue = self.responses[(method, path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _keys(*fps: str) -> dict[str, Any]:
    return {"ssh_keys": [{"id": i, "name": f"k{i}", "fingerprint": fp} for i, fp in enumerate(fps)]}


def test_list_ssh_keys_follows_pagination(monkeypatch) -> None:
    api = _Api(monkeypatch)
    nxt = "https://api.digitalocean.com/v2/account/keys?page=2&per_page=200"
    api.on("GET", "/v2/account/keys", (200, {**_keys("a"), "links": {"pages": {"next": nxt}}}))
    api.on("GET", nxt, (200, _keys("b")))
    keys, diags, ok = list_ssh_keys(FAKE_TOKEN)
    assert ok and diags == [] and [k["fingerprint"] for k in keys] == ["a", "b"]


def test_list_ssh_keys_never_follows_a_foreign_next_link(monkeypatch) -> None:
    api = _Api(monkeypatch)
    api.on("GET", "/v2/account/keys", (200, {**_keys("a"), "links": {"pages": {
        "next": "https://evil.example/v2/account/keys?page=2"}}}))
    keys, diags, ok = list_ssh_keys(FAKE_TOKEN)
    assert not ok and keys == [] and FAKE_TOKEN not in diags[0].message
    assert [c[1] for c in api.calls] == ["/v2/account/keys"]


def test_create_ssh_key_posts_name_and_key(monkeypatch) -> None:
    api = _Api(monkeypatch)
    api.on("POST", "/v2/account/keys", (201, {"ssh_key": {"id": 9, "name": KEY_NAME}}))
    status, key = create_ssh_key(FAKE_TOKEN, name=KEY_NAME, public_key=UPLOAD)
    assert status == 201 and key == {"id": 9, "name": KEY_NAME}
    assert api.calls == [("POST", "/v2/account/keys", {"name": KEY_NAME, "public_key": UPLOAD})]


def _ensure():
    return ensure_ssh_key(
        FAKE_TOKEN, name=KEY_NAME, public_key=UPLOAD, fingerprint=ED25519_FP,
    )


def test_ensure_reuses_a_key_with_the_same_fingerprint(monkeypatch) -> None:
    api = _Api(monkeypatch)
    api.on("GET", "/v2/account/keys", (200, _keys("11:22", ED25519_FP.upper())))
    key, created, problem = _ensure()
    assert key is not None and key["id"] == 1 and not created and problem == ""
    assert all(c[0] == "GET" for c in api.calls)  # nothing created


def test_ensure_creates_the_key_when_absent(monkeypatch) -> None:
    api = _Api(monkeypatch)
    api.on("GET", "/v2/account/keys", (200, _keys("11:22")))
    api.on("POST", "/v2/account/keys", (201, {"ssh_key": {"id": 7, "name": KEY_NAME}}))
    key, created, problem = _ensure()
    assert key == {"id": 7, "name": KEY_NAME} and created and problem == ""
    assert [c[0] for c in api.calls] == ["GET", "POST"]


def test_ensure_relists_once_on_a_422_race(monkeypatch) -> None:
    api = _Api(monkeypatch)
    api.on("GET", "/v2/account/keys", (200, _keys()), (200, _keys(ED25519_FP)))
    api.on("POST", "/v2/account/keys", (422, {"message": "SSH Key is already in use"}))
    key, created, problem = _ensure()
    assert key is not None and not created and problem == ""
    assert [c[0] for c in api.calls] == ["GET", "POST", "GET"]


def test_ensure_422_without_the_key_fails(monkeypatch) -> None:
    api = _Api(monkeypatch)
    api.on("GET", "/v2/account/keys", (200, _keys()))
    api.on("POST", "/v2/account/keys", (422, {"message": "Key invalid"}))
    key, created, problem = _ensure()
    assert key is None and "422" in problem
    assert [c[0] for c in api.calls] == ["GET", "POST", "GET"]  # re-listed ONCE


@pytest.mark.parametrize("status", [0, 401, 403, 500])
def test_ensure_create_failure(monkeypatch, status: int) -> None:
    api = _Api(monkeypatch)
    api.on("GET", "/v2/account/keys", (200, _keys()))
    api.on("POST", "/v2/account/keys", (status, {}))
    key, _, problem = _ensure()
    assert key is None and f"status {status}" in problem and FAKE_TOKEN not in problem


def test_ensure_list_failure_never_creates(monkeypatch) -> None:
    api = _Api(monkeypatch)
    api.on("GET", "/v2/account/keys", (500, {}))
    key, _, problem = _ensure()
    assert key is None and "listing" in problem
    assert [c[0] for c in api.calls] == ["GET"]


# ---------------------------------------------------------------------------
# apply / resume preflight
# ---------------------------------------------------------------------------


@pytest.fixture
def lab_factory(tmp_path: Path):
    loaded, diags = load_config(CONFIG_DIR)
    assert diags == []
    base = resolve_lab(loaded, "cloud-smoke")

    def make(image: str | None, key_text: str = ED25519):
        key_path = tmp_path / "id.pub"
        key_path.write_text(key_text + "\n")
        providers = {base.backend: {
            **dict(base.providers.get(base.backend, {})),
            "ssh_public_key_path": str(key_path),
        }}
        ov = {"cloud-digitalocean": {"image": image}} if image else {}
        vms = [
            base.vms[0].model_copy(update={"name": "node1", "provider_overrides": {}}),
            base.vms[0].model_copy(update={"name": "central", "provider_overrides": ov}),
        ]
        return base.model_copy(update={"providers": providers, "vms": vms})

    return make


@pytest.fixture
def shims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub everything around the preflight; stop the run at tofu-init."""
    from playground.runs import StepResult

    seen: dict[str, Any] = {"ensure": [], "steps": []}
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(runner, "verify_token", lambda t: 200)
    monkeypatch.setattr(runner, "_fetch_all_sizes", lambda t: ([], True, None, 200))
    monkeypatch.setattr(runner, "check_size_region_availability", lambda *a, **k: [])
    monkeypatch.setattr(runner, "list_vpcs", lambda t: ([], [], True))
    monkeypatch.setattr(runner, "schedule_workloads", lambda r: ({v.name: [] for v in r.vms}, []))
    monkeypatch.setattr(runner, "stage_workload_files", lambda *a, **k: ({}, []))

    def fake_init(tofu_dir, log_path, *, bus, run_id):
        seen["steps"].append("tofu-init")
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        return StepResult(
            name="tofu-init", command=["tofu"], exit_code=1, log_path=str(log_path),
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:00+00:00",
        ), []

    monkeypatch.setattr(runner, "run_tofu_init", fake_init)

    def fake_ensure(token, *, name, public_key, fingerprint):
        seen["ensure"].append((name, public_key, fingerprint))
        return seen.get("ensure_result", ({"id": 1, "name": name}, True, ""))

    monkeypatch.setattr(runner, "ensure_ssh_key", fake_ensure)
    src = tmp_path / "tofu" / "cloud_digitalocean"
    src.mkdir(parents=True)
    (src / "main.tf").write_text("")
    seen["tofu_dir"] = tmp_path / "tofu"
    seen["state_dir"] = tmp_path / ".playground"
    return seen


def _run(shims, resolved, fn=runner.execute_apply):
    bus = EventBus()
    lines: list[str] = []
    bus.subscribe(lambda ev: lines.append(json.dumps(ev.model_dump(mode="json"), default=str)))
    run, diags = fn(
        resolved=resolved, state_dir=shims["state_dir"], tofu_dir=shims["tofu_dir"],
        ansible_dir=REPO_ROOT / "ansible", config_dir=CONFIG_DIR, bus=bus,
    )
    return run, diags, "\n".join(lines)


def _tfvars(shims) -> dict[str, Any]:
    per_lab = shims["state_dir"] / "state" / "cloud-digitalocean" / "cloud-smoke"
    path = per_lab / "cloud-smoke.tfvars.json"
    return json.loads(path.read_text())


def test_all_ubuntu_lab_never_touches_account_keys(shims, lab_factory) -> None:
    run, _, _ = _run(shims, lab_factory(None, key_text="ssh-rsa AAAA fakekey"))
    assert shims["ensure"] == []
    assert shims["steps"] == ["tofu-init"]  # reached tofu despite an odd key
    assert _tfvars(shims)["vm_ssh_keys"] == {}


@pytest.mark.parametrize("fn", [runner.execute_apply, runner.execute_resume])
def test_non_ubuntu_vm_registers_the_key_before_tofu(shims, lab_factory, fn) -> None:
    run, _, events = _run(shims, lab_factory("rockylinux-8-x64"), fn=fn)
    assert shims["ensure"] == [(KEY_NAME, UPLOAD, ED25519_FP)]
    assert shims["steps"] == ["tofu-init"]
    assert _tfvars(shims)["vm_ssh_keys"] == {"central": [ED25519_FP]}
    assert "registered DigitalOcean SSH key 'playground-d64282da'" in events
    assert "kept on destroy" in events
    preflight = next(s for s in run.steps if s.name == "cloud-preflight")
    assert preflight.exit_code == 0
    assert ED25519_FP in Path(preflight.log_path).read_text()


@pytest.mark.parametrize("fn", [runner.execute_apply, runner.execute_resume])
def test_key_failure_fails_preflight_before_tofu(shims, lab_factory, fn) -> None:
    shims["ensure_result"] = (None, False, "registering the key failed (status 403)")
    run, diags, events = _run(shims, lab_factory("rockylinux-8-x64"), fn=fn)
    assert run is not None and run.status == "failed"
    assert [d.id for d in diags] == ["runtime.apply.do_ssh_key_failed"]
    assert diags[0].severity == "error"
    assert "central" in diags[0].message and "status 403" in diags[0].message
    assert shims["steps"] == []
    assert run.steps[-1].name == "cloud-preflight" and run.steps[-1].exit_code == 1
    run_json = (shims["state_dir"] / "runs" / run.run_id / "run.json").read_text()
    for text in (run_json, events, diags[0].message, diags[0].suggestion or ""):
        assert FAKE_TOKEN not in text


def test_unparseable_key_is_refused_before_the_run(shims, lab_factory) -> None:
    run, diags, _ = _run(shims, lab_factory("rockylinux-8-x64", key_text="ssh-rsa AAAA x"))
    assert run is None
    assert [d.id for d in diags] == ["runtime.apply.do_ssh_key_failed"]
    assert "not a parseable OpenSSH public key" in diags[0].message
    assert shims["ensure"] == [] and shims["steps"] == []
    assert not (shims["state_dir"] / "runs").exists()


def test_key_check_runs_after_network_preflight(shims, lab_factory, monkeypatch) -> None:
    """A failing VPC preflight must not register anything in the account."""
    vpc = {"id": "v", "name": "other", "ip_range": "10.60.0.0/16", "region": "nyc3"}
    monkeypatch.setattr(runner, "list_vpcs", lambda t: ([vpc], [], True))
    run, diags, _ = _run(shims, lab_factory("rockylinux-8-x64"))
    assert [d.id for d in diags] == ["runtime.cloud.vpc_conflict"]
    assert shims["ensure"] == []


def test_teardown_renders_the_same_vm_ssh_keys_without_the_api(
    shims, lab_factory, monkeypatch,
) -> None:
    monkeypatch.setattr(runner, "run_tofu_destroy", lambda *a, **k: (
        runner._noop_step("tofu-destroy", Path(a[2])), []))
    monkeypatch.setattr(runner, "list_droplets_by_tag", lambda t, tag: ([], [], True))
    monkeypatch.setattr(runner, "find_vpc", lambda t, n: (None, [], True))
    monkeypatch.setattr(runner, "list_private_images", lambda t: ([], [], True))
    resolved = lab_factory("rockylinux-8-x64")
    per_lab = shims["state_dir"] / "state" / "cloud-digitalocean" / "cloud-smoke"
    per_lab.mkdir(parents=True)
    run, _ = runner.execute_destroy(
        resolved=resolved, state_dir=shims["state_dir"], tofu_dir=shims["tofu_dir"],
        bus=EventBus(), config_dir=CONFIG_DIR,
    )
    assert run.status == "succeeded"
    assert shims["ensure"] == []  # destroy never registers keys
    assert _tfvars(shims)["vm_ssh_keys"] == {"central": [ED25519_FP]}
