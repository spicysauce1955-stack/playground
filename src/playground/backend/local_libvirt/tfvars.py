"""Render an OpenTofu ``-var-file`` payload from :class:`ResolvedLab`.

Closes the last manual handoff between the lab YAML and the tofu module.
Without this, the operator has to copy ``lab.spec.vms[*].name`` into
``var.vm_names`` (in ``tofu/terraform.tfvars`` or via ``-var``) by hand for
``playground inventory render`` to pair lab VMs with tofu IPs.

Scope: today the renderer only emits ``vm_names``. Per-VM resources
(``memory_mb``, ``vcpu``, ``disk_gb``) are intentionally **not** emitted —
the current ``tofu/main.tf`` accepts only global ``var.vm_memory`` /
``var.vm_vcpu`` and a hardcoded 20 GB disk. The
``config.backend.per_vm_resources_unsupported`` validator warning fires
when the lab declares heterogeneous per-VM resources that today's tofu
cannot honor; a future slice will enrich tofu to accept per-VM resources
and the renderer will start emitting them.

This module is a pure data transformer — see ``validator.py`` for the
diagnostics surface.
"""

from __future__ import annotations

from typing import Any

from playground.models.resolved import ResolvedLab


def render_tfvars(resolved: ResolvedLab) -> dict[str, Any]:
    """Produce a tofu ``-var-file`` payload (JSON-shaped dict) for ``resolved``.

    Pure function: no I/O, no diagnostics. The caller serializes the
    returned dict with :func:`json.dumps` and writes it under
    ``.playground/state/tofu/``. Backend-capability warnings about
    heterogeneous per-VM resources are emitted by
    :func:`playground.validation.validate` so they also surface under
    ``playground validate``.
    """
    return {
        "vm_names": [vm.name for vm in resolved.vms],
    }


__all__ = ["render_tfvars"]
