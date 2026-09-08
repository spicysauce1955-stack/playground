from __future__ import annotations

from typer.testing import CliRunner

from playground.cli.main import app

runner = CliRunner()


def test_device_list_reports_attached_and_known(tmp_path, monkeypatch):
    from playground.devices.adb import AttachedDevice

    monkeypatch.setattr(
        "playground.cli.device_commands.list_attached",
        lambda: ([AttachedDevice(serial="S1", state="connected", model="Pixel 7")], []),
    )
    result = runner.invoke(app, ["device", "list", "--state-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "S1" in result.stdout
    assert "Pixel 7" in result.stdout


def test_device_list_flags_unauthorized_with_the_remedy(tmp_path, monkeypatch):
    from playground.devices.adb import AttachedDevice

    monkeypatch.setattr(
        "playground.cli.device_commands.list_attached",
        lambda: ([AttachedDevice(serial="S1", state="unauthorized")], []),
    )
    result = runner.invoke(app, ["device", "list", "--state-dir", str(tmp_path)])
    assert "unauthorized" in result.stdout
    assert "Allow" in result.stdout


def test_device_note_defaults_to_the_shared_surface(tmp_path):
    config_dir = tmp_path / "config"
    (config_dir / "devices").mkdir(parents=True)
    (config_dir / "devices" / "my-pixel.yaml").write_text(
        "apiVersion: playground/v1\nkind: DeviceProfile\n"
        "metadata:\n  name: my-pixel\nspec: {}\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["device", "note", "my-pixel", "--append", "unlocked in March",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0
    assert (config_dir / "devices" / "my-pixel.md").read_text(encoding="utf-8").strip() == (
        "unlocked in March"
    )


def test_device_note_private_writes_state_not_config(tmp_path):
    result = runner.invoke(
        app,
        ["device", "note", "S1", "--private", "--append", "account bob",
         "--config-dir", str(tmp_path / "c"), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0
    assert (tmp_path / "s" / "state" / "devices" / "S1" / "notes.md").is_file()
    assert not (tmp_path / "c" / "devices").exists()


def test_shared_note_against_a_profileless_serial_needs_a_name(tmp_path):
    """Non-TTY, no --as: fail rather than invent a profile name."""
    result = runner.invoke(
        app,
        ["device", "note", "S1", "--append", "x",
         "--config-dir", str(tmp_path / "c"), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code != 0
    assert "note_name_required" in result.stdout or "note_name_required" in result.stderr


def test_as_creates_the_profile_then_the_note(tmp_path):
    config_dir = tmp_path / "c"
    result = runner.invoke(
        app,
        ["device", "note", "S1", "--as", "my-pixel", "--append", "x",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0
    assert (config_dir / "devices" / "my-pixel.yaml").is_file()
    assert (config_dir / "devices" / "my-pixel.md").is_file()


def test_interactive_prompt_creates_the_profile_then_the_note(tmp_path, monkeypatch):
    """A TTY session with no --as prompts for a profile name instead of
    failing -- `CliRunner`'s piped stdin is never a tty, so `_is_interactive`
    is monkeypatched to reach the branch, and the prompt answer is fed
    through `input=` rather than a real terminal."""
    monkeypatch.setattr("playground.cli.device_commands._is_interactive", lambda: True)
    config_dir = tmp_path / "c"
    result = runner.invoke(
        app,
        ["device", "note", "S1", "--append", "x",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
        input="my-pixel\n",
    )
    assert result.exit_code == 0
    assert (config_dir / "devices" / "my-pixel.yaml").is_file()
    assert (config_dir / "devices" / "my-pixel.md").is_file()
    assert (config_dir / "devices" / "my-pixel.md").read_text(encoding="utf-8").strip() == "x"


def test_device_list_shows_a_note_from_a_model_only_profile(tmp_path, monkeypatch):
    """A profile that selects by `match.model` alone (no serial) must
    surface its note in `device list` exactly as it does for binding a lab
    slot -- the same selector semantics, via `devices.binding.device_matches`,
    must decide "does this profile correspond to this device" everywhere,
    not just for serial-keyed profiles."""
    from playground.devices.adb import AttachedDevice

    monkeypatch.setattr(
        "playground.cli.device_commands.list_attached",
        lambda: ([AttachedDevice(serial="S1", state="connected", model="Pixel 7")], []),
    )
    config_dir = tmp_path / "config"
    (config_dir / "devices").mkdir(parents=True)
    (config_dir / "devices" / "my-pixel.yaml").write_text(
        "apiVersion: playground/v1\nkind: DeviceProfile\n"
        "metadata:\n  name: my-pixel\n"
        "spec:\n  match:\n    model: Pixel 7\n",
        encoding="utf-8",
    )
    (config_dir / "devices" / "my-pixel.md").write_text(
        "unlocked in March\n", encoding="utf-8"
    )

    result = runner.invoke(
        app,
        ["device", "list", "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0
    assert "unlocked in March" in result.stdout


def test_device_list_shows_no_note_when_two_profiles_match_ambiguously(tmp_path, monkeypatch):
    """Two loosely-selected profiles can both match one handset. `device
    list` must not guess which note belongs to the device -- it must show
    none rather than an arbitrary one."""
    from playground.devices.adb import AttachedDevice

    monkeypatch.setattr(
        "playground.cli.device_commands.list_attached",
        lambda: ([AttachedDevice(serial="S1", state="connected", model="Pixel 7")], []),
    )
    config_dir = tmp_path / "config"
    (config_dir / "devices").mkdir(parents=True)
    for name in ("generic-a", "generic-b"):
        (config_dir / "devices" / f"{name}.yaml").write_text(
            f"apiVersion: playground/v1\nkind: DeviceProfile\n"
            f"metadata:\n  name: {name}\nspec: {{}}\n",
            encoding="utf-8",
        )
    (config_dir / "devices" / "generic-a.md").write_text(
        "note from profile A\n", encoding="utf-8"
    )
    result = runner.invoke(
        app,
        ["device", "list", "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0
    assert "S1" in result.stdout
    assert "note from profile A" not in result.stdout
