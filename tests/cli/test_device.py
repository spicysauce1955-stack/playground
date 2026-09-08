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


# ---------------------------------------------------------------------------
# device note: reads must never write under config/
# ---------------------------------------------------------------------------


def _profile(config_dir, name: str, body: str = "spec: {}") -> None:
    (config_dir / "devices").mkdir(parents=True, exist_ok=True)
    (config_dir / "devices" / f"{name}.yaml").write_text(
        "apiVersion: playground/v1\nkind: DeviceProfile\n"
        f"metadata:\n  name: {name}\n{body}\n",
        encoding="utf-8",
    )


def test_note_show_creates_no_committed_file(tmp_path):
    """A READ must not write under `config/`. The spec allows exactly three
    paths to create a profile -- the discovery prompt, `device save`, and
    WRITING a shared note -- precisely so adb polling and note reading
    never churn `git status`. `--show` used to run `_write_profile` before
    it had even looked at which sub-verb was asked for."""
    config_dir = tmp_path / "c"
    result = runner.invoke(
        app,
        ["device", "note", "S1", "--show", "--as", "my-pixel",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0, result.output
    assert not (config_dir / "devices" / "my-pixel.yaml").exists()
    assert not (config_dir / "devices" / "my-pixel.md").exists()


def test_note_clear_creates_no_committed_file(tmp_path):
    """Same rule for `--clear`: deleting a note that does not exist must
    not conjure a profile into the tracked tree."""
    config_dir = tmp_path / "c"
    result = runner.invoke(
        app,
        ["device", "note", "S1", "--clear", "--as", "my-pixel",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0, result.output
    assert not (config_dir / "devices" / "my-pixel.yaml").exists()


def test_note_clear_still_deletes_an_existing_note(tmp_path):
    config_dir = tmp_path / "c"
    _profile(config_dir, "my-pixel")
    (config_dir / "devices" / "my-pixel.md").write_text("gone soon\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["device", "note", "my-pixel", "--clear",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0, result.output
    assert not (config_dir / "devices" / "my-pixel.md").exists()


def test_note_read_never_prompts_to_create_a_profile(tmp_path, monkeypatch):
    """Even on a TTY, a read has nothing to create, so it must not prompt
    -- prompting was what made an interactive `--show` write a committed
    profile."""
    monkeypatch.setattr("playground.cli.device_commands._is_interactive", lambda: True)
    config_dir = tmp_path / "c"
    result = runner.invoke(
        app,
        ["device", "note", "S1", "--show",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
        input="my-pixel\n",
    )
    assert result.exit_code != 0
    assert "note_name_required" in result.output + result.stderr
    assert not (config_dir / "devices").exists()


def test_empty_as_name_is_rejected_before_anything_is_written(tmp_path):
    """`--as ""` reached `_write_profile` and created files literally named
    `.yaml` and `.md`."""
    config_dir = tmp_path / "c"
    result = runner.invoke(
        app,
        ["device", "note", "S1", "--as", "", "--append", "x",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code != 0
    assert "note_name_required" in result.output + result.stderr
    assert not (config_dir / "devices" / ".yaml").exists()
    assert not (config_dir / "devices" / ".md").exists()


# ---------------------------------------------------------------------------
# device note --json: the documented payload
# ---------------------------------------------------------------------------


def test_note_json_read_returns_the_documented_payload(tmp_path):
    """Spec: `{device, serial, scope, path, markdown, updated_at}`. The
    body key is `markdown`, not `note`."""
    import json

    config_dir = tmp_path / "c"
    _profile(config_dir, "my-pixel", "spec:\n  match:\n    serial: S1")
    (config_dir / "devices" / "my-pixel.md").write_text("root broke\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["device", "note", "my-pixel", "--show", "--json",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "device", "serial", "scope", "path", "markdown", "updated_at"
    }
    assert payload["device"] == "my-pixel"
    assert payload["serial"] == "S1"
    assert payload["scope"] == "shared"
    assert payload["markdown"] == "root broke\n"
    assert payload["path"].endswith("my-pixel.md")
    assert payload["updated_at"] is not None


def test_note_json_write_returns_the_same_payload_shape(tmp_path):
    import json

    config_dir = tmp_path / "c"
    result = runner.invoke(
        app,
        ["device", "note", "S1", "--private", "--append", "account bob", "--json",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "device", "serial", "scope", "path", "markdown", "updated_at"
    }
    assert payload["scope"] == "private"
    assert payload["serial"] == "S1"
    assert payload["markdown"] == "account bob\n"


def test_note_json_for_a_never_written_note_reports_no_timestamp(tmp_path):
    import json

    config_dir = tmp_path / "c"
    _profile(config_dir, "my-pixel")
    result = runner.invoke(
        app,
        ["device", "note", "my-pixel", "--show", "--json",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["markdown"] is None
    assert payload["updated_at"] is None


# ---------------------------------------------------------------------------
# device note: saying plainly that shared means committed
# ---------------------------------------------------------------------------


def test_note_help_says_shared_notes_are_committed(tmp_path):
    result = runner.invoke(app, ["device", "note", "--help"])
    assert result.exit_code == 0
    text = result.output
    assert "git" in text
    assert "--private" in text
    # Every option carries help text; `--private` had none at all, which
    # is the one flag the safety story depends on.
    for flag in ("--append", "--set", "--show", "--clear", "--as", "--json"):
        assert flag in text


def test_a_shared_note_write_says_it_is_committed(tmp_path):
    config_dir = tmp_path / "c"
    _profile(config_dir, "my-pixel")
    result = runner.invoke(
        app,
        ["device", "note", "my-pixel", "--append", "unlocked in March",
         "--config-dir", str(config_dir), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0, result.output
    assert "SHARED" in result.output
    assert "git" in result.output
    assert "--private" in result.output


def test_a_private_note_write_says_it_is_not_committed(tmp_path):
    result = runner.invoke(
        app,
        ["device", "note", "S1", "--private", "--append", "account bob",
         "--config-dir", str(tmp_path / "c"), "--state-dir", str(tmp_path / "s")],
    )
    assert result.exit_code == 0, result.output
    assert "gitignored" in result.output


# ---------------------------------------------------------------------------
# device note --private against a model-only profile (unreadable-note bug)
# ---------------------------------------------------------------------------


def test_private_note_for_a_model_only_profile_resolves_the_real_serial(tmp_path):
    """The private surface is keyed by SERIAL: every reader
    (`adopt._lookup_note`, `_note_for`) looks it up that way. Writing it
    under the profile NAME produced a note that was saved and then
    permanently invisible."""
    from playground.devices.adb import DeviceFacts
    from playground.devices.registry import record_seen

    config_dir = tmp_path / "c"
    state_dir = tmp_path / "s"
    _profile(config_dir, "my-pixel", "spec:\n  match:\n    model: Pixel 7")
    record_seen(state_dir, DeviceFacts(serial="S1", model="Pixel 7"))

    result = runner.invoke(
        app,
        ["device", "note", "my-pixel", "--private", "--append", "root broke",
         "--config-dir", str(config_dir), "--state-dir", str(state_dir)],
    )
    assert result.exit_code == 0, result.output + result.stderr
    assert (state_dir / "state" / "devices" / "S1" / "notes.md").is_file()
    assert not (state_dir / "state" / "devices" / "my-pixel").exists()

    # And a reader finds it: `device show` renders the private note.
    shown = runner.invoke(
        app,
        ["device", "show", "S1",
         "--config-dir", str(config_dir), "--state-dir", str(state_dir)],
    )
    assert "root broke" in shown.output


def test_private_note_refuses_when_the_serial_cannot_be_determined(tmp_path):
    """Ambiguous (or absent) is a refusal, not a silent write to an
    unreadable path -- silently writing an invisible note is the worst of
    the available options."""
    from playground.devices.adb import DeviceFacts
    from playground.devices.registry import record_seen

    config_dir = tmp_path / "c"
    state_dir = tmp_path / "s"
    _profile(config_dir, "my-pixel", "spec:\n  match:\n    model: Pixel 7")
    record_seen(state_dir, DeviceFacts(serial="S1", model="Pixel 7"))
    record_seen(state_dir, DeviceFacts(serial="S2", model="Pixel 7"))

    result = runner.invoke(
        app,
        ["device", "note", "my-pixel", "--private", "--append", "root broke",
         "--config-dir", str(config_dir), "--state-dir", str(state_dir)],
    )
    assert result.exit_code != 0
    assert "note_serial_required" in result.output + result.stderr
    assert not (state_dir / "state" / "devices" / "my-pixel").exists()


def test_private_note_for_a_bare_serial_still_needs_no_profile(tmp_path):
    """Unchanged: `--private` with a raw serial is the case that surface
    exists for, and must not start demanding a profile."""
    state_dir = tmp_path / "s"
    result = runner.invoke(
        app,
        ["device", "note", "SERIAL9", "--private", "--append", "x",
         "--config-dir", str(tmp_path / "c"), "--state-dir", str(state_dir)],
    )
    assert result.exit_code == 0, result.output + result.stderr
    assert (state_dir / "state" / "devices" / "SERIAL9" / "notes.md").is_file()
