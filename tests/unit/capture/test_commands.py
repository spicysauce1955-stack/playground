"""Pure builders for the guest-side capture commands.

No device, no guest, no ssh: these pin the exact strings the CLI sends,
which is where the load-bearing details live (passwordless-sudo
behavior, the unit's instance name, and NOT disabling the unit on stop).
"""

from __future__ import annotations

import shlex

from playground.capture.commands import (
    CAPTURE_DIR,
    remote_capture_dir,
    start_cmd,
    status_cmd,
    stop_cmd,
    unit_name,
)


def test_unit_name_is_the_systemd_instance() -> None:
    assert unit_name("droid1") == "playground-capture@droid1.service"


def test_remote_capture_dir_is_per_vm() -> None:
    assert remote_capture_dir("droid1") == f"{CAPTURE_DIR}/droid1"


def test_start_uses_non_interactive_sudo() -> None:
    """`ubuntu` needs sudo for systemctl. -n makes a misconfigured
    sudoers fail immediately instead of hanging on a password prompt
    that no one is there to answer -- the ssh call has no tty."""
    cmd = start_cmd("droid1")
    assert "sudo -n systemctl start" in cmd
    assert "playground-capture@droid1.service" in cmd


def test_stop_stops_but_does_not_disable() -> None:
    """Disabling is a provisioning concern. `stop` ends one session; it
    must not change whether the unit would come back."""
    cmd = stop_cmd("droid1")
    assert "sudo -n systemctl stop" in cmd
    assert "disable" not in cmd


def test_status_reports_state_files_and_bytes_in_one_line() -> None:
    cmd = status_cmd("droid1")
    assert "is-active" in cmd
    # A wrapping ring is only visible if the file COUNT is reported, and
    # disk pressure only if bytes are.
    assert "state=" in cmd
    assert "files=" in cmd
    assert "bytes=" in cmd


def test_status_survives_a_missing_capture_directory() -> None:
    """Before the first session the directory does not exist. status must
    report zeros, not a shell error."""
    cmd = status_cmd("droid1")
    assert "2>/dev/null" in cmd


def test_a_metacharacter_in_a_vm_name_cannot_become_a_second_command() -> None:
    """VM names are schema-validated, but these strings cross two shells,
    so quoting is the invariant rather than the current validator.

    Asserting on the ARGV the guest's shell would build is the real
    question ("can this become a second command?"); asserting on the
    command string's characters is not.
    """
    nasty = "a b;rm -rf /"
    for build, verb in ((start_cmd, "start"), (stop_cmd, "stop")):
        argv = shlex.split(build(nasty))
        assert argv == [
            "sudo", "-n", "systemctl", verb,
            f"playground-capture@{nasty}.service",
        ]


def test_status_keeps_the_capture_dir_as_one_token() -> None:
    nasty = "a b;rm -rf /"
    # The directory appears inside a command substitution, so assert the
    # quoted form is present rather than tokenizing the whole script.
    assert shlex.quote(f"/var/lib/playground/capture/{nasty}") in status_cmd(nasty)
