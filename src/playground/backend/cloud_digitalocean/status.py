"""Query the cloud-digitalocean backend for a lab's observed state.

Source of truth is the live DigitalOcean API (tag-based lookup) — no tofu
state file is required.  This matches the vbox status shape: dispatch
routes to ``query_status(resolved)`` dropping the ``tofu_dir`` argument.
"""

from __future__ import annotations

from typing import Any

from playground.backend.cloud_digitalocean.do import (
    droplet_summary,
    list_droplets_by_tag,
    read_token,
    token_env_name,
)
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus, VmState, VmStatus


def query_status(resolved: ResolvedLab) -> tuple[LabStatus, list[Diagnostic]]:
    """Build a :class:`LabStatus` from live DigitalOcean Droplet state.

    When the API token is absent all VMs are reported as ``missing`` and a
    warning diagnostic is appended (env-var NAME only, never a value).

    Droplet-to-VM matching is by name: the expected Droplet name for lab VM
    ``<vm>`` is ``<lab>-<vm>`` (mirrors the tofu ``name_prefix`` convention).

    Droplets tagged ``lab:<lab>`` whose names are not expected VM names are
    reported in ``unknown_vms``.
    """
    lab = resolved.lab_name
    diagnostics: list[Diagnostic] = []

    token = read_token(resolved)
    if not token:
        env_name = token_env_name(resolved)
        missing_vms = [
            VmStatus(name=vm.name, role=vm.role, ip=None, state="missing")
            for vm in resolved.vms
        ]
        diagnostics.append(
            Diagnostic(
                id="runtime.status.token_missing",
                severity="warning",
                message=f"set ${env_name} to query DigitalOcean state",
                source=SourceLocation(path="environment"),
                suggestion=(
                    f"export {env_name}=<your-token> and retry"
                ),
            )
        )
        return (
            LabStatus(
                lab=lab,
                backend=resolved.backend,
                expected_vms=len(resolved.vms),
                provisioned_vms=0,
                vms=missing_vms,
                unknown_vms=[],
            ),
            diagnostics,
        )

    droplets, api_diags, list_ok = list_droplets_by_tag(token, f"lab:{lab}")
    diagnostics.extend(api_diags)

    if not list_ok:
        # Provider was unreachable or returned an error; we cannot determine
        # the real state.  Escalate the diagnostic to error severity so
        # callers/scripts key off it, and surface UNKNOWN rather than lying
        # that every VM is "missing".
        escalated = [
            diag.model_copy(update={"severity": "error"}) for diag in api_diags
        ]
        # Replace the warning copies already added with the escalated ones.
        for diag in api_diags:
            diagnostics.remove(diag)
        diagnostics.extend(escalated)
        diagnostics.append(
            Diagnostic(
                id="runtime.status.provider_unreachable",
                severity="error",
                message=(
                    "DigitalOcean API request failed; VM state is UNKNOWN "
                    "— provider was unreachable, not confirmed absent"
                ),
                source=SourceLocation(path="DigitalOcean API"),
                suggestion=(
                    "check network connectivity and that $DIGITALOCEAN_TOKEN "
                    "is valid; retry `playground status`"
                ),
            )
        )
        # Return the LabStatus with all VMs as "missing" but with error
        # diagnostics so the caller knows this is an API failure, not a
        # confirmed teardown.
        missing_vms = [
            VmStatus(name=vm.name, role=vm.role, ip=None, state="missing")
            for vm in resolved.vms
        ]
        return (
            LabStatus(
                lab=lab,
                backend=resolved.backend,
                expected_vms=len(resolved.vms),
                provisioned_vms=0,
                vms=missing_vms,
                unknown_vms=[],
            ),
            diagnostics,
        )

    # Build a map from droplet name -> summary dict.
    droplet_map: dict[str, dict[str, Any]] = {}
    for d in droplets:
        d_summary = droplet_summary(d)
        dname = d_summary.get("name")
        if dname:
            droplet_map[dname] = d_summary

    # Declared VM names (prefixed).
    declared_droplet_names: set[str] = set()
    vm_statuses: list[VmStatus] = []

    for vm in resolved.vms:
        expected_name = f"{lab}-{vm.name}"
        declared_droplet_names.add(expected_name)
        vm_summary: dict[str, Any] | None = droplet_map.get(expected_name)
        if vm_summary is not None:
            do_status = vm_summary.get("status")
            state: VmState = "running" if do_status == "active" else "provisioned"
            raw_ip = vm_summary.get("public_ipv4")
            ip: str | None = str(raw_ip) if raw_ip is not None else None
            raw_id = vm_summary.get("id")
            provider_id: str | None = str(raw_id) if raw_id is not None else None
        else:
            state = "missing"
            ip = None
            provider_id = None
        vm_statuses.append(
            VmStatus(
                name=vm.name,
                role=vm.role,
                ip=ip,
                state=state,
                provider_id=provider_id,
                ssh_host=ip if ip is not None else None,
                ssh_port=22 if ip is not None else None,
            )
        )

    # Droplets present in the API but not declared in the lab spec.
    # Every returned droplet is lab-tagged, so ALL undeclared ones are
    # unknown.  Strip the "<lab>-" prefix if present so the reported name
    # matches the VM name used in the lab YAML; for non-conforming names
    # the raw droplet name is reported as-is.
    prefix = f"{lab}-"
    unknown_vms: list[str] = sorted(
        dname[len(prefix):] if dname.startswith(prefix) else dname
        for dname in droplet_map
        if dname not in declared_droplet_names
    )

    provisioned_vms = sum(
        1 for v in vm_statuses if v.state in ("running", "provisioned")
    )

    return (
        LabStatus(
            lab=lab,
            backend=resolved.backend,
            expected_vms=len(resolved.vms),
            provisioned_vms=provisioned_vms,
            vms=vm_statuses,
            unknown_vms=unknown_vms,
        ),
        diagnostics,
    )


__all__ = ["query_status"]
