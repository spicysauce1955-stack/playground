from __future__ import annotations

from playground.devices.notes import (
    looks_like_credentials,
    note_path,
    read_note,
    write_note,
)


def test_shared_is_the_default_surface_and_lands_in_config(tmp_path):
    config_dir, state_dir = tmp_path / "config", tmp_path / ".playground"
    path, diagnostics = write_note(
        "hardware note", scope="shared", config_dir=config_dir,
        state_dir=state_dir, name="my-pixel", serial="S1",
    )
    assert path == config_dir / "devices" / "my-pixel.md"
    assert path.read_text(encoding="utf-8") == "hardware note"
    assert [d.id for d in diagnostics] == []


def test_private_lands_in_gitignored_state_keyed_by_serial(tmp_path):
    config_dir, state_dir = tmp_path / "config", tmp_path / ".playground"
    path, _ = write_note(
        "account note", scope="private", config_dir=config_dir,
        state_dir=state_dir, name=None, serial="S1",
    )
    assert path == state_dir / "state" / "devices" / "S1" / "notes.md"


def test_private_needs_no_profile_name(tmp_path):
    """The private surface keys on serial, so it works before any profile
    exists -- which is the whole point of that key."""
    path = note_path(
        "private", config_dir=tmp_path / "c", state_dir=tmp_path / "s",
        name=None, serial="S1",
    )
    assert path.name == "notes.md"


def test_credential_shaped_text_warns_and_still_writes(tmp_path):
    """Shared is the default, and the motivating example is itself
    credential-shaped -- a refusing default would reject the use case the
    feature exists to serve."""
    config_dir, state_dir = tmp_path / "config", tmp_path / ".playground"
    body = "telegram account bob, password hunter2"
    path, diagnostics = write_note(
        body, scope="shared", config_dir=config_dir, state_dir=state_dir,
        name="my-pixel", serial="S1",
    )
    assert path.read_text(encoding="utf-8") == body
    assert [d.id for d in diagnostics] == ["device.note_credential_warning"]
    assert diagnostics[0].severity == "warning"
    assert "--private" in diagnostics[0].suggestion


def test_private_writes_never_warn(tmp_path):
    _, diagnostics = write_note(
        "password hunter2", scope="private", config_dir=tmp_path / "c",
        state_dir=tmp_path / "s", name=None, serial="S1",
    )
    assert diagnostics == []


def test_reading_an_absent_note_is_none(tmp_path):
    assert read_note(
        "shared", config_dir=tmp_path, state_dir=tmp_path, name="nope", serial=None
    ) is None


def test_credential_heuristic_hits_and_misses():
    assert looks_like_credentials("password: hunter2")
    assert looks_like_credentials("API_TOKEN=abc")
    assert not looks_like_credentials("bootloader unlocked in March")
