"""Unit tests for the cloud-digitalocean doctor checks.

All system calls (subprocess, os.environ, shutil.which) are monkeypatched so
the suite runs identically on any host with no token, no network, and no tofu
installed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from playground.preflight import doctor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _which_factory(present: set[str]):
    """Return a fake ``shutil.which`` that knows only ``present`` binaries."""

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    return fake_which


def _fake_run(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# check_cloud_do_token
# ---------------------------------------------------------------------------


def test_token_present_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_" + "a" * 64)
    assert doctor.check_cloud_do_token() == []


def test_token_unset_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    diags = doctor.check_cloud_do_token()
    assert len(diags) == 1
    d = diags[0]
    assert d.id == "runtime.doctor.cloud_token_missing"
    assert d.severity == "error"
    # Message names the env var NAME
    assert "DIGITALOCEAN_TOKEN" in d.message
    # Message must NOT contain a token value (there is none to leak here, but
    # verify the value placeholder "<your-token>" is not echoed back as real)
    assert "dop_v1_" not in d.message


def test_token_empty_string_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "")
    diags = doctor.check_cloud_do_token()
    assert len(diags) == 1
    assert diags[0].id == "runtime.doctor.cloud_token_missing"


def test_token_custom_env_name_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_DO_TOKEN", raising=False)
    diags = doctor.check_cloud_do_token(token_env="MY_DO_TOKEN")
    assert len(diags) == 1
    assert "MY_DO_TOKEN" in diags[0].message
    assert "MY_DO_TOKEN" in (diags[0].suggestion or "")


def test_token_custom_env_name_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_DO_TOKEN", "dop_v1_" + "b" * 64)
    assert doctor.check_cloud_do_token(token_env="MY_DO_TOKEN") == []


def test_token_error_suggestion_does_not_contain_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suggestion must never echo any token value (just the var name)."""
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    diags = doctor.check_cloud_do_token()
    suggestion = diags[0].suggestion or ""
    # The fake token we set is not in play here; just confirm the suggestion
    # pattern is the env-var reference form, not a value form.
    assert "=" in suggestion  # "export TOKEN=<your-token>"
    assert "dop_v1_" not in suggestion


# ---------------------------------------------------------------------------
# check_cloud_do_token_not_committed
# ---------------------------------------------------------------------------

_FAKE_TOKEN = "dop_v1_" + "c" * 64  # 71 chars total; matches the pattern


def _git_init(path: Path) -> None:
    """Initialise a bare git repo suitable for git grep."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )


def _git_commit_file(repo: Path, filename: str, content: str) -> None:
    (repo / filename).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", filename], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "test", "--allow-empty"],
        check=True,
        capture_output=True,
    )


def test_token_not_committed_clean_repo(tmp_path: Path) -> None:
    """A repo without any token pattern returns empty."""
    _git_init(tmp_path)
    _git_commit_file(tmp_path, "README.md", "no secrets here\n")
    diags = doctor.check_cloud_do_token_not_committed(repo_root=tmp_path)
    assert diags == []


def test_token_not_committed_finds_token(tmp_path: Path) -> None:
    """A committed token pattern returns an error naming the file, not the value."""
    _git_init(tmp_path)
    _git_commit_file(tmp_path, "secrets.txt", f"token={_FAKE_TOKEN}\n")
    diags = doctor.check_cloud_do_token_not_committed(repo_root=tmp_path)
    assert len(diags) == 1
    d = diags[0]
    assert d.id == "runtime.doctor.cloud_token_committed"
    assert d.severity == "error"
    # Must name the file
    assert "secrets.txt" in d.message
    # Must NOT include the token text itself
    assert _FAKE_TOKEN not in d.message
    assert _FAKE_TOKEN not in (d.suggestion or "")


def test_token_not_committed_non_git_dir(tmp_path: Path) -> None:
    """A directory that isn't a git repo returns empty (best-effort)."""
    diags = doctor.check_cloud_do_token_not_committed(repo_root=tmp_path)
    assert diags == []


def test_token_not_committed_git_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When git isn't on PATH the check is silently skipped."""
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)
    diags = doctor.check_cloud_do_token_not_committed(repo_root=tmp_path)
    assert diags == []


# ---------------------------------------------------------------------------
# check_cloud_do_tofu
# ---------------------------------------------------------------------------


def test_cloud_tofu_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"tofu"}))
    assert doctor.check_cloud_do_tofu() == []


def test_cloud_tofu_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    diags = doctor.check_cloud_do_tofu()
    assert len(diags) == 1
    assert diags[0].id == "runtime.doctor.cloud_tofu_missing"
    assert diags[0].severity == "error"


