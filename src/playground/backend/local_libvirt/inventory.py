"""Render an Ansible inventory from :class:`ResolvedLab` + ``tofu output -json``.

The renderer is intentionally narrow — it produces a single ``[playground]``
group that today's ``ansible/site.yml`` already consumes. Per-role groups
and host-level groupings are not added in this slice (YAGNI: site.yml only
references ``hosts: playground``).

VMs are paired with libvirt IPs **by name**. ``tofu output -json vm_ips`` is
expected to be a map ``{ vm_name -> ip }`` — see ``tofu/outputs.tf``. The
renderer looks up each ``ResolvedLab.vms[i].name`` in that map. The operator
keeps tofu and the lab aligned by setting ``var.vm_names`` in
``tofu/terraform.tfvars`` to match ``lab.spec.vms[*].name``. Mismatches
surface as ``config.inventory.vm_ip_not_found``.

Diagnostic IDs:

- ``config.inventory.tofu_binary_missing``
- ``config.inventory.tofu_command_failed``
- ``config.inventory.tofu_parse_failed``
- ``config.inventory.tofu_no_state``
- ``config.inventory.vm_ip_not_found``
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.planner import schedule_workloads, workload_to_ansible_payload

# Public diagnostic id constants. Importing modules (e.g.
# backend/local_libvirt/status.py) special-case `TOFU_NO_STATE` as
# "nothing has been applied yet" — fine for status queries, an error
# for inventory rendering.
TOFU_NO_STATE_DIAGNOSTIC_ID = "config.inventory.tofu_no_state"


def fetch_vm_ips(
    tofu_dir: Path,
) -> tuple[dict[str, str], list[Diagnostic]]:
    """Shell out to ``tofu output -json`` and return ``vm_ips``.

    Expects ``vm_ips`` to be a JSON object (``dict[str, str]``) keyed by
    libvirt domain name; see ``tofu/outputs.tf``. On any failure returns
    ``({}, diagnostics)`` — the caller decides whether to render partial
    inventory or abort. Specifically does NOT raise on a missing tofu binary
    or empty state; both are common operator situations that should produce
    actionable feedback.
    """
    source = SourceLocation(path=str(tofu_dir))

    if shutil.which("tofu") is None:
        return {}, [
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
        return {}, [
            Diagnostic(
                id="config.inventory.tofu_command_failed",
                severity="error",
                message=f"failed to run `tofu output -json`: {exc}",
                source=source,
            )
        ]

    if completed.returncode != 0:
        return {}, [
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
        return {}, [
            Diagnostic(
                id="config.inventory.tofu_parse_failed",
                severity="error",
                message=f"could not parse `tofu output -json`: {exc}",
                source=source,
            )
        ]

    if not isinstance(data, dict) or "vm_ips" not in data:
        return {}, [
            Diagnostic(
                id=TOFU_NO_STATE_DIAGNOSTIC_ID,
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
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        return {}, [
            Diagnostic(
                id="config.inventory.tofu_parse_failed",
                severity="error",
                message=(
                    "`tofu output -json` `vm_ips` is not a map of string to "
                    "string; expected the shape produced by tofu/outputs.tf "
                    "(map keyed by VM domain name)"
                ),
                source=source,
                suggestion=(
                    "update tofu/outputs.tf to emit vm_ips as a map and "
                    "re-run `tofu apply` so state reflects the new shape"
                ),
            )
        ]

    return value, []


def render_inventory(
    resolved: ResolvedLab,
    vm_ips: dict[str, str],
) -> tuple[str, list[Diagnostic]]:
    """Produce an ``ansible/inventory.ini`` body for ``resolved``.

    Pure function: no I/O, no subprocess. Looks up each VM in ``vm_ips``
    by name. VMs whose name is not in the map produce a
    ``config.inventory.vm_ip_not_found`` diagnostic and are omitted from
    the body — the caller decides whether to write the partial inventory
    or abort.

    Emits a single ``[playground]`` group containing every VM (today's
    ``ansible/site.yml`` targets ``hosts: playground``) plus one
    per-role group keyed on the lab VM's role with dashes normalized to
    underscores (``docker-host`` -> ``docker_host``). Future playbooks
    can target ``hosts: docker_host`` etc. without scanning host vars.
    """
    diagnostics: list[Diagnostic] = []
    source = SourceLocation(path=f"config/labs/{resolved.lab_name}.yaml")

    schedule, schedule_diagnostics = schedule_workloads(resolved)
    diagnostics.extend(schedule_diagnostics)

    placed_vms: list[tuple[str, str, str]] = []  # (name, role, host-line)

    for idx, vm in enumerate(resolved.vms):
        ip = vm_ips.get(vm.name)
        if ip is None:
            diagnostics.append(
                Diagnostic(
                    id="config.inventory.vm_ip_not_found",
                    severity="error",
                    message=(
                        f"lab {resolved.lab_name!r} declares VM {vm.name!r}, "
                        "but tofu state has no matching libvirt domain"
                    ),
                    source=source,
                    key_path=f"spec.vms[{idx}].name",
                    suggestion=(
                        f"add {vm.name!r} to `var.vm_names` in "
                        "tofu/terraform.tfvars and re-run `tofu apply`, or "
                        f"rename the lab VM to match an existing tofu domain "
                        f"(known names: {sorted(vm_ips) or '<none>'})"
                    ),
                )
            )
            continue

        host_vars = [
            f"ansible_host={ip}",
            f"ansible_user={vm.ssh.user}",
            f"pg_role={vm.role}",
        ]
        if vm.networks:
            host_vars.append(f"pg_networks={','.join(vm.networks)}")
        if vm.tags:
            host_vars.append(f"pg_tags={','.join(vm.tags)}")
        workloads = schedule.get(vm.name, [])
        if workloads:
            # JSON-encoded list of workload dicts. Ansible reads it via
            # `pg_workloads | from_json` in the workload_container role.
            # We shell-escape embedded single quotes (rare in practice
            # but possible in env values). Inline JSON in a host var is
            # acceptable for §8a's container-only scope; §8b/§8c will
            # migrate to file-on-disk staging when compose files need
            # to ride along.
            payload = json.dumps(
                [workload_to_ansible_payload(wl) for wl in workloads],
                separators=(",", ":"),
            )
            escaped = payload.replace("'", "'\\''")
            host_vars.append(f"pg_workloads='{escaped}'")
        host_line = f"{vm.name} {' '.join(host_vars)}"
        placed_vms.append((vm.name, vm.role, host_line))

    lines: list[str] = [
        "# Generated by `playground inventory render`; do not edit by hand.",
        f"# Lab: {resolved.lab_name}",
        f"# Source: {resolved.source_map.get('spec', '<unknown>')}",
        "# Pairing: lab VM name -> tofu domain name (set tofu var.vm_names",
        "#          to match lab.spec.vms[*].name).",
        "",
        "[playground]",
        *(host_line for _, _, host_line in placed_vms),
    ]

    # Per-role groups: sorted, kebab-case role names normalized to
    # snake_case so they're valid Ansible group identifiers. Listed by
    # host name only — host vars are already declared in [playground].
    role_to_vms: dict[str, list[str]] = {}
    for name, role, _ in placed_vms:
        role_to_vms.setdefault(_role_group(role), []).append(name)
    for role_group in sorted(role_to_vms):
        lines += [
            "",
            f"[{role_group}]",
            *role_to_vms[role_group],
        ]

    lines += [
        "",
        "[playground:vars]",
        f"pg_lab={resolved.lab_name}",
        "",
    ]

    return "\n".join(lines), diagnostics


def _role_group(role: str) -> str:
    """Normalize a kebab-case role name into a snake_case Ansible group."""
    return role.replace("-", "_")


__all__ = [
    "TOFU_NO_STATE_DIAGNOSTIC_ID",
    "fetch_vm_ips",
    "render_inventory",
]
