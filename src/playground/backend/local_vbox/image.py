"""Ensure a VirtualBox-usable base disk exists for the lab's image.

VirtualBox can't boot the Ubuntu cloud image directly — it ships as
qcow2, which VBox doesn't read. So this module mirrors what the libvirt
path gets for free from the dmacvicar provider: it makes sure the cloud
image is cached locally (downloading it once, honoring ``offline``) and
converts it to a VDI with ``qemu-img``. The VDI is the immutable base;
``vbox.create_vm`` clones a per-VM copy from it (so the base stays
pristine and re-applies are cheap-ish).

Both artifacts are cached under the path declared in
``config/artifacts/sources.yaml`` (``vm_images.ubuntu-noble.local_path``),
so they survive ``playground reset`` (which only scrubs per-lab state).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from playground.models.diagnostic import Diagnostic, SourceLocation

LogWrite = Callable[[str], None]


def ensure_base_vdi(
    *,
    image_source: str,
    qcow2_cache: Path,
    offline: bool,
    log: LogWrite,
) -> tuple[Path | None, list[Diagnostic]]:
    """Return the path to a ready-to-clone base VDI.

    Steps, each skipped when its output already exists:

    1. Download ``image_source`` to ``qcow2_cache`` (unless ``offline``).
    2. ``qemu-img convert -O vdi`` it to a sibling ``*.vdi``.

    Returns ``(vdi_path, [])`` on success, or ``(None, diagnostics)``.
    """
    vdi_path = qcow2_cache.with_suffix(".vdi")
    if vdi_path.is_file():
        log(f"# base VDI cached: {vdi_path}\n")
        return vdi_path, []

    if shutil.which("qemu-img") is None:
        return None, [
            Diagnostic(
                id="runtime.vbox.qemu_img_missing",
                severity="error",
                message=(
                    "`qemu-img` not found on PATH; needed to convert the "
                    "Ubuntu cloud image (qcow2) to a VirtualBox VDI"
                ),
                source=SourceLocation(path="host"),
                suggestion="install qemu-utils (apt install qemu-utils)",
            )
        ]

    # 1. ensure the qcow2 is present
    if not qcow2_cache.is_file():
        if offline:
            return None, [
                Diagnostic(
                    id="runtime.vbox.image_unavailable_offline",
                    severity="error",
                    message=(
                        f"base image not cached at {qcow2_cache} and the lab "
                        "is offline; cannot download"
                    ),
                    source=SourceLocation(path=str(qcow2_cache)),
                    suggestion=(
                        f"pre-stage the image: `curl -fL -o {qcow2_cache} "
                        f"{image_source}` while online"
                    ),
                )
            ]
        dl_diag = _download(image_source, qcow2_cache, log=log)
        if dl_diag is not None:
            return None, [dl_diag]

    # 2. convert qcow2 -> VDI
    log(f"# converting {qcow2_cache.name} -> {vdi_path.name} (qemu-img)\n")
    tmp_vdi = vdi_path.with_suffix(".vdi.part")
    if tmp_vdi.exists():
        tmp_vdi.unlink()
    result = subprocess.run(  # noqa: S603 — explicit args, no shell
        ["qemu-img", "convert", "-O", "vdi", str(qcow2_cache), str(tmp_vdi)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None, [
            Diagnostic(
                id="runtime.vbox.image_convert_failed",
                severity="error",
                message=(
                    f"`qemu-img convert` exited {result.returncode}: "
                    f"{result.stderr.strip()[:300]}"
                ),
                source=SourceLocation(path=str(qcow2_cache)),
            )
        ]
    tmp_vdi.rename(vdi_path)
    log(f"# base VDI ready: {vdi_path}\n")
    return vdi_path, []


def _download(url: str, dest: Path, *, log: LogWrite) -> Diagnostic | None:
    """Download ``url`` to ``dest`` via curl (preferred) or wget.

    Writes to a ``.part`` file and renames on success so an interrupted
    download never leaves a truncated image that looks complete.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if shutil.which("curl"):
        cmd = ["curl", "-fL", "--retry", "3", "-o", str(part), url]
    elif shutil.which("wget"):
        cmd = ["wget", "-O", str(part), url]
    else:
        return Diagnostic(
            id="runtime.vbox.no_downloader",
            severity="error",
            message="neither curl nor wget on PATH to fetch the base image",
            source=SourceLocation(path="host"),
            suggestion="install curl, or pre-stage the image cache manually",
        )
    log(f"# downloading base image: {url}\n")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        if part.exists():
            part.unlink()
        return Diagnostic(
            id="runtime.vbox.image_download_failed",
            severity="error",
            message=(
                f"downloading base image failed (exit {result.returncode}): "
                f"{result.stderr.strip()[:300]}"
            ),
            source=SourceLocation(path=url),
        )
    part.rename(dest)
    return None


__all__ = ["ensure_base_vdi"]
