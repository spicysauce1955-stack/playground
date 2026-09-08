from __future__ import annotations

from playground.devices.adb import AttachedDevice
from playground.devices.discovery import effective_mode, offer_unknown


def test_prompt_is_the_configured_default():
    assert effective_mode("prompt", is_tty=True, no_prompt=False, yes=False) == "prompt"


def test_non_tty_forces_silent():
    """JSON output, CI and the TUI must never block on stdin."""
    assert effective_mode("prompt", is_tty=False, no_prompt=False, yes=False) == "silent"


def test_no_prompt_forces_silent():
    assert effective_mode("prompt", is_tty=True, no_prompt=True, yes=False) == "silent"


def test_yes_also_skips_the_question():
    assert effective_mode("prompt", is_tty=True, no_prompt=False, yes=True) == "silent"


def test_silent_stays_silent_on_a_tty():
    assert effective_mode("silent", is_tty=True, no_prompt=False, yes=False) == "silent"


def _attached(serial="S1"):
    return AttachedDevice(serial=serial, state="connected", model="Pixel 7")


def test_silent_mode_never_asks(tmp_path):
    asked = []
    saved = offer_unknown(
        tmp_path,
        [_attached()],
        mode="silent",
        ask=lambda d: asked.append(d) or "x",
    )
    assert saved == {}
    assert asked == []


def test_prompt_mode_saves_what_the_operator_names(tmp_path):
    saved = offer_unknown(
        tmp_path,
        [_attached()],
        mode="prompt",
        ask=lambda d: "my-pixel",
    )
    assert saved == {"S1": "my-pixel"}


def test_declining_marks_the_device_ignored_so_it_is_not_re_asked(tmp_path):
    from playground.devices.adb import DeviceFacts
    from playground.devices.registry import read_registry, record_seen

    record_seen(tmp_path, DeviceFacts(serial="S1"))
    offer_unknown(tmp_path, [_attached()], mode="prompt", ask=lambda d: None)
    assert read_registry(tmp_path)["S1"].ignored is True


def test_an_ignored_device_is_not_offered_again(tmp_path):
    from playground.devices.adb import DeviceFacts
    from playground.devices.registry import mark_ignored, record_seen

    record_seen(tmp_path, DeviceFacts(serial="S1"))
    mark_ignored(tmp_path, "S1")
    asked = []
    offer_unknown(
        tmp_path,
        [_attached()],
        mode="prompt",
        ask=lambda d: asked.append(d) or None,
    )
    assert asked == []
