"""Tolerant validation for commands that do not provision or render.

Some validator errors only guard NEW provisioning (for example the
DigitalOcean single-network / VPC CIDR / VM size rules). A lab provisioned
before those rules existed must stay reachable and tear-down-able, so every
verb that only reads, connects, or tears down downgrades them to warnings.
Provisioning and rendering verbs (validate, plan, apply, resume, tofu render,
inventory render) keep calling the strict ``playground.validation.validate``.
"""

from __future__ import annotations

from playground.config.loader import LoadedConfig
from playground.models.diagnostic import Diagnostic
from playground.validation import validate as validate_loaded_config

_DOWNGRADED_IDS = frozenset(
    {
        "config.network.do_single_network",
        "config.network.do_vpc_cidr_invalid",
        "config.backend.do_vm_size_invalid",
    }
)


def validate_tolerant(loaded: LoadedConfig, lab: str | None = None) -> list[Diagnostic]:
    """Validate, turning provisioning-only DO errors into warnings."""
    return [
        d.model_copy(update={"severity": "warning"})
        if d.severity == "error" and d.id in _DOWNGRADED_IDS
        else d
        for d in validate_loaded_config(loaded, lab=lab)
    ]
