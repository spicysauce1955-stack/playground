"""Pure builders for the guest-side capture commands.

Everything here returns a shell string to be handed to `ssh`. Keeping
them pure means every command shape is unit-testable without a guest,
and the CLI layer stays a thin argument-parsing shell -- the same split
`playground.android.commands` uses for adb.

The heavy lifting is NOT here: entering the Redroid container's network
namespace is the `capture` Ansible role's wrapper script
(`/usr/local/lib/playground/capture-start`), invoked by an instanced
systemd unit. This module only starts, stops, and interrogates that
unit. That division is deliberate -- a capture must outlive the ssh
session that started it, which rules out running tcpdump directly over
ssh.
"""

from __future__ import annotations

import shlex

UNIT_TEMPLATE = "playground-capture@"
"""Instanced unit installed by the `capture` Ansible role. `%i` is the
lab's VM name."""

CAPTURE_DIR = "/var/lib/playground/capture"
"""Guest-side pcap root. Must match `capture_dir` in
`ansible/roles/capture/defaults/main.yml` -- the two are one contract."""


CONTAINER_PREFIX = "redroid_"
"""Container-name prefix `capture-start.j2` uses to discover the Redroid
container's PID (`capture_container_prefix` in
`ansible/roles/capture/defaults/main.yml`). Duplicated here for the same
reason `CAPTURE_DIR` is duplicated from `capture_dir`: `status_cmd` has
to independently re-discover the container's CURRENT netns from a plain
guest shell to detect a stale capture (see its docstring), and there is
no channel to hand a Jinja default across the Ansible/Python boundary.
Unlike the role's own `redroid_{{ inventory_hostname | regex_replace(...)
}}` naming expression -- deliberately NOT duplicated, per
`capture_container_prefix`'s own comment -- this is a plain string
constant, so the duplication is cheap. Pinned by
`test_ansible_and_python_agree_on_the_guest_contract`."""

NETNS_MARKER_DIR = "/run/playground-capture"
"""Where the wrapper (`capture-start.j2`) records the container's netns
identity at every unit start. Under `/run` (tmpfs) so it disappears on
reboot exactly like a running capture does, and deliberately NOT under
`CAPTURE_DIR` -- `capture fetch` copies that whole directory and the
marker is not a capture artifact."""


def _netns_marker_path(vm: str) -> str:
    """Guest-side path of the netns marker `status_cmd` reads.

    Routed through the same traversal guard as `remote_capture_dir` even
    though nothing here does a destructive operation on it, purely so a
    VM name rejected everywhere else in this module is rejected here
    too, rather than quietly building a path that reads some other VM's
    marker.
    """
    _reject_path_traversal(vm)
    return f"{NETNS_MARKER_DIR}/{vm}.netns"


def unit_name(vm: str) -> str:
    """Return the systemd unit name for a VM.

    This is a DATA function: the return value is stored in CaptureSession
    and persisted to JSON. Quoting happens at the command-builder level
    where the unit is interpolated into shell strings, not here.
    """
    return f"{UNIT_TEMPLATE}{vm}.service"


def _reject_path_traversal(vm: str) -> None:
    """Reject a VM name that could escape the capture directory.

    `LabVm.name` is `Field(min_length=1)` with no charset validator, so
    `shlex.quote` (used throughout this module) correctly blocks shell
    injection but not path TRAVERSAL: a VM named e.g. ``../..`` still
    quotes safely as a single shell token, but `f"{CAPTURE_DIR}/{vm}"`
    then resolves outside the capture root entirely -- and `clean_cmd`
    builds a `sudo -n rm -f` around exactly that path. This is a plain
    charset/value check, not a schema-level validator: a stricter
    `LabVm.name` pattern would be a cross-cutting change affecting every
    lab, not just capture.
    """
    if "/" in vm or vm in (".", ".."):
        raise ValueError(f"invalid VM name for a capture path: {vm!r}")


