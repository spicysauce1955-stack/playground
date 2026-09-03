"""Both real-world split-bundle naming conventions must be accepted.

Verified 2026-09-03 against actual Telegram bundles: an APKMirror .apkm
extracts to `base.apk` + `split_config.*.apk`, while an APKPure .xapk
extracts to `<package>.apk` + `config.*.apk`. Requiring the literal name
`base.apk` accepted the first and rejected the second, even though both are
installable sets — and the project's own apk-fetcher downloads .xapk files.
"""

from __future__ import annotations

from pathlib import Path

from playground.android.splits import find_base_apk, is_split_member


def _paths(*names: str) -> list[Path]:
    return [Path("/tmp/set") / n for n in names]


def test_apkmirror_apkm_convention() -> None:
    base, reason = find_base_apk(
        _paths("base.apk", "split_config.x86_64.apk", "split_config.en.apk")
    )
    assert reason is None
    assert base is not None and base.name == "base.apk"


def test_apkpure_xapk_convention() -> None:
    base, reason = find_base_apk(
        _paths("org.telegram.messenger.apk", "config.arm64_v8a.apk", "config.en.apk")
    )
    assert reason is None
    assert base is not None and base.name == "org.telegram.messenger.apk"


def test_a_single_apk_is_its_own_base() -> None:
    base, reason = find_base_apk(_paths("telegram.apk"))
    assert reason is None
    assert base is not None and base.name == "telegram.apk"


def test_all_splits_and_no_base_is_rejected() -> None:
    """The original bug: a base-less directory reached the device and came
    back as INSTALL_FAILED_INVALID_APK."""
    base, reason = find_base_apk(_paths("split_config.en.apk", "config.hdpi.apk"))
    assert base is None
    assert reason is not None and "base APK is missing" in reason


def test_two_base_candidates_are_rejected_rather_than_guessed() -> None:
    base, reason = find_base_apk(_paths("base.apk", "other.apk", "config.en.apk"))
    assert base is None
    assert reason is not None and "exactly one" in reason


def test_empty_directory_is_rejected() -> None:
    base, reason = find_base_apk([])
    assert base is None
    assert reason is not None and "no *.apk" in reason


def test_split_member_detection_covers_both_prefixes() -> None:
    assert is_split_member("split_config.x86_64.apk")
    assert is_split_member("config.en.apk")
    assert not is_split_member("base.apk")
    assert not is_split_member("org.telegram.messenger.apk")
