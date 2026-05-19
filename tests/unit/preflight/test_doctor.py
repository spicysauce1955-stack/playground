"""Unit tests for ``playground doctor`` check functions.

Each test monkeypatches the system call the check depends on (PATH
probe, ``virsh`` subprocess, file stat, ``grp.getgrnam``) so the suite
runs identically on any host. The end-to-end "doctor on a real
machine" path is exercised by the CLI test.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from playground.preflight import doctor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _which_factory(present: set[str]):
    """Return a fake ``shutil.which`` that only knows about ``present``."""

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    return fake_which


def _fake_completed(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["virsh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# check_iso_tool
# ---------------------------------------------------------------------------


def test_iso_tool_satisfied_by_genisoimage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"genisoimage"}))
    assert doctor.check_iso_tool() == []


def test_iso_tool_satisfied_by_mkisofs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"mkisofs"}))
    assert doctor.check_iso_tool() == []


def test_iso_tool_missing_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    diagnostics = doctor.check_iso_tool()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.iso_tool_missing"
    assert diagnostics[0].severity == "error"
    assert "genisoimage" in diagnostics[0].suggestion or ""


# ---------------------------------------------------------------------------
# check_libvirt_group_membership
# ---------------------------------------------------------------------------


def _fake_libvirt_group(gid: int = 999, members: tuple[str, ...] = ()):
    class _G:
        gr_name = "libvirt"
        gr_gid = gid
        gr_mem = list(members)

    return _G()


def test_libvirt_group_membership_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.grp, "getgrnam", lambda _n: _fake_libvirt_group(members=("alice",)))
    monkeypatch.setattr(doctor.os, "getuid", lambda: 1000)
    monkeypatch.setattr(doctor.pwd, "getpwuid", lambda _u: type("P", (), {"pw_name": "alice"})())
    monkeypatch.setattr(doctor.os, "getgroups", lambda: [999])
    assert doctor.check_libvirt_group_membership() == []


def test_libvirt_group_membership_user_not_added(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.grp, "getgrnam", lambda _n: _fake_libvirt_group(members=()))
    monkeypatch.setattr(doctor.os, "getuid", lambda: 1000)
    monkeypatch.setattr(doctor.pwd, "getpwuid", lambda _u: type("P", (), {"pw_name": "alice"})())
    monkeypatch.setattr(doctor.os, "getgroups", lambda: [])
    diagnostics = doctor.check_libvirt_group_membership()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.libvirt_group_missing"
    assert "alice" in diagnostics[0].message
    assert "usermod -aG libvirt alice" in (diagnostics[0].suggestion or "")


def test_libvirt_group_membership_session_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    # Added to /etc/group but not in current session — common after
    # `sudo usermod -aG libvirt $USER` without re-logging-in.
    monkeypatch.setattr(doctor.grp, "getgrnam", lambda _n: _fake_libvirt_group(members=("alice",)))
    monkeypatch.setattr(doctor.os, "getuid", lambda: 1000)
    monkeypatch.setattr(doctor.pwd, "getpwuid", lambda _u: type("P", (), {"pw_name": "alice"})())
    monkeypatch.setattr(doctor.os, "getgroups", lambda: [])
    diagnostics = doctor.check_libvirt_group_membership()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.libvirt_group_inactive"
    assert diagnostics[0].severity == "warning"


def test_libvirt_group_membership_group_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(_name: str) -> Any:
        raise KeyError(_name)

    monkeypatch.setattr(doctor.grp, "getgrnam", _missing)
    diagnostics = doctor.check_libvirt_group_membership()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.libvirt_group_missing"
    assert "does not exist" in diagnostics[0].message


# ---------------------------------------------------------------------------
# check_virsh
# ---------------------------------------------------------------------------


def test_check_virsh_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"virsh"}))
    assert doctor.check_virsh() == []


def test_check_virsh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    diagnostics = doctor.check_virsh()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.virsh_missing"


# ---------------------------------------------------------------------------
# check_default_pool
# ---------------------------------------------------------------------------


def test_default_pool_skipped_when_virsh_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    assert doctor.check_default_pool() == []


def test_default_pool_missing_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"virsh"}))
    monkeypatch.setattr(doctor, "_run_virsh", lambda _a, **_k: _fake_completed(stdout="other\n"))
    diagnostics = doctor.check_default_pool()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.default_pool_missing"
    assert "pool-define-as default" in (diagnostics[0].suggestion or "")


def test_default_pool_inactive_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"virsh"}))
    call_log: list[list[str]] = []

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        call_log.append(args)
        if "pool-list" in args:
            return _fake_completed(stdout="default\n")
        # pool-info
        return _fake_completed(
            stdout="Name:           default\nState:          inactive\nAutostart:      no\n"
        )

    monkeypatch.setattr(doctor, "_run_virsh", _stub)
    diagnostics = doctor.check_default_pool()
    ids = {d.id for d in diagnostics}
    assert "runtime.doctor.default_pool_inactive" in ids
    assert "runtime.doctor.default_pool_no_autostart" in ids


def test_default_pool_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"virsh"}))

    def _stub(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        if "pool-list" in args:
            return _fake_completed(stdout="default\n")
        return _fake_completed(
            stdout="Name:           default\nState:          running\nAutostart:      yes\n"
        )

    monkeypatch.setattr(doctor, "_run_virsh", _stub)
    assert doctor.check_default_pool() == []


def test_default_pool_virsh_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"virsh"}))

    def _stub(_args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(returncode=1, stderr="error: failed to connect to socket")

    monkeypatch.setattr(doctor, "_run_virsh", _stub)
    diagnostics = doctor.check_default_pool()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.virsh_unreachable"


# ---------------------------------------------------------------------------
# check_pool_path_permissions
# ---------------------------------------------------------------------------


def test_pool_path_permissions_happy(tmp_path: Path) -> None:
    # /tmp/pytest-of-<user>/ is 0700 in some setups, so build a fresh
    # chain we control and chmod it world-traversable end-to-end.
    chain = tmp_path / "pub-chain"
    chain.mkdir()
    chain.chmod(0o755)
    pool = chain / "pool"
    pool.mkdir()
    pool.chmod(0o755)
    # We can't control /tmp/pytest-of-<user>/, so point check_ at the
    # leaf directly — only check the path under our control by passing
    # a resolved path; check_ walks `.parents` until root and stops if
    # an ancestor blocks. To make this hermetic, monkeypatch off the
    # ancestor walk past `chain` — simpler: skip the test if any
    # ancestor of `chain` is already non-traversable.
    import stat as _stat
    for ancestor in chain.parents:
        if not ancestor.stat().st_mode & _stat.S_IXOTH:
            pytest.skip(f"{ancestor} is not world-traversable; environment-specific")
    assert doctor.check_pool_path_permissions(pool) == []


def test_pool_path_permissions_skipped_when_missing(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does-not-exist"
    assert doctor.check_pool_path_permissions(nonexistent) == []


def test_pool_path_permissions_blocked_by_parent(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    pool = parent / "pool"
    pool.mkdir()
    diagnostics = doctor.check_pool_path_permissions(pool)
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.pool_path_unreadable"
    assert str(parent) in (diagnostics[0].source.path if diagnostics[0].source else "")
    assert "chmod o+x" in (diagnostics[0].suggestion or "")


# ---------------------------------------------------------------------------
# check_ssh_public_key
# ---------------------------------------------------------------------------


def test_ssh_public_key_happy(tmp_path: Path) -> None:
    key = tmp_path / "id_rsa.pub"
    key.write_text("ssh-rsa AAAA fake@host\n")
    assert doctor.check_ssh_public_key(key) == []


def test_ssh_public_key_missing(tmp_path: Path) -> None:
    key = tmp_path / "no-key.pub"
    diagnostics = doctor.check_ssh_public_key(key)
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.ssh_public_key_missing"
    assert "ssh-keygen" in (diagnostics[0].suggestion or "")
    assert str(tmp_path / "no-key") in (diagnostics[0].suggestion or "")


# ---------------------------------------------------------------------------
# check_libvirt_apparmor
# ---------------------------------------------------------------------------


def _patch_apparmor_constants(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profiles_path: Path,
    qemu_conf: Path,
    libvirt_dir: Path,
) -> None:
    monkeypatch.setattr(doctor, "_APPARMOR_PROFILES_FILE", profiles_path)
    monkeypatch.setattr(doctor, "_QEMU_CONF", qemu_conf)
    monkeypatch.setattr(doctor, "_APPARMOR_LIBVIRT_DIR", libvirt_dir)


def test_libvirt_apparmor_skipped_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_apparmor_constants(
        monkeypatch,
        profiles_path=tmp_path / "no-apparmor",
        qemu_conf=tmp_path / "missing.conf",
        libvirt_dir=tmp_path / "no-libvirt",
    )
    assert doctor.check_libvirt_apparmor() == []


def test_libvirt_apparmor_security_driver_none_silences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    qemu_conf = tmp_path / "qemu.conf"
    qemu_conf.write_text(
        "# defaults\n"
        '#security_driver = "apparmor"\n'
        'security_driver = "none"\n'
    )
    profiles = tmp_path / "profiles"
    profiles.write_text("nothing\n")
    _patch_apparmor_constants(
        monkeypatch,
        profiles_path=profiles,
        qemu_conf=qemu_conf,
        libvirt_dir=tmp_path / "no-libvirt",
    )
    assert doctor.check_libvirt_apparmor() == []


def test_libvirt_apparmor_machinery_present_silences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    apparmor_libvirt = tmp_path / "apparmor.d" / "libvirt"
    apparmor_libvirt.mkdir(parents=True)
    profiles = tmp_path / "profiles"
    profiles.write_text("nothing\n")
    _patch_apparmor_constants(
        monkeypatch,
        profiles_path=profiles,
        qemu_conf=tmp_path / "missing.conf",
        libvirt_dir=apparmor_libvirt,
    )
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"apparmor_parser"}))
    assert doctor.check_libvirt_apparmor() == []


def test_libvirt_apparmor_warns_when_neither_path_satisfied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profiles = tmp_path / "profiles"
    profiles.write_text("nothing\n")
    _patch_apparmor_constants(
        monkeypatch,
        profiles_path=profiles,
        qemu_conf=tmp_path / "missing.conf",
        libvirt_dir=tmp_path / "no-libvirt",
    )
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    diagnostics = doctor.check_libvirt_apparmor()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.apparmor_libvirt_unconfigured"
    assert diagnostics[0].severity == "warning"


# ---------------------------------------------------------------------------
# check_ansible_and_collections
# ---------------------------------------------------------------------------


def test_ansible_missing_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    diagnostics = doctor.check_ansible_and_collections()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.ansible_missing"


def test_ansible_collections_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor.shutil, "which", _which_factory({"ansible-playbook", "ansible-galaxy"})
    )
    payload = json.dumps(
        {
            "/usr/share/ansible/collections/ansible_collections": {
                "ansible.posix": {"version": "1.5.4"},
                "community.crypto": {"version": "2.16.0"},
                "community.docker": {"version": "3.4.11"},
            }
        }
    )

    def _stub_run(*_a: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(stdout=payload)

    monkeypatch.setattr(doctor.subprocess, "run", _stub_run)
    assert doctor.check_ansible_and_collections() == []


def test_ansible_collections_missing_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor.shutil, "which", _which_factory({"ansible-playbook", "ansible-galaxy"})
    )
    payload = json.dumps(
        {"/usr/share/ansible/collections/ansible_collections": {"ansible.posix": {}}}
    )

    def _stub_run(*_a: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(stdout=payload)

    monkeypatch.setattr(doctor.subprocess, "run", _stub_run)
    diagnostics = doctor.check_ansible_and_collections()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.ansible_collection_missing"
    assert "community.crypto" in diagnostics[0].message
    assert "community.docker" in diagnostics[0].message
    assert "ansible.posix" not in diagnostics[0].message


def test_ansible_collections_tabular_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor.shutil, "which", _which_factory({"ansible-playbook", "ansible-galaxy"})
    )
    # Pretend `--format json` fails (returncode=2, no stdout). Then the
    # fallback parses the default tabular output.
    invocations: list[list[str]] = []

    def _stub_run(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        invocations.append(args)
        if "--format" in args:
            return _fake_completed(returncode=2, stdout="")
        return _fake_completed(
            stdout=(
                "# /home/u/.ansible/collections/ansible_collections\n"
                "Collection         Version\n"
                "------------------ -------\n"
                "ansible.posix      1.5.4\n"
                "community.crypto   2.16.0\n"
                "community.docker   3.4.11\n"
            )
        )

    monkeypatch.setattr(doctor.subprocess, "run", _stub_run)
    assert doctor.check_ansible_and_collections() == []
    # Sanity: fallback path was exercised
    assert any("--format" not in args for args in invocations)


# ---------------------------------------------------------------------------
# run_all_checks orchestrator
# ---------------------------------------------------------------------------


def test_run_all_checks_concatenates_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from playground.models.diagnostic import Diagnostic

    seen: list[str] = []

    def _make(name: str, did: str):
        def _check(*_args: Any, **_kwargs: Any) -> list[Diagnostic]:
            seen.append(name)
            return [
                Diagnostic(
                    id=did,
                    severity="error",
                    message=name,
                )
            ]

        return _check

    monkeypatch.setattr(doctor, "check_iso_tool", _make("iso", "runtime.doctor.iso_tool_missing"))
    monkeypatch.setattr(doctor, "check_virsh", _make("virsh", "runtime.doctor.virsh_missing"))
    monkeypatch.setattr(
        doctor,
        "check_libvirt_group_membership",
        _make("libvirt-group", "runtime.doctor.libvirt_group_missing"),
    )
    monkeypatch.setattr(
        doctor,
        "check_default_pool",
        _make("pool", "runtime.doctor.default_pool_missing"),
    )
    monkeypatch.setattr(
        doctor,
        "check_pool_path_permissions",
        _make("pool-perms", "runtime.doctor.pool_path_unreadable"),
    )
    monkeypatch.setattr(
        doctor,
        "check_ssh_public_key",
        _make("ssh", "runtime.doctor.ssh_public_key_missing"),
    )
    monkeypatch.setattr(
        doctor,
        "check_libvirt_apparmor",
        _make("apparmor", "runtime.doctor.apparmor_libvirt_unconfigured"),
    )
    monkeypatch.setattr(
        doctor,
        "check_ansible_and_collections",
        _make("ansible", "runtime.doctor.ansible_missing"),
    )

    diagnostics = doctor.run_all_checks()
    assert [d.message for d in diagnostics] == [
        "iso",
        "virsh",
        "libvirt-group",
        "pool",
        "pool-perms",
        "ssh",
        "apparmor",
        "ansible",
    ]
    assert seen == [
        "iso",
        "virsh",
        "libvirt-group",
        "pool",
        "pool-perms",
        "ssh",
        "apparmor",
        "ansible",
    ]
