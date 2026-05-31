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


def test_libvirt_apparmor_silent_when_dir_has_only_stock_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty (or stock-only) libvirt dir means no VMs are defined
    yet; nothing to verify, so no diagnostic."""
    apparmor_libvirt = tmp_path / "apparmor.d" / "libvirt"
    apparmor_libvirt.mkdir(parents=True)
    # Stock distro ships these; they aren't per-VM profiles and don't
    # need .files companions.
    (apparmor_libvirt / "libvirt-qemu").write_text("# abstraction\n")
    (apparmor_libvirt / "TEMPLATE.qemu").write_text("# template\n")
    profiles = tmp_path / "profiles"
    profiles.write_text("nothing\n")
    _patch_apparmor_constants(
        monkeypatch,
        profiles_path=profiles,
        qemu_conf=tmp_path / "missing.conf",
        libvirt_dir=apparmor_libvirt,
    )
    assert doctor.check_libvirt_apparmor() == []


def test_libvirt_apparmor_silent_when_every_profile_has_a_companion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    apparmor_libvirt = tmp_path / "apparmor.d" / "libvirt"
    apparmor_libvirt.mkdir(parents=True)
    for uuid in ("aaaa-1111", "bbbb-2222"):
        (apparmor_libvirt / f"libvirt-{uuid}").write_text("profile {}\n")
        (apparmor_libvirt / f"libvirt-{uuid}.files").write_text("/some/disk r,\n")
    profiles = tmp_path / "profiles"
    profiles.write_text("nothing\n")
    _patch_apparmor_constants(
        monkeypatch,
        profiles_path=profiles,
        qemu_conf=tmp_path / "missing.conf",
        libvirt_dir=apparmor_libvirt,
    )
    assert doctor.check_libvirt_apparmor() == []


def test_libvirt_apparmor_errors_on_orphan_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `libvirt-<uuid>` profile WITHOUT a matching `.files` companion
    is the virt-aa-helper-broken signature. Must be reported as ERROR."""
    apparmor_libvirt = tmp_path / "apparmor.d" / "libvirt"
    apparmor_libvirt.mkdir(parents=True)
    # Pair that's fine
    (apparmor_libvirt / "libvirt-good-uuid").write_text("profile {}\n")
    (apparmor_libvirt / "libvirt-good-uuid.files").write_text("/disk r,\n")
    # Orphan profile — virt-aa-helper failed to generate the companion
    (apparmor_libvirt / "libvirt-broken-uuid").write_text("profile {}\n")
    profiles = tmp_path / "profiles"
    profiles.write_text("nothing\n")
    _patch_apparmor_constants(
        monkeypatch,
        profiles_path=profiles,
        qemu_conf=tmp_path / "missing.conf",
        libvirt_dir=apparmor_libvirt,
    )
    diagnostics = doctor.check_libvirt_apparmor()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.apparmor_orphan_profiles"
    assert diagnostics[0].severity == "error"
    assert "libvirt-broken-uuid" in diagnostics[0].message
    # The good pair must NOT appear in the orphan list.
    assert "libvirt-good-uuid" not in diagnostics[0].message
    assert "virt-aa-helper" in (diagnostics[0].suggestion or "")