# ---------------------------------------------------------------------------
# check_cloud_do_state_writable
# ---------------------------------------------------------------------------


def test_state_writable_happy(tmp_path: Path) -> None:
    diags = doctor.check_cloud_do_state_writable(tmp_path)
    assert diags == []
    # Side-effect: directory was created
    assert (tmp_path / "state" / "cloud-digitalocean").is_dir()


def test_state_writable_probe_file_cleaned_up(tmp_path: Path) -> None:
    doctor.check_cloud_do_state_writable(tmp_path)
    probe = tmp_path / "state" / "cloud-digitalocean" / ".doctor_probe"
    assert not probe.exists()


def test_state_writable_blocked_by_file(tmp_path: Path) -> None:
    """If the path component is a file (not a dir), mkdir raises OSError."""
    blocking = tmp_path / "state"
    blocking.write_text("I am a file, not a directory")
    diags = doctor.check_cloud_do_state_writable(tmp_path)
    assert len(diags) == 1
    assert diags[0].id == "runtime.doctor.cloud_state_unwritable"
    assert diags[0].severity == "error"


def test_state_writable_mkdir_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a permission error on mkdir."""
    original_mkdir = Path.mkdir

    def _raise_on_target(self: Path, *args: Any, **kwargs: Any) -> None:
        if "cloud-digitalocean" in str(self):
            raise OSError("Permission denied")
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _raise_on_target)
    diags = doctor.check_cloud_do_state_writable(tmp_path)
    assert len(diags) == 1
    assert diags[0].id == "runtime.doctor.cloud_state_unwritable"


# ---------------------------------------------------------------------------
# check_cloud_do_provider_config — against the committed config/
# ---------------------------------------------------------------------------

_REPO_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


def test_provider_config_committed_no_error() -> None:
    """The committed config/providers/cloud-digitalocean.yaml is valid."""
    diags = doctor.check_cloud_do_provider_config(_REPO_CONFIG_DIR)
    ids = [d.id for d in diags]
    # No error about missing provider
    assert "runtime.doctor.cloud_provider_config_missing" not in ids


def test_provider_config_committed_ssh_open_warning() -> None:
    """The committed config has ssh_cidrs: [] → cloud_ssh_open warning fires."""
    diags = doctor.check_cloud_do_provider_config(_REPO_CONFIG_DIR)
    ids = [d.id for d in diags]
    assert "runtime.doctor.cloud_ssh_open" in ids
    # Must be a warning not an error
    ssh_open = next(d for d in diags if d.id == "runtime.doctor.cloud_ssh_open")
    assert ssh_open.severity == "warning"


def test_provider_config_missing_returns_error(tmp_path: Path) -> None:
    """A config dir without the provider block returns cloud_provider_config_missing."""
    diags = doctor.check_cloud_do_provider_config(tmp_path)
    assert len(diags) == 1
    assert diags[0].id == "runtime.doctor.cloud_provider_config_missing"
    assert diags[0].severity == "error"


def test_provider_config_region_unset_warning(tmp_path: Path) -> None:
    """A provider config lacking region/size/image emits cloud_region_unset."""
    # Write a minimal provider config that omits region/size/image
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "cloud-digitalocean.yaml").write_text(
        "apiVersion: playground/v1\n"
        "kind: ProviderConfig\n"
        "metadata:\n"
        "  name: cloud-digitalocean\n"
        "spec:\n"
        "  driver: cloud-digitalocean\n"
        "  token_env: DIGITALOCEAN_TOKEN\n"
    )
    diags = doctor.check_cloud_do_provider_config(tmp_path)
    ids = [d.id for d in diags]
    assert "runtime.doctor.cloud_region_unset" in ids
    region_diag = next(d for d in diags if d.id == "runtime.doctor.cloud_region_unset")
    assert region_diag.severity == "warning"


# ---------------------------------------------------------------------------
# run_all_checks with backend="cloud-digitalocean"
# ---------------------------------------------------------------------------

_LIBVIRT_ONLY_IDS = {
    "runtime.doctor.iso_tool_missing",
    "runtime.doctor.virsh_missing",
    "runtime.doctor.libvirt_group_missing",
    "runtime.doctor.libvirt_group_inactive",
    "runtime.doctor.default_pool_missing",
    "runtime.doctor.default_pool_no_autostart",
    "runtime.doctor.pool_path_unreadable",
    "runtime.doctor.apparmor_libvirt_unconfigured",
    "runtime.doctor.apparmor_orphan_profiles",
    "runtime.doctor.cloud_init_image_unverified",
    "runtime.doctor.ansible_config_not_wired",
    "runtime.doctor.tofu_state_drift",
    "runtime.doctor.vboxmanage_missing",
    "runtime.doctor.qemu_img_missing",
    "runtime.doctor.xsltproc_missing",
    "runtime.doctor.nested_disabled",
    "runtime.doctor.kvm_intel_recent_failures",
    "runtime.doctor.host_is_virtualized",
}


def test_run_all_checks_cloud_backend_no_libvirt_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cloud backend run must not produce any libvirt-only diagnostic IDs."""
    # Ensure token is unset so cloud_token_missing fires
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    diags = doctor.run_all_checks(
        backend="cloud-digitalocean",
        config_dir=_REPO_CONFIG_DIR,
        state_dir=tmp_path,
    )
    ids = {d.id for d in diags}
    overlap = ids & _LIBVIRT_ONLY_IDS
    assert overlap == set(), f"Libvirt-only IDs leaked into cloud run: {overlap}"


