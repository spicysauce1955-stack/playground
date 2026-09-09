"""Pilot tests for the Textual TUI skeleton."""

from __future__ import annotations

import shutil
from pathlib import Path
from textwrap import dedent

import pytest
from textual.widgets import ListView

from playground.backend import dispatch
from playground.tui.app import PlaygroundTui

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


def _device_lab_config(tmp_path: Path) -> Path:
    """`generic-infra`, plus one `pixel7` device slot -- synthesized
    rather than committed, same rationale as `tests/cli/test_apply_devices.py`'s
    helper of the same name: a permanent device-bearing lab in `config/`
    would be walked by `test_every_committed_yaml_parses` and counted by
    diagnostic-total tests elsewhere.
    """
    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config_dir)
    (config_dir / "labs" / "device-lab.yaml").write_text(
        dedent(
            """
            apiVersion: playground/v1
            kind: Lab
            metadata:
              name: device-lab
            spec:
              backend: local-libvirt
              networks:
                - name: lab-net
                  profile: nat
                  cidr: 10.81.0.0/24
              vms:
                - name: node1
                  role: generic-node
                  networks: [lab-net]
              devices:
                - name: pixel7
                  kind: android
                  required: false
            """
        ).lstrip("\n")
    )
    return config_dir


@pytest.mark.asyncio
async def test_app_boots_and_renders_committed_lab() -> None:
    app = PlaygroundTui(config_dir=CONFIG_DIR)
    async with app.run_test() as pilot:
        await pilot.pause()
        # The committed lab list should be visible.
        labels = [str(item.id) for item in app.query("ListItem")]
        assert "lab-generic-infra" in labels
        # Render generic-infra explicitly rather than relying on which lab
        # sorts first — the default selection shifts as labs are added or
        # removed, so the assertions below must target a known lab.
        listview = app.query_one("#lab-list", ListView)
        listview.index = labels.index("lab-generic-infra")
        await pilot.pause()
        text = app.detail_text
        assert "generic-infra" in text
        assert "backend: local-libvirt" in text
        assert "## Plan" in text
        assert "## Budget" in text


@pytest.mark.asyncio
async def test_app_handles_empty_config_tree(tmp_path: Path) -> None:
    app = PlaygroundTui(config_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "No labs configured" in app.detail_text


@pytest.mark.asyncio
async def test_lab_highlight_never_probes_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for Minor 4: `_render_lab` never displays
    `status.devices`, so highlighting a device-declaring lab must not
    shell out to `adb` (or read the registry) at all -- `query_status`
    is called with `include_devices=False`. A prior, incorrect fix
    attempt merely omitted `state_dir` from the call, which gates
    nothing (`query_status` still probes whenever `resolved.devices` is
    non-empty, regardless of which `state_dir` it's pointed at); this
    test would have failed against that version.
    """
    monkeypatch.setattr(
        dispatch,
        "list_attached",
        lambda: (_ for _ in ()).throw(AssertionError("must not probe devices")),
    )
    config_dir = _device_lab_config(tmp_path)

    app = PlaygroundTui(
        config_dir=config_dir,
        tofu_dir=tmp_path / "tofu",
        state_dir=tmp_path / ".playground",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        labels = [str(item.id) for item in app.query("ListItem")]
        assert "lab-device-lab" in labels
        listview = app.query_one("#lab-list", ListView)
        listview.index = labels.index("lab-device-lab")
        await pilot.pause()
        assert "device-lab" in app.detail_text
