"""One ssh/scp argv builder, shared by six call sites.

Before this module, `exec`, `adb`, `cp`, verify's `_ssh`, and wait's
`_ssh_probe` / `_wait_cloud_init` each hand-rolled the same option block.
"""

from __future__ import annotations

from playground.ssh.argv import (
    BATCH_OPTS,
    SSH_BASE_OPTS,
    build_scp_argv,
    build_ssh_argv,
)


def test_base_opts_are_the_three_shared_by_every_site() -> None:
    assert SSH_BASE_OPTS == (
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
    )


def test_batch_opts_are_unattended_only() -> None:
    assert BATCH_OPTS == ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")


def test_minimal_ssh_argv() -> None:
    assert build_ssh_argv("10.0.0.5", user="ubuntu") == [
        "ssh", *SSH_BASE_OPTS, "ubuntu@10.0.0.5",
    ]


def test_port_22_is_omitted() -> None:
    argv = build_ssh_argv("10.0.0.5", user="ubuntu", port=22)
    assert "-p" not in argv


def test_nonstandard_port_uses_lowercase_p_before_the_opts() -> None:
    argv = build_ssh_argv("127.0.0.1", user="ubuntu", port=2222)
    assert argv[:3] == ["ssh", "-p", "2222"]


def test_none_port_is_omitted() -> None:
    assert "-p" not in build_ssh_argv("h", user="u", port=None)


def test_command_is_the_tail_argument() -> None:
    argv = build_ssh_argv("h", user="u", command="uptime")
    assert argv[-1] == "uptime"
    assert argv[-2] == "u@h"


def test_batch_mode_appends_unattended_options() -> None:
    argv = build_ssh_argv("h", user="u", batch=True, command="true")
    assert "BatchMode=yes" in argv
    assert "ConnectTimeout=10" in argv


def test_local_forward_implies_no_remote_command() -> None:
    argv = build_ssh_argv(
        "h", user="u", local_forward="5555:127.0.0.1:5555", no_remote_command=True
    )
    assert "-N" in argv
    assert "-L" in argv
    assert argv[argv.index("-L") + 1] == "5555:127.0.0.1:5555"
    assert argv[-1] == "u@h", "a tunnel takes no remote command"


def test_scp_uses_capital_p_and_two_operands() -> None:
    argv = build_scp_argv("a.txt", "u@h:/tmp/a.txt", port=2222)
    assert argv[0] == "scp"
    assert argv[1:3] == ["-P", "2222"]
    assert argv[-2:] == ["a.txt", "u@h:/tmp/a.txt"]


def test_scp_recursive_flag() -> None:
    assert "-r" in build_scp_argv("d", "u@h:/tmp/d", recursive=True)


def test_scp_omits_port_22() -> None:
    assert "-P" not in build_scp_argv("a", "u@h:/a", port=22)
