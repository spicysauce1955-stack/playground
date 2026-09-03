"""Pure builders for the guest-side capture commands.

No device, no guest, no ssh: these pin the exact strings the CLI sends,
which is where the load-bearing details live (passwordless-sudo
behavior, the unit's instance name, and NOT disabling the unit on stop).
"""

from __future__ import annotations

import shlex

import pytest

from playground.capture.commands import (
    CAPTURE_DIR,
    CONTAINER_PREFIX,
    NETNS_MARKER_DIR,
    _netns_marker_path,
    clean_cmd,
    is_active_cmd,
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


def test_netns_marker_lives_under_run_not_the_capture_directory() -> None:
    """`capture fetch` copies CAPTURE_DIR wholesale; the marker must not
    be inside it or it would show up as a bogus artifact. `/run` is
    tmpfs, so it also vanishes on reboot the same way a running capture
    does."""
    assert NETNS_MARKER_DIR.startswith("/run/")
    marker = _netns_marker_path("droid1")
    assert marker == f"{NETNS_MARKER_DIR}/droid1.netns"
    assert not marker.startswith(CAPTURE_DIR)


@pytest.mark.parametrize("vm", ["../../etc", "..", ".", "a/b", "/etc"])
def test_netns_marker_path_rejects_path_traversal(vm: str) -> None:
    """Same traversal guard as `remote_capture_dir` -- a VM name rejected
    everywhere else in this module must be rejected here too."""
    with pytest.raises(ValueError):
        _netns_marker_path(vm)


def test_status_checks_the_marker_only_when_the_unit_is_active() -> None:
    """The staleness probe is a safety net gated on `state = active` --
    it must never run (and never risk failing) against a unit that is
    inactive, dead, or has never existed."""
    cmd = status_cmd("droid1")
    assert 'if [ "$state" = active ]' in cmd


def test_status_stale_detection_reads_the_marker_and_the_live_netns() -> None:
    """`capture status` must independently re-derive the container's
    CURRENT netns (never trust the marker alone) and compare it against
    the marker the wrapper wrote at its own last start, downgrading
    `state=active` to `state=stale` on a mismatch -- the fix for the
    silent-data-loss bug where a restarted Redroid container leaves
    tcpdump alive in an orphaned namespace while the unit still reports
    active."""
    cmd = status_cmd("droid1")
    marker = shlex.quote(_netns_marker_path("droid1"))
    assert f"cat {marker}" in cmd
    assert "docker ps" in cmd
    assert "docker inspect" in cmd
    assert f"name=^{CONTAINER_PREFIX}" in cmd
    assert "readlink" in cmd
    assert "ns/net" in cmd
    assert 'state=stale' in cmd
    # The comparison, not just the word "stale" somewhere in the script.
    assert '[ "$mns" != "$cns" ] && state=stale' in cmd


def test_status_netns_probe_never_makes_the_command_itself_fail() -> None:
    """Every new command this check adds must keep the same
    2>/dev/null-guarded, `|| true`-terminated style as the rest of
    status_cmd -- an unprivileged reader, a container that is gone, or a
    `sudo -n` that isn't configured must never turn into a non-zero ssh
    exit code."""
    cmd = status_cmd("droid1")
    assert "docker ps --filter" in cmd and "|| true" in cmd
    assert "docker inspect -f" in cmd and "2>/dev/null || true" in cmd
    assert 'sudo -n readlink "/proc/$cpid/ns/net" 2>/dev/null || true' in cmd


def test_status_uses_sudo_only_for_the_netns_probe() -> None:
    """`capture status`'s `find` calls and `capture fetch`'s `scp` stay
    deliberately unprivileged; the ONE new privileged step is reading
    another (typically root-owned) process's `/proc/<pid>/ns/net`
    symlink, which an unprivileged reader cannot otherwise resolve."""
    cmd = status_cmd("droid1")
    assert cmd.count("sudo -n") == 1
    assert "sudo -n readlink" in cmd


def test_status_marker_metacharacter_cannot_become_a_second_command() -> None:
    nasty = "a b;rm -rf x"
    assert shlex.quote(_netns_marker_path(nasty)) in status_cmd(nasty)


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
    # No literal "/" here (unlike the metacharacter tests above): a VM
    # name containing one is now rejected outright by
    # remote_capture_dir's traversal guard, so this fixture only needs
    # the OTHER shell metacharacters (space, `;`) to make its point.
    nasty = "a b;rm -rf x"
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


def test_clean_cmd_globs_all_rotated_pcap_suffixes() -> None:
    """tcpdump's `-C` rotation appends a counter to whatever `-w` names,
    so the files on disk are `<stamp>.pcap0`, `<stamp>.pcap1`, ... A bare
    `*.pcap` glob matches none of them, which would make `--clean` report
    success while silently deleting nothing.
    """
    cmd = clean_cmd("droid1")
    assert "*.pcap*" in cmd
    assert "*.pcap'" not in cmd


def test_is_active_cmd_never_fails_on_an_inactive_unit() -> None:
    """`systemctl is-active` exits non-zero for an inactive unit -- a
    normal reportable state here, not an error -- so the probe must
    `|| true` to keep the ssh call's exit code meaning "the probe ran",
    leaving the actual state to be read from stdout.
    """
    cmd = is_active_cmd("droid1")
    assert "systemctl is-active" in cmd
    assert cmd.rstrip().endswith("|| true")
    assert "playground-capture@droid1.service" in cmd


def test_is_active_cmd_cannot_become_a_second_command() -> None:
    """The hostile substring is absorbed into one quoted token -- tokens
    from a real shell's word-splitting must never surface `touch` or
    `/tmp/OWNED` on their own."""
    nasty = "x; touch /tmp/OWNED"
    cmd = is_active_cmd(nasty)
    assert shlex.quote(unit_name(nasty)) in cmd
    tokens = shlex.split(cmd)
    assert "touch" not in tokens
    assert "/tmp/OWNED" not in tokens


@pytest.mark.parametrize("vm", ["../../etc", "..", ".", "a/b", "/etc"])
def test_remote_capture_dir_rejects_path_traversal(vm: str) -> None:
    """`LabVm.name` has no charset validator, so `shlex.quote` blocks
    shell injection but not path TRAVERSAL: a VM named `../..` still
    quotes safely as one shell token, but the resulting path resolves
    outside `CAPTURE_DIR` entirely -- and `clean_cmd` runs `sudo -n rm
    -f` around exactly that path. Every guest-side capture path is built
    on `remote_capture_dir`, so the guard belongs there.
    """
    with pytest.raises(ValueError):
        remote_capture_dir(vm)


def test_remote_capture_dir_accepts_ordinary_names() -> None:
    assert remote_capture_dir("droid1") == f"{CAPTURE_DIR}/droid1"
    assert remote_capture_dir("a b;rm -rf x") == f"{CAPTURE_DIR}/a b;rm -rf x"


def test_clean_cmd_cannot_become_a_second_command() -> None:
    """This one runs `rm -f` as root, so a VM name that escapes the path
    is the worst case in this module. The glob must stay expandable while
    the directory stays a single token.

    No literal "/" in the fixture (unlike the metacharacter tests above
    that only exercise `unit_name`): a VM name containing one is now
    rejected outright by `remote_capture_dir`'s traversal guard, so this
    fixture only needs the other shell metacharacters to make its point.
    """
    nasty = "x; touch OWNED"
    cmd = clean_cmd(nasty)
    assert "; touch OWNED/*.pcap*" not in cmd
    assert shlex.quote(f"/var/lib/playground/capture/{nasty}") in cmd
    assert cmd.endswith("/*.pcap*")  # glob outside the quotes, still expandable