def test_libvirt_apparmor_error_silenced_by_security_driver_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit opt-out wins even when orphan profiles exist — the
    operator told libvirt to skip AppArmor entirely."""
    apparmor_libvirt = tmp_path / "apparmor.d" / "libvirt"
    apparmor_libvirt.mkdir(parents=True)
    (apparmor_libvirt / "libvirt-orphan").write_text("profile {}\n")
    qemu_conf = tmp_path / "qemu.conf"
    qemu_conf.write_text('security_driver = "none"\n')
    profiles = tmp_path / "profiles"
    profiles.write_text("nothing\n")
    _patch_apparmor_constants(
        monkeypatch,
        profiles_path=profiles,
        qemu_conf=qemu_conf,
        libvirt_dir=apparmor_libvirt,
    )
    assert doctor.check_libvirt_apparmor() == []


def test_libvirt_apparmor_warns_when_libvirt_dir_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Distinct from the orphan case: the per-VM dir doesn't exist at
    all. Rare on stock distros; keep it as a warning."""
    profiles = tmp_path / "profiles"
    profiles.write_text("nothing\n")
    _patch_apparmor_constants(
        monkeypatch,
        profiles_path=profiles,
        qemu_conf=tmp_path / "missing.conf",
        libvirt_dir=tmp_path / "no-libvirt",
    )
    diagnostics = doctor.check_libvirt_apparmor()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.apparmor_libvirt_unconfigured"
    assert diagnostics[0].severity == "warning"


def test_libvirt_apparmor_lists_at_most_three_orphans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When many orphans are present the message names a few and
    summarizes the rest so it stays scannable."""
    apparmor_libvirt = tmp_path / "apparmor.d" / "libvirt"
    apparmor_libvirt.mkdir(parents=True)
    for i in range(7):
        (apparmor_libvirt / f"libvirt-orphan-{i:02d}").write_text("profile {}\n")
    profiles = tmp_path / "profiles"
    profiles.write_text("nothing\n")
    _patch_apparmor_constants(
        monkeypatch,
        profiles_path=profiles,
        qemu_conf=tmp_path / "missing.conf",
        libvirt_dir=apparmor_libvirt,
    )
    diagnostics = doctor.check_libvirt_apparmor()
    assert len(diagnostics) == 1
    assert "7 total" in diagnostics[0].message


# ---------------------------------------------------------------------------
# check_ansible_config
# ---------------------------------------------------------------------------


_CANONICAL_ANSIBLE_CFG = (
    "[defaults]\n"
    "host_key_checking = False\n"
    "interpreter_python = auto_silent\n"
    "\n"
    "[ssh_connection]\n"
    "ssh_args = -o ControlMaster=auto -o ControlPersist=60s -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=accept-new\n"
    "pipelining = True\n"
)


def test_ansible_config_missing_emits_warning(tmp_path: Path) -> None:
    diagnostics = doctor.check_ansible_config(repo_root=tmp_path)
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.ansible_cfg_missing"
    assert diagnostics[0].severity == "warning"
    assert "host_key_checking" in (diagnostics[0].suggestion or "")


def test_ansible_config_no_arg_resolves_to_playground_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Issue 4 (2026-05-28): when called with no repo_root, the check
    must resolve to the PLAYGROUND install dir — not CWD. Otherwise
    running `playground doctor` from a downstream project (one that
    uses playground as a black-box infra tool) emits a misleading
    warning about ``<cwd>/ansible/ansible.cfg``."""
    # Simulate being inside a downstream project: CWD has no
    # ansible.cfg and no src/playground/ marker.
    monkeypatch.chdir(tmp_path)
    # Without repo_root: should use the playground repo's ansible.cfg
    # (this test runs *from* a playground checkout, so __file__ walks
    # back to it). That file ships in the repo and is well-formed, so
    # we expect zero diagnostics — no false positive.
    diagnostics = doctor.check_ansible_config()
    assert diagnostics == [], (
        f"expected no false-positive warning from non-playground CWD; got: "
        f"{[(d.id, d.message) for d in diagnostics]}"
    )


def test_ansible_config_complete_silences(tmp_path: Path) -> None:
    (tmp_path / "ansible").mkdir()
    (tmp_path / "ansible" / "ansible.cfg").write_text(_CANONICAL_ANSIBLE_CFG)
    assert doctor.check_ansible_config(repo_root=tmp_path) == []


