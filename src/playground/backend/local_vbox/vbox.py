"""Thin wrappers over the ``VBoxManage`` CLI.

This is the I/O edge of the local-vbox backend, analogous to
``local_libvirt/apply.py``. Each helper shells out to ``VBoxManage`` with
explicit args (never a shell), tees a ``$ cmd`` line plus captured output
to a step log, and publishes a ``log_line`` event so the TUI can render
progress. The wrappers do not interpret VirtualBox output beyond exit
codes and do not retry.

The VM create sequence (per VM) is:

1. ``createvm --register``
2. ``clonemedium`` the cached base VDI to a per-VM disk, then
   ``modifymedium --resize`` to the lab's disk size
3. ``modifyvm`` memory / cpus / firmware / NICs (NIC1 NAT with an SSH
   port-forward; one intnet NIC per lab network) + MACs
4. attach the disk (SATA) and the cloud-init seed ISO (IDE dvddrive)
5. ``startvm --type headless``

Teardown is ``controlvm poweroff`` (best-effort) then
``unregistervm --delete``.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

from playground.backend.local_vbox.cloudinit import _colonize
from playground.backend.local_vbox.plan import VboxVmPlan
from playground.events import EventBus
from playground.models.diagnostic import Diagnostic, SourceLocation

VBOXMANAGE = "VBoxManage"
OSTYPE = "Ubuntu_64"
"""Generic 64-bit Ubuntu guest type; fine for the Noble cloud image."""

LogWrite = Callable[[str], None]


def vboxmanage_available() -> bool:
    return shutil.which(VBOXMANAGE) is not None


def run_vbox(
    args: list[str],
    *,
    log: LogWrite,
    bus: EventBus | None = None,
    run_id: str | None = None,
    step: str = "vbox-create",
    timeout: float = 1800.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``VBoxManage <args>``, tee to the step log + bus."""
    cmd = [VBOXMANAGE, *args]
    log(f"$ {' '.join(cmd)}\n")
    if bus is not None and run_id is not None:
        bus.publish(run_id, "log_line", {"step": step, "line": " ".join(cmd)})
    try:
        result = subprocess.run(  # noqa: S603 — explicit args, no shell
            cmd, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT after {timeout}s\n")
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="timeout")
    if result.stdout:
        log(result.stdout if result.stdout.endswith("\n") else result.stdout + "\n")
    if result.stderr:
        log(result.stderr if result.stderr.endswith("\n") else result.stderr + "\n")
    return result


def list_vms() -> list[str]:
    """Names of all registered VMs (empty list if VBoxManage missing)."""
    if not vboxmanage_available():
        return []
    result = subprocess.run(  # noqa: S603
        [VBOXMANAGE, "list", "vms"], capture_output=True, text=True, check=False,
    )
    names: list[str] = []
    for line in result.stdout.splitlines():
        # Format: "<name>" {uuid}
        line = line.strip()
        if line.startswith('"'):
            names.append(line.split('"')[1])
    return names


def list_running_vms() -> list[str]:
    """Names of currently-running VMs (empty if VBoxManage missing)."""
    if not vboxmanage_available():
        return []
    result = subprocess.run(  # noqa: S603
        [VBOXMANAGE, "list", "runningvms"], capture_output=True, text=True, check=False,
    )
    names: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith('"'):
            names.append(line.split('"')[1])
    return names


def vm_exists(name: str) -> bool:
    return name in list_vms()


def vm_running(name: str) -> bool:
    return name in list_running_vms()


def nat_ssh_port(name: str) -> int | None:
    """Host port of the NIC1 ``ssh`` NAT port-forward for ``name``.

    Lets a re-apply reuse an existing VM on the same port instead of
    creating a duplicate. Returns ``None`` if the VM or rule is absent.
    """
    if not vboxmanage_available():
        return None
    result = subprocess.run(  # noqa: S603
        [VBOXMANAGE, "showvminfo", name, "--machinereadable"],
        capture_output=True, text=True, check=False,
    )
    for line in result.stdout.splitlines():
        # Forwarding(0)="ssh,tcp,127.0.0.1,2222,,22"
        if line.startswith("Forwarding(") and '"ssh,' in line:
            value = line.split("=", 1)[1].strip().strip('"')
            parts = value.split(",")
            if len(parts) >= 4 and parts[3].isdigit():
                return int(parts[3])
    return None


