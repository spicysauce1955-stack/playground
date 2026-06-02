"""Host-prerequisite probes for ``playground doctor``.

Each ``check_*`` function returns a ``list[Diagnostic]`` — empty when the
prerequisite is satisfied. The CLI command runs every check and prints
the collected diagnostics through the same renderer as
``playground validate``.

Diagnostic IDs use the ``runtime.doctor.*`` namespace; see
``docs/system_overview.md``. Severities:

- **error** when the prereq blocks ``playground apply`` outright
  (missing binary, no SSH key, no default pool).
- **warning** when the prereq merely makes failures more likely but
  the apply may still succeed for some labs (autostart off, AppArmor
  not explicitly configured, pool path not world-traversable).

The checks are intentionally read-only: doctor reports what is wrong
and prints a one-line fix in the diagnostic's ``suggestion``. It does
not auto-remediate.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from playground.models.diagnostic import Diagnostic, SourceLocation

# Required ansible collections — matches ansible/requirements.yml.
_REQUIRED_COLLECTIONS = ("ansible.posix", "community.crypto", "community.docker")

# Default storage pool target path when virsh isn't asked.
_DEFAULT_POOL_PATH = Path("/var/lib/libvirt/images")

# Where libvirt's apparmor companion files land for running domains.
_APPARMOR_LIBVIRT_DIR = Path("/etc/apparmor.d/libvirt")

# AppArmor enablement sentinel.
_APPARMOR_PROFILES_FILE = Path("/sys/kernel/security/apparmor/profiles")

# Libvirt qemu daemon config — where security_driver lives.
_QEMU_CONF = Path("/etc/libvirt/qemu.conf")

# Where the kvm_intel / kvm_amd module exposes its nested-virt enable
# flag. Reading "Y" or "1" means nested KVM is available; "N"/"0" or a
# missing file means it isn't.
_KVM_INTEL_NESTED = Path("/sys/module/kvm_intel/parameters/nested")
_KVM_AMD_NESTED = Path("/sys/module/kvm_amd/parameters/nested")


@dataclass(frozen=True)
class CheckResult:
    """Wrapper around a per-check diagnostic list.

    Today the orchestrator just concatenates ``diagnostics`` from each
    check. The wrapper exists so we can attach per-check metadata
    (timing, environment context) later without rewriting callers.
    """

    name: str
    diagnostics: list[Diagnostic]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _host_source(path: str = "host") -> SourceLocation:
    """Diagnostic source pointing at the local host (or a host path)."""
    return SourceLocation(path=path)


def _playground_repo_root() -> Path | None:
    """Best-effort path to the playground repo root from this module's
    on-disk location: ``<root>/src/playground/preflight/doctor.py``.

    Returns ``None`` when the layout doesn't match (e.g., this module is
    installed somewhere that isn't a dev checkout — wheel without the
    repo's ``ansible/`` tree alongside). Callers must fall back gracefully.
    """
    here = Path(__file__).resolve()
    # parents[0]=preflight, [1]=playground, [2]=src, [3]=repo root
    try:
        candidate = here.parents[3]
    except IndexError:
        return None
    # Light sanity check: the repo should have at least src/playground/.
    if not (candidate / "src" / "playground").is_dir():
        return None
    return candidate


def _binary_missing(
    binary: str,
    *,
    diagnostic_id: str,
    install_hint: str,
    severity: str = "error",
) -> Diagnostic:
    return Diagnostic(
        id=diagnostic_id,
        severity=severity,  # type: ignore[arg-type]
        message=f"`{binary}` is not on PATH",
        source=_host_source(),
        suggestion=install_hint,
    )


def _run_virsh(args: list[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["virsh", "--quiet", "--connect", "qemu:///system", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_iso_tool() -> list[Diagnostic]:
    """genisoimage / mkisofs on PATH (cloud-init ISO generation)."""
    if shutil.which("genisoimage") or shutil.which("mkisofs"):
        return []
    return [
        Diagnostic(
            id="runtime.doctor.iso_tool_missing",
            severity="error",
            message=(
                "neither `genisoimage` nor `mkisofs` is on PATH; libvirt "
                "needs one of them to build the cloud-init ISO for each VM"
            ),
            source=_host_source(),
            suggestion="sudo apt install -y genisoimage  # or `cdrtools` on Fedora/Arch",
        )
    ]


def check_vboxmanage() -> list[Diagnostic]:
    """`VBoxManage` on PATH — needed only for ``local-vbox`` labs.

    Warning-only: libvirt is the default backend, so a missing
    VirtualBox must not block a libvirt-only operator. It surfaces so
    someone using the vbox backend gets a clear early signal."""
    if shutil.which("VBoxManage"):
        return []
    return [
        Diagnostic(
            id="runtime.doctor.vboxmanage_missing",
            severity="warning",
            message=(
                "`VBoxManage` is not on PATH; required only for "
                "`local-vbox` labs (libvirt labs are unaffected)"
            ),
            source=_host_source(),
            suggestion="sudo apt install -y virtualbox  # only for the local-vbox backend",
        )
    ]


def check_qemu_img() -> list[Diagnostic]:
    """`qemu-img` on PATH — ``local-vbox`` converts the Ubuntu cloud
    image (qcow2) to a VirtualBox VDI. Warning-only for the same reason
    as :func:`check_vboxmanage`."""
    if shutil.which("qemu-img"):
        return []
    return [
        Diagnostic(
            id="runtime.doctor.qemu_img_missing",
            severity="warning",
            message=(
                "`qemu-img` is not on PATH; `local-vbox` needs it to "
                "convert the Ubuntu cloud image (qcow2) to a VDI"
            ),
            source=_host_source(),
            suggestion="sudo apt install -y qemu-utils  # only for the local-vbox backend",
        )
    ]


def check_xsltproc() -> list[Diagnostic]:
    """`xsltproc` on PATH — the dmacvicar/libvirt provider's ``xml {
    xslt = ... }`` escape hatch shells out to xsltproc (libxslt CLI)
    to apply the transform. Needed by labs that use the rung-1
    workaround ``spec.providers.local-libvirt.cpu_features_disable``;
    apply fails at domain creation with "exec: xsltproc: executable
    file not found in $PATH" if the binary isn't installed.

    Warning-only: labs without ``cpu_features_disable`` don't touch
    xsltproc at all, so a missing binary mustn't block doctor's exit
    code for libvirt-only operators who never reach for rung 1.
    """
    if shutil.which("xsltproc"):
        return []
    return [
        Diagnostic(
            id="runtime.doctor.xsltproc_missing",
            severity="warning",
            message=(
                "`xsltproc` is not on PATH; needed only for labs that use "
                "`spec.providers.local-libvirt.cpu_features_disable` "
                "(rung 1 of the nested-virt escalation ladder — the "
                "libvirt provider's xslt escape hatch shells out to "
                "xsltproc to inject the disable elements). Apply will "
                "otherwise fail at domain creation with `exec: "
                "\"xsltproc\": executable file not found in $PATH`."
            ),
            source=_host_source(),
            suggestion=(
                "sudo apt install -y xsltproc  # only when using the "
                "cpu_features_disable knob"
            ),
        )
    ]


def check_kvm_nested_enabled() -> list[Diagnostic]:
    """`nested` flag on kvm_intel / kvm_amd is on.

    Reads ``/sys/module/kvm_intel/parameters/nested`` (and the AMD
    counterpart). When neither vendor module is loaded the host either
    has no KVM at all (bare metal without virt extensions) or runs
    KVM-as-the-only-vendor with nested off — both relevant to the
    redroid-host lab, whose containers need nested-virt features.

    Warning-only: this never blocks apply outright, because the user
    may have legitimately chosen ``cpu_mode: host-model`` or
    ``domain_type: qemu`` to side-step the nested requirement.
    """
    statuses = {
        "kvm_intel": _read_nested(_KVM_INTEL_NESTED),
        "kvm_amd": _read_nested(_KVM_AMD_NESTED),
    }
    # Any vendor module returning Y/1 means nested KVM is enabled — fine.
    if any(state == "on" for state in statuses.values()):
        return []
    # All vendor modules missing → KVM isn't loaded; doctor already has
    # virsh / pool / group checks that surface the bigger problem.
    if all(state == "missing" for state in statuses.values()):
        return []
    # Some module exists but reports nested=off; surface it.
    return [
        Diagnostic(
            id="runtime.doctor.nested_disabled",
            severity="warning",
            message=(
                "KVM nested virtualization is disabled — kvm_intel "
                f"({statuses['kvm_intel']}), kvm_amd ({statuses['kvm_amd']}). "
                "Labs that require nested-virt features (e.g. redroid-host) "
                "will fail to start their guest workloads."
            ),
            source=_host_source(),
            suggestion=(
                "Enable on Intel: `echo 'options kvm_intel nested=1' | "
                "sudo tee /etc/modprobe.d/kvm.conf && sudo modprobe -r "
                "kvm_intel && sudo modprobe kvm_intel`. AMD uses kvm_amd. "
                "If your L0 hypervisor doesn't permit nested VMX you can't "
                "fix this on the L1 — fall back to "
                "`spec.providers.local-libvirt.cpu_features_disable: [vmx]`"
                " or `domain_type: qemu`."
            ),
        )
    ]


def _read_nested(path: Path) -> str:
    """Return ``"on"`` / ``"off"`` / ``"missing"`` based on the contents
    of a kvm module's ``nested`` sysfs entry. Y / 1 → on, N / 0 → off,
    everything else (missing file, IO error) → missing."""
    try:
        raw = path.read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return "missing"
    return "on" if raw.upper() in {"Y", "1"} else "off"


def check_no_recent_vmx_failures() -> list[Diagnostic]:
    """Surface recent `vmread/vmwrite failed` kernel messages — the
    kernel-side signature of nested-VMX going wrong, whether because
    L0 refuses passthrough (this host is L1) or because L0's
    kernel/hardware combo can't handle some nested VMX ops cleanly.

    Reads the kernel log over the last 24 hours. The window is wide
    on purpose: failures often happen at apply time, then the operator
    investigates / fixes things hours later and runs ``doctor`` — a
    1-hour window misses the cause of the very thing they're
    diagnosing (bob-lnx 2026-05-28).

    Tries ``dmesg`` first (no journal-rotation issues) and falls back
    to ``journalctl -k`` when dmesg is restricted (Ubuntu sets
    ``kernel.dmesg_restrict=1`` by default). Both probes fail-open
    silently when their tool is unavailable — doctor's other checks
    still cover the host.
    """
    matches = _read_kernel_vmx_failures()
    if not matches:
        return []
    first = matches[0][:240]
    return [
        Diagnostic(
            id="runtime.doctor.kvm_intel_recent_failures",
            severity="warning",
            message=(
                "Recent `vmread/vmwrite failed` kernel messages found — "
                "nested-VMX KVM operations are crashing on this host "
                "(either L0 refuses passthrough to an L1 guest, OR L0 "
                "itself can't handle some nested VMX ops on this "
                "CPU/kernel). Either way, playground VMs that need "
                f"nested-virt features will be unreliable. Example: {first!r}"
            ),
            source=_host_source(),
            suggestion=(
                "Apply will most likely wedge with the libvirt domain in "
                "`paused (unknown)`. Mitigations in "
                "`spec.providers.local-libvirt`: "
                "(1) `cpu_mode: host-model` + "
                "`cpu_features_disable: [vmx]`, "
                "(2) `domain_type: qemu` (TCG, ~10-100x slower), "
                "(3) re-run on a host with proper nested VMX support."
            ),
        )
    ]


def _read_kernel_vmx_failures() -> list[str]:
    """Return kernel log lines matching ``vmread`` / ``vmwrite`` over
    the last 24 hours, or an empty list when neither dmesg nor
    journalctl can read the log on this host."""
    # Try dmesg first. On Ubuntu kernel.dmesg_restrict=1 by default, so
    # dmesg returns rc=1 with "Operation not permitted" — fall through
    # to journalctl in that case. -kT prints kernel-only with human
    # timestamps so the truncated example in the diagnostic is readable.
    if shutil.which("dmesg") is not None:
        try:
            result = subprocess.run(  # noqa: S603 — explicit args, no shell
                ["dmesg", "-kT"],
                capture_output=True, text=True, check=False, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            result = None
        if result is not None and result.returncode == 0:
            matches = [
                ln for ln in result.stdout.splitlines()
                if "vmread" in ln.lower() or "vmwrite" in ln.lower()
            ]
            if matches:
                return matches
    # Fall back to journalctl. The 24-hour window is wide enough that
    # apply-time failures discovered hours later still surface, but
    # short enough that long-resolved issues don't clutter the report
    # forever. Use plain grep rather than -g for portability.
    if shutil.which("journalctl") is None:
        return []
    try:
        result = subprocess.run(  # noqa: S603 — explicit args, no shell
            [
                "journalctl", "--since", "24 hours ago", "-k", "--no-pager",
                "-q",
            ],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    return [
        ln for ln in result.stdout.splitlines()
        if "vmread" in ln.lower() or "vmwrite" in ln.lower()
    ]


def check_running_inside_hypervisor() -> list[Diagnostic]:
    """Report whether this host is itself virtualized.

    Reuses ``systemd-detect-virt`` which is installed by default on
    every modern Ubuntu. Reporting "we're inside a hypervisor" at
    info severity (rendered as a non-blocking note) makes the L0/L1
    topology explicit so operators don't have to guess why nested-virt
    apply is hard.
    """
    if shutil.which("systemd-detect-virt") is None:
        return []
    try:
        result = subprocess.run(  # noqa: S603 — explicit args, no shell
            ["systemd-detect-virt"],
            capture_output=True, text=True, check=False, timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    # Exit 0 = virtualized; exit 1 + stdout "none" = bare metal.
    virt = result.stdout.strip()
    if not virt or virt == "none":
        return []
    return [
        Diagnostic(
            id="runtime.doctor.host_is_virtualized",
            severity="info",
            message=(
                f"This host is itself running inside a {virt!r} guest "
                "(L1 of a nested-virt topology). KVM/libvirt will run, "
                "but anything inside the playground VMs that needs "
                "nested-virt features depends on the L0 hypervisor "
                "permitting VMX/SVM passthrough."
            ),
            source=_host_source(),
            suggestion=(
                "If apply fails with libvirt_domain_crashed or "
                "kvm_intel vmread/vmwrite errors, the L0 is refusing "
                "VMX. See "
                "`docs/architecture/nested_virtualization.md` for the "
                "full escalation ladder."
            ),
        )
    ]


def check_libvirt_group_membership() -> list[Diagnostic]:
    """Current user is a member of the libvirt group (effective right now).

    ``grp.getgrnam('libvirt').gr_mem`` is authoritative for the user
    database, but session-active group membership is what libvirt
    actually grants on. We check both: missing from gr_mem is a hard
    "you must add yourself", missing from ``os.getgroups()`` is "you
    added yourself but haven't started a fresh session yet".
    """
    try:
        libvirt_group = grp.getgrnam("libvirt")
    except KeyError:
        return [
            Diagnostic(
                id="runtime.doctor.libvirt_group_missing",
                severity="error",
                message="`libvirt` group does not exist on this host",
                source=_host_source(),
                suggestion="install libvirt: sudo apt install -y libvirt-daemon-system",
            )
        ]

    try:
        user_name = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return [
            Diagnostic(
                id="runtime.doctor.libvirt_group_missing",
                severity="error",
                message=f"cannot resolve username for uid {os.getuid()}",
                source=_host_source(),
            )
        ]

    in_db = user_name in libvirt_group.gr_mem
    in_session = libvirt_group.gr_gid in os.getgroups()

    if in_db and in_session:
        return []

    if in_db and not in_session:
        return [
            Diagnostic(
                id="runtime.doctor.libvirt_group_inactive",
                severity="warning",
                message=(
                    f"user {user_name!r} is in the libvirt group, but the "
                    "current login session predates that change (newgrp or "
                    "re-login required)"
                ),
                source=_host_source(),
                suggestion="log out + back in, or run `newgrp libvirt`",
            )
        ]

    return [
        Diagnostic(
            id="runtime.doctor.libvirt_group_missing",
            severity="error",
            message=(
                f"user {user_name!r} is not a member of the `libvirt` group; "
                "libvirt will refuse qemu:///system connections"
            ),
            source=_host_source(),
            suggestion=f"sudo usermod -aG libvirt {user_name} && log out + back in",
        )
    ]


def check_virsh() -> list[Diagnostic]:
    """`virsh` on PATH — gates the pool checks that follow."""
    if shutil.which("virsh"):
        return []
    return [
        _binary_missing(
            "virsh",
            diagnostic_id="runtime.doctor.virsh_missing",
            install_hint="sudo apt install -y libvirt-clients",
        )
    ]


def check_default_pool() -> list[Diagnostic]:
    """qemu:///system `default` storage pool defined, active, autostart."""
    if shutil.which("virsh") is None:
        # check_virsh already surfaced this; nothing useful to add.
        return []

    try:
        listing = _run_virsh(["pool-list", "--all", "--name"])
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return [
            Diagnostic(
                id="runtime.doctor.virsh_unreachable",
                severity="error",
                message=f"could not run `virsh pool-list`: {exc}",
                source=_host_source(),
                suggestion=(
                    "verify libvirtd is running: `sudo systemctl status libvirtd`"
                ),
            )
        ]

    if listing.returncode != 0:
        return [
            Diagnostic(
                id="runtime.doctor.virsh_unreachable",
                severity="error",
                message=(
                    f"`virsh pool-list` failed (exit {listing.returncode}): "
                    f"{listing.stderr.strip() or '(no stderr)'}"
                ),
                source=_host_source(),
                suggestion=(
                    "verify your user is in the libvirt group and libvirtd "
                    "is running"
                ),
            )
        ]

    pool_names = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
    if "default" not in pool_names:
        return [
            Diagnostic(
                id="runtime.doctor.default_pool_missing",
                severity="error",
                message="qemu:///system has no storage pool named `default`",
                source=_host_source(),
                suggestion=(
                    "virsh pool-define-as default dir --target "
                    f"{_DEFAULT_POOL_PATH} && virsh pool-build default && "
                    "virsh pool-start default && virsh pool-autostart default"
                ),
            )
        ]

    info = _run_virsh(["pool-info", "default"])
    diagnostics: list[Diagnostic] = []
    if info.returncode != 0:
        diagnostics.append(
            Diagnostic(
                id="runtime.doctor.default_pool_inactive",
                severity="error",
                message=(
                    f"`virsh pool-info default` failed: "
                    f"{info.stderr.strip() or '(no stderr)'}"
                ),
                source=_host_source(),
            )
        )
        return diagnostics

    state = _virsh_field(info.stdout, "State")
    autostart = _virsh_field(info.stdout, "Autostart")

    if state != "running":
        diagnostics.append(
            Diagnostic(
                id="runtime.doctor.default_pool_inactive",
                severity="error",
                message=(
                    f"default storage pool exists but is not active (state: "
                    f"{state or 'unknown'!r})"
                ),
                source=_host_source(),
                suggestion="virsh pool-start default",
            )
        )

    if autostart not in ("yes", "enable"):
        diagnostics.append(
            Diagnostic(
                id="runtime.doctor.default_pool_no_autostart",
                severity="warning",
                message=(
                    f"default storage pool is not set to autostart "
                    f"(current: {autostart or 'unknown'!r}); it will need a "
                    "manual `virsh pool-start default` after each libvirtd "
                    "restart"
                ),
                source=_host_source(),
                suggestion="virsh pool-autostart default",
            )
        )

    return diagnostics