def test_ansible_config_missing_host_key_checking(tmp_path: Path) -> None:
    (tmp_path / "ansible").mkdir()
    (tmp_path / "ansible" / "ansible.cfg").write_text(
        _CANONICAL_ANSIBLE_CFG.replace(
            "host_key_checking = False\n", ""
        )
    )
    diagnostics = doctor.check_ansible_config(repo_root=tmp_path)
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.ansible_cfg_misconfigured"
    assert "host_key_checking" in diagnostics[0].message


def test_ansible_config_missing_pipelining(tmp_path: Path) -> None:
    (tmp_path / "ansible").mkdir()
    (tmp_path / "ansible" / "ansible.cfg").write_text(
        _CANONICAL_ANSIBLE_CFG.replace("pipelining = True\n", "")
    )
    diagnostics = doctor.check_ansible_config(repo_root=tmp_path)
    assert len(diagnostics) == 1
    assert "pipelining" in diagnostics[0].message


def test_ansible_config_missing_controlmaster(tmp_path: Path) -> None:
    (tmp_path / "ansible").mkdir()
    (tmp_path / "ansible" / "ansible.cfg").write_text(
        _CANONICAL_ANSIBLE_CFG.replace("ControlMaster=auto -o ", "")
    )
    diagnostics = doctor.check_ansible_config(repo_root=tmp_path)
    assert len(diagnostics) == 1
    assert "ControlMaster" in diagnostics[0].message


def test_ansible_config_accepts_arbitrary_spacing(tmp_path: Path) -> None:
    """Operators write `key=value` and `key  =  value` both — the
    check should accept either."""
    (tmp_path / "ansible").mkdir()
    (tmp_path / "ansible" / "ansible.cfg").write_text(
        "[defaults]\n"
        "host_key_checking=False\n"   # no spaces
        "[ssh_connection]\n"
        "ssh_args = -o ControlMaster=auto -o ControlPersist=60s\n"
        "pipelining  =  True\n"  # extra spaces
    )
    assert doctor.check_ansible_config(repo_root=tmp_path) == []


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
# check_cloud_init_on_image (Move 2)
# ---------------------------------------------------------------------------


_VARIABLES_TF_TEMPLATE = '''\
variable "ubuntu_image_url" {{
  description = "Source for the Ubuntu Cloud Image."
  type        = string
  default     = "{url}"
}}
'''


def test_cloud_init_image_recognized_silences(tmp_path: Path) -> None:
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()
    (tofu_dir / "variables.tf").write_text(
        _VARIABLES_TF_TEMPLATE.format(
            url="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
        )
    )
    assert doctor.check_cloud_init_on_image(tofu_dir=tofu_dir) == []


def test_cloud_init_image_unrecognized_warns(tmp_path: Path) -> None:
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()
    (tofu_dir / "variables.tf").write_text(
        _VARIABLES_TF_TEMPLATE.format(
            url="https://example.com/my-custom-vanilla-server.iso"
        )
    )
    diagnostics = doctor.check_cloud_init_on_image(tofu_dir=tofu_dir)
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.cloud_init_image_unverified"
    assert diagnostics[0].severity == "warning"
    assert "my-custom-vanilla-server.iso" in diagnostics[0].message


def test_cloud_init_image_skipped_when_tofu_missing(tmp_path: Path) -> None:
    # No tofu/ subdir → check returns empty silently.
    assert doctor.check_cloud_init_on_image(tofu_dir=tmp_path / "absent") == []


def test_cloud_init_image_warns_on_parse_failure(tmp_path: Path) -> None:
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()
    (tofu_dir / "variables.tf").write_text("# no variable block here\n")
    diagnostics = doctor.check_cloud_init_on_image(tofu_dir=tofu_dir)
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.cloud_init_image_unverified"
    assert "could not parse" in diagnostics[0].message


# ---------------------------------------------------------------------------
# check_ansible_config_wired (Move 2)
# ---------------------------------------------------------------------------


