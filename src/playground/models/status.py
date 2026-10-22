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

DeviceState = Literal["connected", "unauthorized", "offline", "missing"]
"""``unauthorized`` is deliberately distinct from ``missing``: the handset
is attached and visible, but its RSA trust prompt was never accepted. It
is the most common real-world failure and needs a diagnostic that names
the remedy rather than one that says "not reachable"."""


class DeviceStatus(StrictModel):
    """Intent + observed state for one physical device slot."""

    name: str
    state: DeviceState
    serial: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    sdk: int | None = None
    abi: str | None = None
    rooted: bool | None = None
    last_verified: str | None = None
    rebind_candidates: list[str] | None = None
    """Serials of attached handsets that match this slot's selector while
    the slot's own bound serial is absent -- the cable-swap case.

    Observed state, like ``serial``: it is what a live `adb devices -l`
    saw, never anything config declared. ``None`` (not ``[]``) when the
    question does not arise, so `model_dump(exclude_none=True)` leaves
    every other slot's JSON byte-identical to what it was before this
    field existed.

    Carrying the fact HERE rather than as a diagnostic out of
    `query_status` is what lets `status` report a swapped handset without
    hard-failing (`_exit_on_errors` aborts on any error diagnostic) while
    `app`/`adb` still refuse to act on the slot with
    `device.rebind_required` rather than the misleading
    `device.not_connected`. Nothing in this path ever rebinds: only
    `apply --rebind` does."""


class VmStatus(StrictModel):
    """Intent + observed state for one VM."""

    name: str
    role: str
    ip: str | None = None
    """Primary address of the VM. libvirt: the DHCP lease; vbox: the
    guest-side address; cloud-digitalocean: the PRIVATE VPC IPv4 (``None``
    when the Droplet has none -- it never falls back to the public one).
    Display/in-lab addressing only; anything that CONNECTS to the VM must
    use ``ssh_host`` / ``ssh_port``."""
    public_ip: str | None = None
    """Publicly routable IPv4, set only by backends whose VMs have both a
    private and a public address (cloud-digitalocean). ``None`` on local
    backends, so their JSON output is unchanged (``exclude_none``)."""
    state: VmState
    provider_id: str | None = None
    """Cloud provider resource ID (e.g. DigitalOcean Droplet ID as a string).
    ``None`` for local backends (libvirt, vbox) that have no external resource ID."""
    ssh_host: str | None = None
    """Reachable SSH host for a backend-neutral consumer.
    libvirt sets this to the VM IP; cloud-digitalocean to the PUBLIC IP
    (not ``ip``, which is the private VPC address); vbox uses
    ``127.0.0.1`` (NAT port-forward). ``None`` when the VM is not
    yet provisioned or otherwise unreachable."""
    ssh_port: int | None = None
    """Reachable SSH port for a backend-neutral consumer.
    libvirt and cloud backends use 22; vbox uses the per-VM
    NAT-forwarded host port. ``None`` when the VM is not yet
    provisioned or otherwise unreachable."""


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
    devices: list[DeviceStatus] = Field(default_factory=list)


__all__ = ["DeviceState", "DeviceStatus", "LabStatus", "VmState", "VmStatus"]
