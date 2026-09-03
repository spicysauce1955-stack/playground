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


def test_start_requires_the_unit_to_stay_active() -> None:
    """`systemctl start` returning 0 is not sufficient evidence: the unit
    is `Type=exec`, so the start job completes when /bin/sh execs -- not
    when the wrapper's own `exec` of nsenter+tcpdump succeeds. A wrapper
    that dies immediately (no Redroid container, an AppArmor denial)
    still exits 0 there.

    `start_cmd` must therefore re-check `systemctl is-active` after a
    delay and require exactly `active`, failing (with a message naming
    the observed state) on anything else -- `activating` included, since
    `RestartSec=5` means a dead wrapper reports `activating` while its
    auto-restart is pending.
    """
    cmd = start_cmd("droid1")
    assert 'state=$(systemctl is-active' in cmd
    assert '[ "$state" = active ]' in cmd
    assert "sleep" in cmd


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


def test_a_metacharacter_in_a_vm_name_cannot_become_a_second_command_stop() -> None:
    """VM names are schema-validated, but these strings cross two shells,
    so quoting is the invariant rather than the current validator.

    Asserting on the ARGV the guest's shell would build is the real
    question ("can this become a second command?"); asserting on the
    command string's characters is not.

    `stop_cmd` is still a single `sudo -n systemctl stop <unit>` call, so
    it still reduces to one clean 5-element argv.
    """
    nasty = "a b;rm -rf /"
    argv = shlex.split(stop_cmd(nasty))
    assert argv == [
        "sudo", "-n", "systemctl", "stop",
        f"playground-capture@{nasty}.service",
    ]


def test_a_metacharacter_in_a_vm_name_cannot_become_a_second_command_start() -> None:
    """`start_cmd` is now a multi-statement script (start, then confirm
    the unit stayed active), so it no longer reduces to one clean argv --
    but it must still be injection-safe.

    Proof, not vibes: (1) the hostile VM name is never woven into the
    script unquoted -- its ONE occurrence is a `shlex.quote`d token
    assigned to a shell variable, referenced everywhere else as `$unit`
    -- and (2) tokenizing the WHOLE script with `shlex.split` -- which
    follows the same POSIX quoting rules a real shell does for
    word-splitting -- never produces a bare `rm` token. If quoting were
    dropped, `rm` and `-rf` would show up as their own tokens (the
    classic `; rm -rf /` injection) instead of being absorbed into the
    quoted unit-name token.
    """
    nasty = "a b;rm -rf /"
    cmd = start_cmd(nasty)
    quoted_unit = shlex.quote(unit_name(nasty))
    # The literal, quoted unit name is assigned to a variable exactly
    # once; every other use is the safe `$unit` reference.
    assert cmd.count(quoted_unit) == 1
    assert cmd.count("$unit") >= 3
    # Every occurrence of the raw, unquoted VM name is INSIDE that one
    # quoted assignment -- stripping it out must remove every trace of
    # the hostile substring, proving it never appears unquoted elsewhere
    # in the script.
    assert nasty not in cmd.replace(quoted_unit, "<UNIT>")

    tokens = shlex.split(cmd)
    assert "rm" not in tokens
    assert "-rf" not in tokens
    assert "/" not in tokens


def test_status_keeps_the_capture_dir_as_one_token() -> None:
    nasty = "a b;rm -rf /"
    # The directory appears inside a command substitution, so assert the
    # quoted form is present rather than tokenizing the whole script.
    assert shlex.quote(f"/var/lib/playground/capture/{nasty}") in status_cmd(nasty)


def test_status_glob_matches_tcpdumps_rotation_suffix() -> None:
    """tcpdump under -C appends a rotation counter to whatever -w names,
    so the files on disk are `<stamp>.pcap0`, `<stamp>.pcap1`, ... A
    `*.pcap` glob matches none of them, which would make `capture status`
    report files=0 bytes=0 for a healthy, actively recording session.
    """
    cmd = status_cmd("droid1")
    assert "'*.pcap*'" in cmd
    assert "'*.pcap'" not in cmd