def test_ansible_config_wired_silences_when_kwarg_present(tmp_path: Path) -> None:
    runner = tmp_path / "runner.py"
    runner.write_text(
        "from playground.backend.local_libvirt.apply import run_ansible_playbook\n"
        "step, diag = run_ansible_playbook(playbook, inventory, log,\n"
        "    cwd=cwd, bus=bus, ansible_cfg=cfg)\n"
    )
    assert doctor.check_ansible_config_wired(runner_path=runner) == []


def test_ansible_config_wired_warns_when_kwarg_absent(tmp_path: Path) -> None:
    runner = tmp_path / "runner.py"
    runner.write_text(
        "from playground.backend.local_libvirt.apply import run_ansible_playbook\n"
        "step, diag = run_ansible_playbook(playbook, inventory, log, cwd=cwd)\n"
    )
    diagnostics = doctor.check_ansible_config_wired(runner_path=runner)
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.ansible_config_not_wired"
    assert "ansible/ansible.cfg" in diagnostics[0].message


def test_ansible_config_wired_skipped_when_runner_missing(tmp_path: Path) -> None:
    assert doctor.check_ansible_config_wired(runner_path=tmp_path / "nope.py") == []


# ---------------------------------------------------------------------------
# check_tofu_state_alignment (Move 2)
# ---------------------------------------------------------------------------


def test_tofu_state_drift_skipped_when_tofu_dir_absent(tmp_path: Path) -> None:
    assert doctor.check_tofu_state_alignment(tofu_dir=tmp_path / "absent") == []


def test_tofu_state_drift_skipped_when_tofu_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    assert doctor.check_tofu_state_alignment(tofu_dir=tofu_dir) == []


def test_tofu_state_drift_silent_on_empty_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty state list = no prior apply = no drift to report."""
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/tofu")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_a, **_kw: _fake_completed(stdout="\n"),
    )
    assert doctor.check_tofu_state_alignment(tofu_dir=tofu_dir) == []


def test_tofu_state_drift_silent_on_playground_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """State has the expected playground domain entries → no drift."""
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/tofu")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_a, **_kw: _fake_completed(
            stdout=(
                "libvirt_network.lab[\"playground_net\"]\n"
                "libvirt_volume.ubuntu_image\n"
                "libvirt_domain.playground_node[0]\n"
            )
        ),
    )
    assert doctor.check_tofu_state_alignment(tofu_dir=tofu_dir) == []


def test_tofu_state_drift_warns_when_no_playground_domain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """State has entries but none match playground's tofu module."""
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/tofu")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_a, **_kw: _fake_completed(
            stdout="random_resource.foo\nanother_resource.bar\n"
        ),
    )
    diagnostics = doctor.check_tofu_state_alignment(tofu_dir=tofu_dir)
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.tofu_state_drift"
    assert "2 entries" in diagnostics[0].message


def test_tofu_state_drift_warns_on_command_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir()
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/tofu")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_a, **_kw: _fake_completed(
            returncode=1, stderr="error: state backend unreachable"
        ),
    )
    diagnostics = doctor.check_tofu_state_alignment(tofu_dir=tofu_dir)
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.tofu_state_drift"
    assert "state backend unreachable" in diagnostics[0].message


# ---------------------------------------------------------------------------
# check_xsltproc
# ---------------------------------------------------------------------------


def test_xsltproc_silent_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply via `cpu_features_disable` needs xsltproc; when it's
    installed the probe must stay quiet."""
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"xsltproc"}))
    assert doctor.check_xsltproc() == []


def test_xsltproc_warns_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug D (2026-05-28): xsltproc was a silent dependency of the
    rung-1 escape hatch; apply failed at domain creation with
    "exec: xsltproc: executable file not found in $PATH" on hosts where
    the binary wasn't installed. Doctor surfaces it in preflight as a
    warning — libvirt-only operators that never reach for rung 1 are
    not blocked, but those that do get a clear early signal."""
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    diagnostics = doctor.check_xsltproc()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.xsltproc_missing"
    assert diagnostics[0].severity == "warning"
    # Message must teach the operator both the scope (only
    # cpu_features_disable labs) and the exact failure mode.
    msg = diagnostics[0].message
    assert "cpu_features_disable" in msg
    assert "xsltproc" in msg
    suggestion = diagnostics[0].suggestion or ""
    assert "apt install" in suggestion
    assert "xsltproc" in suggestion


