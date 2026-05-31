"""Pure provisioning plan for the cloud-digitalocean backend.

:func:`build_do_plan` translates a :class:`~playground.models.resolved.ResolvedLab`
plus already-merged provider settings into a :class:`DoPlan` dataclass.
No subprocesses, no I/O, no network — fully unit-testable.

The caller (the runner, in the next slice) merges the provider config
defaults with lab-level overrides and passes the result as
``provider_settings``. Tests pass the dict directly.

DigitalOcean tag names allow letters, digits, ``:``, ``-``, and ``_``.
The :func:`ownership_tags` function returns a stable set of three tags
that downstream operations (status, suspend, destroy) use for tag-based
sweeps to catch orphaned, still-billing resources.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playground.models.resolved import ResolvedLab

DEFAULT_REGION = "nyc3"
"""Default DigitalOcean region slug when the provider config omits ``region``."""

DEFAULT_SIZE = "s-1vcpu-1gb"
"""Default Droplet size slug when the provider config omits ``size``."""

DEFAULT_IMAGE = "ubuntu-24-04-x64"
"""Default Droplet base image slug when the provider config omits ``image``."""


def ownership_tags(lab_name: str) -> list[str]:
    """Return the three ownership tags applied to every Droplet and firewall.

    These are the canonical cleanup/ownership identifiers.  The destroy and
    suspend runners perform a tag-sweep for ``lab:<lab_name>`` to catch any
    resources that tofu state may have lost track of.

    DigitalOcean tag names allow letters, digits, ``:``, ``-``, and ``_``.

    :param lab_name: The lab name from ``ResolvedLab.lab_name``.
    """
    return ["playground", f"lab:{lab_name}", "backend:cloud-digitalocean"]


@dataclass(frozen=True)
class DoPlan:
    """Full provisioning plan for one cloud-digitalocean lab run.

    All fields are resolved at plan time from the ``ResolvedLab`` and
    ``provider_settings``; the runner uses this struct as its authoritative
    source of truth.
    """

    lab_name: str
    """Lab name — matches ``ResolvedLab.lab_name``."""

    name_prefix: str
    """Prefix prepended to every Droplet name; equals ``lab_name``."""

    region: str
    """DigitalOcean region slug, e.g. ``"nyc3"``."""

    size: str
    """Droplet size slug, e.g. ``"s-1vcpu-1gb"``."""

    image: str
    """Droplet base image slug, e.g. ``"ubuntu-24-04-x64"``."""

    vm_names: list[str]
    """VM names in lab declaration order.  Each maps to one Droplet named
    ``<name_prefix>-<vm_name>`` in the OpenTofu root."""

    ssh_key_fingerprints: list[str]
    """DigitalOcean-registered SSH key fingerprints (belt-and-braces with
    the cloud-init injection; may be empty)."""

    firewall_ssh_cidrs: list[str]
    """Source CIDRs allowed to reach port 22.  Empty list means allow all
    (0.0.0.0/0 + ::/0); the doctor warns when this is empty."""

    tags: list[str]
    """Ownership tags applied to every Droplet and the firewall."""

    dns_domain: str
    """Per-lab DNS domain, used for cloud-init hostname / fqdn."""

    @property
    def vm_count(self) -> int:
        """Number of Droplets this plan will create."""
        return len(self.vm_names)


def build_do_plan(
    resolved: ResolvedLab,
    *,
    provider_settings: dict[str, Any],
) -> DoPlan:
    """Build a :class:`DoPlan` from a resolved lab. Pure.

    ``provider_settings`` is the already-merged dict of provider-config
    defaults plus lab-level overrides (the runner merges them in the next
    slice; tests pass the dict directly).

    Resolution rules:

    - ``region``, ``size``, ``image`` fall back to the module defaults when
      the key is absent or falsy in ``provider_settings``.
    - ``vm_names`` preserves lab declaration order (``resolved.vms``).
    - ``ssh_key_fingerprints`` defaults to an empty list.
    - ``firewall_ssh_cidrs`` is read from the nested
      ``provider_settings["firewall"]["ssh_cidrs"]`` first; the flat
      ``provider_settings["firewall_ssh_cidrs"]`` key is accepted as a
      fallback.
    - ``tags`` comes from :func:`ownership_tags`.
    - ``dns_domain`` is taken directly from ``resolved.dns_domain``.

    :param resolved: Fully resolved, backend-neutral lab model.
    :param provider_settings: Merged provider config + lab overrides dict.
    """
    region: str = provider_settings.get("region") or DEFAULT_REGION
    size: str = provider_settings.get("size") or DEFAULT_SIZE
    image: str = provider_settings.get("image") or DEFAULT_IMAGE

    vm_names: list[str] = [vm.name for vm in resolved.vms]

    ssh_key_fingerprints: list[str] = list(
        provider_settings.get("ssh_key_fingerprints") or []
    )

    # firewall.ssh_cidrs (nested) wins; flat firewall_ssh_cidrs key is a
    # fallback for callers that pre-flatten the provider dict.
    firewall_block = provider_settings.get("firewall") or {}
    if isinstance(firewall_block, dict):
        ssh_cidrs_raw = firewall_block.get("ssh_cidrs")
    else:
        ssh_cidrs_raw = None
    if ssh_cidrs_raw is None:
        ssh_cidrs_raw = provider_settings.get("firewall_ssh_cidrs")
    firewall_ssh_cidrs: list[str] = list(ssh_cidrs_raw or [])

    tags = ownership_tags(resolved.lab_name)

    return DoPlan(
        lab_name=resolved.lab_name,
        name_prefix=resolved.lab_name,
        region=region,
        size=size,
        image=image,
        vm_names=vm_names,
        ssh_key_fingerprints=ssh_key_fingerprints,
        firewall_ssh_cidrs=firewall_ssh_cidrs,
        tags=tags,
        dns_domain=resolved.dns_domain,
    )


__all__ = [
    "DEFAULT_IMAGE",
    "DEFAULT_REGION",
    "DEFAULT_SIZE",
    "DoPlan",
    "build_do_plan",
    "ownership_tags",
]
