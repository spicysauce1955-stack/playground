# Physical Android Devices — Plan 1 (Device as a Target)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a USB-attached Android handset a declarable, targetable member of a lab, driven by the existing `app` / `adb` / `shell` verbs with no new verb vocabulary.

**Architecture:** `AndroidTarget` grows a transport. `android/commands.py` stops returning rendered shell strings and returns adb *argument lists*; a `GuestAdb` renderer wraps them in today's `adb connect …; adb -s 127.0.0.1:5555 …` shell string for ssh, and a new `LocalAdb` renderer produces `["adb", "-s", <serial>, …]` executed with no shell at all. Above that seam nothing changes. Handsets are declared as `spec.devices` slots holding a *selector* and *assertions*; every probed fact lives in machine-owned state, never in config.

**Tech Stack:** Python 3.12+ / Pydantic v2 / Typer / pytest. `adb` on the controller's PATH. No new runtime dependency.

**Spec:** `docs/superpowers/specs/2026-09-08-physical-android-devices-design.md` — read it first; this plan argues from it.

**Out of scope (Plan 2, blocked on a spike):** all traffic capture from handsets. Do not touch `capture/state.py`'s record shape, do not add capture strategies, do not create network namespaces. The one capture-adjacent change in this plan is Task 15's replacement of a hardcoded backend allowlist with a capability check.

## Global Constraints

- **No hardcoded secrets.** PRD constraint. Device notes are committed by default (operator decision) — that makes the constraint *more* live, not less. Never log note bodies, never echo a note into a diagnostic message body except the deliberate `require`-failure path in Task 14.
- **Config declares intent, state holds facts.** `ResolvedDevice` carries `name`, `kind`, `match`, `require`, `tags`, `required` and **nothing else**. Any probed value (model, SDK, ABI, root, bound serial) belongs in `DeviceStatus` or the registry. A capability field on `ResolvedDevice` is a plan violation.
- **Never write under `config/` without an explicit human action.** Exactly three actions may create a `DeviceProfile`: answering yes to the discovery prompt, `device save`, and writing a shared note.
- **Rebinding is never automatic.** A different handset matching a bound slot's selector is an error naming `--rebind`, never a silent retarget.
- **Non-TTY forces `silent` discovery.** `sys.stdin.isatty()` false ⇒ never prompt. JSON output, CI and the TUI must not block on stdin.
- **`LabSpec` and `DefaultsSpec` are `BaseModel` with `ConfigDict(extra="forbid", frozen=True)`**, not `StrictModel`. New nested models use `StrictModel`.
- **Keep `__all__` lists alphabetically sorted** in `models/kinds.py`, `models/resolved.py`, `models/status.py`.
- **`ruff` line-length is 100**, target `py312`. Run `ruff check src tests` before every commit.
- **Devices are excluded from budget accounting.** `max_vms` counts VMs only.
- **The `spec.device_discovery` key is deliberately NOT `spec.devices.discovery`** — `LabSpec.devices` is a list, and a same-named mapping on `Defaults` would make two config kinds disagree about one key's shape.

---

## File Structure

**New:**

