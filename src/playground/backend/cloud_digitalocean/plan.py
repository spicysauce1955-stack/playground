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

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from typing import Any

from playground.models.resolved import ResolvedLab

DEFAULT_REGION = "nyc3"
"""Default DigitalOcean region slug when the provider config omits ``region``."""

DEFAULT_SIZE = "s-1vcpu-1gb"
"""Default Droplet size slug when the provider config omits ``size``."""

DEFAULT_IMAGE = "ubuntu-24-04-x64"
"""Default Droplet base image slug when the provider config omits ``image``."""

DEFAULT_IMAGE_DISTRIBUTION = "Unknown"
"""Distribution label of a custom image when ``image_distribution`` is unset."""

PROVIDER_IMAGE_DISTRIBUTIONS = (
    "Arch Linux",
    "CentOS",
    "CoreOS",
    "Debian",
    "Fedora",
    "Fedora Atomic",
    "FreeBSD",
    "Gentoo",
    "openSUSE",
    "RancherOS",
    "Ubuntu",
    "Unknown",
    "Unknown OS",
)
"""``distribution`` values the OpenTofu provider accepts on
``digitalocean_custom_image``.  Its schema validation rejects anything else
at plan/validate time, before any API call -- even labels the DigitalOcean
API itself accepts, such as ``"Rocky Linux"``.  Verified against
digitalocean/digitalocean 2.100.0 and 2.106.0 with ``tofu validate``."""

UBUNTU_IMAGE_PREFIX = "ubuntu-"
"""Slug prefix of DigitalOcean's stock Ubuntu images.  Only these accept a
Droplet create without ``ssh_keys``; see :attr:`DoPlan.vms_needing_do_ssh_key`."""

CUSTOM_IMAGE_KEY_PREFIX = "img-"
"""Prefix of :func:`custom_image_key`; the image is named ``<lab>-img-<hash>``."""


def ownership_tags(lab_name: str) -> list[str]:
    """Return the three ownership tags applied to every Droplet and firewall.

    These are the canonical cleanup/ownership identifiers.  The destroy and
    suspend runners perform a tag-sweep for ``lab:<lab_name>`` to catch any
    resources that tofu state may have lost track of.

    DigitalOcean tag names allow letters, digits, ``:``, ``-``, and ``_``.

    :param lab_name: The lab name from ``ResolvedLab.lab_name``.
    """
    return ["playground", f"lab:{lab_name}", "backend:cloud-digitalocean"]


def custom_image_key(url: str) -> str:
    """Stable per-URL key of a custom image: ``img-<sha256(url)[:10]>``.

    VMs importing the same URL share one key, hence one import.  The key is
    also the tofu ``for_each`` key and the image name suffix
    (``<lab>-img-<hash>``), so it must never change for a given URL.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"{CUSTOM_IMAGE_KEY_PREFIX}{digest[:10]}"


@dataclass(frozen=True)
class CustomImage:
    """A DigitalOcean custom image the lab imports from a URL."""

    url: str
    """``https://`` URL DigitalOcean downloads the image from."""

    distribution: str
    """DigitalOcean distribution label (``"Unknown"`` by default)."""


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
    """Lab-wide Droplet size slug, e.g. ``"s-1vcpu-1gb"``.  The default for
    any VM without a per-VM override."""

    vm_sizes: dict[str, str]
    """Effective Droplet size slug for EVERY VM (VM name -> slug): the
    per-VM ``provider_overrides.cloud-digitalocean.size`` when set, else
    :attr:`size`."""

    image: str
    """Lab-wide Droplet base image slug or numeric ID (as a string), e.g.
    ``"ubuntu-24-04-x64"``.  The default for any VM without a per-VM
    override."""

    vm_images: dict[str, str]
    """Effective image for EVERY VM (VM name -> image): the per-VM
    ``provider_overrides.cloud-digitalocean.image`` when set, else
    :attr:`image`; for a VM in :attr:`vm_custom_images` it is that VM's
    custom image key (``img-<hash>``), which tofu resolves to the imported
    image's ID."""

    custom_images: dict[str, CustomImage]
    """Custom images to import, keyed by :func:`custom_image_key`.  One
    entry per distinct ``image_url``, so VMs sharing a URL share the import."""

    vm_custom_images: dict[str, str]
    """VM name -> :attr:`custom_images` key, only for VMs whose per-VM
    override sets ``image_url``."""

    package_upgrade: bool
    """Lab-wide cloud-init ``package_upgrade`` (default ``True``)."""

    vm_package_upgrade: dict[str, bool]
    """Effective cloud-init ``package_upgrade`` for EVERY VM: the per-VM
    override when it is a bool, else :attr:`package_upgrade`."""

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

    vpc_cidr: str
    """CIDR of the per-lab VPC: the lab's single network CIDR, normalised to
    its network address (DigitalOcean rejects host bits).  Empty string only
    for a lab with no network, which the validator rejects."""

    @property
    def vms_needing_do_ssh_key(self) -> list[str]:
        """VMs (declaration order) whose Droplet create needs ``ssh_keys``.

        DigitalOcean refuses to create a Droplet from a custom image, or from
        any stock image other than Ubuntu, without a DO-registered SSH key
        (HTTP 422 "The image for this droplet does not use root passwords").
        Stock Ubuntu slugs (``ubuntu-*``) accept an empty ``ssh_keys``.
        """
        return [
            name
            for name in self.vm_names
            if name in self.vm_custom_images
            or not self.vm_images[name].startswith(UBUNTU_IMAGE_PREFIX)
        ]

    @property
    def distinct_sizes(self) -> list[str]:
        """Distinct size slugs in use, in first-seen VM order."""
        return list(dict.fromkeys(self.vm_sizes[n] for n in self.vm_names))

    @property
    def vm_count(self) -> int:
        """Number of Droplets this plan will create."""
        return len(self.vm_names)


