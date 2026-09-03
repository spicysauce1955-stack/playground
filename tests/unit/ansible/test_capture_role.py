"""The capture role's guest contract, and the three defaults that break it.

Every assertion here encodes a failure that is invisible without a live
guest, which is why they are pinned statically instead:

* `tcpdump` on Debian/Ubuntu drops privileges to the `tcpdump` user by
  default and then cannot write to a root-owned directory (`-Z root`).
* Ubuntu's AppArmor profile confines where `tcpdump` may write, and
  /var/lib/playground is outside it.
* The Redroid container's PID changes across restarts
  (`restart_policy: unless-stopped`), so it must be resolved when the
  unit starts -- never baked in at provision time.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[3]
ROLE = REPO_ROOT / "ansible" / "roles" / "capture"
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
WRAPPER = ROLE / "templates" / "capture-start.j2"
UNIT = ROLE / "templates" / "playground-capture@.service.j2"
APPARMOR = ROLE / "templates" / "apparmor-local-tcpdump.j2"

_yaml = YAML(typ="safe")


def test_tcpdump_drops_no_privileges() -> None:
    """Without -Z root, tcpdump setuid()s to the `tcpdump` user and then
    cannot write into a root-owned capture directory."""
    # Assert the real invocation, not a comment mentioning the flag: a
    # comment can say "-Z root" while the actual command drops it.
    assert "tcpdump -i any -U -Z root" in WRAPPER.read_text()


def test_apparmor_override_grants_the_capture_path() -> None:
    """Ubuntu's usr.sbin.tcpdump profile confines writes. The shipped
    profile includes <local/usr.sbin.tcpdump>, so a local override is the
    supported way to widen it."""
    text = APPARMOR.read_text()
    # Assert the GRANT lines, not the prose: a doc comment mentioning the
    # path would satisfy a substring check while the rule itself pointed
    # somewhere else entirely.
    assert "{{ capture_dir }}/ rw," in text
    assert "{{ capture_dir }}/** rw," in text
    tasks = _yaml.load(TASKS.read_text())
    dests = [
        str(t.get("ansible.builtin.template", {}).get("dest", "")) for t in tasks
    ]
    assert "/etc/apparmor.d/local/usr.sbin.tcpdump" in dests
    assert "apparmor_parser" in TASKS.read_text(), "the override must be reloaded"


def test_container_pid_is_resolved_at_run_time_not_provision_time() -> None:
    """A PID baked into the unit is stale after the first container
    restart. The wrapper must resolve it itself, and the unit must call
    the wrapper rather than tcpdump directly."""
    wrapper = WRAPPER.read_text()
    assert "docker inspect" in wrapper
    assert "State.Pid" in wrapper
    unit = UNIT.read_text()
    assert "ExecStart={{ capture_bin }} %i" in unit
    assert str(_yaml.load(DEFAULTS.read_text())["capture_bin"]).endswith(
        "/capture-start"
    )
    assert "nsenter" not in unit, "nsenter belongs in the wrapper, not the unit"


def test_wrapper_fails_loudly_on_ambiguous_or_absent_container() -> None:
    """Discovery, not a duplicated naming convention. If it finds zero or
    more than one candidate it must exit nonzero with a message, never
    capture the wrong namespace."""
    wrapper = WRAPPER.read_text()
    assert "exit 1" in wrapper
    for token in ("no redroid container", "more than one"):
        assert token in wrapper.lower()


def test_unit_is_instanced_and_restarts() -> None:
    unit = UNIT.read_text()
    # Assert the functional directives, not prose that happens to mention
    # "%i" or "Restart=always" in a comment while the real line drifted.
    # (ExecStart=... %i is already covered by
    # test_container_pid_is_resolved_at_run_time_not_provision_time.)
    assert (
        "Description=playground packet capture for Redroid device %i" in unit
    ), "must be an instanced unit (one session per VM)"
    assert "After=docker.service" in unit
    assert "\nRestart=always\n" in unit


def test_role_does_not_start_capture() -> None:
    """A re-apply must never begin recording, and must never interrupt a
    session already in progress. The unit is enabled-but-stopped."""
    tasks = _yaml.load(TASKS.read_text())
    for task in tasks:
        systemd = task.get("ansible.builtin.systemd", {})
        state = str(systemd.get("state", ""))
        assert state not in ("started", "restarted"), (
            f"task {task['name']!r} sets state={state!r}; provisioning must "
            "not start or restart a capture session"
        )


def test_the_disabled_guard_precedes_the_package_install() -> None:
    """`capture.enabled: false` must skip provisioning ENTIRELY. If the
    end_host guard drifts below the apt task, a disabled lab silently
    installs tcpdump again -- and nothing else in this file would notice.
    """
    tasks = _yaml.load(TASKS.read_text())
    meta_i = next(i for i, t in enumerate(tasks) if "ansible.builtin.meta" in t)
    apt_i = next(i for i, t in enumerate(tasks) if "ansible.builtin.apt" in t)
    assert meta_i < apt_i


def test_every_apt_task_tolerates_an_unreachable_archive() -> None:
    tasks = _yaml.load(TASKS.read_text())
    apt_tasks = [t for t in tasks if "ansible.builtin.apt" in t]
    assert apt_tasks, "expected an apt task installing tcpdump"
    for t in apt_tasks:
        assert t.get("failed_when") is False, (
            f"apt task {t['name']!r} can abort the play on an unreachable "
            "archive; gate on a probe instead"
        )


def test_a_probe_gates_on_tcpdump_actually_being_present() -> None:
    text = TASKS.read_text()
    assert "tcpdump --version" in text
    fails = [t for t in _yaml.load(text) if "ansible.builtin.fail" in t]
    assert fails, "expected an abort when tcpdump is missing"
    assert any(
        "capture_install_tcpdump" in str(f["ansible.builtin.fail"]["msg"])
        for f in fails
    ), "the abort must name the opt-out variable"


def test_defaults_declare_the_opt_out_and_paths() -> None:
    defaults = _yaml.load(DEFAULTS.read_text())
    assert defaults["capture_install_tcpdump"] is True
    assert defaults["capture_dir"] == "/var/lib/playground/capture"


def test_ansible_and_python_agree_on_the_guest_contract() -> None:
    """The pcap path and unit name are each written twice -- once here in
    the Ansible role, once in `playground.capture.commands` -- because
    Ansible cannot import Python. A value duplicated across layers is
    exactly the "implicit cross-layer dependency hidden by a hardcoded
    value" shape `docs/architecture/CONTRACTS.md` exists to catch: if
    these drift, the guest writes pcaps where the CLI does not look and
    `capture fetch` silently returns nothing at all. The duplication is
    unavoidable; leaving it unenforced is not.
    """
    from playground.capture.commands import CAPTURE_DIR, unit_name

    assert _yaml.load(DEFAULTS.read_text())["capture_dir"] == CAPTURE_DIR
    # The template's filename IS the instance template the CLI names.
    assert UNIT.name == "playground-capture@.service.j2"
    assert unit_name("droid1") == "playground-capture@droid1.service"
