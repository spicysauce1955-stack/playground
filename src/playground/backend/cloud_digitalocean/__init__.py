"""DigitalOcean cloud backend adapter.

Provisions VMs as DigitalOcean Droplets via a committed OpenTofu root
(``tofu/cloud_digitalocean/``) copied per-lab under
``.playground/state/cloud-digitalocean/<lab>/``. Droplets have routable
public IPs so the configure half (wait-for-vms-ready -> ansible-playbook
-> verify-lab) uses ``ssh_port=22`` with no NAT port-forward. The
configure half is the same backend-neutral code the libvirt and vbox
adapters use.

The API token is passed via the ``DIGITALOCEAN_TOKEN`` environment
variable only — it never appears in any ``.tf`` file, tfvars dict, or
log event.

See ``docs/architecture/cloud_digitalocean_design.md`` for the full
design and ``docs/architecture/CONTRACTS.md`` for pipeline contracts.
"""

from playground.backend.cloud_digitalocean.plan import (
    DEFAULT_IMAGE,
    DEFAULT_REGION,
    DEFAULT_SIZE,
    DoPlan,
    build_do_plan,
    ownership_tags,
)
from playground.backend.cloud_digitalocean.pricing import estimate_cost
from playground.backend.cloud_digitalocean.runner import (
    execute_apply,
    execute_destroy,
    execute_reset,
    execute_resume,
    execute_suspend,
)
from playground.backend.cloud_digitalocean.settings import merge_provider_settings
from playground.backend.cloud_digitalocean.status import query_status
from playground.backend.cloud_digitalocean.tfvars import render_do_tfvars

__all__ = [
    "DEFAULT_IMAGE",
    "DEFAULT_REGION",
    "DEFAULT_SIZE",
    "DoPlan",
    "build_do_plan",
    "estimate_cost",
    "execute_apply",
    "execute_destroy",
    "execute_reset",
    "execute_resume",
    "execute_suspend",
    "merge_provider_settings",
    "ownership_tags",
    "query_status",
    "render_do_tfvars",
]