def _virsh_field(text: str, name: str) -> str:
    """Parse a `Name: value` line out of `virsh pool-info`-style output."""
    prefix = f"{name}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def check_pool_path_permissions(
    pool_path: Path = _DEFAULT_POOL_PATH,
) -> list[Diagnostic]:
    """Every ancestor of the pool path is traversable; pool itself readable.

    libvirt-qemu (the user libvirtd runs domains as) must be able to
    read VM disks. The common breakage is a parent directory without
    world-execute — operators who define a pool inside their home
    directory hit this constantly. We don't require strict ownership
    here; just that the path chain is reachable.
    """
    if not pool_path.exists():
        # check_default_pool covers the "pool missing" case; if the path
        # itself doesn't exist there isn't a perms problem to report.
        return []

    diagnostics: list[Diagnostic] = []
    current = pool_path.resolve()
    for parent in [current, *current.parents]:
        try:
            mode = parent.stat().st_mode
        except OSError as exc:
            diagnostics.append(
                Diagnostic(
                    id="runtime.doctor.pool_path_unreadable",
                    severity="warning",
                    message=(
                        f"cannot stat {parent} (ancestor of {pool_path}): {exc}"
                    ),
                    source=_host_source(str(parent)),
                )
            )
            break
        # World-execute required to traverse the directory.
        if not mode & stat.S_IXOTH:
            diagnostics.append(
                Diagnostic(
                    id="runtime.doctor.pool_path_unreadable",
                    severity="warning",
                    message=(
                        f"{parent} is not world-traversable (mode "
                        f"{stat.filemode(mode)}); libvirt-qemu likely cannot "
                        f"reach {pool_path} through this directory"
                    ),
                    source=_host_source(str(parent)),
                    suggestion=f"sudo chmod o+x {parent}",
                )
            )
            break
        if parent == parent.parent:
            break

    # Pool dir itself needs read+execute for libvirt-qemu to enumerate
    # disk files. Most setups grant via the libvirt-qemu user's primary
    # group (typically `kvm`), so don't require world-r here unless
    # ownership is unfamiliar.
    return diagnostics