def _vm_size_override(provider_overrides: dict[str, Any]) -> str | None:
    """Return the per-VM ``cloud-digitalocean`` size override, or ``None``.

    Only a non-empty string counts; a malformed value is the validator's
    error to report (``config.backend.do_vm_size_invalid``), so the plan
    quietly falls back to the lab-wide size rather than raising.
    """
    block = provider_overrides.get("cloud-digitalocean")
    if not isinstance(block, dict):
        return None
    value = block.get("size")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _do_override_block(provider_overrides: dict[str, Any]) -> dict[str, Any]:
    """The VM's ``cloud-digitalocean`` override mapping, or ``{}``."""
    block = provider_overrides.get("cloud-digitalocean")
    return block if isinstance(block, dict) else {}


_SIGNED_INT_RE = re.compile(r"[+-]?\d+")


def normalise_image(value: Any) -> str | None:
    """Normalise an image value to a string, or ``None`` when malformed.

    A non-empty string (slug or numeric ID) is stripped; a positive integer
    image ID (YAML int) becomes its decimal string.  A numeric string must be
    positive too (``"0"`` / ``"-3"`` are not IDs).  Anything else --
    including ``bool``, which is an ``int`` subclass -- is ``None``; the
    validator reports it as ``config.backend.do_vm_image_invalid``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if _SIGNED_INT_RE.fullmatch(text) and int(text) <= 0:
            return None
        return text
    return None


def _vm_custom_image(provider_overrides: dict[str, Any]) -> CustomImage | None:
    """The VM's ``image_url`` custom image, or ``None``.

    Only an ``https://`` URL counts, and only when ``image`` is not also set
    (they are mutually exclusive; the validator reports both cases).
    """
    block = _do_override_block(provider_overrides)
    url = block.get("image_url")
    if "image" in block or not isinstance(url, str):
        return None
    url = url.strip()
    if not url.lower().startswith("https://"):
        return None
    dist = block.get("image_distribution")
    distribution = (
        dist.strip()
        if isinstance(dist, str) and dist.strip()
        else DEFAULT_IMAGE_DISTRIBUTION
    )
    return CustomImage(url=url, distribution=distribution)


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
    - ``vm_sizes`` maps every VM to its per-VM
      ``provider_overrides["cloud-digitalocean"]["size"]`` (a non-empty
      string) when set, else the lab-wide ``size``.
    - ``image`` (lab-wide) and each per-VM
      ``provider_overrides["cloud-digitalocean"]["image"]`` accept a
      non-empty string slug / numeric-string ID or a positive integer ID,
      normalised to ``str``.  ``vm_images`` maps every VM to its override,
      else the lab-wide ``image``.
    - A per-VM ``image_url`` (``https://`` only, and not together with
      ``image``) imports a custom image: ``custom_images`` holds one entry
      per distinct URL keyed by :func:`custom_image_key`,
      ``vm_custom_images`` maps the VM to that key, and ``vm_images`` holds
      the key.  When two VMs share a URL the first VM's
      ``image_distribution`` wins (the validator flags a mismatch).
    - ``package_upgrade`` (lab-wide, default ``True``) and the per-VM
      override must be bools; ``vm_package_upgrade`` covers every VM.
    - Malformed values fall back quietly; the validator reports them.
    - ``vm_names`` preserves lab declaration order (``resolved.vms``).
    - ``ssh_key_fingerprints`` defaults to an empty list.
    - ``firewall_ssh_cidrs`` is read from the nested
      ``provider_settings["firewall"]["ssh_cidrs"]`` first; the flat
      ``provider_settings["firewall_ssh_cidrs"]`` key is accepted as a
      fallback.
    - ``tags`` comes from :func:`ownership_tags`.
    - ``dns_domain`` is taken directly from ``resolved.dns_domain``.
    - ``vpc_cidr`` is the first (only; the validator enforces exactly one)
      lab network's CIDR, normalised with host bits cleared.

    :param resolved: Fully resolved, backend-neutral lab model.
    :param provider_settings: Merged provider config + lab overrides dict.
    """
    region: str = provider_settings.get("region") or DEFAULT_REGION
    size: str = provider_settings.get("size") or DEFAULT_SIZE
    image: str = normalise_image(provider_settings.get("image")) or DEFAULT_IMAGE
    raw_upgrade = provider_settings.get("package_upgrade")
    package_upgrade = raw_upgrade if isinstance(raw_upgrade, bool) else True

    vm_names: list[str] = [vm.name for vm in resolved.vms]
    vm_sizes: dict[str, str] = {
        vm.name: _vm_size_override(vm.provider_overrides) or size
        for vm in resolved.vms
    }

    custom_images: dict[str, CustomImage] = {}
    vm_custom_images: dict[str, str] = {}
    vm_images: dict[str, str] = {}
    vm_package_upgrade: dict[str, bool] = {}
    for vm in resolved.vms:
        block = _do_override_block(vm.provider_overrides)
        custom = _vm_custom_image(vm.provider_overrides)
        if custom is not None:
            key = custom_image_key(custom.url)
            custom_images.setdefault(key, custom)
            vm_custom_images[vm.name] = key
            vm_images[vm.name] = key
        else:
            vm_images[vm.name] = normalise_image(block.get("image")) or image
        vm_upgrade = block.get("package_upgrade")
        vm_package_upgrade[vm.name] = (
            vm_upgrade if isinstance(vm_upgrade, bool) else package_upgrade
        )

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

    vpc_cidr = _vpc_cidr(resolved)

    return DoPlan(
        lab_name=resolved.lab_name,
        name_prefix=resolved.lab_name,
        region=region,
        size=size,
        vm_sizes=vm_sizes,
        image=image,
        vm_images=vm_images,
        custom_images=custom_images,
        vm_custom_images=vm_custom_images,
        package_upgrade=package_upgrade,
        vm_package_upgrade=vm_package_upgrade,
        vm_names=vm_names,
        ssh_key_fingerprints=ssh_key_fingerprints,
        firewall_ssh_cidrs=firewall_ssh_cidrs,
        tags=tags,
        dns_domain=resolved.dns_domain,
        vpc_cidr=vpc_cidr,
    )


def _vpc_cidr(resolved: ResolvedLab) -> str:
    """Return the lab network CIDR as a canonical network address, or ``""``."""
    if not resolved.networks:
        return ""
    raw = resolved.networks[0].cidr
    try:
        return str(ipaddress.ip_network(raw, strict=False))
    except ValueError:
        return raw


__all__ = [
    "CUSTOM_IMAGE_KEY_PREFIX",
    "DEFAULT_IMAGE",
    "DEFAULT_IMAGE_DISTRIBUTION",
    "DEFAULT_REGION",
    "DEFAULT_SIZE",
    "PROVIDER_IMAGE_DISTRIBUTIONS",
    "UBUNTU_IMAGE_PREFIX",
    "CustomImage",
    "DoPlan",
    "build_do_plan",
    "custom_image_key",
    "normalise_image",
    "ownership_tags",
]
