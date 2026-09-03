"""Packet capture for Redroid devices, from the host VM's side.

`commands` builds the guest-side shell strings, `targets` answers "which
VMs can be captured", and `state` records the active session per VM. The
CLI surface lives in `playground.cli.capture_commands`.
"""