def check_ssh_public_key(path: Path | None = None) -> list[Diagnostic]:
    """The SSH public key tofu injects via cloud-init exists.

    Honors an explicit ``path`` override (the CLI's ``--ssh-key`` flag).
    When ``path`` is ``None`` we default to ``~/.ssh/id_rsa.pub`` — the
    same default as ``var.ssh_public_key_path`` in
    ``tofu/variables.tf``.
    """
    target = path if path is not None else Path("~/.ssh/id_rsa.pub").expanduser()
    if target.is_file():
        return []
    return [
        Diagnostic(
            id="runtime.doctor.ssh_public_key_missing",
            severity="error",
            message=(
                f"SSH public key {target} does not exist; cloud-init has "
                "nothing to inject into the `ubuntu` user"
            ),
            source=_host_source(str(target)),
            suggestion=f'ssh-keygen -t rsa -b 4096 -f {str(target).removesuffix(".pub")}',
        )
    ]


def check_libvirt_apparmor() -> list[Diagnostic]:
    """Verify libvirt + AppArmor are actually working together.

    Two failure modes get distinct diagnostics:

    1. ``apparmor_libvirt_unconfigured`` (warning) — AppArmor is
       loaded but ``/etc/apparmor.d/libvirt/`` doesn't even exist;
       the per-VM profile machinery is absent entirely. Rare on a
       stock distro install; mostly catches custom rebuilds.
    2. ``apparmor_orphan_profiles`` (error) — the machinery is
       installed but ``virt-aa-helper`` isn't producing the
       ``libvirt-<uuid>.files`` companions next to the
       ``libvirt-<uuid>`` profile files. In this state libvirt
       starts domains but qemu fails to read their disk images
       because the AppArmor profile has no path includes. Concrete
       symptom: ``virsh start <vm>`` succeeds, then qemu logs
       "permission denied" on the disk path.

    Both checks are skipped when AppArmor isn't loaded
    (``/sys/kernel/security/apparmor/profiles`` missing) or when
    ``security_driver = "none"`` is set in
    ``/etc/libvirt/qemu.conf`` (explicit opt-out).

    Note: when ``/etc/apparmor.d/libvirt/`` exists but is empty
    (or has only the stock ``libvirt-qemu`` abstraction + a
    ``TEMPLATE.qemu``), the check silently passes — no VM has
    been defined yet, so there's nothing to verify. The error
    only fires when at least one ``libvirt-<uuid>`` profile
    exists but its ``.files`` companion does not.
    """
    if not _APPARMOR_PROFILES_FILE.exists():
        return []

    if _security_driver_disabled():
        return []

    if not _APPARMOR_LIBVIRT_DIR.is_dir():
        return [
            Diagnostic(
                id="runtime.doctor.apparmor_libvirt_unconfigured",
                severity="warning",
                message=(
                    "AppArmor is active but the per-VM profile directory "
                    f"{_APPARMOR_LIBVIRT_DIR} does not exist; "
                    "libvirt's qemu driver has no way to drop per-domain "
                    "profiles, so new VMs may fail to read their disk images"
                ),
                source=_host_source(str(_QEMU_CONF)),
                suggestion=(
                    "easiest fix: echo 'security_driver = \"none\"' | sudo tee -a "
                    f"{_QEMU_CONF} && sudo systemctl restart libvirtd"
                ),
            )
        ]

    orphans = _orphan_libvirt_profiles(_APPARMOR_LIBVIRT_DIR)
    if not orphans:
        return []

    sample = ", ".join(sorted(orphans)[:3])
    if len(orphans) > 3:
        sample = f"{sample}, … ({len(orphans)} total)"
    return [
        Diagnostic(
            id="runtime.doctor.apparmor_orphan_profiles",
            severity="error",
            message=(
                f"AppArmor profile(s) without matching `.files` companion "
                f"in {_APPARMOR_LIBVIRT_DIR}: {sample}. "
                "virt-aa-helper isn't generating path includes for these "
                "domains, so qemu will be denied access to their disk "
                "images even though `virsh start` succeeds"
            ),
            source=_host_source(str(_APPARMOR_LIBVIRT_DIR)),
            suggestion=(
                "check `dpkg -l libvirt-daemon-system apparmor-utils` and "
                "`ls -l /usr/lib/libvirt/virt-aa-helper` (must be setuid); "
                "as a fallback, set `security_driver = \"none\"` in "
                f"{_QEMU_CONF} and restart libvirtd"
            ),
        )
    ]


