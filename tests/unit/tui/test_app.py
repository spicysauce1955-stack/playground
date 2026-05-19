"""Pilot tests for the Textual TUI skeleton."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.tui.app import PlaygroundTui

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.mark.asyncio
async def test_app_boots_and_renders_committed_lab() -> None:
    app = PlaygroundTui(config_dir=CONFIG_DIR)
    async with app.run_test() as pilot:
        await pilot.pause()
        # The committed lab list should be visible.
        labels = [str(item.id) for item in app.query("ListItem")]
        assert "lab-generic-infra" in labels
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
