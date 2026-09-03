"""Guards for the redroid role's binder remediation.

Ubuntu cloud images build binder as a module in linux-modules-extra, which
is not installed by default. Without remediation the role's binder
assertion fails on every stock cloud image (and on the repo's own Noble
libvirt guests). These tests pin the remediation and its persistence so a
refactor cannot silently drop them.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKS = REPO_ROOT / "ansible" / "roles" / "redroid" / "tasks" / "main.yml"
DEFAULTS = REPO_ROOT / "ansible" / "roles" / "redroid" / "defaults" / "main.yml"

_yaml = YAML(typ="safe")


def _tasks() -> list[dict]:
    return _yaml.load(TASKS.read_text())


def test_opt_out_variable_defaults_to_true() -> None:
    defaults = _yaml.load(DEFAULTS.read_text())
    assert defaults["redroid_install_kernel_modules"] is True


def test_installs_modules_extra_for_every_installed_kernel() -> None:
    """LIVE BUG 2026-08-31: cloud-init's package_upgrade stages a NEWER
    kernel than the one running at apply time. Installing modules-extra
    only for the running kernel means the next reboot boots a kernel with
    no binder at all."""
    install = [
        t for t in _tasks()
        if "linux-modules-extra" in str(t.get("ansible.builtin.apt", ""))
    ]
    assert len(install) == 1, "expected exactly one modules-extra install task"
    task = install[0]
    assert "loop" in task, "must loop over every installed kernel, not just ansible_kernel"
    assert "installed_kernels" in str(task["loop"])
    assert "redroid_install_kernel_modules" in str(task["when"]), (
        "must honor the air-gap opt-out"
    )


def test_enumerates_installed_kernels_from_lib_modules() -> None:
    text = TASKS.read_text()
    assert "/lib/modules" in text
    assert "installed_kernels" in text


def test_module_load_is_persisted_across_reboot() -> None:
    text = TASKS.read_text()
    assert "/etc/modules-load.d/" in text, "modprobe must survive a reboot"
    assert "/etc/modprobe.d/" in text, "the devices= parameter must survive a reboot"


def test_binder_is_reprobed_after_remediation() -> None:
    """A single probe cannot prove remediation worked."""
    probes = [
        t for t in _tasks()
        if "grep -qw binder /proc/filesystems" in str(t.get("ansible.builtin.command", ""))
    ]
    assert len(probes) == 2, "expected a probe before and after remediation"


def test_abort_message_mentions_the_attempted_remediation() -> None:
    text = TASKS.read_text()
    assert "linux-modules-extra" in text
    # Select the BINDER abort by content rather than by count: the role
    # legitimately has more than one abort now (adb presence is a separate
    # honest gate), and counting would make any future gate a failure here.
    fail_tasks = [t for t in _tasks() if "ansible.builtin.fail" in t]
    binder_aborts = [
        f for f in fail_tasks
        if "binder" in str(f["ansible.builtin.fail"]["msg"]).lower()
    ]
    assert len(binder_aborts) == 1, "expected exactly one binder abort"
    msg = binder_aborts[0]["ansible.builtin.fail"]["msg"]
    assert "modules-extra" in msg, "the abort must say remediation was already tried"


def test_ashmem_modprobe_stays_best_effort() -> None:
    """ashmem_linux does not exist on kernels >= 5.18; Redroid uses memfd."""
    text = TASKS.read_text()
    assert "ashmem_linux" in text
    ashmem_tasks = [t for t in _tasks() if "ashmem" in str(t)]
    assert any(t.get("ignore_errors") is True for t in ashmem_tasks)


def test_binderfs_is_never_persisted_in_fstab() -> None:
    """LIVE BUG 2026-08-31, high severity: an fstab entry for /dev/binderfs
    is attempted at local-fs.target, BEFORE systemd-modules-load has loaded
    binder_linux, and /dev is a devtmpfs so the mountpoint does not exist.
    The mount fails, the boot degrades to `maintenance`, and sshd never
    starts -- the VM is unreachable until a hard power-cycle."""
    mount_tasks = [t for t in _tasks() if "ansible.posix.mount" in t]
    for task in mount_tasks:
        state = task["ansible.posix.mount"].get("state")
        assert state == "absent_from_fstab", (
            "the only permitted ansible.posix.mount use is removing a stale "
            f"fstab entry; got state={state!r}"
        )


def test_binderfs_mount_unit_is_ordered_after_module_load_and_before_docker() -> None:
    unit = (
        REPO_ROOT / "ansible" / "roles" / "redroid" / "templates"
        / "redroid-binderfs.service.j2"
    ).read_text()
    assert "After=systemd-modules-load.service" in unit, (
        "the mount must not race module loading"
    )
    assert "Before=docker.service" in unit, (
        "binderfs must be mounted before the redroid container starts"
    )
    assert "RemainAfterExit=yes" in unit


def test_mount_unit_is_enabled_so_it_survives_reboot() -> None:
    systemd_tasks = [t for t in _tasks() if "ansible.builtin.systemd" in t]
    # ruamel's safe loader is YAML 1.2, where `yes` is the STRING "yes";
    # Ansible parses YAML 1.1, where it is boolean true. Accept both.
    truthy = (True, "yes", "true")
    enabled = [
        t for t in systemd_tasks
        if t["ansible.builtin.systemd"].get("enabled") in truthy
        and "binderfs" in str(t["ansible.builtin.systemd"].get("name", ""))
    ]
    assert enabled, "the binderfs unit must be enabled, not just started"
