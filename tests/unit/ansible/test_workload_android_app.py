"""Structural guards for the android_app workload role.

The two guards below are what make `playground apply` idempotent
(principle 7): without them a converged host reports `changed` on every
run. They fail quietly in production -- the lab still works -- so they are
pinned here rather than trusted to review.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[3]
ROLE = REPO_ROOT / "ansible" / "roles" / "workload_android_app"
TASKS = ROLE / "tasks" / "main.yml"
SITE = REPO_ROOT / "ansible" / "site.yml"

_yaml = YAML(typ="safe")


def _tasks() -> list[dict]:
    return _yaml.load(TASKS.read_text())


def test_role_filters_on_the_android_app_type() -> None:
    text = TASKS.read_text()
    assert "android_app" in text
    assert "selectattr" in text, "must filter pg_workloads by type"


def test_role_ends_host_when_no_android_workloads() -> None:
    metas = [t for t in _tasks() if "ansible.builtin.meta" in t]
    assert any(t["ansible.builtin.meta"] == "end_host" for t in metas)


def test_install_is_guarded_by_an_installed_package_query() -> None:
    """Idempotency: never reinstall an app that is already present."""
    text = TASKS.read_text()
    assert "pm list packages" in text, "must query installed packages first"
    install = [t for t in _tasks() if "install" in str(t.get("name", "")).lower()]
    assert install, "expected an install task"
    assert any("when" in t for t in install), "install must be conditional"


def test_launch_checks_the_process_before_starting() -> None:
    """`launch: true` means ensure-running, not start-every-apply."""
    text = TASKS.read_text()
    assert "pidof" in text, (
        "launch must check whether the app is already running, or every "
        "apply reports changed"
    )


def test_split_install_uses_install_multiple() -> None:
    assert "install-multiple" in TASKS.read_text()


def test_role_waits_for_boot_completed_before_installing() -> None:
    """Right after apply the container is up but Android is still booting."""
    assert "sys.boot_completed" in TASKS.read_text()


def test_site_yml_runs_the_role_after_redroid() -> None:
    plays = _yaml.load(SITE.read_text())
    names = [p.get("name", "") for p in plays]
    redroid_idx = next(i for i, n in enumerate(names) if "Redroid" in n)
    android_idx = next(i for i, n in enumerate(names) if "android_app" in n)
    assert android_idx > redroid_idx, (
        "the Android container must exist before an APK can be installed"
    )


def test_site_yml_play_is_platform_level_like_other_workload_plays() -> None:
    plays = _yaml.load(SITE.read_text())
    play = next(p for p in plays if "android_app" in p.get("name", ""))
    assert play["hosts"] == "playground"
    assert play["become"] in (True, "yes")
    assert "workload_android_app" in str(play["roles"])
