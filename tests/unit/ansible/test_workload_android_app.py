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


# --- Defect C: monkey must not be the primary launch path, and its rc (or
# the discovery script's rc) must not abort the whole apply. -----------


def _launch_tasks() -> list[dict]:
    return [
        t
        for t in _tasks()
        if "launch" in str(t.get("name", "")).lower()
        and "shell" in str(t.get("ansible.builtin.shell", ""))
    ]


def test_launch_resolves_the_real_activity_before_falling_back_to_monkey() -> None:
    text = TASKS.read_text()
    assert "resolve-activity" in text, (
        "must resolve the real launcher component like "
        "src/playground/android/commands.py::launch_cmd, not rely on monkey "
        "as the primary launch path"
    )
    # monkey must still exist, but only as the fallback inside the same
    # discovery script -- not as its own unconditional primary task.
    assert "monkey" in text


def test_monkey_fallback_task_does_not_fail_the_play_on_non_zero_rc() -> None:
    """LIVE BUG 2026-08-31: monkey returned 251 on a successful launch and
    later stopped launching org.fdroid.fdroid entirely; the trailing pidof
    rc with no failed_when aborted the whole apply."""
    discovery = [
        t
        for t in _tasks()
        if "monkey" in str(t.get("ansible.builtin.shell", ""))
        or "resolve-activity" in str(t.get("ansible.builtin.shell", ""))
    ]
    assert discovery, "expected a launch-discovery/monkey-fallback task"
    for task in discovery:
        assert task.get("failed_when") is False, (
            f"{task.get('name')!r} must set failed_when: false -- whether "
            "the app ends up running is a property of the app, not this role"
        )


def test_device_side_discovery_script_is_quoted_as_a_single_argument() -> None:
    """$(...) and ; must run on the DEVICE shell, not the guest invoking adb."""
    task = next(
        t
        for t in _tasks()
        if "resolve-activity" in str(t.get("ansible.builtin.shell", ""))
    )
    shell = task["ansible.builtin.shell"]
    assert "shell '" in shell or 'shell "' in shell, (
        "the resolve-activity script must be passed to `adb shell` as one "
        "quoted argument so it executes on the device"
    )


# --- Defect D: a changed APK copy must force a reinstall even when the
# package name is already present. -------------------------------------


def test_copy_tasks_are_registered() -> None:
    copy_tasks = [t for t in _tasks() if "ansible.builtin.copy" in t]
    assert len(copy_tasks) == 2, "expected the single-APK and split-APK copy tasks"
    for task in copy_tasks:
        assert "register" in task, (
            f"{task.get('name')!r} must be registered so a checksum change "
            "can force a reinstall (BUG: silent version drift)"
        )


def test_install_tasks_are_gated_on_the_copy_result_changing() -> None:
    install = [
        t
        for t in _tasks()
        if str(t.get("name", "")).lower().startswith("install")
    ]
    assert len(install) == 2
    copy_registers = {
        t["register"] for t in _tasks() if "ansible.builtin.copy" in t
    }
    for task in install:
        when_text = str(task["when"])
        assert any(reg in when_text for reg in copy_registers), (
            f"{task.get('name')!r}'s when must reference the copy task's "
            "registered result, or a rebuilt APK silently never reinstalls"
        )
        # Must match per-item (loop results), not the whole registered
        # object, or one workload's change would trigger another's reinstall.
        assert "item.name" in when_text


# --- Defect B: a single bad `pm grant` permission must not abort the
# entire apply (it runs after the APK is already installed). -----------


def test_grant_permissions_task_does_not_abort_the_apply() -> None:
    grant = next(
        t
        for t in _tasks()
        if str(t.get("name", "")).lower().startswith("grant")
    )
    assert grant.get("failed_when") is False, (
        "pm grant fails for install-time-only or undeclared permissions; "
        "that must not abort the play after the APK is already installed"
    )


# --- Stale split APKs must be purged before staging a new split set. ---


def test_stale_split_apks_are_purged_before_staging() -> None:
    tasks = _tasks()
    names = [str(t.get("name", "")) for t in tasks]
    purge_idx = next(
        i for i, n in enumerate(names) if "purge" in n.lower() and "split" in n.lower()
    )
    stage_split_idx = next(
        i for i, n in enumerate(names) if "split-apk directory" in n.lower()
    )
    assert purge_idx < stage_split_idx, (
        "stale splits must be removed before staging the current split set"
    )
    purge_task = tasks[purge_idx]
    assert "ansible.builtin.file" in purge_task
    assert purge_task["ansible.builtin.file"].get("state") == "absent"