def pick_free_ports(count: int, *, start: int = 2222) -> list[int]:
    """Return ``count`` free TCP ports on 127.0.0.1, scanning up from
    ``start``. Used for the per-VM NAT SSH port-forwards."""
    ports: list[int] = []
    candidate = start
    while len(ports) < count and candidate < start + 1000:
        if _port_free(candidate):
            ports.append(candidate)
        candidate += 1
    return ports


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def create_vm(
    vm: VboxVmPlan,
    *,
    base_vdi: Path,
    seed_iso: Path,
    disk_path: Path,
    ssh_host_port: int,
    log: LogWrite,
    bus: EventBus | None = None,
    run_id: str | None = None,
) -> list[Diagnostic]:
    """Create, configure and boot one VM. Returns diagnostics (empty = OK).

    Stops and reports at the first failing VBoxManage command.
    """
    def step(args: list[str], what: str) -> Diagnostic | None:
        result = run_vbox(args, log=log, bus=bus, run_id=run_id)
        if result.returncode != 0:
            return Diagnostic(
                id="runtime.vbox.create_failed",
                severity="error",
                message=(
                    f"VM {vm.vbox_name!r}: {what} failed "
                    f"(VBoxManage exit {result.returncode}): "
                    f"{result.stderr.strip()[:300] or '(no stderr)'}"
                ),
                source=SourceLocation(path=vm.vbox_name),
            )
        return None

    # 1. register the VM
    if (d := step(
        ["createvm", "--name", vm.vbox_name, "--ostype", OSTYPE, "--register"],
        "createvm",
    )):
        return [d]

    # 2. clone base disk -> per-VM disk, then resize
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    if disk_path.exists():
        disk_path.unlink()
    if (d := step(
        ["clonemedium", "disk", str(base_vdi), str(disk_path), "--format", "VDI"],
        "clonemedium",
    )):
        return [d]
    resize_mb = max(vm.disk_gb * 1024, 1024)
    if (d := step(
        ["modifymedium", "disk", str(disk_path), "--resize", str(resize_mb)],
        "modifymedium --resize",
    )):
        return [d]

    # 3. base hardware + NICs
    modify = [
        "modifyvm", vm.vbox_name,
        "--memory", str(vm.memory_mb),
        "--cpus", str(vm.vcpu),
        "--firmware", "bios",
        "--boot1", "disk",
        "--graphicscontroller", "vmsvga",
    ]
    for nic in vm.nics:
        i = nic.index
        modify += [f"--macaddress{i}", nic.mac.upper()]
        if nic.kind == "nat":
            modify += [f"--nic{i}", "nat"]
        else:
            modify += [f"--nic{i}", "intnet", f"--intnet{i}", nic.intnet_name or "lab"]
    if (d := step(modify, "modifyvm")):
        return [d]

    # NAT SSH port-forward on NIC1.
    if (d := step(
        [
            "modifyvm", vm.vbox_name,
            "--natpf1", f"ssh,tcp,127.0.0.1,{ssh_host_port},,22",
        ],
        "modifyvm --natpf1",
    )):
        return [d]

    # 4. storage: SATA disk + IDE dvddrive for the seed ISO
    if (d := step(
        ["storagectl", vm.vbox_name, "--name", "SATA", "--add", "sata",
         "--controller", "IntelAhci", "--portcount", "2"],
        "storagectl SATA",
    )):
        return [d]
    if (d := step(
        ["storageattach", vm.vbox_name, "--storagectl", "SATA",
         "--port", "0", "--device", "0", "--type", "hdd", "--medium", str(disk_path)],
        "storageattach disk",
    )):
        return [d]
    if (d := step(
        ["storagectl", vm.vbox_name, "--name", "IDE", "--add", "ide"],
        "storagectl IDE",
    )):
        return [d]
    if (d := step(
        ["storageattach", vm.vbox_name, "--storagectl", "IDE",
         "--port", "0", "--device", "0", "--type", "dvddrive", "--medium", str(seed_iso)],
        "storageattach seed ISO",
    )):
        return [d]

    # 5. boot headless
    if (d := step(["startvm", vm.vbox_name, "--type", "headless"], "startvm")):
        return [d]

    log(f"# {vm.vbox_name}: created, MAC1={_colonize(vm.nics[0].mac)}, "
        f"ssh 127.0.0.1:{ssh_host_port}\n")
    return []


def destroy_vm(
    name: str,
    *,
    log: LogWrite,
    bus: EventBus | None = None,
    run_id: str | None = None,
    step_name: str = "vbox-destroy",
) -> None:
    """Best-effort poweroff + delete. Never raises; tolerates absent VMs."""
    if not vm_exists(name):
        log(f"# {name}: not registered — skipping\n")
        return
    # poweroff may fail if already off; ignore.
    run_vbox(["controlvm", name, "poweroff"], log=log, bus=bus, run_id=run_id, step=step_name)
    run_vbox(["unregistervm", name, "--delete"], log=log, bus=bus, run_id=run_id, step=step_name)


__all__ = [
    "OSTYPE",
    "VBOXMANAGE",
    "create_vm",
    "destroy_vm",
    "list_running_vms",
    "list_vms",
    "nat_ssh_port",
    "pick_free_ports",
    "run_vbox",
    "vboxmanage_available",
    "vm_exists",
    "vm_running",
]