| Path | Responsibility |
|---|---|
| `src/playground/android/transport.py` | `AdbInvocation`, `GuestAdb`, `LocalAdb`, `AndroidTarget`, and the two renderers |
| `src/playground/devices/__init__.py` | package marker |
| `src/playground/devices/adb.py` | `adb devices -l` parsing and the fact probe |
| `src/playground/devices/registry.py` | serial-keyed known-device registry under `.playground/` |
| `src/playground/devices/binding.py` | selector matching, slot→serial bindings, rebind refusal |
| `src/playground/devices/notes.py` | shared and private note surfaces |
| `src/playground/devices/discovery.py` | prompt/silent policy resolution |
| `src/playground/devices/adopt.py` | adopt / release lifecycle |
| `src/playground/cli/device_commands.py` | the `playground device` verb group |
| `config/devices/` | committed `DeviceProfile` YAML + `.md` note sidecars (directory created by Task 6's test fixture) |

**Modified:**

| Path | Change |
|---|---|
| `src/playground/android/commands.py` | builders return `AdbInvocation`; outer quoting moves to the renderer |
| `src/playground/android/runner.py` | dispatch on transport; local path runs shell-free |
| `src/playground/android/targets.py` | `vm_name` → `name`; device targets from status |
| `src/playground/models/kinds.py` | `LabDevice`, `DeviceProfile`, `LabSpec.devices`, `device_discovery` |
| `src/playground/models/resolved.py` | `ResolvedDevice`, `ResolvedLab.devices` |
| `src/playground/models/status.py` | `DeviceStatus`, `DeviceState`, `LabStatus.devices` |
| `src/playground/config/loader.py` / `discovery.py` | load `DeviceProfile`, skip `.md` sidecars |
| `src/playground/config/resolver.py` | resolve devices, layer profiles |
| `src/playground/validation/validator.py` | device-slot validation |
| `src/playground/cli/app_commands.py` | transport-conditional staging; `target.name` |
| `src/playground/capture/targets.py` | capability check replaces backend allowlist |
| `src/playground/preflight/doctor.py` | adb + device checks |
| `src/playground/cli/main.py` | register `device_app`; adopt during `apply` |

---

## Phase A — The transport seam

Phase A changes no behaviour. It ends with every existing test green and the emulator path byte-identical.

### Task 1: Characterization tests for every command builder

**Files:**
- Test: `tests/unit/android/test_commands_characterization.py` (create)

**Interfaces:**
- Consumes: the current public surface of `playground.android.commands`.
- Produces: a frozen record of today's output that Task 2 must reproduce exactly.

- [ ] **Step 1: Write the characterization test**

```python
"""Frozen snapshots of every adb builder's output, taken BEFORE the
transport refactor.

Task 2 moves outer adb-argv quoting out of these builders and into the
GuestAdb renderer. That refactor is correct only if the guest-rendered
string is byte-identical to what shipped. This file is the proof, and it
must not be edited to accommodate the refactor -- if a value here has to
change, the refactor changed guest behaviour and is wrong.
"""

from __future__ import annotations

import pytest

from playground.android import commands as c

PREFIX = "adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true; adb -s 127.0.0.1:5555"


def test_adb_prefix_is_frozen():
    assert c.adb_prefix() == PREFIX


@pytest.mark.parametrize(
    ("built", "expected"),
    [
        (c.install_cmd("/tmp/a.apk"), f"{PREFIX} install -r /tmp/a.apk"),
        (c.install_cmd("/tmp/my app.apk"), f"{PREFIX} install -r '/tmp/my app.apk'"),
        (
            c.install_multiple_cmd(["/t/a.apk", "/t/b.apk"]),
            f"{PREFIX} install-multiple -r /t/a.apk /t/b.apk",
        ),
        (c.uninstall_cmd("com.example"), f"{PREFIX} uninstall com.example"),
        (c.adb_shell("getprop ro.build.version.sdk"),
         f"{PREFIX} shell 'getprop ro.build.version.sdk'"),
        (c.clear_cmd("com.example"), f"{PREFIX} shell 'pm clear com.example'"),
        (c.screenshot_cmd("/sdcard/s.png"), f"{PREFIX} shell 'screencap -p /sdcard/s.png'"),
        (c.ui_dump_cmd("/sdcard/u.xml"), f"{PREFIX} shell 'uiautomator dump /sdcard/u.xml'"),
        (c.logcat_cmd(follow=False, lines=None, clear=True), f"{PREFIX} logcat -c"),
        (c.logcat_cmd(follow=False, lines=None), f"{PREFIX} logcat -d"),
        (c.logcat_cmd(follow=True, lines=None), f"{PREFIX} logcat"),
        (c.logcat_cmd(follow=False, lines=5), f"{PREFIX} logcat -d -t 5"),
        (c.logcat_cmd(follow=True, lines=None, priority="w"), f"{PREFIX} logcat '*:W'"),
        (c.logcat_cmd(follow=True, lines=None, tag="Zygote"), f"{PREFIX} logcat -s Zygote:V"),
        (c.list_packages_cmd(pattern=None, third_party=False),
         f"{PREFIX} shell 'pm list packages'"),
        (c.list_packages_cmd(pattern="exam", third_party=True),
         f"{PREFIX} shell 'pm list packages -3 exam'"),
        (c.input_cmd("key", ["KEYCODE_HOME"]), f"{PREFIX} shell 'input keyevent KEYCODE_HOME'"),
        (c.input_cmd("text", ["hello world"]),
         f'{PREFIX} shell \'input text \'"\'"\'hello world\'"\'"\'\''),
    ],
)
def test_builder_output_is_frozen(built, expected):
    assert built == expected


def test_launch_deep_link_is_frozen():
    assert c.launch_cmd("com.example", activity=None, url="https://x.test") == (
        f"{PREFIX} shell 'am start -a android.intent.action.VIEW -d https://x.test'"
    )


def test_launch_activity_is_frozen():
    assert c.launch_cmd("com.example", activity=".Main", url=None) == (
        f"{PREFIX} shell 'am start -n com.example/.Main'"
    )


def test_launch_discovery_script_is_frozen():
    built = c.launch_cmd("com.example", activity=None, url=None)
    assert built.startswith(f"{PREFIX} shell '")
    assert "cmd package resolve-activity --brief com.example" in built
    assert "monkey -p com.example" in built


def test_stop_script_is_frozen():
    built = c.stop_cmd("com.example")
    assert built.startswith(f"{PREFIX} shell '")
    assert "pm path com.example >/dev/null 2>&1 || exit 1" in built
    assert "pidof com.example >/dev/null 2>&1 && exit 1" in built


def test_logcat_package_script_is_frozen():
    built = c.logcat_cmd(follow=False, lines=3, package="com.example")
    assert built.startswith(f"{PREFIX} shell '")
    assert "PID=$(pidof com.example 2>/dev/null)" in built
    assert '--pid="$PID"' in built
    assert "| tail -n 3" in built
```

- [ ] **Step 2: Run to verify it passes against unchanged code**

Run: `pytest tests/unit/android/test_commands_characterization.py -v`
Expected: PASS. If any case fails, the *expectation* is wrong — fix the expectation to match today's real output before continuing. Never fix by editing `commands.py`; this task must not change production code.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/android/test_commands_characterization.py
git commit -m "test(android): freeze adb builder output before the transport refactor"
```

---

### Task 2: The transport module and the invocation refactor

**Files:**
- Create: `src/playground/android/transport.py`
- Test: `tests/unit/android/test_transport.py` (create)
- Modify: `src/playground/android/commands.py`, `src/playground/android/targets.py`, `src/playground/android/runner.py`, `src/playground/cli/app_commands.py`, `src/playground/cli/capture_commands.py`

**Interfaces:**
- Consumes: Task 1's frozen snapshots.
- Produces:
  - `AdbInvocation(args: tuple[str, ...])`
  - `GuestAdb(ssh_host: str, ssh_port: int, ssh_user: str)`
  - `LocalAdb(serial: str)`
  - `Transport = GuestAdb | LocalAdb`
  - `AndroidTarget(name: str, transport: Transport)`
  - `render_guest_command(inv: AdbInvocation) -> str`
  - `local_argv(t: LocalAdb, inv: AdbInvocation) -> list[str]`
  - Every builder in `android/commands.py` now returns `AdbInvocation`.

- [ ] **Step 1: Write the failing test**

```python
"""Transport rendering: the guest path preserves today's shell string,
the local path never builds a shell string at all."""

from __future__ import annotations

from playground.android import commands as c
from playground.android.transport import (
    AdbInvocation,
    AndroidTarget,
    GuestAdb,
    LocalAdb,
    local_argv,
    render_guest_command,
)

PREFIX = "adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true; adb -s 127.0.0.1:5555"


def test_guest_render_reproduces_the_legacy_prefix():
    inv = AdbInvocation(("install", "-r", "/tmp/a.apk"))
    assert render_guest_command(inv) == f"{PREFIX} install -r /tmp/a.apk"


def test_guest_render_quotes_only_what_needs_it():
    inv = AdbInvocation(("install", "-r", "/tmp/my app.apk"))
    assert render_guest_command(inv) == f"{PREFIX} install -r '/tmp/my app.apk'"


def test_guest_render_quotes_a_device_script_as_one_token():
    inv = AdbInvocation(("shell", "pm clear com.example"))
    assert render_guest_command(inv) == f"{PREFIX} shell 'pm clear com.example'"


def test_local_argv_is_shell_free_and_unquoted():
    inv = AdbInvocation(("shell", "pm clear com.example"))
    assert local_argv(LocalAdb("1A2B3C"), inv) == [
        "adb", "-s", "1A2B3C", "shell", "pm clear com.example",
    ]


def test_local_argv_keeps_a_glob_intact():
    """The guest renderer quotes `*:W` for the guest shell; the local path
    must hand adb the raw two-character filterspec, not `'*:W'`."""
    inv = c.logcat_cmd(follow=True, lines=None, priority="w")
    assert local_argv(LocalAdb("S"), inv) == ["adb", "-s", "S", "logcat", "*:W"]
    assert render_guest_command(inv) == f"{PREFIX} logcat '*:W'"


def test_builders_return_invocations():
    assert isinstance(c.install_cmd("/tmp/a.apk"), AdbInvocation)
    assert c.install_cmd("/tmp/a.apk").args == ("install", "-r", "/tmp/a.apk")


def test_android_target_carries_a_transport():
    t = AndroidTarget(name="droid1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    assert t.name == "droid1"
    assert isinstance(t.transport, GuestAdb)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/android/test_transport.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'playground.android.transport'`

- [ ] **Step 3: Write the transport module**

```python
"""Where an adb invocation is rendered for one transport or another.

`android/commands.py` builds WHAT to run as an argument list; this module
decides HOW it reaches a device. Two transports:

`GuestAdb` -- today's path. The invocation is rendered into a shell
string, prefixed with `adb connect`, and handed to ssh. Because a shell
is involved, this renderer applies the outer `shlex.quote` that used to
live in the builders. Output is byte-identical to the pre-refactor
strings; `tests/unit/android/test_commands_characterization.py` is the
proof, and it must never be edited to accommodate a change here.

`LocalAdb` -- a handset on the operator's own USB bus. The invocation
becomes an argv executed with no shell anywhere in the chain, so the
guest/device double-parse hazard documented in `commands.adb_shell`
simply does not exist on this path. That is why the builders must return
UNQUOTED outer arguments: a `shlex.quote` baked into a builder would ship
literal quote characters to adb here.

Inner quoting is a different thing and stays where it is. A device shell
script (`am start …`, `pm clear …`) is one argument to `adb shell`, and
the quoting of package names and free text INSIDE that script defends
against the DEVICE's own `sh -c` on both transports.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

ADB_PORT = 5555
"""Port the Redroid container publishes on the VM (ansible/roles/redroid)."""

GUEST_SERIAL = f"127.0.0.1:{ADB_PORT}"


@dataclass(frozen=True)
class AdbInvocation:
    """The adb arguments that follow `-s <serial>`."""

    args: tuple[str, ...]


@dataclass(frozen=True)
class GuestAdb:
    """adb runs inside a lab guest, reached over ssh."""

    ssh_host: str
    ssh_port: int
    ssh_user: str


@dataclass(frozen=True)
class LocalAdb:
    """adb runs on the controller against a USB-attached handset."""

    serial: str


Transport = GuestAdb | LocalAdb


@dataclass(frozen=True)
class AndroidTarget:
    """One reachable Android device, however it is reached."""

    name: str
    transport: Transport


def guest_prefix() -> str:
    """`adb connect` then `adb -s`, so no command depends on a running server.

    `|| true` because reconnecting an already-connected device exits
    non-zero.
    """
    return (
        f"adb connect {GUEST_SERIAL} >/dev/null 2>&1 || true; "
        f"adb -s {GUEST_SERIAL}"
    )


def render_guest_command(inv: AdbInvocation) -> str:
    """Render for a guest login shell. Byte-identical to the legacy strings."""
    return " ".join([guest_prefix(), *(shlex.quote(a) for a in inv.args)])


def local_argv(transport: LocalAdb, inv: AdbInvocation) -> list[str]:
    """Render for direct execution. No shell, therefore no quoting."""
    return ["adb", "-s", transport.serial, *inv.args]


__all__ = [
    "ADB_PORT",
    "AdbInvocation",
    "AndroidTarget",
    "GUEST_SERIAL",
    "GuestAdb",
    "LocalAdb",
    "Transport",
    "guest_prefix",
    "local_argv",
    "render_guest_command",
]
```

- [ ] **Step 4: Convert every builder in `commands.py` to return `AdbInvocation`**

Rules, applied mechanically:

1. Delete `ADB_PORT`, `_SERIAL` and `adb_prefix` from `commands.py`; re-export `ADB_PORT` from `transport` for the one CLI import (`from playground.android.transport import ADB_PORT`).
2. A builder that produced `f"{adb_prefix()} X Y Z"` now returns `AdbInvocation(("X", "Y", "Z"))`.
3. **Remove the outer `shlex.quote` from builder arguments.** `install_cmd` returns `AdbInvocation(("install", "-r", remote_apk))` — unquoted.
4. **Keep every inner `shlex.quote`** that guards the DEVICE shell inside a script string. `adb_shell(command)` becomes `AdbInvocation(("shell", command))`; the `shlex.quote(command)` it used to apply is now the renderer's job.
5. `_logcat_filterspec` returns unquoted tokens: `["-s", f"{tag}:{level}"]` and `[f"*:{priority.upper()}"]`.
6. `-t N` becomes two arguments: `("-t", str(int(lines)))`.

```python
def adb_shell(command: str) -> AdbInvocation:
    """Run ``command`` on the device shell as exactly one adb argument.

    `adb shell` joins its argv with plain spaces and hands the result to
    /system/bin/sh -c on the device for a second, independent parse. The
    whole script must therefore arrive as ONE argument, which is what
    this returns.

    Callers remain responsible for `shlex.quote`-ing dynamic values
    (package names, free text) INSIDE ``command``: that inner quoting is
    what makes the device's own sh treat them as single arguments, and it
    is required on both transports. The OUTER quoting that used to live
    here now belongs to `transport.render_guest_command`, because only
    the guest path has a shell to defend against.
    """
    return AdbInvocation(("shell", command))


def install_cmd(remote_apk: str) -> AdbInvocation:
    return AdbInvocation(("install", "-r", remote_apk))


def install_multiple_cmd(remote_apks: list[str]) -> AdbInvocation:
    return AdbInvocation(("install-multiple", "-r", *remote_apks))


def uninstall_cmd(package: str) -> AdbInvocation:
    return AdbInvocation(("uninstall", package))


def clear_cmd(package: str) -> AdbInvocation:
    return adb_shell(f"pm clear {shlex.quote(package)}")


def screenshot_cmd(remote_path: str) -> AdbInvocation:
    return adb_shell(f"screencap -p {shlex.quote(remote_path)}")


def ui_dump_cmd(remote_path: str) -> AdbInvocation:
    return adb_shell(f"uiautomator dump {shlex.quote(remote_path)}")


def _logcat_filterspec(*, tag: str | None, priority: str | None) -> list[str]:
    """Trailing filterspec tokens, UNQUOTED.

    These used to be `shlex.quote`d here so the guest shell would not glob
    `*:W` against its working directory. That protection now lives in
    `render_guest_command`, which quotes every argument; returning a
    quoted token here would ship literal `'` characters to adb on the
    local transport.
    """
    if tag is not None:
        return ["-s", f"{tag}:{(priority or 'V').upper()}"]
    if priority is not None:
        return [f"*:{priority.upper()}"]
    return []
```

`logcat_cmd`'s branches become:

```python
    if clear:
        return AdbInvocation(("logcat", "-c"))
    ...
    parts = ["logcat"]
    if not follow:
        parts.append("-d")
    filterspec = _logcat_filterspec(tag=tag, priority=priority)
    if lines is not None and filterspec and not follow:
        device = " ".join(["logcat", "-d", *(shlex.quote(f) for f in filterspec)])
        return adb_shell(f"{device} | tail -n {int(lines)}")
    if lines is not None:
        parts.extend(("-t", str(int(lines))))
    parts.extend(filterspec)
    return AdbInvocation(tuple(parts))
```

Note the device-side pipe form quotes the filterspec again — that string runs in the DEVICE shell, so it needs its own inner quoting on both transports. Keep `_logcat_package_cmd` unchanged apart from returning `adb_shell(...)`'s new type.

Leave `wait_booted_cmd` alone in this task; Task 3 removes it.

- [ ] **Step 5: Update the three consumers**

In `android/targets.py` and `capture/targets.py`, build `AndroidTarget(name=..., transport=GuestAdb(ssh_host, ssh_port, user))` and drop the `ssh_host`/`ssh_port`/`ssh_user`/`vm_name` fields (the dataclass now lives in `transport.py`; re-export it from `targets.py` so existing imports keep working: `from playground.android.transport import AndroidTarget`).

In `android/runner.py`, `_run_one` renders before running:

```python
from playground.android.transport import AdbInvocation, GuestAdb, render_guest_command

def _run_one(target: AndroidTarget, inv: AdbInvocation, timeout: float) -> TargetResult:
    transport = target.transport
    if isinstance(transport, GuestAdb):
        argv = build_ssh_argv(
            transport.ssh_host,
            user=transport.ssh_user,
            port=transport.ssh_port,
            command=render_guest_command(inv),
            batch=True,
        )
    else:
        argv = local_argv(transport, inv)
    ...
```

In `cli/app_commands.py` and `cli/capture_commands.py`, replace every `result.target.vm_name` with `result.target.name`, and change `_dispatch`/`_stream` parameter types from `str` to `AdbInvocation`.

- [ ] **Step 6: Run the full suite**

Run: `pytest tests/unit/android tests/cli -v`
Expected: PASS, **including `test_commands_characterization.py` unchanged.** If a characterization case fails, the refactor changed guest behaviour — fix the refactor, not the test.

- [ ] **Step 7: Commit**

```bash
git add src/playground/android tests/unit/android src/playground/cli src/playground/capture
git commit -m "refactor(android): split adb invocation from transport rendering"
```

---

### Task 3: Replace the guest-side boot-wait loop with a runner poll

**Files:**
- Modify: `src/playground/android/commands.py` (delete `wait_booted_cmd`), `src/playground/android/runner.py`, `src/playground/cli/app_commands.py:532`
- Test: `tests/unit/android/test_runner_wait.py` (create)

**Interfaces:**
- Consumes: `AdbInvocation`, `run_on_targets` from Task 2.
- Produces: `wait_booted(targets: list[AndroidTarget], *, attempts: int, interval: float) -> list[str]` returning the names of targets that never reported a completed boot.

`wait_booted_cmd` is the one builder that is not a single adb invocation — it is a guest shell `for` loop wrapping repeated adb calls, which cannot cross to a shell-free transport. Moving the loop into Python removes the last shell dependency from the boot gate and makes it identical on both transports.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from playground.android.runner import TargetResult, wait_booted
from playground.android.transport import AndroidTarget, LocalAdb


def _target(name: str = "pixel7") -> AndroidTarget:
    return AndroidTarget(name=name, transport=LocalAdb("S1"))


def test_wait_booted_returns_empty_when_boot_completes(monkeypatch):
    t = _target()

    def fake_run(targets, inv, *, timeout):
        return [TargetResult(targets[0], 0, "1\n", "")]

    monkeypatch.setattr("playground.android.runner.run_on_targets", fake_run)
    assert wait_booted([t], attempts=3, interval=0) == []


def test_wait_booted_names_targets_that_never_boot(monkeypatch):
    t = _target()

    def fake_run(targets, inv, *, timeout):
        return [TargetResult(targets[0], 0, "0\n", "")]

    monkeypatch.setattr("playground.android.runner.run_on_targets", fake_run)
    assert wait_booted([t], attempts=2, interval=0) == ["pixel7"]


def test_wait_booted_tolerates_a_carriage_return(monkeypatch):
    """getprop over adb returns CRLF on some devices."""
    t = _target()

    def fake_run(targets, inv, *, timeout):
        return [TargetResult(targets[0], 0, "1\r\n", "")]

    monkeypatch.setattr("playground.android.runner.run_on_targets", fake_run)
    assert wait_booted([t], attempts=1, interval=0) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/android/test_runner_wait.py -v`
Expected: FAIL with `ImportError: cannot import name 'wait_booted'`

- [ ] **Step 3: Implement**

```python
import time

from playground.android.commands import adb_shell

BOOT_PROP_TIMEOUT = 15.0
"""Per-attempt timeout for one getprop round trip."""


def wait_booted(
    targets: list[AndroidTarget], *, attempts: int, interval: float
) -> list[str]:
    """Poll until Android reports a completed boot; return the stragglers.

    This used to be a shell `for` loop built by `commands.wait_booted_cmd`
    and executed on the guest. A handset transport has no shell, so the
    loop moved here -- which also means one poll implementation for both
    transports instead of a string that only one of them can run.
    """
    invocation = adb_shell("getprop sys.boot_completed")
    pending = list(targets)
    for attempt in range(attempts):
        if not pending:
            break
        results = run_on_targets(pending, invocation, timeout=BOOT_PROP_TIMEOUT)
        pending = [
            r.target for r in results if r.stdout.strip() != "1"
        ]
        if pending and attempt < attempts - 1 and interval:
            time.sleep(interval)
    return [t.name for t in pending]
```

- [ ] **Step 4: Update the call site**

`app_commands.py:532` currently runs `wait_booted_cmd(_BOOT_WAIT_SECONDS)` through `run_on_targets`. Replace with:

```python
    stragglers = wait_booted([target], attempts=_BOOT_WAIT_SECONDS, interval=1.0)
    if stragglers:
        _fail(
            Diagnostic(
                id="runtime.app.boot_timeout",
                severity="error",
                message=(
                    f"device {stragglers[0]!r} did not report sys.boot_completed=1 "
                    f"within {_BOOT_WAIT_SECONDS}s"
                ),
                source=SourceLocation(path="device"),
                suggestion="check the device is unlocked and finished booting",
            )
        )
```

Delete `wait_booted_cmd` from `commands.py`, its import in `app_commands.py`, and its two references in `tests/unit/android/test_commands.py` (the import list and `test_wait_booted_cmd`). Remove the `wait_booted_cmd` line from the characterization test's import block only if present — it is not.

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/android tests/cli/test_app.py -v`
Expected: PASS. `tests/cli/test_app.py:678` (the BUG C regression that installs must gate on a boot wait) must still pass — update its monkeypatch target from `wait_booted_cmd` to `wait_booted` if it patches the builder.

- [ ] **Step 6: Commit**

```bash
git add src/playground/android src/playground/cli/app_commands.py tests
git commit -m "refactor(android): poll for boot in Python instead of a guest shell loop"
```

---

### Task 4: Transport-conditional file staging

**Files:**
- Modify: `src/playground/cli/app_commands.py` (`_scp_one`, `_capture_to_local`, `install_command`, `push_command`, `pull_command`, `screenshot_command`, `ui_dump_command`)
- Test: `tests/cli/test_app_local_transport.py` (create)

**Interfaces:**
- Consumes: `GuestAdb`, `LocalAdb`, `AdbInvocation` from Task 2; `run_on_targets` from `android/runner.py`.
- Produces:
  - `push_cmd(src: str, dest: str) -> AdbInvocation` and `pull_cmd(src: str, dest: str) -> AdbInvocation` in `android/commands.py` (neither exists today — `push` and `pull` are built inline in `app_commands.py` against `adb_prefix()`)
  - `stage_for(target: AndroidTarget, local_path: Path) -> str`
  - `retrieve_from(target: AndroidTarget, remote_path: str, dest: Path) -> None`
  - `_guest_cleanup(target: AndroidTarget, remote_path: str) -> None`

A guest target must scp the APK to the VM first because adb runs there. A local target must not: `adb install` already reads a controller path, and copying would be both pointless and wrong.

Today's `push_command` builds a compound guest shell string —
`adb push …; status=$?; rm -f <tmp>; exit $status` — to delete its staging
file. That cannot survive as one `AdbInvocation`, and a local target has
no staging file to delete. The cleanup therefore moves into Python, the
same move Task 3 made for the boot loop.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from pathlib import Path

from playground.android.transport import AndroidTarget, GuestAdb, LocalAdb
from playground.cli.app_commands import stage_for


def test_local_target_stages_in_place(tmp_path):
    apk = tmp_path / "a.apk"
    apk.write_bytes(b"x")
    target = AndroidTarget(name="pixel7", transport=LocalAdb("S1"))
    assert stage_for(target, apk) == str(apk)


def test_guest_target_stages_under_tmp(tmp_path, monkeypatch):
    apk = tmp_path / "a.apk"
    apk.write_bytes(b"x")
    copied: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "playground.cli.app_commands._scp_one",
        lambda target, src, dst: copied.append((str(src), dst)),
    )
    target = AndroidTarget(name="droid1", transport=GuestAdb("10.0.0.5", 22, "ubuntu"))
    remote = stage_for(target, apk)
    assert remote.startswith("/tmp/")
    assert remote.endswith("a.apk")
    assert copied == [(str(apk), remote)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cli/test_app_local_transport.py -v`
Expected: FAIL with `ImportError: cannot import name 'stage_for'`

- [ ] **Step 3: Implement**

First add the two missing builders to `android/commands.py`:

```python
def push_cmd(src: str, dest: str) -> AdbInvocation:
    """Copy a file from wherever adb is running onto the device."""
    return AdbInvocation(("push", src, dest))


def pull_cmd(src: str, dest: str) -> AdbInvocation:
    """Copy a file off the device to wherever adb is running."""
    return AdbInvocation(("pull", src, dest))
```

Then, in `app_commands.py`:

```python
def stage_for(target: AndroidTarget, local_path: Path) -> str:
    """Return the path adb should be handed for ``local_path``.

    A guest target runs adb on the VM, so the file has to travel there
    first. A local target runs adb on this machine, where the file
    already is -- copying it would be a no-op at best and a wrong path at
    worst.
    """
    if isinstance(target.transport, LocalAdb):
        return str(local_path)
    remote = f"/tmp/{local_path.name}"
    _scp_one(target, local_path, remote)
    return remote


def _guest_cleanup(target: AndroidTarget, remote_path: str) -> None:
    """Delete a guest-side staging file.

    A local target never has one. This used to ride along inside the adb
    command as `; rm -f <tmp>; exit $status`, which only worked because a
    guest shell was parsing it.
    """
    if isinstance(target.transport, LocalAdb):
        return
    transport = target.transport
    subprocess.run(  # noqa: S603
        build_ssh_argv(
            transport.ssh_host,
            user=transport.ssh_user,
            port=transport.ssh_port,
            command=f"rm -f {shlex.quote(remote_path)}",
            batch=True,
        ),
        capture_output=True, text=True, check=False, timeout=30,
    )


def retrieve_from(target: AndroidTarget, remote_path: str, dest: Path) -> None:
    """Bring a device-side file back to ``dest`` on this machine.

    Guest: `adb pull` lands it on the VM, then scp brings it here. Local:
    `adb pull` already lands it here, so there is no second hop.
    """
    if isinstance(target.transport, LocalAdb):
        run_on_targets([target], pull_cmd(remote_path, str(dest)), timeout=_DEFAULT_TIMEOUT)
        return
    guest_tmp = f"/tmp/{dest.name}"
    run_on_targets([target], pull_cmd(remote_path, guest_tmp), timeout=_DEFAULT_TIMEOUT)
    _capture_to_local(target, guest_tmp, dest)
    _guest_cleanup(target, guest_tmp)
```

Route `install_command` and `push_command` through `stage_for` (then
`_guest_cleanup` after the push completes), and `pull_command`,
`screenshot_command`, `ui_dump_command` through `retrieve_from`.
Split-APK installs (`splits.py`) call `stage_for` per part.

- [ ] **Step 4: Run tests**

Run: `pytest tests/cli -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/playground/cli/app_commands.py tests/cli/test_app_local_transport.py
git commit -m "feat(android): stage files per transport instead of always via the guest"
```

---

## Phase B — The device config model

### Task 5: `LabDevice`, `ResolvedDevice`, and the resolver

**Files:**
- Modify: `src/playground/models/kinds.py`, `src/playground/models/resolved.py`, `src/playground/config/resolver.py`, `src/playground/validation/validator.py`
- Test: `tests/unit/config/test_device_schema.py` (create)

**Interfaces:**
- Produces:
  - `DeviceMatch(serial: str | None, model: str | None, manufacturer: str | None)`
  - `DeviceRequire(root: bool | None, min_sdk: int | None)`
  - `LabDevice(name, kind, profile, match, require, tags, required)`
  - `LabSpec.devices: list[LabDevice]`
  - `ResolvedDevice(name, kind, match, require, tags, required)`
  - `ResolvedLab.devices: list[ResolvedDevice]`

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from playground.models.kinds import DeviceMatch, DeviceRequire, LabDevice
from playground.models.resolved import ResolvedDevice


def test_device_defaults_to_android_and_required():
    d = LabDevice(name="pixel7")
    assert d.kind == "android"
    assert d.required is True
    assert d.match is None
    assert d.require is None


def test_match_is_an_optional_selector():
    d = LabDevice(name="pixel7", match=DeviceMatch(serial="1A2B"))
    assert d.match.serial == "1A2B"


def test_require_carries_assertions_not_facts():
    d = LabDevice(name="pixel7", require=DeviceRequire(root=True, min_sdk=30))
    assert d.require.root is True
    assert d.require.min_sdk == 30


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        LabDevice(name="pixel7", rooted=True)


def test_resolved_device_has_no_capabilities_field():
    """Facts must not reach the resolved model -- the resolver runs with no
    handset attached and cannot know them."""
    assert "capabilities" not in ResolvedDevice.model_fields
    assert set(ResolvedDevice.model_fields) == {
        "name", "kind", "match", "require", "tags", "required",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/config/test_device_schema.py -v`
Expected: FAIL with `ImportError: cannot import name 'DeviceMatch'`

- [ ] **Step 3: Implement the models**

In `models/kinds.py`:

```python
class DeviceMatch(StrictModel):
    """Selector, not a description. Every field is optional; the fields that
    ARE set must all match for a device to be a candidate."""

    serial: str | None = None
    model: str | None = None
    manufacturer: str | None = None


class DeviceRequire(StrictModel):
    """Assertions checked at adopt time against the live handset.

    This is the honest direction of the dependency: config cannot know a
    phone is rooted, but it can say this lab needs one that is.
    """

    root: bool | None = None
    min_sdk: int | None = Field(default=None, ge=1)


class LabDevice(StrictModel):
    """One physical-device slot in a lab.

    Deliberately NOT a `LabVm`: a handset has no image, no vcpu/memory
    sizing and no Ansible provisioners, and bending the VM schema to fit
    would make role resolution answer questions it cannot answer.
    """

    name: str = Field(min_length=1)
    kind: Literal["android"] = "android"
    profile: str | None = None
    match: DeviceMatch | None = None
    require: DeviceRequire | None = None
    tags: list[str] = Field(default_factory=list)
    required: bool = True
```

Add `devices: list[LabDevice] = Field(default_factory=list)` to `LabSpec`.

In `models/resolved.py`:

```python
class ResolvedDevice(StrictModel):
    """A device slot, fully resolved -- and intent ONLY.

    The resolver runs from configuration with no handset attached, so it
    cannot hold a probed fact. Bound serial, model, SDK, ABI and root live
    in `DeviceStatus` and the registry. Do not add a `capabilities` field
    here; an earlier draft of the design did, and it was wrong.
    """

    name: str
    kind: Literal["android"]
    match: DeviceMatch | None = None
    require: DeviceRequire | None = None
    tags: list[str] = Field(default_factory=list)
    required: bool = True
```

Add `devices: list[ResolvedDevice] = Field(default_factory=list)` to `ResolvedLab`, and update both `__all__` lists (alphabetical).

In `config/resolver.py`, map each `LabDevice` to a `ResolvedDevice`, carrying `match`/`require` through unchanged (profile layering arrives in Task 6). In `validation/validator.py`, reject duplicate device names within a lab (`config.device.duplicate_name`) and a device name colliding with a VM name (`config.device.name_collides_with_vm`).

**Budget:** find where the validator counts `max_vms` and confirm it iterates `spec.vms`. Add a test asserting a lab with 1 VM and 3 devices under `max_vms: 1` validates clean.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/config tests/unit/models tests/unit/validation -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/playground/models src/playground/config src/playground/validation tests/unit
git commit -m "feat(config): declare physical device slots as intent-only lab members"
```

---

### Task 6: The `DeviceProfile` kind, loader support, and `.md` sidecars

**Files:**
- Modify: `src/playground/models/kinds.py`, `src/playground/config/loader.py`, `src/playground/config/discovery.py`, `src/playground/config/resolver.py`
- Test: `tests/unit/config/test_device_profile.py` (create)

**Interfaces:**
- Produces:
  - `DeviceProfileSpec(match, require, tags)` and `DeviceProfile(ResourceEnvelope)`
  - `LoadedConfig.device_profiles: dict[str, DeviceProfile]`
  - `shared_note_path(config_dir: Path, name: str) -> Path`

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from pathlib import Path

from playground.config.loader import load_config
from playground.models.kinds import DeviceMatch


def _write_profile(config_dir: Path) -> None:
    (config_dir / "devices").mkdir(parents=True, exist_ok=True)
    (config_dir / "devices" / "my-pixel.yaml").write_text(
        "apiVersion: playground/v1\n"
        "kind: DeviceProfile\n"
        "metadata:\n"
        "  name: my-pixel\n"
        "spec:\n"
        "  match:\n"
        "    serial: 1A2B3C4D5E\n"
        "  require:\n"
        "    root: true\n",
        encoding="utf-8",
    )


def test_device_profile_loads(tmp_path):
    _write_profile(tmp_path)
    loaded, diagnostics = load_config(tmp_path)
    assert not [d for d in diagnostics if d.severity == "error"]
    assert loaded.device_profiles["my-pixel"].spec.match == DeviceMatch(serial="1A2B3C4D5E")


def test_markdown_sidecar_is_skipped_not_parsed(tmp_path):
    """config/ discovery globs YAML and validates every file as a kind. A
    note sidecar must be ignored, not tripped over."""
    _write_profile(tmp_path)
    (tmp_path / "devices" / "my-pixel.md").write_text(
        "# my-pixel\n\nBootloader unlocked 2026-03.\n", encoding="utf-8"
    )
    loaded, diagnostics = load_config(tmp_path)
    assert not [d for d in diagnostics if d.severity == "error"]
    assert "my-pixel" in loaded.device_profiles


def test_lab_device_layers_over_its_profile(tmp_path):
    """A lab-level `require` wins; anything it omits comes from the profile."""
    from playground.config.resolver import resolve_lab

    _write_profile(tmp_path)
    resolved = resolve_lab_fixture(tmp_path, profile="my-pixel", require_min_sdk=30)
    device = resolved.devices[0]
    assert device.match.serial == "1A2B3C4D5E"   # from the profile
    assert device.require.root is True            # from the profile
    assert device.require.min_sdk == 30           # from the lab
```

Add the `resolve_lab_fixture` helper at the top of the test file:

```python
def resolve_lab_fixture(config_dir: Path, *, profile: str, require_min_sdk: int):
    from playground.config.loader import load_config
    from playground.config.resolver import resolve_lab

    (config_dir / "labs").mkdir(parents=True, exist_ok=True)
    (config_dir / "labs" / "l.yaml").write_text(
        "apiVersion: playground/v1\n"
        "kind: Lab\n"
        "metadata:\n  name: l\n"
        "spec:\n"
        "  backend: local-libvirt\n"
        "  devices:\n"
        f"    - name: pixel7\n      profile: {profile}\n"
        f"      require:\n        min_sdk: {require_min_sdk}\n",
        encoding="utf-8",
    )
    loaded, _ = load_config(config_dir)
    return resolve_lab(loaded, "l", config_dir=config_dir)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/config/test_device_profile.py -v`
Expected: FAIL — `DeviceProfile` is not a known kind.

- [ ] **Step 3: Implement**

```python
class DeviceProfileSpec(StrictModel):
    """A reusable selector plus assertions for one physical handset.

    Sits alongside VmRole / NetworkProfile / CommandPreset so it inherits
    the existing envelope, loader and validator machinery. Deliberately
    NOT a `DeviceRole`: the VM role system carries an image, resource
    sizing and Ansible provisioners, none of which apply to a handset.
    """

    match: DeviceMatch | None = None
    require: DeviceRequire | None = None
    tags: list[str] = Field(default_factory=list)


class DeviceProfile(ResourceEnvelope):
    kind: Literal["DeviceProfile"]
    spec: DeviceProfileSpec
```

In `config/discovery.py`, add `config/devices/*.yaml` (and `*.yml`) to the globbed set and **exclude `*.md` explicitly** — a bare "everything in devices/" glob would hand a sidecar to the YAML parser. In `loader.py`, add `device_profiles: dict[str, DeviceProfile]` to `LoadedConfig` and dispatch the new kind.

In `resolver.py`, layer profile → lab when `LabDevice.profile` is set: for `match` and `require`, a field set on the lab device wins; a field the lab omits falls back to the profile's; `tags` concatenate (profile first). An unknown `profile:` yields `config.device.profile_not_found`.

Add to `loader.py`:

```python
def shared_note_path(config_dir: Path, name: str) -> Path:
    """The committed note sidecar for a profile, associated by basename."""
    return config_dir / "devices" / f"{name}.md"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/config -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/playground/models src/playground/config tests/unit/config
git commit -m "feat(config): add the DeviceProfile kind and its markdown sidecar"
```

---

### Task 7: `DeviceStatus` on `LabStatus`

**Files:**
- Modify: `src/playground/models/status.py`
- Test: `tests/unit/models/test_device_status.py` (create)

**Interfaces:**
- Produces: `DeviceState = Literal["connected", "unauthorized", "offline", "missing"]`, `DeviceStatus`, `LabStatus.devices`.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from playground.models.status import DeviceStatus, LabStatus


def test_device_status_carries_the_probed_facts():
    d = DeviceStatus(
        name="pixel7", state="connected", serial="1A2B",
        model="Pixel 7", sdk=34, abi="arm64-v8a", rooted=False,
    )
    assert d.state == "connected"
    assert d.rooted is False


def test_unauthorized_is_its_own_state():
    """Never tapping the RSA prompt is the most common real failure and
    must not be reported as generic unreachability."""
    d = DeviceStatus(name="pixel7", state="unauthorized", serial="1A2B")
    assert d.state == "unauthorized"
    assert d.model is None


def test_lab_status_devices_defaults_empty():
    s = LabStatus(lab="l", backend="local-libvirt", expected_vms=0, provisioned_vms=0)
    assert s.devices == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/models/test_device_status.py -v`
Expected: FAIL with `ImportError: cannot import name 'DeviceStatus'`

- [ ] **Step 3: Implement**

```python
DeviceState = Literal["connected", "unauthorized", "offline", "missing"]
"""``unauthorized`` is deliberately distinct from ``missing``: the handset
is attached and visible, but its RSA trust prompt was never accepted. It
is the most common real-world failure and needs a diagnostic that names
the remedy rather than one that says "not reachable"."""


class DeviceStatus(StrictModel):
    """Intent + observed state for one physical device slot."""

    name: str
    state: DeviceState
    serial: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    sdk: int | None = None
    abi: str | None = None
    rooted: bool | None = None
    last_verified: str | None = None
```

Add `devices: list[DeviceStatus] = Field(default_factory=list)` to `LabStatus` and update `__all__`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/models -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/playground/models/status.py tests/unit/models/test_device_status.py
git commit -m "feat(models): report physical device state on LabStatus"
```

---

## Phase C — Device runtime

### Task 8: `adb devices -l` parsing and the fact probe

**Files:**
- Create: `src/playground/devices/__init__.py`, `src/playground/devices/adb.py`
- Test: `tests/unit/devices/__init__.py`, `tests/unit/devices/test_adb.py` (create)

**Interfaces:**
- Produces:
  - `AttachedDevice(serial: str, state: DeviceState, model: str | None, manufacturer: str | None)`
  - `parse_devices_l(stdout: str) -> list[AttachedDevice]`
  - `list_attached() -> tuple[list[AttachedDevice], list[Diagnostic]]`
  - `probe_facts(serial: str) -> DeviceFacts`
  - `DeviceFacts(serial, model, manufacturer, sdk, abi, rooted, fingerprint)`

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from playground.devices.adb import AttachedDevice, parse_devices_l

SAMPLE = """List of devices attached
1A2B3C4D5E             device product:panther model:Pixel_7 device:panther transport_id:1
9Z8Y7X                 unauthorized usb:1-2
0Q0Q0Q                 offline
emulator-5554          device product:sdk_gphone model:sdk_gphone64 device:emu transport_id:3

"""


def test_parses_an_authorized_device():
    devices = parse_devices_l(SAMPLE)
    first = devices[0]
    assert first == AttachedDevice(
        serial="1A2B3C4D5E", state="connected", model="Pixel 7", manufacturer=None
    )


def test_underscores_in_the_model_become_spaces():
    assert parse_devices_l(SAMPLE)[0].model == "Pixel 7"


def test_unauthorized_is_reported_not_dropped():
    unauth = [d for d in parse_devices_l(SAMPLE) if d.serial == "9Z8Y7X"]
    assert unauth[0].state == "unauthorized"


def test_offline_is_its_own_state():
    off = [d for d in parse_devices_l(SAMPLE) if d.serial == "0Q0Q0Q"]
    assert off[0].state == "offline"


def test_header_and_blank_lines_are_ignored():
    assert len(parse_devices_l(SAMPLE)) == 4


def test_empty_output_is_no_devices():
    assert parse_devices_l("List of devices attached\n\n") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/devices/test_adb.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'playground.devices'`

- [ ] **Step 3: Implement**

```python
"""Talk to the local adb server about attached handsets.

Everything here runs `adb` on the CONTROLLER. Nothing in this module
touches ssh or a lab guest -- a guest's Redroid device is reached through
`android.transport.GuestAdb` instead.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from playground.models.base import StrictModel
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.status import DeviceState

_ADB_TIMEOUT = 20.0

_STATE_MAP: dict[str, DeviceState] = {
    "device": "connected",
    "unauthorized": "unauthorized",
    "offline": "offline",
}

_KV = re.compile(r"(\w+):(\S+)")


@dataclass(frozen=True)
class AttachedDevice:
    serial: str
    state: DeviceState
    model: str | None = None
    manufacturer: str | None = None


class DeviceFacts(StrictModel):
    serial: str
    model: str | None = None
    manufacturer: str | None = None
    sdk: int | None = None
    abi: str | None = None
    rooted: bool = False
    fingerprint: str | None = None


def parse_devices_l(stdout: str) -> list[AttachedDevice]:
    """Parse `adb devices -l`.

    An `unauthorized` or `offline` line carries no `model:` key, so those
    devices come back with `model=None` -- they are still reported, never
    dropped, because "attached but untrusted" is the state the operator
    most needs to see.
    """
    devices: list[AttachedDevice] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        serial, rest = parts[0], parts[1]
        state_word = rest.split(maxsplit=1)[0]
        state = _STATE_MAP.get(state_word)
        if state is None:
            continue
        fields = dict(_KV.findall(rest))
        model = fields.get("model")
        devices.append(
            AttachedDevice(
                serial=serial,
                state=state,
                model=model.replace("_", " ") if model else None,
                manufacturer=None,
            )
        )
    return devices


def _adb(args: list[str], *, timeout: float = _ADB_TIMEOUT) -> tuple[int, str, str]:
    try:
        done = subprocess.run(  # noqa: S603
            ["adb", *args], capture_output=True, text=True, check=False, timeout=timeout
        )
    except FileNotFoundError:
        return 127, "", "adb not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"adb timed out after {timeout}s"
    return done.returncode, done.stdout, done.stderr


def list_attached() -> tuple[list[AttachedDevice], list[Diagnostic]]:
    code, out, err = _adb(["devices", "-l"])
    if code != 0:
        return [], [
            Diagnostic(
                id="device.adb_unavailable",
                severity="error",
                message=f"`adb devices -l` failed: {err.strip() or code}",
                source=SourceLocation(path="adb"),
                suggestion="install android-platform-tools and re-run `playground doctor`",
            )
        ]
    return parse_devices_l(out), []


def _getprop(serial: str, prop: str) -> str | None:
    code, out, _ = _adb(["-s", serial, "shell", "getprop", prop])
    value = out.strip()
    return value if code == 0 and value else None


def probe_facts(serial: str) -> DeviceFacts:
    """Read the handset's own description of itself.

    Root is probed rather than declared: `su -c id` printing uid=0 is the
    only answer that means anything, and a device that has no `su` at all
    simply fails the command.
    """
    sdk_raw = _getprop(serial, "ro.build.version.sdk")
    root_code, root_out, _ = _adb(["-s", serial, "shell", "su", "-c", "id"])
    return DeviceFacts(
        serial=serial,
        model=_getprop(serial, "ro.product.model"),
        manufacturer=_getprop(serial, "ro.product.manufacturer"),
        sdk=int(sdk_raw) if sdk_raw and sdk_raw.isdigit() else None,
        abi=_getprop(serial, "ro.product.cpu.abi"),
        rooted=root_code == 0 and "uid=0" in root_out,
        fingerprint=_getprop(serial, "ro.build.fingerprint"),
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/devices -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/playground/devices tests/unit/devices
git commit -m "feat(devices): parse adb device listings and probe handset facts"
```

---

### Task 9: The known-device registry

**Files:**
- Create: `src/playground/devices/registry.py`
- Test: `tests/unit/devices/test_registry.py` (create)

**Interfaces:**
- Consumes: `DeviceFacts` (Task 8).
- Produces:
  - `RegistryEntry(facts, first_seen, last_seen, last_verified, ignored)`
  - `registry_path(state_dir: Path) -> Path`
  - `read_registry(state_dir) -> dict[str, RegistryEntry]`
  - `record_seen(state_dir, facts) -> tuple[RegistryEntry, list[Diagnostic]]`
  - `mark_ignored(state_dir, serial) -> None`

Path layout follows `capture/state.py`: `<state_dir>/state/devices/registry.json`.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from playground.devices.adb import DeviceFacts
from playground.devices.registry import read_registry, record_seen


def _facts(serial="1A2B", fingerprint="goog/panther/1"):
    return DeviceFacts(serial=serial, model="Pixel 7", sdk=34, fingerprint=fingerprint)


def test_first_sighting_is_recorded(tmp_path):
    entry, diagnostics = record_seen(tmp_path, _facts())
    assert diagnostics == []
    assert entry.first_seen == entry.last_seen
    assert read_registry(tmp_path)["1A2B"].facts.model == "Pixel 7"


def test_second_sighting_updates_last_seen_not_first(tmp_path):
    first, _ = record_seen(tmp_path, _facts())
    second, _ = record_seen(tmp_path, _facts())
    assert second.first_seen == first.first_seen
    assert second.last_seen >= first.last_seen


def test_an_unplugged_device_stays_in_the_registry(tmp_path):
    record_seen(tmp_path, _facts())
    assert "1A2B" in read_registry(tmp_path)


def test_serial_collision_refuses_rather_than_merging(tmp_path):
    """Cloned handsets ship duplicate serials and adb cannot tell them
    apart. Merging two devices' history into one record is worse than
    failing."""
    record_seen(tmp_path, _facts(fingerprint="goog/panther/1"))
    entry, diagnostics = record_seen(tmp_path, _facts(fingerprint="other/clone/9"))
    assert [d.id for d in diagnostics] == ["device.serial_collision"]
    assert read_registry(tmp_path)["1A2B"].facts.fingerprint == "goog/panther/1"


def test_a_corrupt_registry_reads_as_empty(tmp_path):
    from playground.devices.registry import registry_path

    path = registry_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert read_registry(tmp_path) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/devices/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'playground.devices.registry'`

- [ ] **Step 3: Implement**

```python
"""Every handset this machine has ever seen, keyed by serial.

Machine-owned and never hand-edited: this is the *facts* half of the
intent/facts split, the mirror of `config/devices/*.yaml`. It is what
lets `device list` show a phone that is currently unplugged, and what
makes a note survive a disconnect.

Layout follows `capture/state.py`: `<state_dir>/state/devices/`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from playground.devices.adb import DeviceFacts
from playground.models.base import StrictModel
from playground.models.diagnostic import Diagnostic, SourceLocation


class RegistryEntry(StrictModel):
    facts: DeviceFacts
    first_seen: str
    last_seen: str
    last_verified: str
    ignored: bool = False
    """Set when the operator answered "no" to the discovery prompt, so the
    same device is not offered again on every poll."""


def devices_dir(state_dir: Path) -> Path:
    return state_dir / "state" / "devices"


def registry_path(state_dir: Path) -> Path:
    return devices_dir(state_dir) / "registry.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read_registry(state_dir: Path) -> dict[str, RegistryEntry]:
    """The registry, or an empty mapping when there is none to read.

    A corrupt or truncated file reads as empty rather than raising, the
    same tolerance `capture.state.read_session` applies: a damaged cache
    of observations must never block a command.
    """
    path = registry_path(state_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {k: RegistryEntry.model_validate(v) for k, v in raw["devices"].items()}
    except Exception:  # noqa: BLE001 - any malformed shape is "no registry"
        return {}


def _write(state_dir: Path, entries: dict[str, RegistryEntry]) -> None:
    path = registry_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"devices": {k: v.model_dump() for k, v in entries.items()}}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def record_seen(
    state_dir: Path, facts: DeviceFacts
) -> tuple[RegistryEntry, list[Diagnostic]]:
    """Record a sighting, refusing to merge two handsets that share a serial."""
    entries = read_registry(state_dir)
    now = _now()
    existing = entries.get(facts.serial)

    if existing is not None and existing.facts.fingerprint and facts.fingerprint:
        if existing.facts.fingerprint != facts.fingerprint:
            return existing, [
                Diagnostic(
                    id="device.serial_collision",
                    severity="error",
                    message=(
                        f"serial {facts.serial!r} is already registered to a "
                        f"different handset (fingerprint "
                        f"{existing.facts.fingerprint!r} vs {facts.fingerprint!r})"
                    ),
                    source=SourceLocation(path=str(registry_path(state_dir))),
                    suggestion=(
                        "duplicate USB serials are common on cloned devices; "
                        "attach only one of them, or clear its registry entry"
                    ),
                )
            ]

    entry = RegistryEntry(
        facts=facts,
        first_seen=existing.first_seen if existing else now,
        last_seen=now,
        last_verified=now,
        ignored=existing.ignored if existing else False,
    )
    entries[facts.serial] = entry
    _write(state_dir, entries)
    return entry, []


def mark_ignored(state_dir: Path, serial: str) -> None:
    entries = read_registry(state_dir)
    entry = entries.get(serial)
    if entry is None:
        return
    entries[serial] = entry.model_copy(update={"ignored": True})
    _write(state_dir, entries)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/devices -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/playground/devices/registry.py tests/unit/devices/test_registry.py
git commit -m "feat(devices): keep a serial-keyed registry of known handsets"
```

---

### Task 10: Selector matching and slot bindings

**Files:**
- Create: `src/playground/devices/binding.py`
- Test: `tests/unit/devices/test_binding.py` (create)

**Interfaces:**
- Consumes: `AttachedDevice` (Task 8), `ResolvedDevice` (Task 5).
- Produces:
  - `Binding(slot: str, serial: str, bound_at: str)`
  - `binding_path(state_dir, lab, slot) -> Path`
  - `read_binding` / `write_binding` / `clear_binding`
  - `match_candidates(device: ResolvedDevice, attached: list[AttachedDevice]) -> list[AttachedDevice]`
  - `resolve_binding(state_dir, lab, device, attached, *, rebind: bool) -> tuple[str | None, list[Diagnostic]]`

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from playground.devices.adb import AttachedDevice
from playground.devices.binding import (
    match_candidates,
    resolve_binding,
    write_binding,
)
from playground.models.kinds import DeviceMatch
from playground.models.resolved import ResolvedDevice


def _device(**kw) -> ResolvedDevice:
    return ResolvedDevice(name="pixel7", kind="android", **kw)


def _attached(serial, model="Pixel 7") -> AttachedDevice:
    return AttachedDevice(serial=serial, state="connected", model=model)


def test_no_selector_and_one_device_binds_it(tmp_path):
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [_attached("S1")], rebind=False
    )
    assert diagnostics == []
    assert serial == "S1"


def test_no_selector_and_several_devices_is_ambiguous(tmp_path):
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [_attached("S1"), _attached("S2")], rebind=False
    )
    assert serial is None
    assert [d.id for d in diagnostics] == ["config.device.ambiguous_match"]
    assert "S1" in diagnostics[0].message and "S2" in diagnostics[0].message


def test_serial_selector_narrows(tmp_path):
    device = _device(match=DeviceMatch(serial="S2"))
    serial, diagnostics = resolve_binding(
        tmp_path, "l", device, [_attached("S1"), _attached("S2")], rebind=False
    )
    assert serial == "S2"


def test_model_selector_narrows():
    device = _device(match=DeviceMatch(model="Pixel 7"))
    attached = [_attached("S1", "Pixel 7"), _attached("S2", "Galaxy S22")]
    assert [d.serial for d in match_candidates(device, attached)] == ["S1"]


def test_selector_matching_nothing_is_an_error(tmp_path):
    device = _device(match=DeviceMatch(serial="NOPE"))
    serial, diagnostics = resolve_binding(
        tmp_path, "l", device, [_attached("S1")], rebind=False
    )
    assert serial is None
    assert [d.id for d in diagnostics] == ["config.device.no_match"]


def test_a_bound_serial_that_is_gone_reports_not_connected(tmp_path):
    write_binding(tmp_path, "l", "pixel7", "S1")
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [], rebind=False
    )
    assert serial is None
    assert [d.id for d in diagnostics] == ["device.not_connected"]


def test_a_different_device_matching_the_slot_refuses_to_rebind(tmp_path):
    """Installing an APK onto the wrong phone because a cable was swapped
    is exactly the failure that must not be silent."""
    write_binding(tmp_path, "l", "pixel7", "S1")
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [_attached("S2")], rebind=False
    )
    assert serial is None
    assert [d.id for d in diagnostics] == ["device.rebind_required"]
    assert "--rebind" in diagnostics[0].suggestion


def test_rebind_flag_allows_the_swap(tmp_path):
    write_binding(tmp_path, "l", "pixel7", "S1")
    serial, diagnostics = resolve_binding(
        tmp_path, "l", _device(), [_attached("S2")], rebind=True
    )
    assert diagnostics == []
    assert serial == "S2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/devices/test_binding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'playground.devices.binding'`

- [ ] **Step 3: Implement**

```python
"""Which physical handset fills a lab's device slot.

Two separate questions, and the split matters:

*Matching* asks which attached devices satisfy the slot's selector. It is
pure and testable with no filesystem.

*Binding* is the persisted answer -- slot name to serial, under
`<state_dir>/state/devices/<lab>/<slot>.json`, the same per-lab directory
shape `capture/state.py` uses. It exists so a serial never has to be
written into a committed lab file, exactly as a VM's IP never is.

Rebinding is never automatic. A bound slot whose serial has been replaced
by a different matching handset is an error, not a silent retarget.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from playground.devices.adb import AttachedDevice
from playground.models.base import StrictModel
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedDevice


class Binding(StrictModel):
    slot: str
    serial: str
    bound_at: str


def lab_devices_dir(state_dir: Path, lab: str) -> Path:
    return state_dir / "state" / "devices" / lab


def binding_path(state_dir: Path, lab: str, slot: str) -> Path:
    return lab_devices_dir(state_dir, lab) / f"{slot}.json"


def read_binding(state_dir: Path, lab: str, slot: str) -> Binding | None:
    path = binding_path(state_dir, lab, slot)
    if not path.is_file():
        return None
    try:
        return Binding.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a damaged binding is "not bound"
        return None


def write_binding(state_dir: Path, lab: str, slot: str, serial: str) -> Binding:
    binding = Binding(
        slot=slot, serial=serial,
        bound_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    path = binding_path(state_dir, lab, slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(binding.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return binding


def clear_binding(state_dir: Path, lab: str, slot: str) -> None:
    binding_path(state_dir, lab, slot).unlink(missing_ok=True)


def match_candidates(
    device: ResolvedDevice, attached: list[AttachedDevice]
) -> list[AttachedDevice]:
    """Attached devices satisfying every field the selector actually sets.

    An absent selector matches everything -- that is what makes a
    single-handset lab need no serial in its YAML at all.
    """
    match = device.match
    if match is None:
        return list(attached)
    out = []
    for candidate in attached:
        if match.serial is not None and candidate.serial != match.serial:
            continue
        if match.model is not None and candidate.model != match.model:
            continue
        if match.manufacturer is not None and candidate.manufacturer != match.manufacturer:
            continue
        out.append(candidate)
    return out


def _source(lab: str) -> SourceLocation:
    return SourceLocation(path=f"config/labs/{lab}.yaml")


def resolve_binding(
    state_dir: Path,
    lab: str,
    device: ResolvedDevice,
    attached: list[AttachedDevice],
    *,
    rebind: bool,
) -> tuple[str | None, list[Diagnostic]]:
    """The serial this slot should act on, or the reason it cannot."""
    candidates = match_candidates(device, attached)
    existing = read_binding(state_dir, lab, device.name)

    if existing is not None:
        if any(c.serial == existing.serial for c in candidates):
            return existing.serial, []
        if not candidates:
            return None, [
                Diagnostic(
                    id="device.not_connected",
                    severity="error",
                    message=(
                        f"device {device.name!r} is bound to serial "
                        f"{existing.serial!r}, which is not attached"
                    ),
                    source=_source(lab),
                    suggestion="plug the handset in, or pass --rebind to bind another",
                )
            ]
        if not rebind:
            found = ", ".join(c.serial for c in candidates)
            return None, [
                Diagnostic(
                    id="device.rebind_required",
                    severity="error",
                    message=(
                        f"device {device.name!r} is bound to serial "
                        f"{existing.serial!r}, but a different handset matches "
                        f"its selector ({found})"
                    ),
                    source=_source(lab),
                    suggestion=(
                        "pass --rebind to point this slot at the attached "
                        "handset; playground never retargets a slot silently"
                    ),
                )
            ]

    if not candidates:
        return None, [
            Diagnostic(
                id="config.device.no_match",
                severity="error",
                message=(
                    f"no attached handset matches device {device.name!r}"
                ),
                source=_source(lab),
                key_path="spec.devices",
                suggestion="run `playground device list` to see what is attached",
            )
        ]
    if len(candidates) > 1:
        found = ", ".join(f"{c.serial} ({c.model or 'unknown model'})" for c in candidates)
        return None, [
            Diagnostic(
                id="config.device.ambiguous_match",
                severity="error",
                message=(
                    f"device {device.name!r} matches {len(candidates)} attached "
                    f"handsets: {found}"
                ),
                source=_source(lab),
                key_path="spec.devices",
                suggestion="narrow spec.devices[].match, e.g. with a serial",
            )
        ]

    write_binding(state_dir, lab, device.name, candidates[0].serial)
    return candidates[0].serial, []
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/devices -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/playground/devices/binding.py tests/unit/devices/test_binding.py
git commit -m "feat(devices): match selectors to handsets and persist slot bindings"
```

---

### Task 11: Notes — shared by default, private on request

**Files:**
- Create: `src/playground/devices/notes.py`
- Test: `tests/unit/devices/test_notes.py` (create)

**Interfaces:**
- Produces:
  - `NoteScope = Literal["shared", "private"]`
  - `note_path(scope, *, config_dir, state_dir, name=None, serial=None) -> Path`
  - `read_note(...) -> str | None`
  - `write_note(...) -> tuple[Path, list[Diagnostic]]`
  - `looks_like_credentials(text: str) -> bool`

**Behaviour, per the operator decision of 2026-09-08:** `shared` is the default scope. `--private` selects the gitignored, serial-keyed surface.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/devices/test_notes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'playground.devices.notes'`

- [ ] **Step 3: Implement**

```python
"""Operator notes about a handset, on two surfaces.

`shared` (the default) is `config/devices/<name>.md` -- committed, keyed
by profile name, portable to whoever else works the lab.

`private` is `<state_dir>/state/devices/<serial>/notes.md` -- gitignored,
keyed by serial, needing no profile at all.

NEITHER IS A SECRET STORE. Both are plaintext on disk; `private` only
keeps the file out of git and off a teammate's machine. Nothing here may
be documented as protecting note contents.

The credential heuristic is advisory. It warns on the committed surface
and never blocks: the feature's motivating example ("this phone has
Telegram with account X and password Y") is itself credential-shaped, so
a refusing default would reject the use case the feature exists to serve.
Substring matching does not reliably detect secrets and must never be
presented as a safety guarantee.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from playground.models.diagnostic import Diagnostic, SourceLocation

NoteScope = Literal["shared", "private"]

_CREDENTIAL_HINTS = re.compile(
    r"\b(pass(word|phrase)|secret|token|api[_-]?key|credential)\b", re.IGNORECASE
)


def looks_like_credentials(text: str) -> bool:
    """Advisory only. See the module docstring."""
    return _CREDENTIAL_HINTS.search(text) is not None


def note_path(
    scope: NoteScope,
    *,
    config_dir: Path,
    state_dir: Path,
    name: str | None,
    serial: str | None,
) -> Path:
    if scope == "private":
        if serial is None:
            raise ValueError("the private note surface is keyed by serial")
        return state_dir / "state" / "devices" / serial / "notes.md"
    if name is None:
        raise ValueError("the shared note surface is keyed by profile name")
    return config_dir / "devices" / f"{name}.md"


def read_note(
    scope: NoteScope,
    *,
    config_dir: Path,
    state_dir: Path,
    name: str | None,
    serial: str | None,
) -> str | None:
    path = note_path(
        scope, config_dir=config_dir, state_dir=state_dir, name=name, serial=serial
    )
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def write_note(
    body: str,
    *,
    scope: NoteScope,
    config_dir: Path,
    state_dir: Path,
    name: str | None,
    serial: str | None,
) -> tuple[Path, list[Diagnostic]]:
    path = note_path(
        scope, config_dir=config_dir, state_dir=state_dir, name=name, serial=serial
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")

    diagnostics: list[Diagnostic] = []
    if scope == "shared" and looks_like_credentials(body):
        diagnostics.append(
            Diagnostic(
                id="device.note_credential_warning",
                severity="warning",
                message=(
                    f"note written to {path} looks like it contains a "
                    "credential, and that file is committed to git"
                ),
                source=SourceLocation(path=str(path)),
                suggestion=(
                    "use `--private` to keep it in .playground/ instead; note "
                    "that neither surface encrypts anything"
                ),
            )
        )
    return path, diagnostics
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/devices -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/playground/devices/notes.py tests/unit/devices/test_notes.py
git commit -m "feat(devices): add shared and private note surfaces"
```

---

## Phase D — CLI, discovery, adoption

### Task 12: Discovery policy

**Files:**
- Create: `src/playground/devices/discovery.py`
- Modify: `src/playground/models/kinds.py` (`DefaultsSpec.device_discovery`, `LabSpec.device_discovery`), `src/playground/models/resolved.py` (`ResolvedLab.device_discovery`), `src/playground/config/resolver.py`
- Test: `tests/unit/devices/test_discovery.py` (create)

**Interfaces:**
- Produces:
  - `DiscoveryMode = Literal["prompt", "silent"]`
  - `effective_mode(configured: DiscoveryMode, *, is_tty: bool, no_prompt: bool, yes: bool) -> DiscoveryMode`
  - `offer_unknown(...)` returning the serials the operator chose to save

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from playground.devices.discovery import effective_mode


def test_prompt_is_the_configured_default():
    assert effective_mode("prompt", is_tty=True, no_prompt=False, yes=False) == "prompt"


def test_non_tty_forces_silent():
    """JSON output, CI and the TUI must never block on stdin."""
    assert effective_mode("prompt", is_tty=False, no_prompt=False, yes=False) == "silent"


def test_no_prompt_forces_silent():
    assert effective_mode("prompt", is_tty=True, no_prompt=True, yes=False) == "silent"


def test_yes_also_skips_the_question():
    assert effective_mode("prompt", is_tty=True, no_prompt=False, yes=True) == "silent"


def test_silent_stays_silent_on_a_tty():
    assert effective_mode("silent", is_tty=True, no_prompt=False, yes=False) == "silent"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/devices/test_discovery.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
"""Whether an unknown attached handset raises a question or is filed silently.

The configured mode is `spec.device_discovery` -- deliberately NOT
`spec.devices.discovery`, because `LabSpec.devices` is a list and a
same-named mapping on the `Defaults` kind would make two config kinds
disagree about one key's shape.
"""

from __future__ import annotations

from typing import Literal

DiscoveryMode = Literal["prompt", "silent"]


def effective_mode(
    configured: DiscoveryMode, *, is_tty: bool, no_prompt: bool, yes: bool
) -> DiscoveryMode:
    """Resolve the configured mode against this invocation's reality.

    A non-TTY session can never prompt, whatever the config says: JSON
    output, CI and the TUI must not block on stdin.
    """
    if not is_tty or no_prompt or yes:
        return "silent"
    return configured
```

Add to `DefaultsSpec`: `device_discovery: DiscoveryMode = "prompt"`. Add to `LabSpec`: `device_discovery: DiscoveryMode | None = None`. Resolve as lab-then-defaults into `ResolvedLab.device_discovery: DiscoveryMode`.

- [ ] **Step 4: Add the prompt itself**

`effective_mode` decides *whether* to ask; this is the asking. The
confirm/ask callables are injected so the behaviour is testable with no
terminal.

Append to `tests/unit/devices/test_discovery.py`:

```python
from playground.devices.adb import AttachedDevice
from playground.devices.discovery import offer_unknown


def _attached(serial="S1"):
    return AttachedDevice(serial=serial, state="connected", model="Pixel 7")


def test_silent_mode_never_asks(tmp_path):
    asked = []
    saved = offer_unknown(
        tmp_path, [_attached()], mode="silent",
        ask=lambda d: asked.append(d) or "x",
    )
    assert saved == {}
    assert asked == []


def test_prompt_mode_saves_what_the_operator_names(tmp_path):
    saved = offer_unknown(
        tmp_path, [_attached()], mode="prompt", ask=lambda d: "my-pixel",
    )
    assert saved == {"S1": "my-pixel"}


def test_declining_marks_the_device_ignored_so_it_is_not_re_asked(tmp_path):
    from playground.devices.adb import DeviceFacts
    from playground.devices.registry import read_registry, record_seen

    record_seen(tmp_path, DeviceFacts(serial="S1"))
    offer_unknown(tmp_path, [_attached()], mode="prompt", ask=lambda d: None)
    assert read_registry(tmp_path)["S1"].ignored is True


def test_an_ignored_device_is_not_offered_again(tmp_path):
    from playground.devices.adb import DeviceFacts
    from playground.devices.registry import mark_ignored, record_seen

    record_seen(tmp_path, DeviceFacts(serial="S1"))
    mark_ignored(tmp_path, "S1")
    asked = []
    offer_unknown(
        tmp_path, [_attached()], mode="prompt",
        ask=lambda d: asked.append(d) or None,
    )
    assert asked == []
```

Implement:

```python
from collections.abc import Callable

from playground.devices.adb import AttachedDevice
from playground.devices.registry import mark_ignored, read_registry


def offer_unknown(
    state_dir: Path,
    attached: list[AttachedDevice],
    *,
    mode: DiscoveryMode,
    ask: Callable[[AttachedDevice], str | None],
) -> dict[str, str]:
    """Offer to save a profile for each attached device we do not know.

    Returns serial -> chosen profile name for the ones the operator named.
    A declined device is marked ignored so the same question is not asked
    on every subsequent adb poll. ``ask`` is injected so this is testable
    without a terminal; the CLI passes a `typer.prompt` wrapper.
    """
    if mode == "silent":
        return {}
    known = read_registry(state_dir)
    chosen: dict[str, str] = {}
    for device in attached:
        entry = known.get(device.serial)
        if entry is not None and entry.ignored:
            continue
        name = ask(device)
        if name:
            chosen[device.serial] = name
        else:
            mark_ignored(state_dir, device.serial)
    return chosen
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/devices tests/unit/config -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/playground/devices/discovery.py src/playground/models src/playground/config tests/unit
git commit -m "feat(devices): resolve prompt/silent discovery, forcing silent off a TTY"
```

---

### Task 13: Adoption and release

**Files:**
- Create: `src/playground/devices/adopt.py`
- Test: `tests/unit/devices/test_adopt.py` (create)

**Interfaces:**
- Consumes: Tasks 8–12.
- Produces:
  - `adopt(state_dir, config_dir, lab, resolved, attached, *, rebind=False) -> tuple[list[DeviceStatus], list[Diagnostic]]`
  - `release(state_dir, lab, resolved) -> list[Diagnostic]`
  - `check_requirements(device, facts, note) -> list[Diagnostic]`

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from playground.devices.adb import AttachedDevice, DeviceFacts
from playground.devices.adopt import adopt, check_requirements
from playground.models.kinds import DeviceRequire
from playground.models.resolved import ResolvedDevice


def _device(**kw):
    return ResolvedDevice(name="pixel7", kind="android", **kw)


def test_unauthorized_gets_its_own_diagnostic(tmp_path, monkeypatch):
    attached = [AttachedDevice(serial="S1", state="unauthorized")]
    statuses, diagnostics = adopt(
        tmp_path / "s", tmp_path / "c", "l", [_device()], attached
    )
    assert statuses[0].state == "unauthorized"
    assert [d.id for d in diagnostics] == ["device.unauthorized"]
    assert "Allow" in diagnostics[0].suggestion


def test_a_missing_optional_device_is_not_an_error(tmp_path):
    statuses, diagnostics = adopt(
        tmp_path / "s", tmp_path / "c", "l", [_device(required=False)], []
    )
    assert statuses[0].state == "missing"
    assert [d.severity for d in diagnostics] == ["warning"]


def test_a_missing_required_device_is_an_error(tmp_path):
    _, diagnostics = adopt(tmp_path / "s", tmp_path / "c", "l", [_device()], [])
    assert diagnostics and diagnostics[0].severity == "error"


def test_require_root_fails_on_an_unrooted_handset():
    device = _device(require=DeviceRequire(root=True))
    facts = DeviceFacts(serial="S1", rooted=False, sdk=34)
    diagnostics = check_requirements(device, facts, note=None)
    assert [d.id for d in diagnostics] == ["device.require_unmet"]


def test_require_failure_prints_the_operator_note():
    """The note earning its keep: the context you would otherwise hunt for,
    printed at the moment the assertion fails."""
    device = _device(require=DeviceRequire(root=True))
    facts = DeviceFacts(serial="S1", rooted=False)
    note = "root broken since the September OTA, needs Magisk re-flash"
    diagnostics = check_requirements(device, facts, note=note)
    assert note in diagnostics[0].message


def test_min_sdk_is_checked():
    device = _device(require=DeviceRequire(min_sdk=34))
    facts = DeviceFacts(serial="S1", sdk=30, rooted=True)
    assert [d.id for d in check_requirements(device, facts, note=None)] == [
        "device.require_unmet"
    ]


def test_satisfied_requirements_produce_nothing():
    device = _device(require=DeviceRequire(root=True, min_sdk=30))
    facts = DeviceFacts(serial="S1", sdk=34, rooted=True)
    assert check_requirements(device, facts, note=None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/devices/test_adopt.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
"""Adopt and release physical device slots.

Adoption is STRICTLY ADDITIVE. It resolves a selector, asserts the
handset is trusted, probes what it is, checks the lab's assertions and
records the binding. It installs nothing the lab did not declare, changes
no setting, and never wipes anything.

Release forgets the binding. It uninstalls only packages playground
itself installed, and never factory-resets or reverts a setting
playground did not set.
"""

from __future__ import annotations

from pathlib import Path

from playground.devices.adb import AttachedDevice, DeviceFacts, probe_facts
from playground.devices.binding import clear_binding, resolve_binding
from playground.devices.registry import record_seen
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedDevice
from playground.models.status import DeviceStatus


def check_requirements(
    device: ResolvedDevice, facts: DeviceFacts, *, note: str | None = None
) -> list[Diagnostic]:
    """Verify the lab's `require` assertions against the live handset.

    On failure the operator's own note is appended to the message. That is
    the whole reason notes exist: "root broken since the September OTA" is
    exactly the context otherwise gone hunting for.
    """
    require = device.require
    if require is None:
        return []
    unmet: list[str] = []
    if require.root is True and not facts.rooted:
        unmet.append("root: true (handset is not rooted)")
    if require.min_sdk is not None and (facts.sdk is None or facts.sdk < require.min_sdk):
        unmet.append(f"min_sdk: {require.min_sdk} (handset reports {facts.sdk})")
    if not unmet:
        return []
    message = f"device {device.name!r} does not satisfy: {'; '.join(unmet)}"
    if note:
        message = f"{message}\nnote: {note.strip()}"
    return [
        Diagnostic(
            id="device.require_unmet",
            severity="error",
            message=message,
            source=SourceLocation(path="device"),
            key_path="spec.devices[].require",
            suggestion="relax spec.devices[].require, or attach a handset that satisfies it",
        )
    ]
```

`adopt` walks each `ResolvedDevice`: resolve the binding; on `unauthorized` emit `device.unauthorized` with the suggestion `"tap Allow on the handset's RSA prompt, then re-run"`; on absence emit an error for `required: true` and a warning otherwise, with `state="missing"`; otherwise `probe_facts`, `record_seen`, `check_requirements`, and build a `DeviceStatus`. `release` calls `clear_binding` per slot.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/devices -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/playground/devices/adopt.py tests/unit/devices/test_adopt.py
git commit -m "feat(devices): adopt handsets additively and release them cleanly"
```

---

### Task 14: The `playground device` verb group

**Files:**
- Create: `src/playground/cli/device_commands.py`
- Modify: `src/playground/cli/main.py` (register `device_app`)
- Test: `tests/cli/test_device.py` (create)

**Interfaces:**
- Produces the CLI: `device list`, `device save`, `device show`, `device note`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cli/test_device.py -v`
Expected: FAIL — no `device` command group.

- [ ] **Step 3: Implement the verb group**

`device list` merges `list_attached()` with `read_registry()`: attached-and-known, attached-and-new, known-but-absent. Print serial, model, SDK, root, state, the first line of any note, and **the note's age derived from `last_verified`**.

The age is the spec's mitigation for the one failure playground cannot detect: a factory reset keeps the serial and keeps matching the profile, while the note now describes an account that no longer exists on the handset. Nothing can spot that, so the display says how stale the claim is instead of pretending to validate it.

An `unauthorized` row prints the remedy inline.

`device save <serial> --as <name>` writes `config/devices/<name>.yaml` with `match.serial`.

`device show <device>` prints registry facts plus both note bodies, rendered.

`device note <device>` implements the API from the spec:

```python
@device_app.command("note", help="Read or write an operator note about a handset.")
def note_command(
    device: Annotated[str, typer.Argument(help="Profile name or raw serial.")],
    append: Annotated[str | None, typer.Option("--append")] = None,
    set_: Annotated[bool, typer.Option("--set", help="Read markdown from stdin.")] = False,
    show: Annotated[bool, typer.Option("--show")] = False,
    clear: Annotated[bool, typer.Option("--clear")] = False,
    as_name: Annotated[str | None, typer.Option("--as", help="Profile to create.")] = None,
    private: Annotated[bool, typer.Option("--private")] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
    config_dir: Annotated[Path, typer.Option("--config-dir", "-c")] = Path("config"),
    state_dir: Annotated[Path, typer.Option("--state-dir")] = Path(".playground"),
) -> None:
    ...
```

Scope is `"private"` when `--private`, else `"shared"`. For the shared scope the profile name is `device` if a profile of that name exists, else `as_name`; when neither resolves and the session is not a TTY, fail with:

```python
Diagnostic(
    id="config.device.note_name_required",
    severity="error",
    message=(
        f"{device!r} has no DeviceProfile, so there is no shared note to "
        "write; name one with --as <name>"
    ),
    source=SourceLocation(path=str(config_dir / "devices")),
    suggestion="or use --private, which keys on serial and needs no profile",
)
```

When `as_name` is given and the profile is absent, create `config/devices/<name>.yaml` first — writing a note is the explicit human action that authorises a `config/` write, the third and last such path. Print every diagnostic `write_note` returns; a warning does not change the exit code.

Register in `main.py`: `app.add_typer(device_app, name="device")`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/cli -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/playground/cli tests/cli/test_device.py
git commit -m "feat(cli): add the device verb group with the note API"
```

---

### Task 15: Devices as targets for `app` / `adb` / `shell`

**Files:**
- Modify: `src/playground/android/targets.py`, `src/playground/capture/targets.py`, `src/playground/cli/app_commands.py` (`_resolve`), `src/playground/cli/main.py` (`adb_command`)
- Test: `tests/unit/android/test_targets_devices.py` (create)

**Interfaces:**
- Consumes: `LabStatus.devices` (Task 7), `LocalAdb` (Task 2).
- Produces: `resolve_targets` returning `AndroidTarget`s for both VMs and devices.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from playground.android.targets import resolve_targets
from playground.android.transport import GuestAdb, LocalAdb
from playground.models.status import DeviceStatus, LabStatus


def test_a_connected_device_becomes_a_local_target(resolved_lab_with_device):
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=0, provisioned_vms=0,
        devices=[DeviceStatus(name="pixel7", state="connected", serial="S1")],
    )
    targets, diagnostics = resolve_targets(
        resolved_lab_with_device, status, on="pixel7", role=None,
        all_devices=False, user="ubuntu",
    )
    assert diagnostics == []
    assert targets[0].transport == LocalAdb("S1")


def test_an_unauthorized_device_is_not_a_target(resolved_lab_with_device):
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=0, provisioned_vms=0,
        devices=[DeviceStatus(name="pixel7", state="unauthorized", serial="S1")],
    )
    targets, diagnostics = resolve_targets(
        resolved_lab_with_device, status, on="pixel7", role=None,
        all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["device.unauthorized"]


def test_all_spans_emulators_and_handsets(resolved_mixed_lab):
    status = LabStatus(
        lab="l", backend="local-libvirt", expected_vms=1, provisioned_vms=1,
        vms=[{"name": "droid1", "role": "redroid-host", "state": "provisioned",
              "ssh_host": "10.0.0.5", "ssh_port": 22}],
        devices=[DeviceStatus(name="pixel7", state="connected", serial="S1")],
    )
    targets, _ = resolve_targets(
        resolved_mixed_lab, status, on=None, role=None, all_devices=True, user="ubuntu",
    )
    kinds = {type(t.transport) for t in targets}
    assert kinds == {GuestAdb, LocalAdb}
```

Add the two fixtures to `tests/unit/android/conftest.py`, building `ResolvedLab`s with a `redroid-host` VM and/or a `pixel7` device.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/android/test_targets_devices.py -v`
Expected: FAIL — `resolve_targets` ignores `resolved.devices`.

- [ ] **Step 3: Implement**

Extend `android_vm_names` into `android_target_names(resolved)` returning VM names (`capabilities["redroid"]`) followed by `resolved.devices` names, in lab order.

When `--on` names something that is neither, the existing branch raises `config.app.unknown_vm`. Split it: a name absent from both lists still raises `config.app.unknown_vm` when the lab has no devices, and raises `config.device.unknown_device` (listing both VMs and device slots) when it does — otherwise the message tells an operator with a handset to look for a VM. In the per-name loop, a name that is a device looks up `status.devices`:

```python
        device_status = devices_by_name.get(name)
        if device_status is not None:
            if device_status.state == "unauthorized":
                diagnostics.append(
                    Diagnostic(
                        id="device.unauthorized",
                        severity="error",
                        message=(
                            f"device {name!r} (serial {device_status.serial}) has not "
                            "accepted this machine's adb key"
                        ),
                        source=source,
                        suggestion="tap Allow on the handset's RSA prompt, then re-run",
                    )
                )
                continue
            if device_status.state != "connected" or not device_status.serial:
                diagnostics.append(
                    Diagnostic(
                        id="device.not_connected",
                        severity="error",
                        message=f"device {name!r} is not attached",
                        source=source,
                        suggestion="plug the handset in and re-run `playground apply`",
                    )
                )
                continue
            targets.append(
                AndroidTarget(name=name, transport=LocalAdb(device_status.serial))
            )
            continue
```

In `capture/targets.py`, **delete `_CAPTURE_BACKENDS` and its guard.** Capturability is a property of the target, not of `spec.backend`; a handset in a `local-vbox` lab would be wrongly excluded by the allowlist. Replace with a per-target capability check: VMs keep `capabilities.get("capture")`, and devices are excluded in this plan with `config.capture.device_unsupported` ("capture from a physical handset ships in Plan 2") so the verb fails honestly rather than silently doing nothing.

In `main.py`'s `adb_command`, a device target needs no tunnel — adb is already local. Print `adb -s <serial>` as the connection line and exit 0 instead of blocking on an ssh forward.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit tests/cli -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/playground/android src/playground/capture src/playground/cli tests
git commit -m "feat(android): target physical handsets from the existing app verbs"
```

---

### Task 16: Adoption in `apply`, release in `destroy`, and doctor checks

**Files:**
- Modify: `src/playground/cli/main.py` (`apply_command`, `destroy_command`, `status_command`), `src/playground/preflight/doctor.py`
- Test: `tests/cli/test_apply_devices.py`, `tests/unit/preflight/test_doctor_devices.py` (create)

**Interfaces:**
- Consumes: `adopt` / `release` (Task 13), `list_attached` (Task 8).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/preflight/test_doctor_devices.py
from __future__ import annotations

from playground.preflight.doctor import check_adb_available


def test_missing_adb_is_a_warning_not_a_hard_error(monkeypatch):
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: None)
    result = check_adb_available()
    assert result.severity == "warning"
    assert "android-platform-tools" in result.suggestion


def test_present_adb_passes(monkeypatch):
    monkeypatch.setattr("playground.preflight.doctor.shutil.which", lambda _: "/usr/bin/adb")
    assert check_adb_available() is None or check_adb_available().severity != "error"
```

```python
# tests/cli/test_apply_devices.py
def test_apply_adopts_declared_devices(tmp_path, monkeypatch):
    """A lab with a device slot runs adoption; a required handset that is
    absent fails the apply."""
    calls: list[str] = []
    monkeypatch.setattr(
        "playground.cli.main.adopt",
        lambda *a, **k: (calls.append("adopt"), ([], []))[1],
    )
    ...  # invoke apply against a fixture lab declaring one device
    assert calls == ["adopt"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/preflight/test_doctor_devices.py tests/cli/test_apply_devices.py -v`
Expected: FAIL with `ImportError: cannot import name 'check_adb_available'`

- [ ] **Step 3: Implement**

In `doctor.py`, following the module's existing `shutil.which` pattern:

```python
def check_adb_available() -> Diagnostic | None:
    """`adb` on PATH -- required only for labs declaring `spec.devices`.

    A warning rather than an error: a lab with no handset never touches
    adb on the controller, and the Redroid path runs adb inside the guest.
    """
    if shutil.which("adb"):
        return None
    return Diagnostic(
        id="preflight.adb.missing",
        severity="warning",
        message="`adb` is not on PATH; required only for labs with spec.devices",
        source=SourceLocation(path="PATH"),
        suggestion="install android-platform-tools (Debian/Ubuntu: apt install adb)",
    )


def check_attached_devices() -> list[Diagnostic]:
    """Report every attached handset, naming the remedy for unauthorized ones."""
    if not shutil.which("adb"):
        return []
    attached, diagnostics = list_attached()
    for device in attached:
        if device.state == "unauthorized":
            diagnostics.append(
                Diagnostic(
                    id="preflight.adb.unauthorized",
                    severity="warning",
                    message=f"handset {device.serial} has not accepted this machine's adb key",
                    source=SourceLocation(path="adb"),
                    suggestion="tap Allow on the handset's RSA prompt",
                )
            )
    return diagnostics
```

In `apply_command`, after the backend apply succeeds and before the summary, call `adopt(...)` when `resolved.devices` is non-empty, print returned diagnostics, and fail the apply on any error. In `destroy_command`, call `release(...)` for the lab's device slots. In `status_command`, populate `LabStatus.devices` and render a device section in `_render_status_human`.

- [ ] **Step 4: Run the full suite**

Run: `pytest -q && ruff check src tests`
Expected: PASS, no lint findings.

- [ ] **Step 5: Commit**

```bash
git add src/playground tests
git commit -m "feat(cli): adopt devices during apply, release on destroy, check adb in doctor"
```

---

## Verification

Run before declaring Plan 1 done:

```bash
pytest -q
ruff check src tests
mypy src/playground
```

Manual smoke test with a real handset (the only part CI cannot cover):

```bash
playground device list                                  # discover the serial
playground device note <serial> --as my-pixel --append "test handset"
playground apply <lab>                                  # adopts, probes, binds
playground app list --lab <lab> --on pixel7             # packages over USB
playground app install --lab <lab> --on pixel7 ./x.apk  # stages locally, no scp
playground adb --lab <lab> --on pixel7                  # prints `adb -s <serial>`
playground destroy <lab>                                # releases, never wipes
```

Then unplug the handset, replug it, and re-run `playground app list` — it must succeed without re-adoption, and `playground device list` must show the note between the two.