# ---------------------------------------------------------------------------
# check_kvm_nested_enabled
# ---------------------------------------------------------------------------


def test_nested_silent_when_neither_module_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """All vendor modules missing → KVM isn't even loaded; bigger
    problems are surfaced elsewhere by check_virsh / check_default_pool.
    Nested check must stay silent here."""
    monkeypatch.setattr(doctor, "_KVM_INTEL_NESTED", tmp_path / "intel-missing")
    monkeypatch.setattr(doctor, "_KVM_AMD_NESTED", tmp_path / "amd-missing")
    assert doctor.check_kvm_nested_enabled() == []


def test_nested_silent_when_kvm_intel_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    intel = tmp_path / "kvm_intel_nested"
    intel.write_text("Y\n")
    monkeypatch.setattr(doctor, "_KVM_INTEL_NESTED", intel)
    monkeypatch.setattr(doctor, "_KVM_AMD_NESTED", tmp_path / "amd-missing")
    assert doctor.check_kvm_nested_enabled() == []


def test_nested_warns_when_kvm_intel_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The canonical signature of a host that loads kvm_intel but with
    `nested=0` set in modprobe. Apply still runs, but anything that
    needs nested-virt features can't work."""
    intel = tmp_path / "kvm_intel_nested"
    intel.write_text("N\n")
    monkeypatch.setattr(doctor, "_KVM_INTEL_NESTED", intel)
    monkeypatch.setattr(doctor, "_KVM_AMD_NESTED", tmp_path / "amd-missing")
    diagnostics = doctor.check_kvm_nested_enabled()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.nested_disabled"
    assert diagnostics[0].severity == "warning"
    suggestion = diagnostics[0].suggestion or ""
    assert "options kvm_intel nested=1" in suggestion
    # The suggestion must teach the operator about the escape hatches
    # when L0 makes nested impossible.
    assert "cpu_features_disable" in suggestion
    assert "domain_type" in suggestion


def test_nested_reads_one_as_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Linux 6.x kvm_intel reports nested as `1` rather than `Y` —
    handle both."""
    intel = tmp_path / "kvm_intel_nested"
    intel.write_text("1\n")
    monkeypatch.setattr(doctor, "_KVM_INTEL_NESTED", intel)
    monkeypatch.setattr(doctor, "_KVM_AMD_NESTED", tmp_path / "amd-missing")
    assert doctor.check_kvm_nested_enabled() == []


# ---------------------------------------------------------------------------
# check_no_recent_vmx_failures
# ---------------------------------------------------------------------------


def test_vmx_failures_silent_when_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither dmesg nor journalctl on PATH — probe fail-opens."""
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    assert doctor.check_no_recent_vmx_failures() == []


def _make_run_router(*, dmesg_stdout: str = "", dmesg_rc: int = 0,
                     journal_stdout: str = "", journal_rc: int = 0):
    """Build a subprocess.run replacement that branches on argv[0]."""

    def _run(args, **_kwargs):
        if args[0] == "dmesg":
            return _fake_completed(returncode=dmesg_rc, stdout=dmesg_stdout)
        if args[0] == "journalctl":
            return _fake_completed(returncode=journal_rc, stdout=journal_stdout)
        return _fake_completed(returncode=0, stdout="")

    return _run


def test_vmx_failures_silent_when_dmesg_and_journal_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor.shutil, "which", _which_factory({"dmesg", "journalctl"}),
    )
    monkeypatch.setattr(doctor.subprocess, "run", _make_run_router())
    assert doctor.check_no_recent_vmx_failures() == []


