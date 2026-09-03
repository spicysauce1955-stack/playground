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


_SYSTEMD_MODULES = (
    "ansible.builtin.systemd",
    "ansible.builtin.systemd_service",
    "ansible.builtin.service",
)
_SHELL_MODULES = (
    "ansible.builtin.command",
    "ansible.builtin.shell",
    "command",
    "shell",
)


def test_role_does_not_start_capture() -> None:
    """A re-apply must never begin recording, and must never interrupt a
    session already in progress. The unit is installed but STOPPED --
    never enabled: an enabled instance would start recording at boot,
    which the role's own comment (tasks/main.yml:10-17) calls out as the
    load-bearing risk here -- as much a violation of "provisioning never
    starts a session" as calling `state: started` directly.

    Must catch all of: `ansible.builtin.systemd` / `systemd_service` /
    `service` setting `state: started|restarted` OR `enabled: yes`, and a
    raw `command:`/`shell:` task invoking `systemctl start`/`systemctl
    enable` directly.
    """
    tasks = _yaml.load(TASKS.read_text())
    for task in tasks:
        name = task.get("name", "<unnamed>")
        for module in _SYSTEMD_MODULES:
            args = task.get(module)
            if not isinstance(args, dict):
                continue
            state = str(args.get("state", ""))
            assert state not in ("started", "restarted"), (
                f"task {name!r} sets state={state!r} via {module}; "
                "provisioning must not start or restart a capture session"
            )
            enabled = args.get("enabled")
            assert enabled not in (True, "yes", "true", "Yes", "True"), (
                f"task {name!r} enables the unit via {module}; an enabled "
                "instance would start recording at boot, violating the "
                "never-enabled invariant"
            )
        for module in _SHELL_MODULES:
            args = task.get(module)
            if args is None:
                continue
            text = args if isinstance(args, str) else str(args.get("cmd", args))
            lowered = text.lower()
            assert "systemctl start" not in lowered, (
                f"task {name!r} shells out to `systemctl start` via "
                f"{module}; provisioning must not start the capture unit"
            )
            assert "systemctl enable" not in lowered, (
                f"task {name!r} shells out to `systemctl enable` via "
                f"{module}; the unit must never be enabled"
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

    Pinning the directory ROOT and the unit template's FILENAME is not
    enough -- three more things are each written twice and each break the
    feature, silently, with a green suite, if they drift:

    1. The unit-install task's `dest:` -- rename it without renaming the
       `.j2` and `systemctl start playground-capture@<vm>.service` targets
       a unit that was never installed.
    2. The wrapper's per-VM subdirectory (`capture-start.j2`) -- Python's
       `remote_capture_dir()` assumes exactly one level of nesting under
       the root; move the wrapper's output a level deeper (or shallower)
       and `fetch`/`status`/`clean` silently look in the wrong place.
    3. The `.pcap` basename the wrapper's `-w` writes -- every Python glob
       is `*.pcap*`; change the wrapper's extension and `status` reports
       `files=0 bytes=0` forever, and `clean` deletes nothing.
    """
    from playground.capture.commands import (
        CAPTURE_DIR,
        clean_cmd,
        remote_capture_dir,
        status_cmd,
        unit_name,
    )

    assert _yaml.load(DEFAULTS.read_text())["capture_dir"] == CAPTURE_DIR
    # The template's filename IS the instance template the CLI names.
    assert UNIT.name == "playground-capture@.service.j2"
    assert unit_name("droid1") == "playground-capture@droid1.service"

    # (1) The unit-install task's real `dest:`, not just the template's
    # filename -- find the template task whose `src` IS the unit template,
    # and assert its `dest` basename is the `@` instance form `unit_name()`
    # produces (an empty instance name, i.e. the template itself: systemd
    # expects `<name>@.service` on disk with the instance filled in at
    # `systemctl start <name>@<instance>.service` time).
    tasks = _yaml.load(TASKS.read_text())
    unit_install = next(
        t
        for t in tasks
        if t.get("ansible.builtin.template", {}).get("src") == UNIT.name
    )
    dest = str(unit_install["ansible.builtin.template"]["dest"])
    assert Path(dest).name == unit_name(""), (
        "the unit-install task's dest must be the `@` instance-template "
        "form of unit_name(), or `systemctl start` targets a unit that "
        "was never installed"
    )

    # (2) The per-VM subdirectory: the wrapper must nest exactly the shape
    # `remote_capture_dir()` builds -- `<capture_dir>/<vm>`.
    wrapper = WRAPPER.read_text()
    assert 'dir="{{ capture_dir }}/${vm}"' in wrapper, (
        "the wrapper must write into <capture_dir>/<vm>, matching "
        "remote_capture_dir()'s '<root>/<vm>' shape"
    )
    assert remote_capture_dir("droid1") == f"{CAPTURE_DIR}/droid1"

    # (3) The `.pcap` basename: the wrapper's `-w` target, and every glob
    # Python builds against it.
    assert '-w "${dir}/${stamp}.pcap"' in wrapper, (
        "the wrapper's -w target must end in .pcap, matching the *.pcap* "
        "glob every Python-side command uses"
    )
    assert "*.pcap*" in status_cmd("droid1")
    assert "*.pcap*" in clean_cmd("droid1")
