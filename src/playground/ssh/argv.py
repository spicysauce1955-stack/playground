"""Build ssh / scp argv lists.

Six call sites used to hand-roll the same option block: the `exec`, `adb`,
and `cp` CLI verbs, verify's `_ssh`, and wait's `_ssh_probe` /
`_wait_cloud_init`. They agreed on three options and disagreed on
everything else, so this module builds ARGV only — each caller keeps its
own subprocess invocation, timeout policy, and error handling, because
those differences are real.

`UserKnownHostsFile=/dev/null` matters for local-vbox: it reuses
127.0.0.1:<port> across VM rebuilds, so a pinned known_hosts entry would
trip a host-key mismatch on every recreate.
"""

from __future__ import annotations

SSH_BASE_OPTS: tuple[str, ...] = (
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
)
"""Options every site shares. Interactive and unattended alike."""

BATCH_OPTS: tuple[str, ...] = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
"""Unattended-only. The three CLI verbs omit these because a human is
present; the backend probes set them so a hung auth prompt cannot stall a
pipeline step."""

DEFAULT_SSH_PORT = 22


def build_ssh_argv(
    host: str,
    *,
    user: str,
    port: int | None = None,
    command: str | None = None,
    batch: bool = False,
    local_forward: str | None = None,
    no_remote_command: bool = False,
) -> list[str]:
    """Return an ``ssh`` argv.

    ``port`` is emitted only when it is set and not 22, matching every
    existing call site (libvirt and cloud use 22; vbox NAT does not).
    ``local_forward`` is an ``-L`` spec such as ``5555:127.0.0.1:5555``;
    pair it with ``no_remote_command=True`` for a pure tunnel.
    """
    argv = ["ssh"]
    if port is not None and port != DEFAULT_SSH_PORT:
        argv += ["-p", str(port)]
    if no_remote_command:
        argv.append("-N")
    if local_forward is not None:
        argv += ["-L", local_forward]
    argv += list(SSH_BASE_OPTS)
    if batch:
        argv += list(BATCH_OPTS)
    argv.append(f"{user}@{host}")
    if command is not None:
        argv.append(command)
    return argv


def build_scp_argv(
    src: str,
    dst: str,
    *,
    port: int | None = None,
    recursive: bool = False,
) -> list[str]:
    """Return an ``scp`` argv.

    scp spells the port ``-P`` (capital) and takes two path operands
    instead of a host plus a remote command, which is why it does not go
    through :func:`build_ssh_argv`.
    """
    argv = ["scp"]
    if port is not None and port != DEFAULT_SSH_PORT:
        argv += ["-P", str(port)]
    if recursive:
        argv.append("-r")
    argv += list(SSH_BASE_OPTS)
    argv += [src, dst]
    return argv