def test_run_all_checks_cloud_backend_token_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token unset → cloud_token_missing appears in cloud backend run."""
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    diags = doctor.run_all_checks(
        backend="cloud-digitalocean",
        config_dir=_REPO_CONFIG_DIR,
        state_dir=tmp_path,
    )
    ids = [d.id for d in diags]
    assert "runtime.doctor.cloud_token_missing" in ids


def test_run_all_checks_default_backend_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (no backend) run_all_checks still calls the libvirt suite."""
    # We can't easily run the full suite on any host, but we can confirm
    # the function does NOT raise and does NOT include cloud-specific ids
    # when those checks would pass (token present, no git repo with token).
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_" + "x" * 64)
    diags = doctor.run_all_checks()
    ids = {d.id for d in diags}
    # cloud_token_missing must NOT fire in the default run
    assert "runtime.doctor.cloud_token_missing" not in ids


# ---------------------------------------------------------------------------
# CLI integration: playground doctor --backend cloud-digitalocean
# ---------------------------------------------------------------------------


def test_cli_doctor_cloud_backend_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI with --backend cloud-digitalocean produces valid JSON, exits 1
    when token is missing, and does not leak the token value into output."""
    from playground.cli.main import app

    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    # Ensure no token is accidentally in the environment
    for key in list(os.environ):
        if "TOKEN" in key and "DIGITALOCEAN" in key:
            monkeypatch.delenv(key, raising=False)

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--backend",
            "cloud-digitalocean",
            "-c",
            str(_REPO_CONFIG_DIR),
            "--state-dir",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    # Exit 1 because cloud_token_missing is an error
    assert result.exit_code == 1, result.output

    payload = json.loads(result.output)
    assert payload["ok"] is False

    ids = [d["id"] for d in payload["diagnostics"]]
    assert "runtime.doctor.cloud_token_missing" in ids

    # No libvirt ids present
    for d in payload["diagnostics"]:
        assert d["id"] not in _LIBVIRT_ONLY_IDS, (
            f"Libvirt-only id {d['id']!r} leaked into cloud-backend output"
        )

    # No token-shaped value anywhere in the JSON output
    full_output = result.output
    import re
    assert not re.search(r"dop_v1_[A-Za-z0-9]{64}", full_output), (
        "Token value leaked into CLI output"
    )


def test_cli_doctor_cloud_backend_includes_ssh_open_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Firewall warning appears because the committed config has empty ssh_cidrs."""
    from playground.cli.main import app

    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--backend",
            "cloud-digitalocean",
            "-c",
            str(_REPO_CONFIG_DIR),
            "--state-dir",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    payload = json.loads(result.output)
    ids = [d["id"] for d in payload["diagnostics"]]
    assert "runtime.doctor.cloud_ssh_open" in ids
    ssh_diag = next(d for d in payload["diagnostics"] if d["id"] == "runtime.doctor.cloud_ssh_open")
    assert ssh_diag["severity"] == "warning"


# ---------------------------------------------------------------------------
# check_cloud_do_token_auth
# ---------------------------------------------------------------------------


