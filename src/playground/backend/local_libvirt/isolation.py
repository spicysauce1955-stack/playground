"""Post-provision egress isolation for local-libvirt labs (``isolate`` / ``connect``).

STUB: signatures are the cross-agent contract; bodies land in the
libvirt isolation slice. Enforcement is a libvirt nwfilter referenced
from every lab VM interface, so libvirtd applies it on the host.
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
    """Cut every lab VM's egress outside the lab's own networks. Never raises."""
    raise NotImplementedError


def execute_connect(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    """Undo :func:`execute_isolate`. Never raises."""
    raise NotImplementedError


def remove_isolation_filter(lab_name: str) -> list[Diagnostic]:
    """Best-effort: undefine the lab's nwfilter after its domains are gone."""
    raise NotImplementedError


__all__ = ["execute_connect", "execute_isolate", "remove_isolation_filter"]
