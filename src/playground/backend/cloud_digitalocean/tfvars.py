"""Render OpenTofu tfvars for the cloud-digitalocean backend.

:func:`render_do_tfvars` produces a dict whose keys are **exactly** the
variables declared in ``tofu/cloud_digitalocean/variables.tf``.  An
explicit allowlist prevents accidental inclusion of provider-config keys
that must never leave Python (especially the API token — the token is
passed via the ``DIGITALOCEAN_TOKEN`` environment variable only and must
not appear in any tfvars file, log event, or subprocess argument).

The function is pure: no I/O, no subprocess, no network access.
"""

from __future__ import annotations

from typing import Any

from playground.backend.cloud_digitalocean.plan import DoPlan
from playground.backend.cloud_digitalocean.ssh_keys import md5_fingerprint

# Exact set of variable names declared in tofu/cloud_digitalocean/variables.tf.
# A unit test asserts this set == the variables extracted from the HCL file,
# so any drift between Python and Terraform fails fast at test time.
_TFVARS_KEYS = frozenset(
    {
        "name_prefix",
        "vm_names",
        "region",
        "size",
        "vm_sizes",
        "image",
        "vm_images",
        "custom_images",
        "vm_custom_images",
        "vm_package_upgrade",
        "ssh_public_key",
        "ssh_key_fingerprints",
        "vm_ssh_keys",
        "tags",
        "firewall_ssh_cidrs",
        "dns_domain",
        "vpc_cidr",
        "egress_isolated",
    }
)


def render_do_tfvars(
    plan: DoPlan,
    *,
    ssh_public_key: str,
    egress_isolated: bool = False,
) -> dict[str, Any]:
    """Render a tfvars dict for ``tofu/cloud_digitalocean/``.

    Keys are exactly the allowlist matching ``variables.tf``.  The API
    token is **never** included — it is passed to ``tofu apply`` via the
    ``DIGITALOCEAN_TOKEN`` environment variable only.

    :param plan: The resolved :class:`~playground.backend.cloud_digitalocean.plan.DoPlan`.
    :param ssh_public_key: SSH public key *content* (not a file path) to
        inject into each Droplet via cloud-init.  The runner reads the key
        from disk and passes it here; keeping it a parameter keeps this
        function pure and testable.
    :param egress_isolated: Whether the lab firewall's outbound rules are
        narrowed to the lab's own droplets.  A normal apply renders ``False``; only
        ``playground isolate`` flips it (by editing the last-applied tfvars
        file, not by re-rendering).

    ForceNew stability: ``image``, ``ssh_keys`` and ``user_data`` force
    Droplet replacement, so for a lab of stock Ubuntu VMs with no per-VM
    image keys the extra maps must leave all three unchanged:
    ``vm_images`` only repeats each VM's effective image, ``custom_images``
    / ``vm_custom_images`` / ``vm_ssh_keys`` are empty, and
    ``vm_package_upgrade`` is all ``True`` (the template's default).

    ``vm_ssh_keys`` maps each VM in :attr:`DoPlan.vms_needing_do_ssh_key`
    to ``[<md5 fingerprint of ssh_public_key>]`` unless that fingerprint is
    already in ``ssh_key_fingerprints`` (it would then be attached twice);
    it is empty when the key does not parse (apply refuses that case before
    rendering).
    """
    fingerprint = (
        md5_fingerprint(ssh_public_key) if plan.vms_needing_do_ssh_key else None
    )
    listed = {str(fp).lower() for fp in plan.ssh_key_fingerprints}
    vm_ssh_keys: dict[str, list[str]] = {}
    if fingerprint is not None and fingerprint not in listed:
        vm_ssh_keys = {name: [fingerprint] for name in plan.vms_needing_do_ssh_key}
    return {
        "name_prefix": plan.name_prefix,
        "vm_names": list(plan.vm_names),
        "region": plan.region,
        "size": plan.size,
        "vm_sizes": dict(plan.vm_sizes),
        "image": plan.image,
        # Custom-image VMs take their image from digitalocean_custom_image,
        # so their placeholder key is not rendered as a slug.
        "vm_images": {
            name: img
            for name, img in plan.vm_images.items()
            if name not in plan.vm_custom_images
        },
        "custom_images": {
            key: {"url": img.url, "distribution": img.distribution}
            for key, img in plan.custom_images.items()
        },
        "vm_custom_images": dict(plan.vm_custom_images),
        "vm_package_upgrade": dict(plan.vm_package_upgrade),
        "ssh_public_key": ssh_public_key,
        "ssh_key_fingerprints": list(plan.ssh_key_fingerprints),
        "vm_ssh_keys": vm_ssh_keys,
        "tags": list(plan.tags),
        "firewall_ssh_cidrs": list(plan.firewall_ssh_cidrs),
        "dns_domain": plan.dns_domain,
        "vpc_cidr": plan.vpc_cidr,
        "egress_isolated": egress_isolated,
    }


__all__ = ["render_do_tfvars"]