def test_vmx_failures_warns_on_dmesg_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Primary path: dmesg is readable and contains vmwrite errors —
    no journalctl call needed."""
    monkeypatch.setattr(
        doctor.shutil, "which", _which_factory({"dmesg", "journalctl"}),
    )
    monkeypatch.setattr(
        doctor.subprocess, "run",
        _make_run_router(dmesg_stdout=(
            "[Wed May 28 11:09:05 2026] vmread failed: field=4400\n"
            "[Wed May 28 11:09:05 2026] vmwrite failed: field=4812 val=ffff err=0\n"
        )),
    )
    diagnostics = doctor.check_no_recent_vmx_failures()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.kvm_intel_recent_failures"
    assert diagnostics[0].severity == "warning"
    suggestion = diagnostics[0].suggestion or ""
    assert "cpu_features_disable" in suggestion
    assert "domain_type" in suggestion
    # Message clarifies the host could be L0 OR L1 — bob-lnx is L0
    # bare-metal yet still hits these errors.
    msg = diagnostics[0].message
    assert "nested-VMX" in msg


def test_vmx_failures_falls_back_to_journalctl_when_dmesg_restricted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug C (2026-05-28): Ubuntu's default kernel.dmesg_restrict=1
    makes dmesg return rc=1 with empty stdout for non-root users.
    The probe must then fall through to journalctl rather than
    declaring 'no failures.'"""
    monkeypatch.setattr(
        doctor.shutil, "which", _which_factory({"dmesg", "journalctl"}),
    )
    monkeypatch.setattr(
        doctor.subprocess, "run",
        _make_run_router(
            dmesg_rc=1, dmesg_stdout="",
            journal_stdout=(
                "May 28 11:09:05 bob-lnx kernel: vmread failed: field=4400\n"
                "May 28 11:09:05 bob-lnx kernel: vmwrite failed: field=4812\n"
            ),
        ),
    )
    diagnostics = doctor.check_no_recent_vmx_failures()
    assert len(diagnostics) == 1
    assert "vmread failed" in diagnostics[0].message


def test_vmx_failures_uses_24_hour_window_not_1_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug C root cause: a 1-hour window missed the failures because
    the operator wedged apply, fixed things, then ran doctor 8+ hours
    later. Lock in the wider window via the journalctl args."""
    seen_args: list[list[str]] = []

    def _capture(args, **_kwargs):
        seen_args.append(args)
        if args[0] == "dmesg":
            return _fake_completed(returncode=1, stdout="")
        return _fake_completed(returncode=0, stdout="")

    monkeypatch.setattr(
        doctor.shutil, "which", _which_factory({"dmesg", "journalctl"}),
    )
    monkeypatch.setattr(doctor.subprocess, "run", _capture)
    doctor.check_no_recent_vmx_failures()

    journal_calls = [a for a in seen_args if a[0] == "journalctl"]
    assert journal_calls, "journalctl fallback never invoked"
    assert "24 hours ago" in journal_calls[0]


def test_vmx_failures_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor.shutil, "which", _which_factory({"dmesg", "journalctl"}),
    )

    def _timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd=["journalctl"], timeout=5.0)

    monkeypatch.setattr(doctor.subprocess, "run", _timeout)
    # Both dmesg and journalctl timing out must not crash doctor.
    assert doctor.check_no_recent_vmx_failures() == []


# ---------------------------------------------------------------------------
# check_running_inside_hypervisor
# ---------------------------------------------------------------------------


def test_virtualized_silent_when_detector_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_factory(set()))
    assert doctor.check_running_inside_hypervisor() == []


def test_virtualized_silent_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare metal — exit 1, stdout "none"."""
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"systemd-detect-virt"}))
    monkeypatch.setattr(
        doctor.subprocess, "run",
        lambda *_a, **_kw: _fake_completed(returncode=1, stdout="none\n"),
    )
    assert doctor.check_running_inside_hypervisor() == []


