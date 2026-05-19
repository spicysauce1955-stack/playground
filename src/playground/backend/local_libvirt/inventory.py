"""Render an Ansible inventory from :class:`ResolvedLab` + ``tofu output -json``.

The renderer is intentionally narrow — it produces a single ``[playground]``
group that today's ``ansible/site.yml`` already consumes. Per-role groups
and host-level groupings are not added in this slice (YAGNI: site.yml only
references ``hosts: playground``).

VMs are paired with libvirt IPs by **declaration order** — the i-th VM in
``ResolvedLab.vms`` is matched with the i-th IP in ``tofu output -json
vm_ips``. This is the same mapping the existing manual ``ansible/inventory.ini``
flow has used; the bridge inherits its fragility. A future slice should
enrich ``tofu/outputs.tf`` to emit a name-keyed map so the bridge can match
on names instead of indices. For now, if the counts disagree we emit
``config.inventory.count_mismatch`` and leave it to the operator to align
``var.vm_count`` with the lab.

Diagnostic IDs:

- ``config.inventory.tofu_binary_missing``
- ``config.inventory.tofu_command_failed``
- ``config.inventory.tofu_parse_failed``
- ``config.inventory.tofu_no_state``
- ``config.inventory.count_mismatch``
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab


def fetch_vm_ips(
    tofu_dir: Path,
) -> tuple[list[str], list[Diagnostic]]:
    """Shell out to ``tofu output -json`` and return ``vm_ips``.

    On any failure returns ``([], diagnostics)`` — the caller decides
    whether to render partial inventory or abort. Specifically does NOT
    raise on a missing tofu binary or empty state; both are common
    operator situations that should produce actionable feedback.
    """
    source = SourceLocation(path=str(tofu_dir))

    if shutil.which("tofu") is None:
        return [], [
            Diagnostic(
                id="config.inventory.tofu_binary_missing",
                severity="error",
                message="`tofu` binary not found on PATH",
                source=source,
                suggestion=(
                    "install OpenTofu (https://opentofu.org/docs/intro/install/) "
                    "or pass --tofu-dir to a directory whose tofu state has been "
                    "exported via `tofu output -json` to a file"
                ),
            )
        ]

    try:
        completed = subprocess.run(  # noqa: S603 — explicit args, no shell
            ["tofu", "output", "-json"],
            cwd=tofu_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return [], [
            Diagnostic(
                id="config.inventory.tofu_command_failed",
                severity="error",
                message=f"failed to run `tofu output -json`: {exc}",
                source=source,
            )
        ]

    if completed.returncode != 0:
        return [], [
            Diagnostic(
                id="config.inventory.tofu_command_failed",
                severity="error",
                message=(
                    f"`tofu output -json` exited {completed.returncode}: "
                    f"{completed.stderr.strip() or completed.stdout.strip()}"
                ),
                source=source,
                suggestion=(
                    "run `tofu init` in the tofu directory, or `tofu apply` "
                    "if state is missing"
                ),
            )
        ]

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return [], [
            Diagnostic(
                id="config.inventory.tofu_parse_failed",
                severity="error",
                message=f"could not parse `tofu output -json`: {exc}",
                source=source,
            )
        ]

    if not isinstance(data, dict) or "vm_ips" not in data:
        return [], [
            Diagnostic(
                id="config.inventory.tofu_no_state",
                severity="error",
                message=(
                    "`tofu output -json` returned no `vm_ips` — has the lab "
                    "been applied?"
                ),
                source=source,
                suggestion=(
                    "cd into the tofu directory and run `tofu apply -auto-approve`"
                ),
            )
        ]

    value = data["vm_ips"].get("value") if isinstance(data["vm_ips"], dict) else None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return [], [
            Diagnostic(
                id="config.inventory.tofu_parse_failed",
                severity="error",
                message="`tofu output -json` `vm_ips` is not a list of strings",
                source=source,
            )
        ]

    return value, []


def render_inventory(
    resolved: ResolvedLab,
    vm_ips: list[str],
) -> tuple[str, list[Diagnostic]]:
    """Produce an ``ansible/inventory.ini`` body for ``resolved``.

    Pure function: no I/O, no subprocess. Pairs ``resolved.vms[i]`` with
    ``vm_ips[i]`` and emits a ``config.inventory.count_mismatch`` diagnostic
    if the counts disagree. When mismatched, the renderer still returns a
    best-effort body covering the prefix that matches — the caller decides
    whether to write or abort.
    """
    diagnostics: list[Diagnostic] = []
    source = SourceLocation(path=f"config/labs/{resolved.lab_name}.yaml")

    if len(vm_ips) != len(resolved.vms):
        diagnostics.append(
            Diagnostic(
                id="config.inventory.count_mismatch",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} declares {len(resolved.vms)} VMs "
                    f"but tofu state has {len(vm_ips)} IPs"
                ),
                source=source,
                suggestion=(
                    "set `var.vm_count` to match the lab, or align the lab's "
                    "`spec.vms` count with the deployed VMs"
                ),
            )
        )

    lines: list[str] = [
        "# Generated by `playground inventory render`; do not edit by hand.",
        f"# Lab: {resolved.lab_name}",
        f"# Source: {resolved.source_map.get('spec', '<unknown>')}",
        "# Pairing: declaration order (lab.spec.vms[i] <-> tofu vm_ips[i]).",
        "#          Reordering VMs in YAML will silently re-route Ansible roles.",
        "",
        "[playground]",
    ]

    for vm, ip in zip(resolved.vms, vm_ips, strict=False):
        host_vars = [
            f"ansible_host={ip}",
            f"ansible_user={vm.ssh.user}",
            f"pg_role={vm.role}",
        ]
        if vm.networks:
            host_vars.append(f"pg_networks={','.join(vm.networks)}")
        if vm.tags:
            host_vars.append(f"pg_tags={','.join(vm.tags)}")
        lines.append(f"{vm.name} {' '.join(host_vars)}")

    lines += [
        "",
        "[playground:vars]",
        f"pg_lab={resolved.lab_name}",
        "",
    ]

    return "\n".join(lines), diagnostics


__all__ = ["fetch_vm_ips", "render_inventory"]
