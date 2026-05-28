"""Backend selection for the apply / destroy / reset / status lifecycle.

The CLI and TUI call these functions instead of reaching into a specific
backend package. Each one routes on ``ResolvedLab.backend`` to the
matching adapter:

- ``local-libvirt`` → :mod:`playground.backend.local_libvirt`
- ``local-vbox``    → :mod:`playground.backend.local_vbox`

``local-libvirt`` is tofu-centric and takes a ``tofu_dir``; ``local-vbox``
drives VBoxManage directly and ignores it. The dispatch functions present
the libvirt-style signature (with ``tofu_dir``) and simply drop it for
vbox, so callers don't branch on backend.

Unknown backends are rejected at the CLI via
:func:`unsupported_backend_diagnostic` before any of these are called
(the validator already guarantees the backend has a ProviderConfig, but
that's not the same as having an implemented adapter).
"""

from __future__ import annotations

from pathlib import Path

from playground.backend import local_libvirt, local_vbox
from playground.events import EventBus
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus
from playground.runs import OperationRun

LIBVIRT = "local-libvirt"
VBOX = "local-vbox"
SUPPORTED_BACKENDS = (LIBVIRT, VBOX)


def is_supported(backend: str) -> bool:
    return backend in SUPPORTED_BACKENDS


def unsupported_backend_diagnostic(backend: str) -> Diagnostic:
    return Diagnostic(
        id="runtime.backend.unsupported",
        severity="error",
        message=(
            f"backend {backend!r} has no implemented adapter "
            f"(supported: {', '.join(SUPPORTED_BACKENDS)})"
        ),
        source=SourceLocation(path="config"),
        suggestion=(
            "set spec.backend to one of the supported backends, or "
            "implement an adapter under src/playground/backend/"
        ),
    )


def execute_apply(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    ansible_dir: Path,
    config_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun | None, list[Diagnostic]]:
    if resolved.backend == VBOX:
        return local_vbox.execute_apply(
            resolved=resolved, state_dir=state_dir,
            ansible_dir=ansible_dir, config_dir=config_dir, bus=bus,
        )
    if resolved.backend == LIBVIRT:
        return local_libvirt.execute_apply(
            resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir,
            ansible_dir=ansible_dir, config_dir=config_dir, bus=bus,
        )
    return None, [unsupported_backend_diagnostic(resolved.backend)]


def execute_destroy(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    if resolved.backend == VBOX:
        return local_vbox.execute_destroy(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )
    # CLI guards unsupported backends before here; libvirt is the default.
    return local_libvirt.execute_destroy(
        resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
    )


def execute_reset(
    *,
    resolved: ResolvedLab,
    state_dir: Path,
    tofu_dir: Path,
    bus: EventBus,
) -> tuple[OperationRun, list[Diagnostic]]:
    if resolved.backend == VBOX:
        return local_vbox.execute_reset(
            resolved=resolved, state_dir=state_dir, bus=bus,
        )
    return local_libvirt.execute_reset(
        resolved=resolved, state_dir=state_dir, tofu_dir=tofu_dir, bus=bus,
    )


def query_status(
    resolved: ResolvedLab, tofu_dir: Path,
) -> tuple[LabStatus, list[Diagnostic]]:
    if resolved.backend == VBOX:
        return local_vbox.query_status(resolved)
    return local_libvirt.query_status(resolved, tofu_dir)


__all__ = [
    "SUPPORTED_BACKENDS",
    "execute_apply",
    "execute_destroy",
    "execute_reset",
    "is_supported",
    "query_status",
    "unsupported_backend_diagnostic",
]