def remote_capture_dir(vm: str) -> str:
    """Return the guest-side capture directory path for a VM.

    This is a DATA function: the return value is used as a path argument
    to scp and rm, which handle their own quoting. Quoting happens at
    the command-builder level, not here, to avoid double-quoting bugs.

    Every guest-side capture path is derived from this function, so the
    traversal guard lives here rather than being repeated at each call
    site.
    """
    _reject_path_traversal(vm)
    return f"{CAPTURE_DIR}/{vm}"


def start_cmd(vm: str) -> str:
    """Start the capture unit and confirm it actually came up.

    `systemctl start` is not sufficient evidence on its own: the unit is
    `Type=exec`, so the start job completes when /bin/sh execs -- not
    when the wrapper's own `exec` of nsenter+tcpdump succeeds. A wrapper
    that dies immediately (no Redroid container, an AppArmor denial)
    still exits 0 here, and `playground capture start` would record a
    session for a capture that is not running.

    Hence the delay before the check rather than an immediate probe: with
    `RestartSec=5` on the unit, a wrapper that died reports `activating`
    (auto-restart pending) during the gap, so sleeping inside that gap and
    then requiring exactly `active` separates healthy from broken.

    The unit name is assigned to a shell variable ONCE
    (``unit=<shlex.quote(...)>``) rather than interpolated at each use
    site: `shlex.quote` produces a token that is safe to place BARE on a
    command line, but the final diagnostic message needs the unit name
    INSIDE an already-double-quoted `echo "..."` string, where single
    quotes are not special. Splicing a `shlex.quote`d literal straight
    into that context breaks (found live, via `sh -n`, for a VM name
    containing a single quote: the embedded `'...'"'"'...'` sequence does
    not close before the surrounding double quotes do). Assigning once
    and referencing `$unit`/`"$unit"` sidesteps this: the shell expands a
    variable's value verbatim, with no re-parsing for quotes, `$`, or
    backticks in that value, so it is safe in both the bare argument
    positions (`"$unit"`, quoted against word-splitting) and inside the
    double-quoted message.
    """
    # -n (non-interactive) rather than a bare `sudo`: the ssh call has no
    # tty, so a sudoers misconfiguration would otherwise hang until the
    # subprocess timeout instead of failing with a readable message.
    unit_literal = shlex.quote(unit_name(vm))
    return (
        f"unit={unit_literal}; "
        'sudo -n systemctl start "$unit" || exit 1; '
        "sleep 2; "
        'state=$(systemctl is-active "$unit" 2>/dev/null || true); '
        '[ "$state" = active ] || { '
        'echo "capture: $unit is $state, not active" >&2; exit 1; }'
    )


def stop_cmd(vm: str) -> str:
    # `stop`, never `disable`. Whether the unit is enabled is the
    # provisioning layer's business; this ends one session.
    return f"sudo -n systemctl stop {shlex.quote(unit_name(vm))}"