def _orphan_libvirt_profiles(libvirt_dir: Path) -> set[str]:
    """Return the set of per-VM profile names whose ``.files`` is missing.

    Per-VM profile filenames look like ``libvirt-<uuid>`` where
    ``<uuid>`` contains a hyphen. That excludes the stock
    ``libvirt-qemu`` abstraction (which ships on every libvirt
    install and never has a ``.files`` companion). Returns an empty
    set on any I/O error so callers don't crash on a permissions
    glitch.
    """
    try:
        entries = list(libvirt_dir.iterdir())
    except OSError:
        return set()

    profiles: set[str] = set()
    files: set[str] = set()
    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name
        if not name.startswith("libvirt-"):
            continue
        if name.endswith(".files"):
            stripped = name[: -len(".files")]
            if "-" in stripped[len("libvirt-"):]:
                files.add(stripped)
        else:
            # `libvirt-qemu` is the stock abstraction; per-VM profile
            # names have an extra hyphen in the suffix (UUIDs do).
            if "-" in name[len("libvirt-"):]:
                profiles.add(name)

    return profiles - files


def _security_driver_disabled() -> bool:
    """Best-effort parse of ``/etc/libvirt/qemu.conf`` for opt-out."""
    if not _QEMU_CONF.is_file():
        return False
    try:
        for raw in _QEMU_CONF.read_text().splitlines():
            line = raw.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "security_driver" and value.strip().strip('"') == "none":
                return True
    except OSError:
        return False
    return False


