"""The redroid role must put `adb` on the guest.

Both layers of the app-lifecycle feature run adb ON the VM (see
docs/superpowers/specs/2026-08-31-android-app-lifecycle-design.md), so the
guest needs the client. Verified available: Ubuntu Noble universe ships
`adb` from source android-platform-tools 34.0.4-1build3.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKS = REPO_ROOT / "ansible" / "roles" / "redroid" / "tasks" / "main.yml"
DEFAULTS = REPO_ROOT / "ansible" / "roles" / "redroid" / "defaults" / "main.yml"

_yaml = YAML(typ="safe")


def test_adb_install_defaults_to_true() -> None:
    assert _yaml.load(DEFAULTS.read_text())["redroid_install_adb"] is True


def test_role_installs_adb_package() -> None:
    tasks = _yaml.load(TASKS.read_text())
    adb_installs = [
        t for t in tasks
        if t.get("ansible.builtin.apt", {}).get("name") == "adb"
    ]
    assert len(adb_installs) == 1, "expected exactly one adb install task"
    assert "redroid_install_adb" in str(adb_installs[0].get("when", "")), (
        "the adb install must honor the opt-out"
    )


def test_adb_install_runs_before_the_container_starts() -> None:
    """Ordering only matters for readability here, but a reviewer should see
    adb installed alongside the rest of the runtime, not appended after."""
    names = [t["name"] for t in _yaml.load(TASKS.read_text())]
    adb_idx = next(i for i, n in enumerate(names) if "adb" in n.lower())
    run_idx = next(i for i, n in enumerate(names) if n == "Run redroid container")
    assert adb_idx < run_idx
