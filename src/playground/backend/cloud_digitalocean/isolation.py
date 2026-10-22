"""Post-provision egress isolation for cloud-digitalocean labs (``isolate`` / ``connect``).

STUB: signatures are the cross-agent contract; bodies land in the
DigitalOcean isolation slice. Enforcement is the lab's Cloud Firewall
(outbound rules narrowed to the lab VPC CIDR).
"""

from __future__ import annotations

from pathlib import Path

from playground.events import EventBus
from playground.models.diagnostic import Diagnostic
from playground.models.resolved import ResolvedLab
from playground.runs import OperationRun


def execute_isolate(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Narrow the lab firewall's outbound rules to the lab VPC. Never raises."""
    raise NotImplementedError


def execute_connect(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Undo :func:`execute_isolate`. Never raises."""
    raise NotImplementedError


__all__ = ["execute_connect", "execute_isolate"]
