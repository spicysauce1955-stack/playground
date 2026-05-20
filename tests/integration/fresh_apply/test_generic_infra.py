"""Fresh-state end-to-end smoke test for `playground apply`.

The single highest-leverage test in the suite: destroys any prior
lab state, applies the canonical generic-infra lab from scratch
against real libvirt + cloud-init + ansible, verifies the lab
is healthy, asserts idempotence by re-applying, then tears down.

Five of the six bugs in the strategic hardening plan
(docs/architecture/CONTRACTS.md → "Cross-layer pitfalls") would
have surfaced in this test the first time it ran. The 90%
unit-mock test surface can't catch library-default mismatches or
implicit cross-layer dependencies; only a real-binary E2E run can.

Default: skipped. Set ``PLAYGROUND_LIVE_INFRA=1`` to enable.

Requirements when enabled:

- Real libvirt + qemu (`playground doctor` passes modulo env-specific
  warnings)
- ~6 GiB RAM and ~60 GiB free disk in the default storage pool
- Membership in the `libvirt` group (and the session has it active)
- ~/.ssh/id_rsa (the private key that matches
  var.ssh_public_key_path)

Phases the test asserts:

1. **reset** — wipe any prior state for the lab
2. **doctor** — must exit 0 (errors block); warnings are OK
3. **apply (first pass)** — must succeed; produces a run record
4. **liveness** — every VM responds to ssh + cloud-init done
5. **status** — `playground status` reflects what we just applied
6. **idempotence** — second `apply` reports `changed=0` from
   ansible. The single most useful regression signal.
7. **destroy** — tofu destroys cleanly
8. **reset (post-destroy)** — second reset is a no-op
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LAB_NAME = "generic-infra"

pytestmark = pytest.mark.skipif(
    not os.environ.get("PLAYGROUND_LIVE_INFRA"),
    reason="needs real libvirt; set PLAYGROUND_LIVE_INFRA=1 to enable.",
)


def _playground(*args: str, timeout: float = 1800.0) -> subprocess.CompletedProcess[str]:
    """Run ``playground <args>`` from the repo root and capture output.

    Generous default timeout (30 min) covers `apply` on a slow
    network where cloud-init's `package_upgrade` is the long pole.
    """
    return subprocess.run(
        ["playground", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _ssh(host_ip: str, command: str, *, user: str = "ubuntu") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "ssh",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            f"{user}@{host_ip}",
            command,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60.0,
    )


def _parse_ansible_recap_changed(log_text: str) -> dict[str, int]:
    """Parse the PLAY RECAP block at the end of an ansible log.

    Returns ``{host: changed_count}``. Used by the idempotence
    assertion: every host must report changed=0 on the second
    apply if the roles are truly idempotent.
    """
    by_host: dict[str, int] = {}
    # The PLAY RECAP block is the last "host : ok=N changed=M ..." lines
    # in ansible-playbook output. Format is stable across modern ansible.
    pattern = re.compile(
        r"^(?P<host>[A-Za-z0-9._-]+)\s*:\s*ok=\d+\s+changed=(?P<changed>\d+)",
        re.MULTILINE,
    )
    for match in pattern.finditer(log_text):
        by_host[match.group("host")] = int(match.group("changed"))
    return by_host


def _find_latest_ansible_log(runs_dir: Path) -> Path:
    """Locate the most recent apply run's ansible.log under .playground/runs/.

    The fresh-state test runs apply twice; we need the second log
    for the idempotence assertion.
    """
    apply_runs = sorted(
        (run_dir for run_dir in runs_dir.iterdir() if "apply" in run_dir.name),
        key=lambda p: p.stat().st_mtime,
    )
    assert apply_runs, f"no apply run dirs under {runs_dir}"
    return apply_runs[-1] / "logs" / "ansible.log"


def test_fresh_apply_generic_infra_full_cycle() -> None:
    state_dir = REPO_ROOT / ".playground"

    # 1. RESET — scrub any prior state. Idempotent on a clean host.
    reset_result = _playground("reset", LAB_NAME)
    assert reset_result.returncode == 0, (
        f"initial reset failed:\n"
        f"stdout:\n{reset_result.stdout}\nstderr:\n{reset_result.stderr}"
    )

    # 2. DOCTOR — must exit 0 (errors block; warnings OK).
    doctor_result = _playground("doctor")
    assert doctor_result.returncode == 0, (
        f"playground doctor reported errors; fix host prereqs first:\n"
        f"stdout:\n{doctor_result.stdout}\nstderr:\n{doctor_result.stderr}"
    )

    # 3. APPLY (first pass) — must succeed end-to-end.
    apply1 = _playground("apply", LAB_NAME, "--output", "json")
    assert apply1.returncode == 0, (
        f"first apply failed (exit {apply1.returncode}):\n"
        f"stdout:\n{apply1.stdout}\nstderr:\n{apply1.stderr}"
    )
    apply1_payload = json.loads(apply1.stdout)
    assert apply1_payload["status"] == "succeeded"
    apply1_steps = {s["name"] for s in apply1_payload["steps"]}
    # Contract from CONTRACTS.md: every apply produces these steps.
    assert {"tofu-apply", "wait-for-vms-ready", "ansible-playbook"} <= apply1_steps

    try:
        # 4. STATUS — gives us VM names + IPs back.
        status_result = _playground("status", LAB_NAME, "--output", "json")
        assert status_result.returncode == 0, status_result.stderr
        status = json.loads(status_result.stdout)
        vm_ips = {vm["name"]: vm["ip"] for vm in status["vms"] if vm.get("ip")}
        assert vm_ips, f"status reported no VM IPs: {status}"

        # 5. LIVENESS — every VM responds to SSH and cloud-init is done.
        for vm_name, vm_ip in vm_ips.items():
            ssh_check = _ssh(vm_ip, "cloud-init status")
            assert ssh_check.returncode == 0, (
                f"VM {vm_name!r} ({vm_ip}) cloud-init status failed:\n"
                f"stdout: {ssh_check.stdout}\nstderr: {ssh_check.stderr}"
            )
            assert "done" in ssh_check.stdout, (
                f"VM {vm_name!r} cloud-init not done: {ssh_check.stdout!r}"
            )

        # 6. IDEMPOTENCE — second apply must report changed=0 on every host.
        # This catches roles that look idempotent in unit tests but mutate
        # state on every run (the #1 source of apply-time surprises).
        apply2 = _playground("apply", LAB_NAME, "--output", "json")
        assert apply2.returncode == 0, (
            f"second apply failed (exit {apply2.returncode}):\n"
            f"stdout:\n{apply2.stdout}\nstderr:\n{apply2.stderr}"
        )
        apply2_payload = json.loads(apply2.stdout)
        assert apply2_payload["status"] == "succeeded"

        # Parse the second apply's ansible.log for the PLAY RECAP.
        ansible_log = _find_latest_ansible_log(state_dir / "runs")
        log_text = ansible_log.read_text()
        changed_by_host = _parse_ansible_recap_changed(log_text)
        assert changed_by_host, (
            f"could not parse PLAY RECAP from {ansible_log}; "
            "log shape may have drifted"
        )
        non_idempotent = {h: c for h, c in changed_by_host.items() if c > 0}
        assert not non_idempotent, (
            f"non-idempotent roles on second apply: {non_idempotent}. "
            "A role is mutating state on every run. Inspect "
            f"{ansible_log} for the offending tasks."
        )
    finally:
        # 7. DESTROY — always run, even on assertion failure, so the
        # next test invocation starts clean. A destroy-side failure
        # surfaces after we've recorded the apply-side outcome.
        destroy_result = _playground("destroy", LAB_NAME)
        if destroy_result.returncode != 0:
            pytest.fail(
                f"playground destroy failed:\n"
                f"stdout:\n{destroy_result.stdout}\n"
                f"stderr:\n{destroy_result.stderr}"
            )

        # 8. RESET (post-destroy) — second reset on a clean lab is a no-op.
        # Asserts the scrub-by-name path is idempotent as designed.
        post_reset = _playground("reset", LAB_NAME)
        assert post_reset.returncode == 0, (
            f"post-destroy reset failed:\n"
            f"stdout:\n{post_reset.stdout}\nstderr:\n{post_reset.stderr}"
        )


# Lint guard: assert the test's module-level skipif kicks in by default.
# Same shape as tests/integration/multi_vm/test_cross_vm_deploy.py — if a
# CI run accidentally enables PLAYGROUND_LIVE_INFRA=1, this companion
# documents the invariant.

def test_live_infra_flag_default_skips() -> None:
    if os.environ.get("PLAYGROUND_LIVE_INFRA"):
        pytest.skip("operator opted into live infra; the skipif marker takes over.")
    assert pytestmark.kwargs["reason"].startswith("needs real libvirt")