def check_ansible_config(repo_root: Path | None = None) -> list[Diagnostic]:
    """Verify ``ansible/ansible.cfg`` exists with the settings that
    keep `playground apply` from racing first-boot SSH/cloud-init.

    Two failure modes get distinct diagnostics:

    1. ``ansible_cfg_missing`` (warning) — file isn't there at all;
       Ansible falls back to its defaults, which include
       ``host_key_checking=True`` and no ControlMaster. A fresh apply
       will hang on first-SSH host-key prompts or fail with confusing
       "Permission denied" / sftp errors.
    2. ``ansible_cfg_misconfigured`` (warning) — file exists but is
       missing one of the load-bearing knobs
       (``host_key_checking=False``, ``pipelining=True``, or
       ``ControlMaster=auto`` in ``ssh_args``). Names the missing
       keys so the fix is obvious.

    The check is intentionally lenient about line formatting; it
    does a simple substring scan rather than a strict INI parse.

    When ``repo_root`` is not provided, resolve the playground repo
    root from this module's location (``<root>/src/playground/preflight``)
    instead of CWD. The CWD fallback was a false-positive source for
    operators running ``playground doctor`` from a *downstream* project
    that uses playground as a black-box infra tool: they'd see a
    warning about ``<their-project>/ansible/ansible.cfg`` even though
    the file that matters is the playground's own.
    """
    if repo_root is not None:
        root = repo_root
    else:
        root = _playground_repo_root()
        if root is None or not (root / "ansible" / "ansible.cfg").is_file():
            # Dev-checkout path didn't yield a usable ansible.cfg
            # (installed wheel without ansible/ tree, or shadowed). Fall
            # back to CWD only if it actually looks like a playground
            # tree (has src/playground/), otherwise skip silently.
            cwd = Path.cwd()
            if (cwd / "src" / "playground").is_dir():
                root = cwd
            else:
                return []
    cfg_path = root / "ansible" / "ansible.cfg"
    if not cfg_path.is_file():
        return [
            Diagnostic(
                id="runtime.doctor.ansible_cfg_missing",
                severity="warning",
                message=(
                    f"{cfg_path} is missing; Ansible will run with defaults "
                    "(host_key_checking=True, no ControlMaster) which often "
                    "breaks fresh `playground apply` runs"
                ),
                source=_host_source(str(cfg_path)),
                suggestion=(
                    "create ansible/ansible.cfg with host_key_checking=False, "
                    "pipelining=True, and ControlMaster=auto in ssh_args"
                ),
            )
        ]

    try:
        text = cfg_path.read_text()
    except OSError as exc:
        return [
            Diagnostic(
                id="runtime.doctor.ansible_cfg_missing",
                severity="warning",
                message=f"could not read {cfg_path}: {exc}",
                source=_host_source(str(cfg_path)),
            )
        ]

    missing: list[str] = []
    lowered = text.lower()
    if "host_key_checking" not in lowered or "host_key_checking = false" not in lowered.replace(" ", " "):
        # Look for the directive with flexible spacing/casing.
        if not re.search(r"(?im)^\s*host_key_checking\s*=\s*false\s*$", text):
            missing.append("host_key_checking = False")
    if not re.search(r"(?im)^\s*pipelining\s*=\s*true\s*$", text):
        missing.append("pipelining = True")
    if "controlmaster=auto" not in lowered.replace(" ", ""):
        missing.append("ControlMaster=auto in ssh_args")

    if not missing:
        return []

    return [
        Diagnostic(
            id="runtime.doctor.ansible_cfg_misconfigured",
            severity="warning",
            message=(
                f"{cfg_path} is missing recommended setting(s): "
                + "; ".join(missing)
                + ". Fresh `playground apply` runs may hang on first-boot "
                "SSH prompts or run slowly without pipelining"
            ),
            source=_host_source(str(cfg_path)),
            suggestion=(
                "see docs/developer_guide.md §'Ansible config' for the "
                "canonical ansible.cfg contents"
            ),
        )
    ]


def check_ansible_and_collections() -> list[Diagnostic]:
    """ansible-playbook on PATH + the three collections playground roles need."""
    if shutil.which("ansible-playbook") is None:
        return [
            _binary_missing(
                "ansible-playbook",
                diagnostic_id="runtime.doctor.ansible_missing",
                install_hint=(
                    "sudo apt install -y ansible  # then "
                    "`ansible-galaxy collection install -r ansible/requirements.yml`"
                ),
            )
        ]

    if shutil.which("ansible-galaxy") is None:
        # ansible without galaxy is unusual; if it happens, surface it
        # and skip the collection check rather than guessing.
        return [
            _binary_missing(
                "ansible-galaxy",
                diagnostic_id="runtime.doctor.ansible_missing",
                install_hint="reinstall the ansible package",
            )
        ]

    installed = _list_ansible_collections()
    missing = [name for name in _REQUIRED_COLLECTIONS if name not in installed]
    if not missing:
        return []

    return [
        Diagnostic(
            id="runtime.doctor.ansible_collection_missing",
            severity="error",
            message=(
                "required ansible collections are not installed: "
                + ", ".join(missing)
            ),
            source=_host_source(),
            suggestion=(
                "ansible-galaxy collection install -r ansible/requirements.yml"
            ),
        )
    ]


