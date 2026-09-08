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
