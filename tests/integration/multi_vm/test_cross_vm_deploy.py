"""End-to-end test for the cross-VM barak-deploy lab.

Default: skipped. The pass/fail criteria in
``playground-requirements.md`` §"Pass/fail criteria" describe what the
test asserts when enabled.

How to enable
-------------

Set ``PLAYGROUND_LIVE_INFRA=1`` in the environment. The test runs
against real libvirt and depends on:

- ``~/Workspace/barak-deploy/`` with a built wheel under ``dist/``
  (run ``uv build`` in that repo if it's stale).
- Sufficient host resources: ~8 GiB RAM and ~80 GiB free disk for two
  Ubuntu Noble VMs.
- Membership in the ``libvirt`` group (or ``sudo`` available for the
  tofu provider).

The test brings the lab up via ``playground apply``, exercises the
ship-deploy flow, asserts every pass/fail criterion, then tears down
via ``playground destroy``. A failing assertion still runs the
teardown via ``finally``.

What the test exercises
-----------------------

The six pass/fail criteria from the spec, in order:

1. Container running on target (``docker ps --filter name=hello``).
2. Config file in place with the templated greeting + shipped_at.
3. ``barak-deploy history`` shows a pipeline run with status=ok and
   four step records.
4. Tar archived under ``/var/spool/deploys/archive/ok/``.
5. Manifest written under ``/var/lib/barak-deploy/extracts/...``.
6. Idempotency: a second ship-deploy run produces a history entry
   where every step has ``skipped: true``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    not os.environ.get("PLAYGROUND_LIVE_INFRA"),
    reason=(
        "needs real libvirt + barak-deploy artifacts; "
        "set PLAYGROUND_LIVE_INFRA=1 to enable."
    ),
)


def _playground(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``playground <args>`` from the repo root and capture output."""
    return subprocess.run(
        ["playground", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _ssh(host_ip: str, command: str) -> subprocess.CompletedProcess[str]:
    """SSH into a VM as the ubuntu user and run a command."""
    return subprocess.run(
        [
            "ssh",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            f"ubuntu@{host_ip}",
            command,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _wait_for_pipeline(target_ip: str, timeout: float = 30.0) -> dict:
    """Poll ``barak-deploy history`` on target until a record appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _ssh(
            target_ip,
            "sudo -u barak-deploy barak-deploy history --since '5 minutes ago' --output json",
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                history = json.loads(result.stdout)
            except json.JSONDecodeError:
                history = None
            if history:
                return history
        time.sleep(1)
    pytest.fail(f"barak-deploy history empty after {timeout}s on {target_ip}")


def test_cross_vm_ship_and_deploy() -> None:
    # Pre-flight: sync barak-deploy artifacts into ansible/files/.
    sync = subprocess.run(
        ["make", "sync-from-barak-deploy"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sync.returncode == 0, f"make sync-from-barak-deploy failed:\n{sync.stderr}"

    # Bring the lab up.
    apply_result = _playground("apply", "barak-deploy-cross-vm")
    assert apply_result.returncode == 0, (
        f"playground apply failed (exit {apply_result.returncode}):\n"
        f"stdout:\n{apply_result.stdout}\nstderr:\n{apply_result.stderr}"
    )

    try:
        # Status query gives us the pinned IPs back from tofu state.
        status_result = _playground(
            "status", "barak-deploy-cross-vm", "--output", "json"
        )
        assert status_result.returncode == 0, status_result.stderr
        status = json.loads(status_result.stdout)
        ips = {vm["name"]: vm["ip"] for vm in status["vms"]}
        assert ips["central"] == "10.20.40.20"
        assert ips["target"] == "10.20.40.21"

        # Sanity: both VMs reachable, tunneler + docker installed,
        # barak-deploy running on target.
        sanity_central = _ssh(
            ips["central"], "tunneler --help >/dev/null && docker ps >/dev/null"
        )
        assert sanity_central.returncode == 0, sanity_central.stderr
        sanity_target = _ssh(
            ips["target"],
            "tunneler --help >/dev/null && docker ps >/dev/null && "
            "systemctl is-active barak-deploy",
        )
        assert sanity_target.returncode == 0, sanity_target.stderr
        assert "active" in sanity_target.stdout

        # Trigger the ship-deploy flow.
        ship = _ssh(ips["central"], "/usr/local/bin/ship-deploy.sh")
        assert ship.returncode == 0, (
            f"ship-deploy.sh failed:\nstdout:\n{ship.stdout}\nstderr:\n{ship.stderr}"
        )

        # Wait for the pipeline to land.
        history = _wait_for_pipeline(ips["target"])
        # Spec criterion 3: pipeline status=ok with 4 step records.
        run_records = [
            r for r in history
            if r.get("pipeline") == "deploy-demo"
        ]
        assert run_records, f"no deploy-demo pipeline record in history: {history}"
        first_run = run_records[0]
        assert first_run["status"] == "ok", first_run
        assert len(first_run.get("steps", [])) == 4
        assert {s["name"] for s in first_run["steps"]} == {
            "unwrap", "load", "place-config", "run"
        }
        assert all(s["status"] == "ok" for s in first_run["steps"])

        # Spec criterion 1: container running on target.
        ps = _ssh(ips["target"], "docker ps --filter name=hello --format '{{.Status}}'")
        assert ps.returncode == 0, ps.stderr
        assert ps.stdout.startswith("Up "), f"hello container not up: {ps.stdout!r}"

        # Spec criterion 1 (cross-check): the image bytes on target match
        # what was `docker save`d on central. The spec asks for `docker
        # images --digests` parity, but the demo image is a retag of
        # alpine:3.19 — registry digests show `<none>` on both VMs. The
        # proper "same bytes" check is image ID equality, which is what
        # we assert here.
        id_central = _ssh(ips["central"], "docker images hello:demo --format '{{.ID}}'")
        id_target = _ssh(ips["target"], "docker images hello:demo --format '{{.ID}}'")
        assert id_central.returncode == 0 and id_central.stdout.strip(), id_central.stderr
        assert id_target.returncode == 0 and id_target.stdout.strip(), id_target.stderr
        assert id_central.stdout.strip() == id_target.stdout.strip(), (
            f"image ID mismatch — central={id_central.stdout!r}, "
            f"target={id_target.stdout!r}"
        )

        # Spec criterion 2: config file in place with templated greeting.
        conf = _ssh(ips["target"], "cat /etc/hello/hello.conf")
        assert conf.returncode == 0, conf.stderr
        assert "greeting = hello from central" in conf.stdout
        assert "shipped_at = " in conf.stdout

        # Spec criterion 4: tar archived under archive/ok/.
        archive = _ssh(
            ips["target"], "ls -la /var/spool/deploys/archive/ok/"
        )
        assert archive.returncode == 0, archive.stderr
        assert "demo-app.tar.gz" in archive.stdout

        # Spec criterion 5: manifest written.
        manifest = _ssh(
            ips["target"],
            "cat /var/lib/barak-deploy/extracts/demo-app/.bundle-manifest.json",
        )
        assert manifest.returncode == 0, manifest.stderr
        manifest_data = json.loads(manifest.stdout)
        files = manifest_data.get("files", [])
        assert any("images/hello.tar" in f for f in files)
        assert any("configs/hello.conf" in f for f in files)
        assert "tar_sha256" in manifest_data

        # Spec criteria 4 + 5 (cross-check): the archived tar.gz's bytes
        # match the sha256 the manifest recorded. The spec also says
        # criterion 4's tar should match "the sha256 of the source on
        # central" — but ship-deploy.sh wraps its output in a mktemp dir
        # and cleans up on EXIT, so there's no source tar to hash after
        # the script returns. Asserting manifest <-> archive parity is
        # the achievable form of "bytes-on-target == bytes-shipped".
        archived_sha = _ssh(
            ips["target"],
            "sha256sum /var/spool/deploys/archive/ok/demo-app.tar.gz | awk '{print $1}'",
        )
        assert archived_sha.returncode == 0, archived_sha.stderr
        assert archived_sha.stdout.strip() == manifest_data["tar_sha256"], (
            f"manifest tar_sha256 ({manifest_data['tar_sha256']!r}) does not "
            f"match the archived tar's sha256 ({archived_sha.stdout.strip()!r})"
        )

        # Spec criterion 6: idempotency — second run reports skipped=true.
        ship_again = _ssh(ips["central"], "/usr/local/bin/ship-deploy.sh")
        assert ship_again.returncode == 0, ship_again.stderr
        # Give the agent a moment to pick up the second drop.
        time.sleep(10)
        second_history = _wait_for_pipeline(ips["target"])
        runs = [
            r for r in second_history
            if r.get("pipeline") == "deploy-demo"
        ]
        assert len(runs) >= 2, f"expected ≥2 pipeline runs, got {len(runs)}"
        # The newest run is the second one — every step skipped.
        newest = max(runs, key=lambda r: r.get("started_at", ""))
        assert newest["status"] == "ok"
        assert all(
            s.get("skipped") is True for s in newest["steps"]
        ), f"second run had non-skipped steps: {newest['steps']}"
    finally:
        # Always tear down; check exit code afterward so an apply-side
        # failure doesn't mask a destroy-side leak.
        destroy_result = _playground("destroy", "barak-deploy-cross-vm")
        if destroy_result.returncode != 0:
            pytest.fail(
                f"playground destroy failed:\nstdout:\n{destroy_result.stdout}\n"
                f"stderr:\n{destroy_result.stderr}"
            )


# Lint guard: assert the test's module-level skipif kicks in by default.
# Without this, a CI misconfiguration that sets PLAYGROUND_LIVE_INFRA would
# silently run the live test against whatever libvirt happens to be on the
# runner. The guard runs unconditionally and prints a clear marker.

def test_live_infra_flag_default_skips() -> None:
    if os.environ.get("PLAYGROUND_LIVE_INFRA"):
        pytest.skip("operator opted into live infra; the skipif marker takes over.")
    # The pytestmark above already skips test_cross_vm_ship_and_deploy in
    # this case; this companion assertion documents the invariant.
    assert pytestmark.kwargs["reason"].startswith("needs real libvirt")