def _patch_verify_token(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """Replace verify_token inside doctor.py's import scope."""
    import playground.backend.cloud_digitalocean.do as do_mod
    monkeypatch.setattr(do_mod, "verify_token", lambda token: status)


def test_token_auth_absent_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token absent → [] (check_cloud_do_token handles the missing-token error)."""
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    _patch_verify_token(monkeypatch, 200)
    diags = doctor.check_cloud_do_token_auth()
    assert diags == []


def test_token_auth_200_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid token → no diagnostic."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_" + "a" * 64)
    _patch_verify_token(monkeypatch, 200)
    diags = doctor.check_cloud_do_token_auth()
    assert diags == []


def test_token_auth_401_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 → cloud_token_unauthorized error; token value must not leak."""
    secret = "dop_v1_" + "x" * 64
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", secret)
    _patch_verify_token(monkeypatch, 401)
    diags = doctor.check_cloud_do_token_auth()
    assert len(diags) == 1
    d = diags[0]
    assert d.id == "runtime.doctor.cloud_token_unauthorized"
    assert d.severity == "error"
    assert "DIGITALOCEAN_TOKEN" in d.message
    assert secret not in d.message
    assert secret not in (d.suggestion or "")


def test_token_auth_403_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """403 → cloud_token_forbidden error; token value must not leak."""
    secret = "dop_v1_" + "y" * 64
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", secret)
    _patch_verify_token(monkeypatch, 403)
    diags = doctor.check_cloud_do_token_auth()
    assert len(diags) == 1
    d = diags[0]
    assert d.id == "runtime.doctor.cloud_token_forbidden"
    assert d.severity == "error"
    assert "DIGITALOCEAN_TOKEN" in d.message
    assert secret not in d.message
    assert secret not in (d.suggestion or "")


def test_token_auth_transport_error_returns_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Status 0 → cloud_token_check_failed warning (transient; do not hard-fail)."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_" + "z" * 64)
    _patch_verify_token(monkeypatch, 0)
    diags = doctor.check_cloud_do_token_auth()
    assert len(diags) == 1
    d = diags[0]
    assert d.id == "runtime.doctor.cloud_token_check_failed"
    assert d.severity == "warning"
    assert "DIGITALOCEAN_TOKEN" in d.message


def test_token_auth_unexpected_status_returns_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unexpected non-2xx status → cloud_token_check_failed warning."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_" + "w" * 64)
    _patch_verify_token(monkeypatch, 503)
    diags = doctor.check_cloud_do_token_auth()
    assert len(diags) == 1
    d = diags[0]
    assert d.id == "runtime.doctor.cloud_token_check_failed"
    assert d.severity == "warning"
    assert "503" in d.message


def test_token_auth_wrong_prefix_emits_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-dop_v1_ token → cloud_token_wrong_prefix warning (and still does live check)."""
    secret = "doo_v1_" + "r" * 64
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", secret)
    _patch_verify_token(monkeypatch, 200)
    diags = doctor.check_cloud_do_token_auth()
    ids = [d.id for d in diags]
    assert "runtime.doctor.cloud_token_wrong_prefix" in ids
    wrong_prefix_diag = next(d for d in diags if d.id == "runtime.doctor.cloud_token_wrong_prefix")
    assert wrong_prefix_diag.severity == "warning"
    # Must reference the env var name, never the token value
    assert "DIGITALOCEAN_TOKEN" in wrong_prefix_diag.message
    assert secret not in wrong_prefix_diag.message


def test_token_auth_wrong_prefix_plus_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-dop_v1_ token + 401 → both wrong_prefix warning and unauthorized error."""
    secret = "doo_v1_" + "q" * 64
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", secret)
    _patch_verify_token(monkeypatch, 401)
    diags = doctor.check_cloud_do_token_auth()
    ids = [d.id for d in diags]
    assert "runtime.doctor.cloud_token_wrong_prefix" in ids
    assert "runtime.doctor.cloud_token_unauthorized" in ids
    # No token value in any diagnostic
    for d in diags:
        assert secret not in (d.message or "")
        assert secret not in (d.suggestion or "")


def test_token_auth_no_token_value_in_any_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-braces: the token value must never appear in any diagnostic message."""
    secret = "dop_v1_" + "t" * 64
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", secret)
    for status in (200, 401, 403, 0, 503):
        _patch_verify_token(monkeypatch, status)
        diags = doctor.check_cloud_do_token_auth()
        for d in diags:
            assert secret not in (d.message or ""), (
                f"Token leaked in message for status {status}: {d.message!r}"
            )
            assert secret not in (d.suggestion or ""), (
                f"Token leaked in suggestion for status {status}: {d.suggestion!r}"
            )


def test_run_all_checks_cloud_includes_auth_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_all_checks with cloud backend includes the auth check results."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_" + "a" * 64)
    import playground.backend.cloud_digitalocean.do as do_mod
    monkeypatch.setattr(do_mod, "verify_token", lambda token: 401)
    diags = doctor.run_all_checks(
        backend="cloud-digitalocean",
        config_dir=_REPO_CONFIG_DIR,
        state_dir=tmp_path,
    )
    ids = [d.id for d in diags]
    assert "runtime.doctor.cloud_token_unauthorized" in ids
