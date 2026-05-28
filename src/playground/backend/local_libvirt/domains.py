"""Probe libvirt domain states post-create to fail fast on QEMU crashes.

Without this, ``wait-for-vms-ready`` would spend its full SSH timeout
(~5 min) trying to TCP-connect to a guest that QEMU killed at startup.
By asking libvirt for each domain's state right after tofu-apply
returns, we can surface a clear, actionable diagnostic in seconds.

The canonical "host can't run this guest" failure mode is QEMU pausing
or instantly crashing the domain because the L0 hypervisor (the
machine that's running *this* host as a VM, on a nested-virt setup)
won't tolerate the VMX flags propagated by
``cpu mode='host-passthrough'``. The kernel logs
``kvm_intel: vmread/vmwrite failed`` and the domain ends up in state
``paused (crashed)`` or ``shut off (crashed)``. Symptom we used to see:
a 5-minute "context deadline exceeded waiting for DHCP" from tofu's
``wait_for_lease`` polling — misleading, because DHCP was never the
issue.
"""

from __future__ import annotations

import shutil
import subprocess

from playground.models.diagnostic import Diagnostic, SourceLocation

VIRSH = "virsh"
VIRSH_URI = "qemu:///system"

# Substrings in `virsh domstate --reason` output that mean
# "this VM is NOT running" right after tofu-apply. We treat all
# non-running states as wrong at this point in the pipeline because:
#   - "crashed" / "shut off (crashed)"  → QEMU killed the guest.
#   - "paused (crashed|io error|watchdog)" → KVM/QEMU paused on error.
#   - "paused (unknown)" → libvirt's catchall when it can't classify
#       the pause reason; commonly seen when the L0 hypervisor rejects
#       VMX passthrough so the guest goes paused right after start.
#   - "shut off" without a reason → the guest never stayed up.
# Match by substring so libvirt's exact wording across versions is
# tolerated.
_NON_RUNNING_MARKERS = ("crashed", "shut off", "paused")


def check_domains_running(
    vm_names: list[str], *, lab: str,
) -> list[Diagnostic]:
    """Probe ``virsh domstate`` for each VM; flag any that aren't running.

    Returns one ``runtime.apply.libvirt_domain_crashed`` diagnostic per
    non-running VM (empty list when everything is running, or when
    ``virsh`` itself isn't on PATH — doctor catches that separately).
    "Non-running" includes ``shut off`` and ``paused`` in addition to
    explicit ``crashed`` states: post-tofu-apply, any state other than
    ``running`` means the apply pipeline can't make progress and
    ``wait-for-vms-ready`` would silently burn its full SSH timeout
    probing TCP :22 on a guest that never came up. We surface a clear
    diagnostic in seconds instead.
    """
    if shutil.which(VIRSH) is None:
        return []
    diagnostics: list[Diagnostic] = []
    for name in vm_names:
        state = _domstate(name)
        if state is None:
            continue
        lowered = state.lower()
        if any(marker in lowered for marker in _NON_RUNNING_MARKERS):
            diagnostics.append(
                Diagnostic(
                    id="runtime.apply.libvirt_domain_crashed",
                    severity="error",
                    message=(
                        f"libvirt domain {name!r} is in state {state!r} "
                        "post-tofu-apply — the guest isn't running, so "
                        "this isn't a cloud-init/DHCP issue (tofu's "
                        "`wait_for_lease` timeout is a misleading symptom)"
                    ),
                    source=SourceLocation(path=name),
                    suggestion=(
                        "check `journalctl --since '5 minutes ago' "
                        "| grep -i kvm_intel` for `vmread/vmwrite failed` "
                        "(L0 hypervisor refusing VMX passthrough on a "
                        "nested-virt host). Workaround: set "
                        "`spec.providers.local-libvirt.cpu_mode: "
                        f"host-model` in the lab YAML, then `playground "
                        f"reset {lab}` and re-apply."
                    ),
                )
            )
    return diagnostics


def _domstate(name: str) -> str | None:
    """Return ``virsh domstate --reason <name>`` text, or ``None`` when
    the domain is absent or virsh fails to query it."""
    try:
        result = subprocess.run(  # noqa: S603 — explicit args, no shell
            [VIRSH, "-c", VIRSH_URI, "domstate", "--reason", name],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


__all__ = ["check_domains_running"]
