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


def test_installs_modules_extra_for_the_running_kernel() -> None:
    """The package must be pinned to ansible_kernel, not 'latest'."""
    text = TASKS.read_text()
    assert "linux-modules-extra-{{ ansible_kernel }}" in text


def test_modules_extra_install_is_gated_on_the_opt_out_and_a_missing_binder() -> None:
    install = [
        t for t in _tasks()
        if "linux-modules-extra" in str(t.get("ansible.builtin.apt", ""))
    ]
    assert len(install) == 1, "expected exactly one modules-extra install task"
    when = str(install[0]["when"])
    assert "redroid_install_kernel_modules" in when, "must honor the air-gap opt-out"
    assert "binder_check" in when, "must only run when binder is actually missing"


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
    fail_tasks = [t for t in _tasks() if "ansible.builtin.fail" in t]
    assert len(fail_tasks) == 1
    msg = fail_tasks[0]["ansible.builtin.fail"]["msg"]
    assert "modules-extra" in msg, "the abort must say remediation was already tried"


def test_ashmem_modprobe_stays_best_effort() -> None:
    """ashmem_linux does not exist on kernels >= 5.18; Redroid uses memfd."""
    text = TASKS.read_text()
    assert "ashmem_linux" in text
    ashmem_tasks = [t for t in _tasks() if "ashmem" in str(t)]
    assert any(t.get("ignore_errors") is True for t in ashmem_tasks)