def status_cmd(vm: str) -> str:
    """One parseable line: unit state, pcap count, total bytes.

    File count is what makes a WRAPPING ring visible (`files=` pinned at
    `max_files` means the oldest data is being overwritten), and bytes is
    what makes disk pressure visible. Both matter enough to be in the
    default output rather than behind a flag.

    Every command is `2>/dev/null`-guarded and `|| true`-terminated
    because none of this exists before the first session: `is-active`
    exits non-zero for a dead unit (which is a normal, reportable state,
    not an error), and the capture directory is absent until the wrapper
    creates it.

    The glob is `*.pcap*`, not `*.pcap`: tcpdump's `-C` rotation appends a
    counter to whatever `-w` names, ZERO-PADDED to the width of `-W`'s
    argument -- with `max_files: 40` the files on disk are observed as
    `<stamp>.pcap00` ... `<stamp>.pcap39`, not `<stamp>.pcap0` /
    `<stamp>.pcap1`. A bare `*.pcap` glob matches none of them, which
    would silently report `files=0 bytes=0` for a healthy, actively
    recording session -- hence `*.pcap*`, which matches any width.

    `state=active` is not, on its own, proof the capture is actually
    recording. Measured live: `nsenter --net` moves tcpdump INTO the
    container's network namespace, and tcpdump's own presence there
    keeps that namespace alive after a Redroid container restart --
    tcpdump does not die, `Restart=always` never fires, and a stale
    tcpdump can sit in an orphaned namespace capturing nothing while the
    unit still reports `active`. `capture-start.j2` is now a watchdog
    that polls for exactly this and self-heals within roughly 5 seconds
    by exiting non-zero so systemd restarts it -- so THIS check is a
    safety net for a crashed watchdog or a future regression, not the
    primary defence. When (and only when) the unit is `active`, this
    also re-derives the container's CURRENT netns independently (its own
    `docker ps` / `docker inspect` / `readlink /proc/<pid>/ns/net`, run
    under `sudo -n` since the container's init typically runs as root
    and an unprivileged reader cannot otherwise resolve another user's
    namespace symlink) and compares it against the marker the wrapper
    wrote at its own most recent start (`NETNS_MARKER_DIR`). A mismatch
    -- including "the marker exists but the container can no longer be
    resolved at all" -- reports `state=stale` instead of `state=active`.
    Skipped entirely when the unit is not `active` or the marker does
    not exist yet, so a host running an old wrapper, or one that has
    never had a session, behaves exactly as before this check existed.
    """
    unit = shlex.quote(unit_name(vm))
    directory = shlex.quote(remote_capture_dir(vm))
    marker = shlex.quote(_netns_marker_path(vm))
    name_filter = shlex.quote(f"name=^{CONTAINER_PREFIX}")
    staleness_check = "".join(
        [
            f'if [ "$state" = active ] && [ -r {marker} ]; then ',
            f"mns=$(cat {marker} 2>/dev/null || true); ",
            f"cids=$(docker ps --filter {name_filter} --format "
            "'{{.ID}}' 2>/dev/null || true); ",
            "ccount=$(printf '%s\\n' \"$cids\" | grep -c . || true); ",
            'if [ "$ccount" -eq 1 ]; then ',
            "cpid=$(docker inspect -f '{{.State.Pid}}' \"$cids\" "
            "2>/dev/null || true); ",
            "else cpid=; fi; ",
            'if [ -n "$cpid" ] && [ "$cpid" != 0 ]; then ',
            'cns=$(sudo -n readlink "/proc/$cpid/ns/net" 2>/dev/null || true); ',
            "else cns=; fi; ",
            '[ -n "$mns" ] && [ "$mns" != "$cns" ] && state=stale; ',
            "fi; ",
        ]
    )
    return (
        f"state=$(systemctl is-active {unit} 2>/dev/null || true); "
        f"files=$(find {directory} -maxdepth 1 -name '*.pcap*' 2>/dev/null | wc -l); "
        f"bytes=$(find {directory} -maxdepth 1 -name '*.pcap*' -printf '%s\\n' "
        "2>/dev/null | awk '{t+=$1} END {print t+0}'); "
        f"{staleness_check}"
        'printf "state=%s files=%s bytes=%s\\n" "$state" "$files" "$bytes"'
    )


def is_active_cmd(vm: str) -> str:
    """Print the unit's activation state, exiting 0 whichever it is.

    `systemctl is-active` exits non-zero for an inactive unit, which is a
    normal reportable state here rather than an error -- so the `|| true`
    keeps the ssh call's exit code meaning "the probe ran", leaving the
    STATE to be read from stdout.
    """
    return (
        f"systemctl is-active {shlex.quote(unit_name(vm))} 2>/dev/null || true"
    )


def clean_cmd(vm: str) -> str:
    """Remove a device's captured pcaps from the guest.

    The directory is quoted but the glob is deliberately left outside the
    quotes, so the guest's shell still expands `*.pcap*` while a VM name
    containing a space or a metacharacter cannot split the path or start a
    second command -- this one runs `rm -f` under `sudo -n`, so it is the
    highest-consequence string this module builds.

    `*.pcap*`, not `*.pcap`: tcpdump's `-C` rotation appends a counter to
    whatever `-w` names, so the files on disk are `<stamp>.pcap0`,
    `<stamp>.pcap1`, ... A bare `*.pcap` glob matches none of them and
    `--clean` would silently delete nothing while reporting success.
    """
    return f"sudo -n rm -f {shlex.quote(remote_capture_dir(vm))}/*.pcap*"
