"""Identify the base APK in a directory of split APKs.

Two ecosystems name split sets differently, and both turn up in practice:

    APKMirror (.apkm) / bundletool   base.apk  + split_config.<qualifier>.apk
    APKPure   (.xapk)                <package>.apk + config.<qualifier>.apk

Requiring the literal name ``base.apk`` therefore accepts an extracted
``.apkm`` and rejects an extracted ``.xapk`` -- even though the second is a
perfectly valid set. Verified 2026-09-03 against real Telegram bundles from
both sources.

So the base is identified by ELIMINATION rather than by name: a split
member is anything named ``split_config.*`` or ``config.*``, and whatever
is left over is the base. That still rejects the two sets that genuinely
cannot be installed -- one with no base at all, and one with several
candidates where guessing would be worse than refusing.

Both the interactive ``playground app install`` and the declarative
``android_app`` staging path use this, because they previously disagreed
about what a valid split set was and the device was left to arbitrate with
``INSTALL_FAILED_INVALID_APK``.
"""

from __future__ import annotations

from pathlib import Path

SPLIT_PREFIXES: tuple[str, ...] = ("split_config.", "config.")
"""Filename prefixes that mark an APK as a split member, not the base."""


def is_split_member(name: str) -> bool:
    """True when ``name`` looks like a split rather than the base APK."""
    return name.startswith(SPLIT_PREFIXES)


def find_base_apk(apks: list[Path]) -> tuple[Path | None, str | None]:
    """Return ``(base, None)`` or ``(None, reason)`` for a split set.

    ``reason`` is a bare phrase suitable for embedding in a caller's
    diagnostic message, so the two call sites can keep their own ids and
    key paths while agreeing on the rule itself.
    """
    if not apks:
        return None, "no *.apk files were found"
    bases = [a for a in apks if not is_split_member(a.name)]
    if not bases:
        names = ", ".join(sorted(a.name for a in apks))
        return None, (
            "every APK looks like a split "
            f"({names}); the base APK is missing"
        )
    if len(bases) > 1:
        names = ", ".join(sorted(b.name for b in bases))
        return None, (
            f"several APKs could be the base ({names}); "
            "a split set needs exactly one"
        )
    return bases[0], None


__all__ = ["SPLIT_PREFIXES", "find_base_apk", "is_split_member"]