def _list_ansible_collections() -> set[str]:
    """Return the set of installed ansible collection names.

    Prefers ``--format json`` (modern ansible-core). Falls back to
    parsing the default tabular output for older versions. Returns an
    empty set on any error so the caller surfaces "collections missing"
    rather than crashing the doctor.
    """
    try:
        result = subprocess.run(
            ["ansible-galaxy", "collection", "list", "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()

    if result.returncode == 0 and result.stdout.strip():
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            names: set[str] = set()
            for collections in payload.values():
                if isinstance(collections, dict):
                    names.update(collections.keys())
            if names:
                return names

    # Fallback: parse tabular output.
    try:
        result = subprocess.run(
            ["ansible-galaxy", "collection", "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()

    names = set()
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if line.lower().startswith("collection"):
            continue
        names.add(line.split()[0])
    return names


# ---------------------------------------------------------------------------
# Move-2 follow-ups: three coverage gaps the strategic audit flagged.
# Each one prevents a "next-bug" by surfacing it early. See
# docs/architecture/CONTRACTS.md for the layer contracts these probes
# defend.
# ---------------------------------------------------------------------------


_RECOGNIZED_IMAGE_PATTERNS = (
    "ubuntu",
    "debian",
    "fedora",
    "centos",
    "rocky",
    "alma",
    "noble",
    "jammy",
    "focal",
)
"""Substrings in `var.ubuntu_image_url` that indicate a cloud image
that ships with cloud-init pre-installed. Conservative: a URL not
matching any of these triggers the warning, not an error — the
operator may know the image has cloud-init even if the name is
non-standard."""


def check_cloud_init_on_image(
    tofu_dir: Path | None = None,
) -> list[Diagnostic]:
    """Verify the image url tofu fetches looks like a cloud image.

    The pipeline assumes cloud-init is present on the VM (it's how
    the SSH key gets injected and how `wait-for-vms-ready`'s phase
    2 blocks on `cloud-init status --wait`). If an operator points
    `var.ubuntu_image_url` at a vanilla server ISO, apply silently
    succeeds at tofu time but the wait step hangs forever on TCP
    :22 because nothing ever provisioned sshd.

    The check is lightweight: parse `tofu/variables.tf` for the
    default value of `ubuntu_image_url` and assert it matches a
    known cloud-image substring. Doesn't reach out to the network.
    """
    root = tofu_dir if tofu_dir is not None else (Path.cwd() / "tofu")
    variables_tf = root / "variables.tf"
    if not variables_tf.is_file():
        # The tofu tree is missing — a different problem entirely.
        # Don't double-report; the rest of doctor or apply will fail
        # at a more useful spot.
        return []

    try:
        text = variables_tf.read_text()
    except OSError as exc:
        return [
            Diagnostic(
                id="runtime.doctor.cloud_init_image_unverified",
                severity="warning",
                message=f"could not read {variables_tf}: {exc}",
                source=_host_source(str(variables_tf)),
            )
        ]

    # Extract the default = "..." line under variable "ubuntu_image_url".
    match = re.search(
        r'variable\s+"ubuntu_image_url"\s*\{[^}]*?default\s*=\s*"([^"]+)"',
        text,
        re.DOTALL,
    )
    if not match:
        return [
            Diagnostic(
                id="runtime.doctor.cloud_init_image_unverified",
                severity="warning",
                message=(
                    f"could not parse `default` for `ubuntu_image_url` in "
                    f"{variables_tf}; cannot verify the image ships "
                    "cloud-init"
                ),
                source=_host_source(str(variables_tf)),
                suggestion=(
                    "confirm the image at var.ubuntu_image_url has cloud-init "
                    "installed; otherwise wait-for-vms-ready will hang"
                ),
            )
        ]

    url = match.group(1)
    lowered = url.lower()
    if any(needle in lowered for needle in _RECOGNIZED_IMAGE_PATTERNS):
        return []

    return [
        Diagnostic(
            id="runtime.doctor.cloud_init_image_unverified",
            severity="warning",
            message=(
                f"`ubuntu_image_url` default ({url!r}) doesn't look like a "
                "known cloud image (no ubuntu/debian/fedora/etc. in URL). "
                "If the image ships cloud-init this is a false positive; if "
                "not, wait-for-vms-ready will hang on TCP :22 forever"
            ),
            source=_host_source(str(variables_tf)),
            suggestion=(
                "either confirm the image has cloud-init installed (boot it "
                "manually and run `cloud-init --version`), or point "
                "var.ubuntu_image_url at a known cloud image like "
                "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
            ),
        )
    ]


def check_ansible_config_wired(
    runner_path: Path | None = None,
) -> list[Diagnostic]:
    """Confirm runner.py actually wires ANSIBLE_CONFIG.

    The `ansible/ansible.cfg` file is only useful if
    `run_ansible_playbook` is called with `ansible_cfg=`. Ansible's
    auto-discovery looks at `./ansible.cfg` relative to cwd (which
    is the repo root, not `ansible/`), so without the explicit env
    var the file is silently ignored. A future refactor could drop
    the kwarg and nobody would notice until the next fresh apply
    hangs on a host-key prompt.

    Static check: grep the runner source for the wiring. If it's
    missing the entire ansible.cfg → fresh-apply hardening is
    unwired and we'd regress to the pre-roadmap-§15 state.
    """
    path = (
        runner_path
        if runner_path is not None
        else (
            Path.cwd()
            / "src"
            / "playground"
            / "backend"
            / "local_libvirt"
            / "runner.py"
        )
    )
    if not path.is_file():
        # Standalone install where doctor runs without the source
        # tree present — don't double-report.
        return []

    try:
        text = path.read_text()
    except OSError as exc:
        return [
            Diagnostic(
                id="runtime.doctor.ansible_config_not_wired",
                severity="warning",
                message=f"could not read {path}: {exc}",
                source=_host_source(str(path)),
            )
        ]

    if "ansible_cfg=" in text:
        return []

    return [
        Diagnostic(
            id="runtime.doctor.ansible_config_not_wired",
            severity="warning",
            message=(
                f"{path} does not pass `ansible_cfg=` to "
                "`run_ansible_playbook` — `ansible/ansible.cfg` will be "
                "silently ignored and ansible will run with stock defaults "
                "(host_key_checking=True, no ControlMaster, no pipelining), "
                "which breaks fresh `playground apply` runs"
            ),
            source=_host_source(str(path)),
            suggestion=(
                "in execute_apply, call "
                "`run_ansible_playbook(..., ansible_cfg=ansible_dir / "
                '"ansible.cfg")`. See roadmap §15 and '
                "docs/architecture/CONTRACTS.md → ansible-playbook"
            ),
        )
    ]


def check_tofu_state_alignment(
    tofu_dir: Path | None = None,
) -> list[Diagnostic]:
    """When tofu state exists, confirm it lists playground domains.

    Catches drift: an operator who ran `tofu apply` directly (not
    through the playground CLI) and left a stale state that the
    inventory renderer would silently mis-pair. Or a tofu state
    that was partially destroyed and never cleaned up — the next
    apply will refresh state and report "5 resources to add" when
    the operator was expecting "no changes."

    Best-effort: skipped when tofu isn't on PATH, when the tofu
    dir doesn't exist, or when state is empty (no prior apply).
    Warning-only because a stale state isn't always a bug — the
    operator may have intended to recreate everything.
    """
    root = tofu_dir if tofu_dir is not None else (Path.cwd() / "tofu")
    if not root.is_dir():
        return []
    if shutil.which("tofu") is None:
        return []
    # `tofu state list` is a no-op if no state file exists — exits 0
    # with empty stdout, which is the right "skip" signal for us.
    try:
        result = subprocess.run(
            ["tofu", "state", "list"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        # State backend errors land here; surface them so the next
        # apply doesn't crash mid-pipeline.
        return [
            Diagnostic(
                id="runtime.doctor.tofu_state_drift",
                severity="warning",
                message=(
                    f"`tofu state list` in {root} failed "
                    f"(exit {result.returncode}): "
                    f"{result.stderr.strip() or '(no stderr)'}"
                ),
                source=_host_source(str(root)),
                suggestion=(
                    "inspect manually: `cd tofu && tofu state list`. "
                    "If the state is irrecoverable, `playground reset "
                    "<lab>` scrubs by name without touching tofu state"
                ),
            )
        ]

    state_entries = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not state_entries:
        # Empty state = no prior apply, perfectly fine.
        return []

    has_playground_domain = any(
        entry.startswith("libvirt_domain.playground_node")
        for entry in state_entries
    )
    if has_playground_domain:
        return []

    # State exists but holds nothing that looks like the playground
    # tofu module. Either an unrelated user of the same tofu dir, or
    # a misconfigured state file. Either way, the next `playground
    # apply` would surprise the operator.
    return [
        Diagnostic(
            id="runtime.doctor.tofu_state_drift",
            severity="warning",
            message=(
                f"tofu state in {root} has {len(state_entries)} entries but "
                "none match `libvirt_domain.playground_node`. The state "
                "may belong to a different tofu module, or a previous "
                "apply was interrupted"
            ),
            source=_host_source(str(root)),
            suggestion=(
                "inspect: `cd tofu && tofu state list`. To clear: "
                "`playground reset <lab>` (or `cd tofu && tofu destroy`)"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# Cloud DigitalOcean checks
# ---------------------------------------------------------------------------


def check_cloud_do_token(token_env: str = "DIGITALOCEAN_TOKEN") -> list[Diagnostic]:
    """DigitalOcean API token is present in the environment.

    Returns an error when the named env var is unset or empty.  The
    token VALUE is never echoed — only the variable name appears in the
    diagnostic so the message is safe to display or log.
    """
    if os.environ.get(token_env):
        return []
    return [
        Diagnostic(
            id="runtime.doctor.cloud_token_missing",
            severity="error",
            message=(
                f"${token_env} is not set; the DigitalOcean backend "
                "can't authenticate"
            ),
            source=_host_source(),
            suggestion=f"export {token_env}=<your-token> (do not commit it)",
        )
    ]


def check_cloud_do_token_auth(
    token_env: str = "DIGITALOCEAN_TOKEN",
) -> list[Diagnostic]:
    """Probe the DigitalOcean API to verify the token is actually accepted.

    Returns ``[]`` when the token is absent/empty (``check_cloud_do_token``
    already surfaces that error — don't double-report).

    Diagnostic IDs:
    - ``runtime.doctor.cloud_token_wrong_prefix`` (warning) — token does not
      start with ``dop_v1_``.
    - ``runtime.doctor.cloud_token_unauthorized`` (error) — 401 response.
    - ``runtime.doctor.cloud_token_forbidden`` (error) — 403 response.
    - ``runtime.doctor.cloud_token_check_failed`` (warning) — transport error
      or unexpected status; credential may still be valid.

    The token VALUE is **never** echoed in any diagnostic message.
    """
    token = os.environ.get(token_env)
    if not token:
        return []

    diagnostics: list[Diagnostic] = []

    # Warn about non-personal-access-token prefixes before attempting the live
    # probe — a doo_v1_ refresh token or OAuth token will be rejected.
    if not token.startswith("dop_v1_"):
        diagnostics.append(
            Diagnostic(
                id="runtime.doctor.cloud_token_wrong_prefix",
                severity="warning",
                message=(
                    f"${token_env} does not start with dop_v1_ "
                    "(a personal access token) — a doo_v1_ refresh token or "
                    "an OAuth token will be rejected."
                ),
                source=_host_source(),
                suggestion=(
                    f"Generate a new personal access token at "
                    "https://cloud.digitalocean.com/account/api/tokens "
                    f"and re-export it as {token_env}."
                ),
            )
        )

    # Local import to avoid a heavy module-load dependency at doctor import
    # time (consistent with how _cloud_token_env lazily imports load_config).
    from playground.backend.cloud_digitalocean.do import verify_token  # noqa: PLC0415

    status = verify_token(token)

    if 200 <= status <= 299:
        # Auth OK — no diagnostic needed.
        pass
    elif status == 401:
        diagnostics.append(
            Diagnostic(
                id="runtime.doctor.cloud_token_unauthorized",
                severity="error",
                message=(
                    f"DigitalOcean rejected ${token_env} (401 Unable to "
                    "authenticate) — the token is expired or revoked. "
                    "Generate a new personal access token at "
                    "https://cloud.digitalocean.com/account/api/tokens "
                    "and re-export it."
                ),
                source=_host_source(),
                suggestion=(
                    f"export {token_env}=<new-token>  "
                    "# generate at https://cloud.digitalocean.com/account/api/tokens"
                ),
            )
        )
    elif status == 403:
        diagnostics.append(
            Diagnostic(
                id="runtime.doctor.cloud_token_forbidden",
                severity="error",
                message=(
                    f"DigitalOcean rejected ${token_env} (403) — the token "
                    "lacks the required scope. Recreate it with read+write "
                    "scopes."
                ),
                source=_host_source(),
                suggestion=(
                    "Delete and recreate the token at "
                    "https://cloud.digitalocean.com/account/api/tokens "
                    "with full read+write permissions."
                ),
            )
        )
    elif status == 0:
        diagnostics.append(
            Diagnostic(
                id="runtime.doctor.cloud_token_check_failed",
                severity="warning",
                message=(
                    f"could not reach DigitalOcean to verify ${token_env}: "
                    "the credential may still be valid; check connectivity."
                ),
                source=_host_source(),
                suggestion=(
                    "Verify internet access and that "
                    "https://api.digitalocean.com is reachable, then re-run "
                    "`playground doctor`."
                ),
            )
        )
    else:
        diagnostics.append(
            Diagnostic(
                id="runtime.doctor.cloud_token_check_failed",
                severity="warning",
                message=(
                    f"DigitalOcean API returned status {status} when verifying "
                    f"${token_env}; the credential may still be valid."
                ),
                source=_host_source(),
                suggestion=(
                    "Retry `playground doctor` or inspect the DigitalOcean "
                    "status page at https://status.digitalocean.com."
                ),
            )
        )

    return diagnostics


def check_cloud_do_token_not_committed(
    repo_root: Path | None = None,
) -> list[Diagnostic]:
    """Scan Git-tracked files for a committed DigitalOcean token literal.

    Uses ``git grep`` so only tracked (committed) content is scanned;
    untracked files and the working tree are ignored.  Best-effort:
    returns ``[]`` when ``git`` isn't on PATH or the directory isn't a
    repo.

    IMPORTANT: diagnostic messages and suggestions contain only the
    matched file path and line number — never the matched line content
    or the token value itself.
    """
    root = repo_root if repo_root is not None else _playground_repo_root()
    if root is None:
        return []
    if shutil.which("git") is None:
        return []

    # DO personal-access tokens: dop_v1_ followed by exactly 64 word chars.
    pattern = r"dop_v1_[A-Za-z0-9]{64}"
    try:
        result = subprocess.run(  # noqa: S603 — explicit args, no shell
            ["git", "grep", "-nE", pattern],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    # rc=0 → matches found; rc=1 → no matches; rc=128 → not a repo
    if result.returncode != 0:
        return []

    # Collect "file:line" pairs — strip the matched text entirely.
    locations: list[str] = []
    for raw_line in result.stdout.splitlines():
        # git grep -n format: path:lineno:content
        parts = raw_line.split(":", 2)
        if len(parts) >= 2:
            locations.append(f"{parts[0]}:{parts[1]}")

    if not locations:
        return []

    loc_summary = ", ".join(locations[:5])
    if len(locations) > 5:
        loc_summary += f" … ({len(locations)} total)"

    return [
        Diagnostic(
            id="runtime.doctor.cloud_token_committed",
            severity="error",
            message=(
                "A DigitalOcean API token pattern (dop_v1_…) was found in "
                f"Git-tracked file(s): {loc_summary}. "
                "Committed tokens must be rotated immediately."
            ),
            source=_host_source(),
            suggestion=(
                "Rotate the token in the DigitalOcean control panel, then "
                "remove it from tracked files and rewrite history with "
                "`git filter-repo` or BFG."
            ),
        )
    ]


def check_cloud_do_tofu() -> list[Diagnostic]:
    """`tofu` binary on PATH (required to run the DO OpenTofu root)."""
    if shutil.which("tofu") is not None:
        return []
    return [
        _binary_missing(
            "tofu",
            diagnostic_id="runtime.doctor.cloud_tofu_missing",
            install_hint=(
                "install OpenTofu: https://opentofu.org/docs/intro/install/"
            ),
        )
    ]


def check_cloud_do_state_writable(state_dir: Path) -> list[Diagnostic]:
    """Verify the per-backend state directory can be created and written.

    Tries to create ``<state_dir>/state/cloud-digitalocean``, write a
    temporary probe file, and remove it.  Returns an error on any
    ``OSError`` so the operator knows before ``apply`` that the state
    path is unusable.
    """
    target = state_dir / "state" / "cloud-digitalocean"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".doctor_probe"
        probe.write_text("probe")
        probe.unlink()
    except OSError as exc:
        return [
            Diagnostic(
                id="runtime.doctor.cloud_state_unwritable",
                severity="error",
                message=(
                    f"Cannot write to state directory {target}: {exc}"
                ),
                source=_host_source(str(target)),
                suggestion=(
                    f"Ensure {target} is writable, or choose a different "
                    "state dir with --state-dir."
                ),
            )
        ]
    return []


def _cloud_token_env(config_dir: Path | None) -> str:
    """Return the ``token_env`` name from the cloud-digitalocean provider
    config, or ``"DIGITALOCEAN_TOKEN"`` when the config is absent or
    unreadable.

    Best-effort: any load error returns the default so ``check_cloud_do_token``
    still runs with a sensible env-var name.
    """
    if config_dir is None:
        return "DIGITALOCEAN_TOKEN"
    try:
        from playground.config.loader import load_config  # local import avoids circular

        loaded, _ = load_config(config_dir)
        provider = loaded.providers.get("cloud-digitalocean")
        if provider is None:
            return "DIGITALOCEAN_TOKEN"
        spec_data = provider.spec.model_dump()
        return str(spec_data.get("token_env", "DIGITALOCEAN_TOKEN"))
    except Exception:  # noqa: BLE001
        return "DIGITALOCEAN_TOKEN"


def check_cloud_do_provider_config(
    config_dir: Path,
    *,
    provider_name: str = "cloud-digitalocean",
) -> list[Diagnostic]:
    """Probe the cloud-digitalocean provider config for common misconfigurations.

    Checks:
    - Provider config block exists in ``config_dir``.
    - Warning if none of region/size/image are set (they have code
      defaults, so this is advisory).
    - Warning when ``firewall.ssh_cidrs`` is absent or empty (SSH open
      to the whole internet).
    """
    from playground.config.loader import load_config  # local import avoids circular

    try:
        loaded, _ = load_config(config_dir)
    except Exception as exc:  # noqa: BLE001
        return [
            Diagnostic(
                id="runtime.doctor.cloud_provider_config_missing",
                severity="error",
                message=f"Could not load config from {config_dir}: {exc}",
                source=_host_source(str(config_dir)),
                suggestion=(
                    f"Add config/providers/{provider_name}.yaml "
                    "with kind: ProviderConfig."
                ),
            )
        ]

    provider = loaded.providers.get(provider_name)
    if provider is None:
        return [
            Diagnostic(
                id="runtime.doctor.cloud_provider_config_missing",
                severity="error",
                message=(
                    f"No ProviderConfig named {provider_name!r} found in "
                    f"{config_dir}"
                ),
                source=_host_source(str(config_dir)),
                suggestion=(
                    f"Add config/providers/{provider_name}.yaml "
                    "with kind: ProviderConfig."
                ),
            )
        ]

    diagnostics: list[Diagnostic] = []
    spec_data = provider.spec.model_dump()

    # Advisory: check whether any of region/size/image are configured.
    missing_keys = [k for k in ("region", "size", "image") if not spec_data.get(k)]
    if missing_keys:
        diagnostics.append(
            Diagnostic(
                id="runtime.doctor.cloud_region_unset",
                severity="warning",
                message=(
                    f"Provider {provider_name!r} is missing spec field(s): "
                    f"{', '.join(missing_keys)}. Code defaults will be used; "
                    "set them explicitly to avoid surprises."
                ),
                source=_host_source(str(config_dir)),
                suggestion=(
                    f"Add region/size/image to "
                    f"config/providers/{provider_name}.yaml spec."
                ),
            )
        )

    # Firewall: warn if ssh_cidrs absent or empty.
    firewall = spec_data.get("firewall") or {}
    ssh_cidrs = firewall.get("ssh_cidrs") if isinstance(firewall, dict) else None
    if not ssh_cidrs:
        diagnostics.append(
            Diagnostic(
                id="runtime.doctor.cloud_ssh_open",
                severity="warning",
                message=(
                    "SSH firewall allows 0.0.0.0/0 (open to the internet); "
                    "set spec.providers.cloud-digitalocean.firewall.ssh_cidrs "
                    "to your operator CIDR."
                ),
                source=_host_source(str(config_dir)),
                suggestion=(
                    f"In config/providers/{provider_name}.yaml, add:\n"
                    "  firewall:\n"
                    "    ssh_cidrs: [<your-ip>/32]"
                ),
            )
        )

    return diagnostics


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_all_checks(
    *,
    ssh_key_path: Path | None = None,
    backend: str | None = None,
    config_dir: Path | None = None,
    state_dir: Path | None = None,
) -> list[Diagnostic]:
    """Run host probes in a stable order and concatenate diagnostics.

    When ``backend == "cloud-digitalocean"`` a cloud-focused subset is
    run: libvirt / vbox / KVM host checks are skipped because a
    cloud-only operator has no virsh, no storage pool, and no KVM
    requirements.  All other ``backend`` values (including ``None``)
    fall through to the original full libvirt/vbox list so existing
    callers are unaffected.

    The order matters for human-readable output: PATH checks first so
    later checks that depend on those binaries don't double-report,
    then backend-specific state, then SSH + ansible.
    """
    if backend == "cloud-digitalocean":
        diagnostics: list[Diagnostic] = []
        diagnostics.extend(check_cloud_do_tofu())
        diagnostics.extend(check_ssh_public_key(ssh_key_path))
        diagnostics.extend(check_ansible_and_collections())
        diagnostics.extend(check_ansible_config())
        # Cloud-specific group
        token_env = _cloud_token_env(config_dir)
        diagnostics.extend(check_cloud_do_token(token_env))
        diagnostics.extend(check_cloud_do_token_auth(token_env))
        diagnostics.extend(check_cloud_do_token_not_committed())
        _state_dir = state_dir if state_dir is not None else Path(".playground")
        diagnostics.extend(check_cloud_do_state_writable(_state_dir))
        if config_dir is not None:
            diagnostics.extend(check_cloud_do_provider_config(config_dir))
        return diagnostics

    # Default: full libvirt/vbox suite (unchanged).
    diagnostics = []
    diagnostics.extend(check_iso_tool())
    diagnostics.extend(check_virsh())
    diagnostics.extend(check_libvirt_group_membership())
    diagnostics.extend(check_default_pool())
    diagnostics.extend(check_pool_path_permissions())
    diagnostics.extend(check_ssh_public_key(ssh_key_path))
    diagnostics.extend(check_libvirt_apparmor())
    diagnostics.extend(check_ansible_and_collections())
    diagnostics.extend(check_ansible_config())
    diagnostics.extend(check_cloud_init_on_image())
    diagnostics.extend(check_ansible_config_wired())
    diagnostics.extend(check_tofu_state_alignment())
    diagnostics.extend(check_vboxmanage())
    diagnostics.extend(check_qemu_img())
    diagnostics.extend(check_xsltproc())
    diagnostics.extend(check_kvm_nested_enabled())
    diagnostics.extend(check_no_recent_vmx_failures())
    diagnostics.extend(check_running_inside_hypervisor())
    return diagnostics
