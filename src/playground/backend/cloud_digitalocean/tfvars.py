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

# Exact set of variable names declared in tofu/cloud_digitalocean/variables.tf.
# A unit test asserts this set == the variables extracted from the HCL file,
# so any drift between Python and Terraform fails fast at test time.
_TFVARS_KEYS = frozenset(
    {
        "name_prefix",
        "vm_names",
        "region",
        "size",
        "image",
        "ssh_public_key",
        "ssh_key_fingerprints",
        "tags",
        "firewall_ssh_cidrs",
        "dns_domain",
    }
)


def render_do_tfvars(plan: DoPlan, *, ssh_public_key: str) -> dict[str, Any]:
    """Render a tfvars dict for ``tofu/cloud_digitalocean/``.

    Keys are exactly the allowlist matching ``variables.tf``.  The API
    token is **never** included — it is passed to ``tofu apply`` via the
    ``DIGITALOCEAN_TOKEN`` environment variable only.

    :param plan: The resolved :class:`~playground.backend.cloud_digitalocean.plan.DoPlan`.
    :param ssh_public_key: SSH public key *content* (not a file path) to
        inject into each Droplet via cloud-init.  The runner reads the key
        from disk and passes it here; keeping it a parameter keeps this
        function pure and testable.
    """
    return {
        "name_prefix": plan.name_prefix,
        "vm_names": list(plan.vm_names),
        "region": plan.region,
        "size": plan.size,
        "image": plan.image,
        "ssh_public_key": ssh_public_key,
        "ssh_key_fingerprints": list(plan.ssh_key_fingerprints),
        "tags": list(plan.tags),
        "firewall_ssh_cidrs": list(plan.firewall_ssh_cidrs),
        "dns_domain": plan.dns_domain,
    }


__all__ = ["render_do_tfvars"]