def test_virtualized_emits_info_when_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    """L1 detection: we're a VM inside a hypervisor. Info-level — never
    blocking, just informational so operators see the topology."""
    monkeypatch.setattr(doctor.shutil, "which", _which_factory({"systemd-detect-virt"}))
    monkeypatch.setattr(
        doctor.subprocess, "run",
        lambda *_a, **_kw: _fake_completed(returncode=0, stdout="kvm\n"),
    )
    diagnostics = doctor.check_running_inside_hypervisor()
    assert len(diagnostics) == 1
    assert diagnostics[0].id == "runtime.doctor.host_is_virtualized"
    assert diagnostics[0].severity == "info"
    assert "'kvm'" in diagnostics[0].message
    assert "nested_virtualization.md" in (diagnostics[0].suggestion or "")


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
    monkeypatch.setattr(
        doctor,
        "check_ansible_config",
        _make("ansible-cfg", "runtime.doctor.ansible_cfg_missing"),
    )
    monkeypatch.setattr(
        doctor,
        "check_cloud_init_on_image",
        _make("cloud-init", "runtime.doctor.cloud_init_image_unverified"),
    )
    monkeypatch.setattr(
        doctor,
        "check_ansible_config_wired",
        _make("ansible-wired", "runtime.doctor.ansible_config_not_wired"),
    )
    monkeypatch.setattr(
        doctor,
        "check_tofu_state_alignment",
        _make("tofu-drift", "runtime.doctor.tofu_state_drift"),
    )
    # New nested-virt probes also get mocked so the orchestrator test
    # is host-independent (the real probes shell out to journalctl /
    # systemd-detect-virt / sysfs).
    monkeypatch.setattr(
        doctor,
        "check_xsltproc",
        _make("xsltproc", "runtime.doctor.xsltproc_missing"),
    )
    monkeypatch.setattr(
        doctor,
        "check_kvm_nested_enabled",
        _make("nested", "runtime.doctor.nested_disabled"),
    )
    monkeypatch.setattr(
        doctor,
        "check_no_recent_vmx_failures",
        _make("vmx-fail", "runtime.doctor.kvm_intel_recent_failures"),
    )
    monkeypatch.setattr(
        doctor,
        "check_running_inside_hypervisor",
        _make("virt-host", "runtime.doctor.host_is_virtualized"),
    )

    diagnostics = doctor.run_all_checks()
    # Filter out vbox/qemu_img diagnostics — those probes still run
    # against the real host (pre-existing coverage gap from the vbox
    # slice; intentionally not fixed here).
    mocked_ids = {
        "runtime.doctor.iso_tool_missing",
        "runtime.doctor.virsh_missing",
        "runtime.doctor.libvirt_group_missing",
        "runtime.doctor.default_pool_missing",
        "runtime.doctor.pool_path_unreadable",
        "runtime.doctor.ssh_public_key_missing",
        "runtime.doctor.apparmor_libvirt_unconfigured",
        "runtime.doctor.ansible_missing",
        "runtime.doctor.ansible_cfg_missing",
        "runtime.doctor.cloud_init_image_unverified",
        "runtime.doctor.ansible_config_not_wired",
        "runtime.doctor.tofu_state_drift",
        "runtime.doctor.xsltproc_missing",
        "runtime.doctor.nested_disabled",
        "runtime.doctor.kvm_intel_recent_failures",
        "runtime.doctor.host_is_virtualized",
    }
    observed = [d.message for d in diagnostics if d.id in mocked_ids]
    expected = [
        "iso", "virsh", "libvirt-group", "pool", "pool-perms",
        "ssh", "apparmor", "ansible", "ansible-cfg", "cloud-init",
        "ansible-wired", "tofu-drift",
        "xsltproc",
        "nested", "vmx-fail", "virt-host",
    ]
    assert observed == expected
    assert seen == expected
