"""Backend-neutral status of a deployed lab.

Status is a snapshot comparison between **intent** (``ResolvedLab.vms``,
``ResolvedLab.networks``) and **observed state** (today: tofu's output).
Backends may grow richer observed state later (ansible reachability,
docker readiness); the shape stays additive.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from playground.models.base import StrictModel

VmState = Literal["provisioned", "missing", "running", "failed", "degraded"]
"""Today only ``provisioned`` and ``missing`` are emitted (tofu observation).
``running`` / ``failed`` / ``degraded`` are reserved for the slice that adds
ansible reachability and docker readiness — widening the literal then would
break older JSON consumers and pattern-matches, so they're declared now."""


class VmStatus(StrictModel):
    """Intent + observed state for one VM."""

    name: str
    role: str
    ip: str | None = None
    state: VmState


class LabStatus(StrictModel):
    """Snapshot of a lab's observed state.

    Today only tofu-side provisioning is reported. ``provisioned_vms``
    is the count of ``vms`` whose ``state == "provisioned"``.
    """

    lab: str
    backend: str
    expected_vms: int
    provisioned_vms: int
    vms: list[VmStatus] = Field(default_factory=list)
    unknown_vms: list[str] = Field(default_factory=list)
    """Backend domain names present in observed state but not declared
    in the lab. Backend-neutral — even though today's data comes from
    libvirt domains, the field name avoids leaking that vocabulary."""


__all__ = ["LabStatus", "VmState", "VmStatus"]