# --- Honesty: the role header must not overclaim unconditional
# idempotency. ------------------------------------------------------------


def test_header_documents_the_reinstall_exception() -> None:
    text = TASKS.read_text()
    assert "reinstall" in text.split("---")[0].lower() or "reinstall" in text[:2000].lower()


def test_header_documents_the_launch_pidof_caveat() -> None:
    text = TASKS.read_text()[:3000]
    assert "splash" in text.lower() or "one-shot" in text.lower(), (
        "header must document that launch idempotency depends on the app "
        "staying running, not just on this role"
    )


# --- Minor defects. -------------------------------------------------------


def test_boot_completed_register_is_not_dead_code() -> None:
    text = TASKS.read_text()
    assert "pg_android_booted" not in text, (
        "either remove the unused register or wire it into a failed_when"
    )


def test_apk_extension_check_is_case_insensitive() -> None:
    text = TASKS.read_text()
    assert text.count("lower) . endswith") == 0
    assert text.count("| lower).endswith('.apk')") >= 4, (
        "the file-vs-directory discriminator must not be case-sensitive "
        "(e.g. a source named App.APK)"
    )


def test_android_app_workloads_without_an_android_block_are_skipped_not_crashed() -> None:
    text = TASKS.read_text()
    guarded = (
        "rejectattr('android', 'defined')" in text
        or "selectattr('android', 'defined')" in text
    )
    assert guarded, (
        "a type: android_app workload with no `android:` block must be "
        "filtered out, not reach item.android.package and crash"
    )


# --- Defect E: the launcher-discovery task must guard on the resolved
# component's OWN package prefix, not just on the presence of a slash,
# exactly like src/playground/android/commands.py::launch_cmd. --------


def _discovery_task() -> dict:
    return next(
        t
        for t in _tasks()
        if "resolve-activity" in str(t.get("ansible.builtin.shell", ""))
    )


def test_discovery_guard_checks_the_resolved_component_belongs_to_the_package() -> None:
    """LIVE BUG: `cmd package resolve-activity` can return a component from
    a DIFFERENT package (e.g. the Resolver activity or a Settings
    fallback) when the requested package has no launcher activity of its
    own. Both contain a `/`, so a slash-only check wrongly launches the
    foreign component instead of falling through to monkey."""
    shell = str(_discovery_task()["ansible.builtin.shell"])
    assert '${ACT#*/}" != "$ACT"' in shell, "must still check for a slash"
    assert '${ACT%%/*}" =' in shell, (
        "must also check the resolved component's package prefix matches "
        "the requested package, or a foreign component (e.g. the "
        "Resolver activity) gets launched instead of falling through to "
        "monkey -- see launch_cmd's matching guard"
    )


def test_discovery_task_polls_for_the_process_instead_of_a_single_pidof() -> None:
    """monkey/am start only dispatch an intent; process creation is async,
    so a single immediate pidof misreads a successful cold start as a
    failure. Must poll like the declared-activity sibling task."""
    shell = str(_discovery_task()["ansible.builtin.shell"])
    assert "while" in shell, (
        "must bound-poll pidof instead of checking it exactly once, like "
        "the declared-activity launch task and launch_cmd itself"
    )
    assert shell.count("pidof") == 1, (
        "the poll loop must be the only pidof check left in this task"
    )


def test_declared_activity_launch_also_verifies_with_pidof() -> None:
    """AUDIT 3d: `am start` returns an exit code that varies by Android
    release and prints its real error on stderr, so it is no more a launch
    oracle than monkey was. The monkey path got pidof verification after it
    failed a live apply; this path is the same shape and must not be left
    as the next live failure."""
    tasks = _tasks()
    task = next(
        t for t in tasks if "declared activity" in str(t.get("name", ""))
    )
    body = str(task["ansible.builtin.shell"])
    assert "pidof" in body, "am start's rc must not decide success"
    assert task.get("failed_when") is False, (
        "one app failing to foreground must not destroy an otherwise "
        "converged host"
    )
