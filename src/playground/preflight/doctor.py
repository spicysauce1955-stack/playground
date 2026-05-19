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
# Orchestrator
# ---------------------------------------------------------------------------


def run_all_checks(*, ssh_key_path: Path | None = None) -> list[Diagnostic]:
    """Run every host probe in a stable order and concatenate diagnostics.

    The order matters for human-readable output: PATH checks first so
    later checks that depend on those binaries don't double-report,
    then libvirt-side state, then SSH + ansible. Each individual check
    is independently skippable / replaceable; the orchestrator just
    bundles them.
    """
    diagnostics: list[Diagnostic] = []
    diagnostics.extend(check_iso_tool())
    diagnostics.extend(check_virsh())
    diagnostics.extend(check_libvirt_group_membership())
    diagnostics.extend(check_default_pool())
    diagnostics.extend(check_pool_path_permissions())
    diagnostics.extend(check_ssh_public_key(ssh_key_path))
    diagnostics.extend(check_libvirt_apparmor())
    diagnostics.extend(check_ansible_and_collections())
    return diagnostics
