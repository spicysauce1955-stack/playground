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


def test_adb_apt_task_tolerates_an_unreachable_archive() -> None:
    """Every other apt task in this role sets failed_when: false so an
    unreachable archive cannot abort the play. This one did not -- and it
    failed even when adb was ALREADY installed, because `update_cache`
    fails before package state is examined. Asymmetry, not a decision."""
    tasks = _yaml.load(TASKS.read_text())
    apt_tasks = [t for t in tasks if "ansible.builtin.apt" in t]
    assert apt_tasks, "expected apt tasks in the redroid role"
    for t in apt_tasks:
        assert t.get("failed_when") is False, (
            f"apt task {t['name']!r} can abort the play on an unreachable "
            "archive; every apt task here must be tolerated and gated by a "
            "probe instead"
        )


def test_a_probe_gates_on_adb_actually_being_present() -> None:
    """apt succeeding is not the same question as adb being usable: the
    operator may have opted out, or shipped it another way. Without this,
    a later `adb` call dies as rc 127 with no message."""
    text = TASKS.read_text()
    assert "adb version" in text, "must probe for a working adb"
    tasks = _yaml.load(text)
    fails = [t for t in tasks if "ansible.builtin.fail" in t]
    adb_fail = [f for f in fails if "adb" in str(f["ansible.builtin.fail"]["msg"]).lower()]
    assert adb_fail, "expected an abort naming adb"
    msg = adb_fail[0]["ansible.builtin.fail"]["msg"]
    assert "redroid_install_adb" in msg, "the abort must name the opt-out"
