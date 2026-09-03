# Android App Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install, launch, and act on Android applications running on Redroid VMs, from both a declarative lab workload and an interactive CLI.

**Architecture:** `adb` runs ON the VM and is driven over SSH, so one mechanism serves both layers and nothing is required on the operator's machine. A declarative `type: android_app` workload covers reproducible labs; a `playground app` verb group covers interactive work with fleet fan-out.

**Tech Stack:** Python 3.12 (Typer, Pydantic v2, pytest), Ansible (`community.docker`, `community.general`, `ansible.posix`), ADB, Redroid.

**Spec:** `docs/superpowers/specs/2026-08-31-android-app-lifecycle-design.md`

**Research:** `docs/research/android-emulation/` — especially `app-install-launch.md` (split APKs, launch discovery) and `recommended-stack.md`.

## Global Constraints

- Python >= 3.12. Ruff `line-length = 100`, rules `["E","F","I","B","UP","W"]`. Mypy `strict = true`.
- Tests: `uv run pytest`. `testpaths = ["tests"]`, `pythonpath = ["src"]`.
- Diagnostic IDs: `config.<area>.<condition>` for config faults, `runtime.<subsystem>.<reason>` for execution faults.
- `Diagnostic.severity` is `Literal["error","warning","info"]`.
- Principle 7: idempotency is mandatory — a second `apply` on a converged host reports `changed=0`.
- Principle 10: warn clearly; block only hard errors.
- Ansible: `become` is set at PLAY level in `site.yml`, never in a role. No `handlers/` in this repo — use `register:` + `when: x.changed`. No `retries:`/`until:` idiom exists; do not introduce one.
- ADB has NO authentication. Never expose 5555. All access is on-VM or through `playground adb`.
- Every adb invocation is prefixed `adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true` so it does not depend on a pre-existing adb server.

## Baseline: the test suite is ALREADY RED on `main`

`uv run pytest` reports **4 failed, 761 passed, 7 skipped**:

```
FAILED tests/cli/test_cli.py::test_validate_committed_config_succeeds
FAILED tests/unit/validation/test_validator.py::test_budget_exceeded_is_error_in_strict_mode
FAILED tests/unit/validation/test_validator.py::test_budget_inherits_from_defaults_when_lab_omits_it
FAILED tests/unit/validation/test_validator.py::test_budget_exceeded_warns_in_permissive_mode
```

Cause: three UNTRACKED lab files in `config/labs/` (`barak-deploy-cross-vm.yaml`, `barak-dev-fleet-cloud.yaml`, `barak-fleet-airgap.yaml`). Tests that load the real `config/` tree assert exact diagnostic counts; `tests/cli/test_cli.py:508` asserts the literal `"0 errors, 1 warnings"`.

**Do NOT delete those files** — operator's local work. **Do NOT fix the failures** — out of scope. Your work must leave the failure set UNCHANGED. To check a config-sensitive assertion in isolation:

```bash
STASH=/tmp/pg-untracked-labs && mkdir -p $STASH
mv config/labs/barak-deploy-cross-vm.yaml config/labs/barak-dev-fleet-cloud.yaml config/labs/barak-fleet-airgap.yaml $STASH/
uv run pytest -q tests/cli/test_cli.py tests/unit/validation/
mv $STASH/*.yaml config/labs/
```

Always restore, even on failure.

## Known facts (already verified — do not re-investigate)

- `adb` is in Ubuntu Noble `universe` (source `android-platform-tools 34.0.4-1build3`) and installs cleanly on the DigitalOcean `ubuntu-24-04-x64` image: a live probe ran `apt-get install -y docker.io adb` and got `Android Debug Bridge version 1.0.41`. **Spec open question 1 is closed.**
- The Redroid container publishes `5555:5555` on the VM, so `adb -s 127.0.0.1:5555` works locally on the guest.
- `redroid-host` resolves to capabilities `{'docker': True, 'compose': True, 'swarm': True, 'redroid': True}`.
- `stage_workload_files` skips ONLY `type == "container"`, and guards with `src.is_file()`. An `android_app` workload is therefore staged automatically — but a DIRECTORY of split APKs fails with `config.workload.source_missing`. Task 7 fixes this.
- `_pick_target_vm`'s `auto` branch is hardcoded to `capabilities.get("docker")`.
- There are **six** ssh/scp argv sites, not four. See Task 2.
- `tests/cli/` has no `__init__.py`, so `_write_ssh_shim` / `_write_apply_shims` are duplicated verbatim between `test_cli.py` and `test_adb.py`. Task 2 de-duplicates via `conftest.py` rather than adding a third copy.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `ansible/roles/redroid/{defaults,tasks}/main.yml` | install `adb` on the guest | 1 |
| `src/playground/ssh/__init__.py`, `src/playground/ssh/argv.py` | the one ssh/scp argv builder | 2 |
| `tests/cli/conftest.py` | shared shim fixtures (replaces two duplicated copies) | 2 |
| `src/playground/android/targets.py` | resolve `--on/--role/--all` to Android devices | 3 |
| `src/playground/android/commands.py` | pure adb-command builders | 3 |
| `src/playground/android/runner.py` | parallel fan-out + result aggregation | 3 |
| `src/playground/cli/app_commands.py` | the `playground app` Typer sub-app | 4 |
| `src/playground/models/kinds.py`, `resolved.py` | `android_app` type + `AndroidAppOptions` | 5 |
| `src/playground/validation/validator.py` | three new diagnostics | 6 |
| `src/playground/planner/scheduling.py` | capability-aware auto + directory staging | 7 |
| `ansible/roles/workload_android_app/` | declarative install/launch role | 8 |
| `src/playground/backend/local_libvirt/verify.py` | package-installed check | 9 |

New CLI code goes in `src/playground/cli/app_commands.py`, NOT in `main.py`. `main.py` is already ~1900 lines; adding eleven subcommands to it makes it worse. Import the sub-app into `main.py` and register it there.

## Phases

**Phase 1 (Tasks 1-4)** delivers a working `playground app` against any applied Redroid lab. It ships on its own.

**Phase 2 (Tasks 5-9)** adds the declarative workload and verify integration.

**Task 10** is live validation and requires both phases.

---

### Task 1: Install `adb` on the guest

**Files:**
- Modify: `ansible/roles/redroid/defaults/main.yml`
- Modify: `ansible/roles/redroid/tasks/main.yml`
- Test: `tests/unit/ansible/test_redroid_adb.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: role variable `redroid_install_adb` (bool, default `true`). Guest has `adb` on PATH. Tasks 4 and 8 depend on this.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/ansible/test_redroid_adb.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/ansible/test_redroid_adb.py -v`
Expected: FAIL — `KeyError: 'redroid_install_adb'`.

- [ ] **Step 3: Add the default**

Append to `ansible/roles/redroid/defaults/main.yml`:

```yaml

# The app-lifecycle layer runs adb ON the guest (never on the operator's
# machine and never over an exposed 5555), so the client must be present.
# Ubuntu Noble ships it in universe as `adb`. Set false for air-gapped
# hosts whose package source lacks it.
redroid_install_adb: true
```

- [ ] **Step 4: Add the install task**

In `ansible/roles/redroid/tasks/main.yml`, insert immediately BEFORE the
`- name: Pull redroid image` task:

```yaml
- name: Install the adb client on the guest
  ansible.builtin.apt:
    name: adb
    state: present
    update_cache: yes
    cache_valid_time: 3600
  when: redroid_install_adb | bool
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/unit/ansible/ -v`
Expected: all pass (the pre-existing 4 skips in `test_workload_from_json_guard.py` are normal — `jinja2` is not installed).

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/redroid tests/unit/ansible/test_redroid_adb.py
git commit -m "redroid: install the adb client on the guest

Both halves of the app-lifecycle layer drive adb ON the VM over SSH, so the
guest needs the client. Ubuntu Noble ships it in universe; verified
installable on the DigitalOcean ubuntu-24-04-x64 image."
```

---

### Task 2: One shared ssh/scp argv builder

**Files:**
- Create: `src/playground/ssh/__init__.py`, `src/playground/ssh/argv.py`
- Create: `tests/unit/ssh/__init__.py`, `tests/unit/ssh/test_argv.py`
- Create: `tests/cli/conftest.py`
- Modify: `src/playground/cli/main.py` (three sites: `exec_command`, `adb_command`, `cp_command`)
- Modify: `src/playground/backend/local_libvirt/verify.py` (`_ssh`)
- Modify: `src/playground/backend/local_libvirt/wait.py` (`_ssh_probe`, `_wait_cloud_init`)
- Modify: `tests/cli/test_cli.py`, `tests/cli/test_adb.py` (drop duplicated shims)

**Interfaces:**
- Consumes: nothing.
- Produces:

```python
SSH_BASE_OPTS: tuple[str, ...]          # the 3 -o flags shared by all six sites
BATCH_OPTS: tuple[str, ...]             # BatchMode=yes + ConnectTimeout=10

def build_ssh_argv(
    host: str,
    *,
    user: str,
    port: int | None = None,
    command: str | None = None,
    batch: bool = False,
    local_forward: str | None = None,
    no_remote_command: bool = False,
) -> list[str]: ...

def build_scp_argv(
    src: str, dst: str, *, port: int | None = None, recursive: bool = False,
) -> list[str]: ...
```

Task 3 and Task 4 use `build_ssh_argv`.

**This is a behavior-preserving refactor.** Every existing test that asserts on ssh/scp argv must pass UNCHANGED. If you find yourself editing an assertion about argv content, you have changed behavior — stop and reconsider.

The six sites and their exact differences:

| Site | Extra opts | Port flag | Command | Subprocess mode |
|---|---|---|---|---|
| `main.py` `exec_command` | none | `-p` if port and != 22 | shlex-joined string | `check=False`, inherits stdio |
| `main.py` `adb_command` | none | `-p` if port and != 22 | none; adds `-N -L <fwd>` | `check=False`, inherits stdio |
| `main.py` `cp_command` | none | `-P` if port and != 22 | n/a (`scp`, two operands) | `check=False`, inherits stdio |
| `verify.py` `_ssh` | `BatchMode=yes`, `ConnectTimeout=10` | `-p` if != 22 | string | `capture_output=True`, `timeout=` |
| `wait.py` `_ssh_probe` | same | `-p` if != 22 | literal `true` | `capture_output=True`, `timeout=15.0` |
| `wait.py` `_wait_cloud_init` | same | `-p` if != 22 | literal `cloud-init status --wait` | `capture_output=True`, `timeout=` |

The helper builds ARGV only. It never calls `subprocess` — each caller keeps its own invocation and error handling, because those genuinely differ.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/ssh/__init__.py` (empty) and `tests/unit/ssh/test_argv.py`:

```python
"""One ssh/scp argv builder, shared by six call sites.

Before this module, `exec`, `adb`, `cp`, verify's `_ssh`, and wait's
`_ssh_probe` / `_wait_cloud_init` each hand-rolled the same option block.
"""

from __future__ import annotations

from playground.ssh.argv import (
    BATCH_OPTS,
    SSH_BASE_OPTS,
    build_scp_argv,
    build_ssh_argv,
)


def test_base_opts_are_the_three_shared_by_every_site() -> None:
    assert SSH_BASE_OPTS == (
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
    )


def test_batch_opts_are_unattended_only() -> None:
    assert BATCH_OPTS == ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")


def test_minimal_ssh_argv() -> None:
    assert build_ssh_argv("10.0.0.5", user="ubuntu") == [
        "ssh", *SSH_BASE_OPTS, "ubuntu@10.0.0.5",
    ]


def test_port_22_is_omitted() -> None:
    argv = build_ssh_argv("10.0.0.5", user="ubuntu", port=22)
    assert "-p" not in argv


def test_nonstandard_port_uses_lowercase_p_before_the_opts() -> None:
    argv = build_ssh_argv("127.0.0.1", user="ubuntu", port=2222)
    assert argv[:3] == ["ssh", "-p", "2222"]


def test_none_port_is_omitted() -> None:
    assert "-p" not in build_ssh_argv("h", user="u", port=None)


def test_command_is_the_tail_argument() -> None:
    argv = build_ssh_argv("h", user="u", command="uptime")
    assert argv[-1] == "uptime"
    assert argv[-2] == "u@h"


def test_batch_mode_appends_unattended_options() -> None:
    argv = build_ssh_argv("h", user="u", batch=True, command="true")
    assert "BatchMode=yes" in argv
    assert "ConnectTimeout=10" in argv


def test_local_forward_implies_no_remote_command() -> None:
    argv = build_ssh_argv(
        "h", user="u", local_forward="5555:127.0.0.1:5555", no_remote_command=True
    )
    assert "-N" in argv
    assert "-L" in argv
    assert argv[argv.index("-L") + 1] == "5555:127.0.0.1:5555"
    assert argv[-1] == "u@h", "a tunnel takes no remote command"


def test_scp_uses_capital_p_and_two_operands() -> None:
    argv = build_scp_argv("a.txt", "u@h:/tmp/a.txt", port=2222)
    assert argv[0] == "scp"
    assert argv[1:3] == ["-P", "2222"]
    assert argv[-2:] == ["a.txt", "u@h:/tmp/a.txt"]


def test_scp_recursive_flag() -> None:
    assert "-r" in build_scp_argv("d", "u@h:/tmp/d", recursive=True)


def test_scp_omits_port_22() -> None:
    assert "-P" not in build_scp_argv("a", "u@h:/a", port=22)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/ssh/test_argv.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'playground.ssh'`.

- [ ] **Step 3: Implement the module**

Create `src/playground/ssh/__init__.py`:

```python
"""Shared SSH/SCP command-line construction."""

from playground.ssh.argv import (
    BATCH_OPTS,
    SSH_BASE_OPTS,
    build_scp_argv,
    build_ssh_argv,
)

__all__ = ["BATCH_OPTS", "SSH_BASE_OPTS", "build_scp_argv", "build_ssh_argv"]
```

Create `src/playground/ssh/argv.py`:

```python
"""Build ssh / scp argv lists.

Six call sites used to hand-roll the same option block: the `exec`, `adb`,
and `cp` CLI verbs, verify's `_ssh`, and wait's `_ssh_probe` /
`_wait_cloud_init`. They agreed on three options and disagreed on
everything else, so this module builds ARGV only — each caller keeps its
own subprocess invocation, timeout policy, and error handling, because
those differences are real.

`UserKnownHostsFile=/dev/null` matters for local-vbox: it reuses
127.0.0.1:<port> across VM rebuilds, so a pinned known_hosts entry would
trip a host-key mismatch on every recreate.
"""

from __future__ import annotations

SSH_BASE_OPTS: tuple[str, ...] = (
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
)
"""Options every site shares. Interactive and unattended alike."""

BATCH_OPTS: tuple[str, ...] = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
"""Unattended-only. The three CLI verbs omit these because a human is
present; the backend probes set them so a hung auth prompt cannot stall a
pipeline step."""

DEFAULT_SSH_PORT = 22


def build_ssh_argv(
    host: str,
    *,
    user: str,
    port: int | None = None,
    command: str | None = None,
    batch: bool = False,
    local_forward: str | None = None,
    no_remote_command: bool = False,
) -> list[str]:
    """Return an ``ssh`` argv.

    ``port`` is emitted only when it is set and not 22, matching every
    existing call site (libvirt and cloud use 22; vbox NAT does not).
    ``local_forward`` is an ``-L`` spec such as ``5555:127.0.0.1:5555``;
    pair it with ``no_remote_command=True`` for a pure tunnel.
    """
    argv = ["ssh"]
    if port is not None and port != DEFAULT_SSH_PORT:
        argv += ["-p", str(port)]
    if no_remote_command:
        argv.append("-N")
    if local_forward is not None:
        argv += ["-L", local_forward]
    argv += list(SSH_BASE_OPTS)
    if batch:
        argv += list(BATCH_OPTS)
    argv.append(f"{user}@{host}")
    if command is not None:
        argv.append(command)
    return argv


def build_scp_argv(
    src: str,
    dst: str,
    *,
    port: int | None = None,
    recursive: bool = False,
) -> list[str]:
    """Return an ``scp`` argv.

    scp spells the port ``-P`` (capital) and takes two path operands
    instead of a host plus a remote command, which is why it does not go
    through :func:`build_ssh_argv`.
    """
    argv = ["scp"]
    if port is not None and port != DEFAULT_SSH_PORT:
        argv += ["-P", str(port)]
    if recursive:
        argv.append("-r")
    argv += list(SSH_BASE_OPTS)
    argv += [src, dst]
    return argv
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/ssh/test_argv.py -v`
Expected: 12 passed.

- [ ] **Step 5: Adopt it at all six sites**

Replace each hand-built list with a call. Keep every surrounding
`try/except FileNotFoundError`, `raise typer.Exit(...)`, timeout, and
`capture_output` exactly as it is.

`main.py` `exec_command` — replace the `ssh_argv = [...]` literal with:

```python
    ssh_argv = build_ssh_argv(
        ssh_host, user=user, port=ssh_port, command=remote_command
    )
```

`main.py` `adb_command` — replace its `ssh_argv = [...]` with:

```python
    ssh_argv = build_ssh_argv(
        ssh_host,
        user=user,
        port=ssh_port,
        local_forward=f"{chosen}:127.0.0.1:{ADB_REMOTE_PORT}",
        no_remote_command=True,
    )
```

`main.py` `cp_command` — replace its `scp_argv = [...]` with:

```python
    scp_argv = build_scp_argv(
        scp_src, scp_dst, port=ssh_port, recursive=recursive
    )
```

`verify.py` `_ssh` — replace its `cmd = [...]` with:

```python
    cmd = build_ssh_argv(
        target.ip,
        user=target.ssh_user,
        port=target.ssh_port,
        command=command,
        batch=True,
    )
```

`wait.py` `_ssh_probe` and `_wait_cloud_init` — same shape, `batch=True`,
with their existing literal commands (`"true"` and
`"cloud-init status --wait"`). Preserve each function's own port variable
and timeout.

Add `from playground.ssh.argv import build_scp_argv, build_ssh_argv` (or
the subset each file needs) to the imports.

**Note the ordering change you must NOT make:** in `adb_command` the
original put `-N -L ...` BEFORE the `-o` block; `build_ssh_argv` preserves
that. Do not reorder to satisfy a failing assertion — if
`tests/cli/test_adb.py` fails, the builder is wrong, not the test.

- [ ] **Step 6: Verify existing tests pass with zero edits**

Run: `uv run pytest tests/cli/ tests/unit/backend/local_libvirt/ tests/unit/ssh/ -v`
Expected: everything passes EXCEPT the one known baseline failure
`test_validate_committed_config_succeeds`. If any `test_exec_*`,
`test_cp_*`, `test_adb_*`, or `test_wait_*` fails, your builder produces a
different argv — fix the builder, never the assertion.

- [ ] **Step 7: De-duplicate the test shims**

`tests/cli/` has no `__init__.py`, so `_write_ssh_shim` and
`_write_apply_shims` exist verbatim in BOTH `test_cli.py` and
`test_adb.py`. Task 4 would add a third copy. Move them to fixtures
instead.

Create `tests/cli/conftest.py` containing `_write_apply_shims`,
`_write_ssh_shim`, and `_write_scp_shim` moved verbatim from
`tests/cli/test_cli.py`, each exposed as a pytest fixture returning the
factory function:

```python
"""Shared PATH-shim factories for CLI tests.

tests/cli/ has no __init__.py, so these helpers were duplicated verbatim
between test_cli.py and test_adb.py. conftest.py is importable by every
test module in this directory without packaging, so they live here now.
"""

from __future__ import annotations

import shlex
import stat
from pathlib import Path
from typing import Callable, Protocol

import pytest


class _ShimFactory(Protocol):
    def __call__(self, tmp_path: Path, **kwargs: object) -> Path: ...


@pytest.fixture
def write_apply_shims() -> Callable[..., Path]:
    """Factory writing `tofu` + `ansible-playbook` PATH shims."""
    return _write_apply_shims


@pytest.fixture
def write_ssh_shim() -> Callable[..., Path]:
    """Factory writing an `ssh` PATH shim that logs its argv."""
    return _write_ssh_shim


@pytest.fixture
def write_scp_shim() -> Callable[..., Path]:
    """Factory writing an `scp` PATH shim that logs its argv."""
    return _write_scp_shim
```

Then paste the three `_write_*` function bodies verbatim above the
fixtures. In `test_cli.py` and `test_adb.py`, delete the local copies and
take the fixture as a test argument, or import the module-level functions
from `conftest` — whichever produces the smaller diff. **Do not change any
assertion.**

- [ ] **Step 8: Lint, typecheck, confirm baseline**

```bash
uv run ruff check src tests && uv run mypy src
uv run pytest -q 2>&1 | tail -8
```
Expected: clean lint; mypy reports only the 9 pre-existing errors in files
you did not touch (`backend/local_vbox/*`, `backend/local_libvirt/*`,
`preflight/doctor.py`); the same 4 baseline test failures and no others.

- [ ] **Step 9: Commit**

```bash
git add src/playground/ssh tests/unit/ssh tests/cli/conftest.py \
        src/playground/cli/main.py src/playground/backend/local_libvirt/verify.py \
        src/playground/backend/local_libvirt/wait.py \
        tests/cli/test_cli.py tests/cli/test_adb.py
git commit -m "ssh: one shared argv builder for all six call sites

exec, adb, cp, verify's _ssh, and wait's _ssh_probe/_wait_cloud_init each
hand-rolled the same three -o options and the same 'omit -p when the port
is 22' rule. Behavior-preserving: every existing argv assertion passes
unchanged.

Also moves the duplicated PATH-shim helpers into tests/cli/conftest.py
rather than letting a third copy appear."
```

---

### Task 3: Android target resolution and adb command builders

**Files:**
- Create: `src/playground/android/__init__.py`, `targets.py`, `commands.py`, `runner.py`
- Create: `tests/unit/android/__init__.py`, `test_targets.py`, `test_commands.py`

**Interfaces:**
- Consumes: `build_ssh_argv` from Task 2.
- Produces:

```python
# targets.py
@dataclass(frozen=True)
class AndroidTarget:
    vm_name: str
    ssh_host: str
    ssh_port: int
    ssh_user: str

def android_vm_names(resolved: ResolvedLab) -> list[str]: ...
def resolve_targets(
    resolved: ResolvedLab, status: LabStatus, *,
    on: str | None, role: str | None, all_devices: bool, user: str,
) -> tuple[list[AndroidTarget], list[Diagnostic]]: ...

# commands.py
ADB_PORT = 5555
def adb_prefix() -> str: ...
def adb_shell(command: str) -> str: ...
def install_cmd(remote_apk: str) -> str: ...
def install_multiple_cmd(remote_apks: list[str]) -> str: ...
def uninstall_cmd(package: str) -> str: ...
def launch_cmd(package: str, *, activity: str | None, url: str | None) -> str: ...
def stop_cmd(package: str) -> str: ...
def clear_cmd(package: str) -> str: ...
def list_packages_cmd(*, pattern: str | None, third_party: bool) -> str: ...
def screenshot_cmd(remote_path: str) -> str: ...
def ui_dump_cmd(remote_path: str) -> str: ...
def logcat_cmd(*, follow: bool, lines: int | None) -> str: ...
def input_cmd(kind: str, args: list[str]) -> str: ...
def wait_booted_cmd(timeout_seconds: int) -> str: ...

# runner.py
@dataclass(frozen=True)
class TargetResult:
    target: AndroidTarget
    returncode: int
    stdout: str
    stderr: str

def run_on_targets(
    targets: list[AndroidTarget], command: str, *, timeout: float,
) -> list[TargetResult]: ...
```

Task 4 consumes all of these.

- [ ] **Step 1: Write the failing tests for command builders**

Create `tests/unit/android/__init__.py` (empty) and
`tests/unit/android/test_commands.py`:

```python
"""adb command-string builders.

These are pure string builders so the CLI layer stays thin and every
command shape is unit-testable without a device. Command syntax follows
docs/research/android-emulation/app-install-launch.md.
"""

from __future__ import annotations

from playground.android.commands import (
    ADB_PORT,
    adb_prefix,
    adb_shell,
    clear_cmd,
    input_cmd,
    install_cmd,
    install_multiple_cmd,
    launch_cmd,
    list_packages_cmd,
    logcat_cmd,
    screenshot_cmd,
    stop_cmd,
    ui_dump_cmd,
    uninstall_cmd,
    wait_booted_cmd,
)


def test_every_command_connects_first() -> None:
    """adb must not depend on a pre-existing server on the guest."""
    assert f"adb connect 127.0.0.1:{ADB_PORT}" in adb_prefix()
    assert "|| true" in adb_prefix(), "a stale connect must not fail the command"


def test_adb_shell_targets_the_serial() -> None:
    cmd = adb_shell("getprop sys.boot_completed")
    assert f"-s 127.0.0.1:{ADB_PORT}" in cmd
    assert cmd.startswith(adb_prefix())


def test_install_uses_r_so_reinstall_is_allowed() -> None:
    assert "install -r" in install_cmd("/tmp/a.apk")


def test_install_multiple_passes_every_split() -> None:
    cmd = install_multiple_cmd(["/tmp/base.apk", "/tmp/split_config.en.apk"])
    assert "install-multiple -r" in cmd
    assert "/tmp/base.apk" in cmd
    assert "/tmp/split_config.en.apk" in cmd


def test_launch_prefers_url_over_activity() -> None:
    cmd = launch_cmd("com.x", activity=".Main", url="https://e.com/p")
    assert "android.intent.action.VIEW" in cmd
    assert "https://e.com/p" in cmd
    assert ".Main" not in cmd


def test_launch_with_activity_uses_am_start_n() -> None:
    cmd = launch_cmd("com.x", activity=".Main", url=None)
    assert "am start -n com.x/.Main" in cmd


def test_launch_without_either_falls_back_to_monkey() -> None:
    cmd = launch_cmd("com.x", activity=None, url=None)
    assert "monkey -p com.x" in cmd
    assert "android.intent.category.LAUNCHER" in cmd


def test_stop_and_clear_are_distinct() -> None:
    assert "am force-stop com.x" in stop_cmd("com.x")
    assert "pm clear com.x" in clear_cmd("com.x")


def test_list_packages_third_party_flag() -> None:
    assert "pm list packages -3" in list_packages_cmd(pattern=None, third_party=True)
    cmd = list_packages_cmd(pattern="maps", third_party=False)
    assert "pm list packages" in cmd
    assert "maps" in cmd


def test_screenshot_and_ui_dump_write_to_the_given_remote_path() -> None:
    assert "/sdcard/s.png" in screenshot_cmd("/sdcard/s.png")
    assert "screencap -p" in screenshot_cmd("/sdcard/s.png")
    assert "uiautomator dump" in ui_dump_cmd("/sdcard/ui.xml")
    assert "/sdcard/ui.xml" in ui_dump_cmd("/sdcard/ui.xml")


def test_logcat_dump_vs_follow() -> None:
    assert "-d" in logcat_cmd(follow=False, lines=None)
    assert "-d" not in logcat_cmd(follow=True, lines=None)
    assert "-t 50" in logcat_cmd(follow=False, lines=50)


def test_input_builds_the_subcommand() -> None:
    assert "input text hello" in input_cmd("text", ["hello"])
    assert "input tap 10 20" in input_cmd("tap", ["10", "20"])
    assert "input keyevent KEYCODE_HOME" in input_cmd("key", ["KEYCODE_HOME"])


def test_shell_metacharacters_are_quoted() -> None:
    """A package or text argument must not be able to inject a shell command."""
    cmd = input_cmd("text", ["a; rm -rf /"])
    assert "; rm -rf /" not in cmd.replace("'a; rm -rf /'", "")


def test_wait_booted_polls_sys_boot_completed() -> None:
    cmd = wait_booted_cmd(60)
    assert "sys.boot_completed" in cmd
    assert "60" in cmd
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/android/test_commands.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'playground.android'`.

- [ ] **Step 3: Implement `commands.py`**

Create `src/playground/android/__init__.py` (empty module docstring) and
`src/playground/android/commands.py`:

```python
"""Pure builders for the adb command strings run on a Redroid guest.

Everything here returns a shell string to be handed to `ssh`. Keeping
them pure means every command shape is unit-testable without a device,
and the CLI layer stays a thin argument-parsing shell.

Every builder starts with `adb connect` so a command never depends on an
adb server already running on the guest; `|| true` because reconnecting
an existing device exits non-zero.

Syntax follows docs/research/android-emulation/app-install-launch.md.
"""

from __future__ import annotations

import shlex

ADB_PORT = 5555
"""Port the Redroid container publishes on the VM (ansible/roles/redroid)."""

_SERIAL = f"127.0.0.1:{ADB_PORT}"


def adb_prefix() -> str:
    return f"adb connect {_SERIAL} >/dev/null 2>&1 || true; adb -s {_SERIAL}"


def adb_shell(command: str) -> str:
    return f"{adb_prefix()} shell {command}"


def install_cmd(remote_apk: str) -> str:
    return f"{adb_prefix()} install -r {shlex.quote(remote_apk)}"


def install_multiple_cmd(remote_apks: list[str]) -> str:
    quoted = " ".join(shlex.quote(p) for p in remote_apks)
    return f"{adb_prefix()} install-multiple -r {quoted}"


def uninstall_cmd(package: str) -> str:
    return f"{adb_prefix()} uninstall {shlex.quote(package)}"


def launch_cmd(package: str, *, activity: str | None, url: str | None) -> str:
    """Three launch forms, resolved in priority order.

    A deep link wins over an explicit activity, which wins over monkey.
    monkey needs no activity name, which is why it is the fallback when
    the launcher activity is unknown.
    """
    if url is not None:
        return adb_shell(
            "am start -a android.intent.action.VIEW -d " + shlex.quote(url)
        )
    if activity is not None:
        return adb_shell(f"am start -n {shlex.quote(package + '/' + activity)}")
    return adb_shell(
        f"monkey -p {shlex.quote(package)} "
        "-c android.intent.category.LAUNCHER 1"
    )


def stop_cmd(package: str) -> str:
    return adb_shell(f"am force-stop {shlex.quote(package)}")


def clear_cmd(package: str) -> str:
    return adb_shell(f"pm clear {shlex.quote(package)}")


def list_packages_cmd(*, pattern: str | None, third_party: bool) -> str:
    parts = ["pm list packages"]
    if third_party:
        parts.append("-3")
    if pattern is not None:
        parts.append(shlex.quote(pattern))
    return adb_shell(" ".join(parts))


def screenshot_cmd(remote_path: str) -> str:
    return adb_shell(f"screencap -p {shlex.quote(remote_path)}")


def ui_dump_cmd(remote_path: str) -> str:
    return adb_shell(f"uiautomator dump {shlex.quote(remote_path)}")


def logcat_cmd(*, follow: bool, lines: int | None) -> str:
    parts = [adb_prefix(), "logcat"]
    if not follow:
        parts.append("-d")
    if lines is not None:
        parts.append(f"-t {int(lines)}")
    return " ".join(parts)


def input_cmd(kind: str, args: list[str]) -> str:
    quoted = " ".join(shlex.quote(a) for a in args)
    return adb_shell(f"input {kind} {quoted}")


def wait_booted_cmd(timeout_seconds: int) -> str:
    """Block until Android reports a completed boot.

    Immediately after apply the container is up but Android is still
    booting, so an install would fail on a device that is merely slow.
    """
    return (
        f"{adb_prefix()} wait-for-device >/dev/null 2>&1; "
        f"for i in $(seq 1 {int(timeout_seconds)}); do "
        f"[ \"$({adb_prefix()} shell getprop sys.boot_completed 2>/dev/null "
        "| tr -d '\\r')\" = 1 ] && exit 0; sleep 1; done; exit 1"
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/android/test_commands.py -v`
Expected: 14 passed.

- [ ] **Step 5: Write the failing test for target resolution**

Create `tests/unit/android/test_targets.py`:

```python
"""Selecting which VMs an `app` command acts on."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.models.status import LabStatus, VmState, VmStatus
from playground.android.targets import android_vm_names, resolve_targets

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def redroid_lab():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "redroid-cloud")


def _status(names: list[str]) -> LabStatus:
    return LabStatus(
        lab_name="redroid-cloud",
        backend="cloud-digitalocean",
        vms=[
            VmStatus(
                name=n, role="redroid-host", state=VmState.running,
                ssh_host=f"10.0.0.{i + 2}", ssh_port=22,
            )
            for i, n in enumerate(names)
        ],
    )


def test_android_vm_names_selects_only_redroid_capable(redroid_lab) -> None:
    assert android_vm_names(redroid_lab) == ["droid1"]


def test_all_devices_resolves_every_android_vm(redroid_lab) -> None:
    targets, diagnostics = resolve_targets(
        redroid_lab, _status(["droid1"]),
        on=None, role=None, all_devices=True, user="ubuntu",
    )
    assert [t.vm_name for t in targets] == ["droid1"]
    assert diagnostics == []
    assert targets[0].ssh_user == "ubuntu"
    assert targets[0].ssh_port == 22


def test_single_android_vm_is_the_default_target(redroid_lab) -> None:
    """No flag needed when the lab has exactly one Android VM."""
    targets, diagnostics = resolve_targets(
        redroid_lab, _status(["droid1"]),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert [t.vm_name for t in targets] == ["droid1"]
    assert diagnostics == []


def test_unknown_vm_is_an_error(redroid_lab) -> None:
    targets, diagnostics = resolve_targets(
        redroid_lab, _status(["droid1"]),
        on="nope", role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.app.unknown_vm"]


def test_vm_without_an_ssh_endpoint_is_an_error(redroid_lab) -> None:
    status = LabStatus(
        lab_name="redroid-cloud", backend="cloud-digitalocean",
        vms=[VmStatus(name="droid1", role="redroid-host", state=VmState.stopped)],
    )
    targets, diagnostics = resolve_targets(
        redroid_lab, status,
        on="droid1", role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.app.vm_not_reachable"]
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests/unit/android/test_targets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'playground.android.targets'`.

Before implementing, confirm the exact `LabStatus` / `VmStatus` / `VmState`
constructor signature:

```bash
uv run python -c "
from playground.models.status import LabStatus, VmStatus, VmState
print(LabStatus.model_fields.keys()); print(VmStatus.model_fields.keys()); print(list(VmState))
"
```
Adjust the fixture to match the real fields rather than forcing the model
to match the test.

- [ ] **Step 7: Implement `targets.py`**

```python
"""Resolve `playground app` targeting flags to concrete Android devices.

"Which VMs are Android?" is answered from resolved role capabilities:
a VM is a device when its role chain declares `redroid: true`. That key
became load-bearing with the cloud-redroid work
(config/providers/*.yaml, src/playground/validation/validator.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus


@dataclass(frozen=True)
class AndroidTarget:
    """One reachable Android device."""

    vm_name: str
    ssh_host: str
    ssh_port: int
    ssh_user: str


def android_vm_names(resolved: ResolvedLab) -> list[str]:
    """VMs whose role declares the `redroid` capability, in lab order."""
    return [vm.name for vm in resolved.vms if vm.capabilities.get("redroid")]


def resolve_targets(
    resolved: ResolvedLab,
    status: LabStatus,
    *,
    on: str | None,
    role: str | None,
    all_devices: bool,
    user: str,
) -> tuple[list[AndroidTarget], list[Diagnostic]]:
    """Turn targeting flags into devices, or explain why none matched."""
    source = SourceLocation(path=f"config/labs/{resolved.lab_name}.yaml")
    android = android_vm_names(resolved)

    if not android:
        return [], [
            Diagnostic(
                id="config.app.no_android_vms",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} has no VM with the `redroid` "
                    "capability"
                ),
                source=source,
                suggestion=(
                    "give a VM the `redroid-host` role, or run against a lab "
                    "that has one"
                ),
            )
        ]

    if on is not None:
        if on not in android:
            return [], [
                Diagnostic(
                    id="config.app.unknown_vm",
                    severity="error",
                    message=(
                        f"VM {on!r} is not an Android device in lab "
                        f"{resolved.lab_name!r} (devices: {android})"
                    ),
                    source=source,
                    key_path="spec.vms",
                )
            ]
        wanted = [on]
    elif role is not None:
        wanted = [
            vm.name for vm in resolved.vms
            if role in vm.roles and vm.capabilities.get("redroid")
        ]
        if not wanted:
            return [], [
                Diagnostic(
                    id="config.app.unknown_vm",
                    severity="error",
                    message=(
                        f"no Android device in lab {resolved.lab_name!r} has "
                        f"role {role!r}"
                    ),
                    source=source,
                )
            ]
    elif all_devices:
        wanted = android
    elif len(android) == 1:
        wanted = android
    else:
        return [], [
            Diagnostic(
                id="config.app.target_required",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} has {len(android)} Android "
                    "devices; pass --on, --role, or --all"
                ),
                source=source,
                suggestion=f"devices: {', '.join(android)}",
            )
        ]

    by_name = {vm.name: vm for vm in status.vms}
    targets: list[AndroidTarget] = []
    diagnostics: list[Diagnostic] = []
    for name in wanted:
        vm_status = by_name.get(name)
        host = vm_status.ssh_host if vm_status else None
        if not host:
            diagnostics.append(
                Diagnostic(
                    id="config.app.vm_not_reachable",
                    severity="error",
                    message=(
                        f"VM {name!r} has no reachable SSH endpoint — has the "
                        "lab been applied?"
                    ),
                    source=source,
                    suggestion=f"run `playground apply {resolved.lab_name}` first",
                )
            )
            continue
        targets.append(
            AndroidTarget(
                vm_name=name,
                ssh_host=host,
                ssh_port=vm_status.ssh_port or 22,
                ssh_user=user,
            )
        )
    return targets, diagnostics
```

- [ ] **Step 8: Implement `runner.py`**

```python
"""Run one adb command across many devices in parallel.

Mirrors the fan-out shape already used by
`backend/local_libvirt/verify.py`: one worker per target, results
re-ordered back into declaration order so logs are stable regardless of
completion order.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from playground.android.targets import AndroidTarget
from playground.ssh.argv import build_ssh_argv


@dataclass(frozen=True)
class TargetResult:
    target: AndroidTarget
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _run_one(target: AndroidTarget, command: str, timeout: float) -> TargetResult:
    argv = build_ssh_argv(
        target.ssh_host,
        user=target.ssh_user,
        port=target.ssh_port,
        command=command,
        batch=True,
    )
    try:
        done = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return TargetResult(target, 124, "", f"timeout after {timeout}s")
    except FileNotFoundError as exc:
        return TargetResult(target, 127, "", f"failed to launch ssh: {exc}")
    return TargetResult(target, done.returncode, done.stdout, done.stderr)


def run_on_targets(
    targets: list[AndroidTarget], command: str, *, timeout: float
) -> list[TargetResult]:
    """Run ``command`` on every target; results keep input order."""
    if not targets:
        return []
    results: dict[str, TargetResult] = {}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {
            pool.submit(_run_one, t, command, timeout): t for t in targets
        }
        for future in as_completed(futures):
            result = future.result()
            results[result.target.vm_name] = result
    return [results[t.vm_name] for t in targets]
```

- [ ] **Step 9: Run, lint, typecheck**

```bash
uv run pytest tests/unit/android/ -v
uv run ruff check src/playground/android tests/unit/android
uv run mypy src/playground/android
```
Expected: all pass, clean.

- [ ] **Step 10: Commit**

```bash
git add src/playground/android tests/unit/android
git commit -m "android: target resolution, adb command builders, fan-out runner

Which VMs are Android is answered from resolved role capabilities
(redroid: true). Command builders are pure strings so every shape is
testable without a device, and shell metacharacters in package names or
input text are quoted rather than interpolated."
```

---

### Task 4: The `playground app` verb group

**Files:**
- Create: `src/playground/cli/app_commands.py`
- Modify: `src/playground/cli/main.py` (register the sub-app only)
- Create: `tests/cli/test_app.py`

**Interfaces:**
- Consumes: `AndroidTarget`, `resolve_targets`, every builder in `commands.py`, `run_on_targets`, `TargetResult`.
- Produces: `app_app: typer.Typer` registered as `playground app`.

New code goes in its own module because `main.py` is already ~1900 lines.
Register with `app.add_typer(app_app, name="app")` next to the existing
`lab` / `inventory` / `tofu` / `runs` groups.

- [ ] **Step 1: Write the failing test**

Create `tests/cli/test_app.py`:

```python
"""`playground app` — install/launch/inspect apps on Redroid VMs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from playground.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, *args):
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    return CliRunner().invoke(
        app,
        ["app", *args, "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir)],
    )


def test_launch_uses_monkey_by_default(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "launch", "com.example.app")
    assert result.exit_code == 0, result.stderr
    log = (tmp_path / "ssh.log").read_text()
    assert "monkey -p com.example.app" in log
    assert "adb connect 127.0.0.1:5555" in log


def test_launch_with_url_fires_a_deep_link(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "launch", "com.example.app", "--url", "https://e.com/p")
    assert result.exit_code == 0
    log = (tmp_path / "ssh.log").read_text()
    assert "android.intent.action.VIEW" in log
    assert "monkey" not in log


def test_url_and_activity_are_mutually_exclusive(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "launch", "com.x", "--url", "https://e.com", "--activity", ".M")
    assert result.exit_code == 1
    assert "config.app.conflicting_launch" in result.output + result.stderr


def test_clear_uses_pm_clear(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "clear", "com.example.app")
    assert result.exit_code == 0
    assert "pm clear com.example.app" in (tmp_path / "ssh.log").read_text()


def test_ui_dump_invokes_uiautomator(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "ui-dump", "--out", str(tmp_path / "ui.xml"))
    assert result.exit_code == 0
    assert "uiautomator dump" in (tmp_path / "ssh.log").read_text()


def test_logcat_follow_rejects_multi_device_targeting(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "logcat", "--follow", "--all")
    assert result.exit_code == 1
    assert "config.app.follow_needs_one_device" in result.output + result.stderr


def test_failure_on_a_device_exits_nonzero(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=1)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    result = CliRunner().invoke(
        app,
        ["app", "stop", "com.x", "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir)],
    )
    assert result.exit_code == 1


def test_install_rejects_a_missing_apk(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim,
                  "install", str(tmp_path / "nope.apk"))
    assert result.exit_code == 1
    assert "config.app.apk_not_found" in result.output + result.stderr
```

Note: `redroid-cloud` declares one VM `droid1`, so no target flag is
needed. The `tofu` shim's default `vm_ips` map has no `droid1` key — the
cloud backend's `query_status` reads Droplet IPs from its own per-lab
state, not from that map. **Before implementing, run one of these tests
and read the actual failure**; if `query_status` cannot resolve `droid1`,
add a `monkeypatch.setattr` on the status query used by
`app_commands.py` to return a stub `LabStatus`, exactly as the test in
Step 1 of Task 3 builds one. Do not weaken the assertions to dodge this.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/cli/test_app.py -v`
Expected: FAIL — `No such command 'app'`.

- [ ] **Step 3: Implement the sub-app**

Create `src/playground/cli/app_commands.py`. Structure:

- `app_app = typer.Typer(no_args_is_help=True, help="Install and drive Android apps on Redroid VMs.")`
- A `_TargetOpts` helper dataclass plus a `_resolve(...)` function performing
  the shared preamble every subcommand needs: load config, validate,
  default the lab, resolve the lab, `query_status`, then `resolve_targets`.
  Exit through `_exit_with_diagnostic` on any returned diagnostic.
- A `_dispatch(targets, command, *, timeout) -> None` helper that calls
  `run_on_targets`, prints `[<vm>] <line>` per output line, prints a
  failure summary naming each failed device, and raises
  `typer.Exit(code=0 if all ok else 1)`.
- One `@app_app.command(...)` per verb, each taking `--lab`, `--on`,
  `--role`, `--all`, `--user`, `--config-dir`, `--tofu-dir`, and building
  its command string from `playground.android.commands`.

Reuse the existing helpers from `main.py` — `_load_config_or_exit`,
`_resolve_lab_or_exit`, `_exit_on_errors`, `_exit_with_diagnostic`,
`_has_errors`, `_print_warnings` — by importing them. If that creates a
circular import (`main` imports `app_commands`, `app_commands` imports
`main`), move those six helpers into a new `src/playground/cli/_shared.py`
and have BOTH modules import from there. Do not duplicate them.

Specific behaviors the tests pin:

- `launch`: `--url` and `--activity` together → `config.app.conflicting_launch`, exit 1.
- `logcat --follow` with more than one resolved target →
  `config.app.follow_needs_one_device`, exit 1.
- `install PATH`: if `PATH` does not exist → `config.app.apk_not_found`,
  exit 1. If it is a file, `scp` it to `/data/local/tmp/<name>` (use
  `build_scp_argv`) then `install_cmd`. If it is a directory, scp every
  `*.apk` inside it and use `install_multiple_cmd`; a directory with no
  `*.apk` → `config.app.apk_not_found`.
- `screenshot`/`ui-dump`: run the capture command, then `adb pull` to a
  controller-side path. Under fan-out write
  `<out>/<vm>-<timestamp>.png` (or `.xml`) so devices cannot collide.
- Non-zero on any device → overall exit 1.

- [ ] **Step 4: Register the sub-app**

In `src/playground/cli/main.py`, next to the other `add_typer` calls:

```python
from playground.cli.app_commands import app_app
...
app.add_typer(app_app, name="app")
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/cli/ -v`
Expected: all pass except the known baseline
`test_validate_committed_config_succeeds`.

- [ ] **Step 6: Lint, typecheck, confirm baseline, commit**

```bash
uv run ruff check src tests && uv run mypy src
uv run pytest -q 2>&1 | tail -8
git add src/playground/cli tests/cli/test_app.py
git commit -m "cli: add \`playground app\` for Android app lifecycle

Eleven verbs over adb-on-the-VM, with --on/--role/--all targeting and
parallel fan-out. Lives in its own module rather than growing main.py
past 1900 lines. Fan-out exits non-zero if any device fails, and
logcat --follow requires a single device."
```

---

### Task 5: `android_app` workload model

**Files:**
- Modify: `src/playground/models/kinds.py`
- Modify: `src/playground/models/resolved.py`
- Modify: `src/playground/config/resolver.py`
- Test: `tests/unit/models/test_android_workload.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:

```python
class AndroidAppOptions(StrictModel):
    package: str
    launch: bool = False
    activity: str | None = None
    permissions: list[str] = Field(default_factory=list)
    reinstall: bool = False
```

`LabWorkload.type` and `ResolvedWorkload.type` both become
`Literal["container", "compose", "swarm", "android_app"]`, and both gain
`android: AndroidAppOptions | None = None`. Tasks 6, 7, 8 depend on these.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/models/test_android_workload.py`:

```python
"""The android_app workload type and its options block."""

from __future__ import annotations

from textwrap import dedent

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from playground.models.kinds import AndroidAppOptions, Lab, parse_resource

_yaml = YAML(typ="safe")


def _lab(workload_yaml: str) -> Lab:
    raw = _yaml.load(dedent(f"""
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: droid-lab
        spec:
          backend: local-libvirt
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: droid1
              role: redroid-host
              networks: [net]
          workloads:
        {workload_yaml}
        """).lstrip("\n"))
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    return lab


def test_android_app_is_an_accepted_workload_type() -> None:
    lab = _lab("""
            - name: my-app
              type: android_app
              source: ./apks/my-app.apk
              placement: {target_role: redroid-host}
              android:
                package: com.example.app
    """)
    wl = lab.spec.workloads[0]
    assert wl.type == "android_app"
    assert wl.android is not None
    assert wl.android.package == "com.example.app"
    assert wl.android.launch is False
    assert wl.android.reinstall is False
    assert wl.android.activity is None
    assert wl.android.permissions == []


def test_android_options_accept_launch_activity_and_permissions() -> None:
    lab = _lab("""
            - name: my-app
              type: android_app
              source: ./apks/my-app.apk
              placement: {target_role: redroid-host}
              android:
                package: com.example.app
                launch: true
                activity: .MainActivity
                permissions: [android.permission.CAMERA]
                reinstall: true
    """)
    opts = lab.spec.workloads[0].android
    assert opts == AndroidAppOptions(
        package="com.example.app",
        launch=True,
        activity=".MainActivity",
        permissions=["android.permission.CAMERA"],
        reinstall=True,
    )


def test_android_block_is_optional_on_other_types() -> None:
    lab = _lab("""
            - name: web
              type: container
              source: nginx:alpine
              placement: {auto: true}
    """)
    assert lab.spec.workloads[0].android is None


def test_package_is_required_when_the_android_block_is_present() -> None:
    with pytest.raises(ValidationError):
        AndroidAppOptions()  # type: ignore[call-arg]


def test_unknown_android_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AndroidAppOptions(package="com.x", nope=1)  # type: ignore[call-arg]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/models/test_android_workload.py -v`
Expected: FAIL — `ImportError: cannot import name 'AndroidAppOptions'`.

- [ ] **Step 3: Add the model**

In `src/playground/models/kinds.py`, immediately above `LabWorkload`:

```python
class AndroidAppOptions(StrictModel):
    """Android-specific fields for a ``type: android_app`` workload.

    Kept in a sub-model so Android concerns do not widen the schema every
    workload type shares. ``package`` is required and cannot be inferred:
    it drives the install idempotency check, the launch, and the
    verify-lab assertion.

    ``activity`` is optional because the launcher activity is
    discoverable at runtime with
    ``cmd package resolve-activity --brief``; when unset the role
    launches via ``monkey``, which needs no activity name.
    """

    package: str = Field(min_length=1)
    launch: bool = False
    activity: str | None = None
    permissions: list[str] = Field(default_factory=list)
    reinstall: bool = False
```

Change `LabWorkload.type` to
`Literal["container", "compose", "swarm", "android_app"]` and add
`android: AndroidAppOptions | None = None`.

- [ ] **Step 4: Mirror it on `ResolvedWorkload`**

In `src/playground/models/resolved.py`, widen `ResolvedWorkload.type` the
same way and add `android: AndroidAppOptions | None = None` (import it
from `kinds`).

- [ ] **Step 5: Carry it through the resolver**

In `src/playground/config/resolver.py`, find where `ResolvedWorkload` is
constructed and add `android=workload.android`. Locate it with:

```bash
grep -n "ResolvedWorkload(" src/playground/config/resolver.py
```

- [ ] **Step 6: Run tests, lint, typecheck, commit**

```bash
uv run pytest tests/unit/models/ tests/unit/config/ tests/unit/planner/ -v
uv run ruff check src tests && uv run mypy src
git add src/playground/models src/playground/config/resolver.py \
        tests/unit/models/test_android_workload.py
git commit -m "models: add the android_app workload type

Android fields live in an AndroidAppOptions sub-model rather than
widening the schema every workload type shares. package is required
because it drives install idempotency, launch, and the verify check."
```

---

### Task 6: Validator diagnostics for `android_app`

**Files:**
- Modify: `src/playground/validation/validator.py`
- Test: `tests/unit/validation/test_android_workload.py` (create)

**Interfaces:**
- Consumes: `AndroidAppOptions`, the widened `LabWorkload` (Task 5).
- Produces: `_check_android_workload(lab, idx, source, loaded) -> list[Diagnostic]`, and diagnostic IDs `config.workload.android_options_missing`, `config.workload.android_options_ignored`, `config.workload.android_target_not_capable`.

Existing `_check_workload_placement` is type-agnostic and needs no change.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/validation/test_android_workload.py`:

```python
"""Validation for android_app workloads."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from ruamel.yaml import YAML

from playground.config.loader import LoadedConfig, load_config
from playground.models.kinds import Lab, parse_resource
from playground.validation.validator import validate

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"
_yaml = YAML(typ="safe")


@pytest.fixture
def committed_load() -> LoadedConfig:
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return loaded


def _lab(name: str, vm_role: str, workload: str) -> Lab:
    raw = _yaml.load(dedent(f"""
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: {name}
        spec:
          backend: local-libvirt
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: vm1
              role: {vm_role}
              networks: [net]
          workloads:
        {workload}
        """).lstrip("\n"))
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    return lab


def _ids(loaded: LoadedConfig, lab: Lab) -> list[str]:
    loaded.labs[lab.metadata.name] = lab
    return [d.id for d in validate(loaded, lab=lab.metadata.name)]


def test_android_app_without_options_is_an_error(committed_load) -> None:
    lab = _lab("no-opts", "redroid-host", """
            - name: a
              type: android_app
              source: ./a.apk
              placement: {target_role: redroid-host}
    """)
    assert "config.workload.android_options_missing" in _ids(committed_load, lab)


def test_android_options_on_a_container_workload_warns(committed_load) -> None:
    lab = _lab("ignored", "docker-host", """
            - name: a
              type: container
              source: nginx:alpine
              placement: {target_role: docker-host}
              android:
                package: com.x
    """)
    assert "config.workload.android_options_ignored" in _ids(committed_load, lab)


def test_android_app_targeting_a_non_redroid_vm_warns(committed_load) -> None:
    lab = _lab("wrong-target", "docker-host", """
            - name: a
              type: android_app
              source: ./a.apk
              placement: {target_role: docker-host}
              android:
                package: com.x
    """)
    ids = _ids(committed_load, lab)
    assert "config.workload.android_target_not_capable" in ids


def test_well_formed_android_workload_is_clean(committed_load) -> None:
    lab = _lab("good", "redroid-host", """
            - name: a
              type: android_app
              source: ./a.apk
              placement: {target_role: redroid-host}
              android:
                package: com.x
    """)
    ids = _ids(committed_load, lab)
    assert "config.workload.android_options_missing" not in ids
    assert "config.workload.android_target_not_capable" not in ids
    assert "config.workload.android_options_ignored" not in ids
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/validation/test_android_workload.py -v`
Expected: FAIL — none of the three IDs are produced.

- [ ] **Step 3: Implement the check**

Add to `validator.py`:

```python
def _check_android_workload(
    lab: Lab,
    workload_idx: int,
    source: SourceLocation,
    loaded: LoadedConfig,
) -> list[Diagnostic]:
    """Validate `type: android_app` workloads and their options block.

    Placement itself is checked by ``_check_workload_placement``, which is
    type-agnostic; this covers only the Android-specific shape.
    """
    workload = lab.spec.workloads[workload_idx]
    key = f"spec.workloads[{workload_idx}]"

    if workload.type != "android_app":
        if workload.android is not None:
            return [
                Diagnostic(
                    id="config.workload.android_options_ignored",
                    severity="warning",
                    message=(
                        f"workload {workload.name!r} is type "
                        f"{workload.type!r} but declares an `android:` block, "
                        "which only applies to type: android_app"
                    ),
                    source=source,
                    key_path=f"{key}.android",
                    suggestion="remove the android: block, or set type: android_app",
                )
            ]
        return []

    if workload.android is None:
        return [
            Diagnostic(
                id="config.workload.android_options_missing",
                severity="error",
                message=(
                    f"workload {workload.name!r} is type android_app but has "
                    "no `android:` block; `package` is required"
                ),
                source=source,
                key_path=f"{key}.android",
                suggestion=(
                    "add `android: {package: com.example.app}` — the package "
                    "name drives install idempotency, launch, and verify"
                ),
            )
        ]

    # Warn (principle 10) when placement resolves only to VMs whose role
    # chain lacks the redroid capability.
    placement = workload.placement
    candidates = [
        vm for vm in lab.spec.vms
        if (placement.target_vm == vm.name)
        or (placement.target_role is not None
            and placement.target_role in _role_ancestors(loaded, vm.role))
        or (placement.target_tag is not None and placement.target_tag in vm.tags)
        or (placement.auto is True)
    ]
    capable = [
        vm for vm in candidates
        if _capabilities_for_vm(loaded, vm).get("redroid")
    ]
    if candidates and not capable:
        return [
            Diagnostic(
                id="config.workload.android_target_not_capable",
                severity="warning",
                message=(
                    f"workload {workload.name!r} installs an APK but its "
                    "placement matches no VM with the `redroid` capability"
                ),
                source=source,
                key_path=f"{key}.placement",
                suggestion=(
                    "target a VM whose role is redroid-host (or extends it)"
                ),
            )
        ]
    return []
```

`_capabilities_for_vm` and `_role_ancestors` already exist in this module.

- [ ] **Step 4: Register it**

In `_check_lab`, inside the existing workload loop, next to
`_check_workload_placement`:

```python
        diagnostics.extend(_check_android_workload(lab, idx, source, loaded))
```

- [ ] **Step 5: Add the three IDs to the module docstring**

Match the surrounding format exactly.

- [ ] **Step 6: Run, verify committed config is unaffected, commit**

```bash
uv run pytest tests/unit/validation/ -v
STASH=/tmp/pg-untracked-labs && mkdir -p $STASH
mv config/labs/barak-deploy-cross-vm.yaml config/labs/barak-dev-fleet-cloud.yaml config/labs/barak-fleet-airgap.yaml $STASH/
uv run pytest -q tests/cli/test_cli.py
mv $STASH/*.yaml config/labs/
uv run ruff check src tests && uv run mypy src
git add src/playground/validation/validator.py tests/unit/validation/test_android_workload.py
git commit -m "validation: check android_app workload shape

Errors when the android: block is missing (package cannot be inferred),
warns when it appears on another workload type, and warns when placement
matches no redroid-capable VM."
```

---

### Task 7: Capability-aware placement and split-APK staging

**Files:**
- Modify: `src/playground/planner/scheduling.py`
- Test: `tests/unit/planner/test_android_scheduling.py` (create)

**Interfaces:**
- Consumes: the widened `ResolvedWorkload` (Task 5).
- Produces: `stage_workload_files` returning `dict[str, dict[str, list[Path]]]` for android_app... **NO — see below.**

**Design constraint that must not be broken:** `stage_workload_files`
currently returns `dict[str, dict[str, Path]]` (one staged path per
workload) and `render_inventory` threads it into
`workload_to_ansible_payload(wl, staged_source=...)`. Changing the return
type breaks compose and swarm. Instead: for a split directory, stage into
a DIRECTORY and return that directory's path. The Ansible role then
copies the directory and globs `*.apk` inside it. One return type, no
breakage.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/planner/test_android_scheduling.py`:

```python
"""Placement and staging for android_app workloads."""

from __future__ import annotations

from pathlib import Path

from playground.models.kinds import AndroidAppOptions, WorkloadPlacement
from playground.models.resolved import ResolvedVm, ResolvedWorkload
from playground.planner.scheduling import _pick_target_vm, stage_workload_files


def _vm(name: str, **caps: bool) -> ResolvedVm:
    return ResolvedVm(
        name=name, role="r", roles=["r"], image="ubuntu-noble",
        vcpu=1, memory_mb=1024, disk_gb=10, networks=["net"],
        capabilities=dict(caps),
    )


def _android_workload(source: str) -> ResolvedWorkload:
    return ResolvedWorkload(
        name="my-app", type="android_app", source=source,
        placement=WorkloadPlacement(auto=True),
        android=AndroidAppOptions(package="com.example.app"),
    )


def test_auto_placement_picks_a_redroid_vm_not_a_docker_one() -> None:
    """`auto` was hardcoded to capabilities['docker']; an APK must not land
    on a plain docker host."""
    vms = [_vm("web", docker=True), _vm("droid", docker=True, redroid=True)]
    picked = _pick_target_vm(_android_workload("./a.apk"), vms)
    assert picked is not None
    assert picked.name == "droid"


def test_auto_placement_for_container_still_picks_docker() -> None:
    wl = ResolvedWorkload(
        name="web", type="container", source="nginx:alpine",
        placement=WorkloadPlacement(auto=True),
    )
    vms = [_vm("web", docker=True), _vm("droid", docker=True, redroid=True)]
    picked = _pick_target_vm(wl, vms)
    assert picked is not None
    assert picked.name == "web"


def test_single_apk_is_staged_as_a_file(tmp_path: Path) -> None:
    base = tmp_path / "src"
    base.mkdir()
    (base / "a.apk").write_bytes(b"PK\x03\x04fake")
    staged, diagnostics = stage_workload_files(
        {"droid": [_android_workload("a.apk")]},
        source_base=base, stage_dir=tmp_path / "stage",
    )
    assert diagnostics == []
    path = staged["droid"]["my-app"]
    assert path.is_file()
    assert path.suffix == ".apk"
    assert path.is_absolute()


def test_split_apk_directory_is_staged_as_a_directory(tmp_path: Path) -> None:
    """A directory of splits must stage, not fail as a missing source."""
    base = tmp_path / "src"
    splits = base / "split-app"
    splits.mkdir(parents=True)
    (splits / "base.apk").write_bytes(b"PK\x03\x04base")
    (splits / "split_config.en.apk").write_bytes(b"PK\x03\x04en")
    staged, diagnostics = stage_workload_files(
        {"droid": [_android_workload("split-app")]},
        source_base=base, stage_dir=tmp_path / "stage",
    )
    assert diagnostics == []
    path = staged["droid"]["my-app"]
    assert path.is_dir()
    assert {p.name for p in path.glob("*.apk")} == {
        "base.apk", "split_config.en.apk"
    }


def test_split_directory_without_base_apk_is_an_error(tmp_path: Path) -> None:
    base = tmp_path / "src"
    splits = base / "split-app"
    splits.mkdir(parents=True)
    (splits / "split_config.en.apk").write_bytes(b"PK\x03\x04en")
    _, diagnostics = stage_workload_files(
        {"droid": [_android_workload("split-app")]},
        source_base=base, stage_dir=tmp_path / "stage",
    )
    assert [d.id for d in diagnostics] == ["config.workload.split_apk_invalid"]


def test_directory_with_no_apks_is_an_error(tmp_path: Path) -> None:
    base = tmp_path / "src"
    (base / "empty").mkdir(parents=True)
    _, diagnostics = stage_workload_files(
        {"droid": [_android_workload("empty")]},
        source_base=base, stage_dir=tmp_path / "stage",
    )
    assert [d.id for d in diagnostics] == ["config.workload.split_apk_invalid"]


def test_aab_is_rejected_with_a_bundletool_message(tmp_path: Path) -> None:
    base = tmp_path / "src"
    base.mkdir()
    (base / "a.aab").write_bytes(b"PK\x03\x04aab")
    _, diagnostics = stage_workload_files(
        {"droid": [_android_workload("a.aab")]},
        source_base=base, stage_dir=tmp_path / "stage",
    )
    assert [d.id for d in diagnostics] == ["config.workload.bundle_unsupported"]
    assert "bundletool" in diagnostics[0].suggestion
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/planner/test_android_scheduling.py -v`
Expected: FAIL — auto placement returns `web`, and the directory cases
produce `config.workload.source_missing`.

Confirm `ResolvedVm`'s real required fields first:
```bash
uv run python -c "
from playground.models.resolved import ResolvedVm
print({k: v.is_required() for k, v in ResolvedVm.model_fields.items()})
"
```
and adjust `_vm()` to match rather than editing the model.

- [ ] **Step 3: Make `auto` capability-aware**

In `_pick_target_vm`, replace the final `auto` return with:

```python
    # `auto` means "any VM that can actually run this". Which capability
    # that is depends on the workload type: an APK needs a Redroid device,
    # everything else needs Docker.
    capability = "redroid" if workload.type == "android_app" else "docker"
    return next(
        (vm for vm in vms if vm.capabilities.get(capability)), None
    )
```

Update the `schedule_workloads` docstring line
``- ``auto``: first VM whose ``capabilities['docker']`` is truthy`` to say
the capability depends on workload type.

- [ ] **Step 4: Teach the stager about directories and bundles**

In `stage_workload_files`, replace the `if not src.is_file():` block with
logic that:

1. Rejects `.aab` / `.apks` suffixes with `config.workload.bundle_unsupported`
   (severity `error`, suggestion naming `bundletool build-apks` /
   `install-apks` and `docs/research/android-emulation/app-install-launch.md`).
2. If `src.is_dir()` and the workload type is `android_app`: glob `*.apk`;
   emit `config.workload.split_apk_invalid` when there are none or when no
   `base.apk` is present; otherwise `shutil.copytree` the `*.apk` files
   into `stage_dir/<vm>/<workload_name>/` and record that DIRECTORY as the
   staged path.
3. If `src.is_file()`: existing behavior, unchanged.
4. Otherwise: existing `config.workload.source_missing`.

Keep the absolute-path resolution — `test_stage_workload_files_records_absolute_path`
is a BUG-8 regression guard and must keep passing.

- [ ] **Step 5: Run everything in planner, commit**

```bash
uv run pytest tests/unit/planner/ -v
uv run ruff check src tests && uv run mypy src
git add src/playground/planner/scheduling.py tests/unit/planner/test_android_scheduling.py
git commit -m "planner: capability-aware auto placement and split-APK staging

auto was hardcoded to capabilities['docker'], so an APK could be placed on
a plain docker host. It now picks the capability by workload type.

stage_workload_files guarded with is_file(), so a directory of split APKs
failed as a missing source. Directories now stage as directories (one
return type, so compose and swarm are untouched), and .aab/.apks are
rejected with a message naming bundletool."
```

---

### Task 8: The `workload_android_app` Ansible role

**Files:**
- Create: `ansible/roles/workload_android_app/defaults/main.yml`, `tasks/main.yml`
- Modify: `ansible/site.yml`
- Test: `tests/unit/ansible/test_workload_android_app.py` (create)

**Interfaces:**
- Consumes: `pg_workloads` entries with `type: android_app`, `staged_source`
  (a file OR a directory), and an `android` dict carrying `package`,
  `launch`, `activity`, `permissions`, `reinstall`.
- Produces: an installed and optionally running app on the guest.

`workload_to_ansible_payload` currently emits only
`name/type/source/ports/volumes/environment` plus `staged_source`. **It must
also emit `android`** — add that in this task:

```python
    if workload.android is not None:
        payload["android"] = workload.android.model_dump()
```

**Idempotency is the whole point of this task.** Install is skipped when
the package is present; launch checks `pidof` first. A second `apply` must
report `changed=0`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/ansible/test_workload_android_app.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/ansible/test_workload_android_app.py -v`
Expected: FAIL — the role directory does not exist.

- [ ] **Step 3: Emit the `android` block into the payload**

In `src/playground/planner/scheduling.py`, in
`workload_to_ansible_payload`, after the `staged_source` block:

```python
    if workload.android is not None:
        payload["android"] = workload.android.model_dump()
```

- [ ] **Step 4: Create `defaults/main.yml`**

```yaml
---
pg_workloads: '[]'

# Where staged APKs land on the guest.
pg_apk_root: /opt/playground/apks

# Android may still be booting immediately after the container starts;
# installing then fails on a device that is merely slow.
pg_android_boot_timeout: 180
```

- [ ] **Step 5: Create `tasks/main.yml`**

Follow the `workload_compose` skeleton exactly: parse `pg_workloads` with
the BUG-6 string-or-list guard, `selectattr('type','equalto','android_app')`,
`meta: end_host` when empty. Then per workload:

1. `ansible.builtin.file` create `{{ pg_apk_root }}/{{ item.name }}`.
2. `ansible.builtin.copy` the `item.staged_source` to that directory.
   Ansible's `copy` handles both a file and a directory source; a source
   path ending in `/` copies the contents. Determine which by testing the
   staged path, and set `dest` accordingly.
3. Wait for boot: `ansible.builtin.shell` running the equivalent of
   `wait_booted_cmd`, `changed_when: false`, failing with a clear message
   on timeout.
4. Query installed packages once:
   `adb connect ...; adb -s 127.0.0.1:5555 shell pm list packages`,
   `register: pg_installed`, `changed_when: false`.
5. Install, `when: item.android.package not in pg_installed.stdout or
   item.android.reinstall | default(false)`. Use `install -r` for a single
   APK and `install-multiple -r <glob>` for a directory.
6. Grant each permission in `item.android.permissions | default([])`,
   `changed_when: false` unless the grant actually changed state.
7. Launch when `item.android.launch | default(false)`: first
   `adb shell pidof <package>` with `failed_when: false`,
   `changed_when: false`; then start the app
   `when: pidof_result.rc != 0`, using `am start -n <pkg>/<activity>` if
   `item.android.activity` is set, else
   `monkey -p <pkg> -c android.intent.category.LAUNCHER 1`.

Every `shell`/`command` task that only reads state MUST set
`changed_when: false`, or `changed=0` on a second apply is impossible.

- [ ] **Step 6: Add the play to `site.yml`**

Immediately after the `Deploy compose workloads` play:

```yaml
- name: Deploy android_app workloads
  hosts: playground
  become: yes
  gather_facts: no
  roles:
    # Platform-level like the other workload_* plays: dispatch happens
    # inside the role via pg_workloads, and the role ends the host early
    # when nothing matches. Must run AFTER the redroid play so the
    # Android container exists before an APK is installed.
    - workload_android_app
```

- [ ] **Step 7: Run tests, syntax-check, commit**

```bash
uv run pytest tests/unit/ansible/ -v
cd ansible && ansible-playbook -i /dev/null site.yml --syntax-check; cd ..
uv run pytest -q 2>&1 | tail -8
git add ansible/roles/workload_android_app ansible/site.yml \
        src/playground/planner/scheduling.py \
        tests/unit/ansible/test_workload_android_app.py
git commit -m "ansible: add the workload_android_app role

Installs a declared APK (single or split) onto a Redroid guest and
optionally ensures it is running. Idempotent by construction: install is
skipped when pm list packages already shows it, and launch checks pidof
first so a second apply reports changed=0."
```

If `ansible-playbook` reports
`Ansible requires blocking IO on stdin/stdout/stderr`, that is a sandbox
artifact, not a YAML fault — note it and move on.

---

### Task 9: verify-lab asserts declared packages are installed

**Files:**
- Modify: `src/playground/backend/local_libvirt/verify.py`
- Test: `tests/unit/backend/local_libvirt/test_verify_android.py` (create)

**Interfaces:**
- Consumes: `schedule_workloads`, the widened `ResolvedWorkload`.
- Produces: `VmTarget.android_packages: tuple[str, ...]` and a fourth
  per-VM sub-check.

`verify.py` does not call `schedule_workloads` today, so `_build_targets`
must compute the schedule to know which packages belong on which VM.
Severity stays warning-only: a failed app check must not fail the run.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/backend/local_libvirt/test_verify_android.py`:

```python
"""verify-lab checks that declared APKs actually installed."""

from __future__ import annotations

import subprocess

import pytest

from playground.backend.local_libvirt import verify as verify_mod
from playground.backend.local_libvirt.verify import VmTarget, _verify_one


def _target(**kw) -> VmTarget:
    base = dict(
        name="droid1", ip="10.0.0.5", ssh_user="ubuntu",
        has_docker=False, ssh_port=22, android_packages=("com.example.app",),
    )
    base.update(kw)
    return VmTarget(**base)  # type: ignore[arg-type]


def test_missing_package_produces_a_diagnostic(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        command = argv[-1]
        if "pm list packages" in command:
            return subprocess.CompletedProcess(argv, 0, "package:com.other\n", "")
        return subprocess.CompletedProcess(argv, 0, "running", "")

    monkeypatch.setattr(verify_mod.subprocess, "run", fake_run)
    outcome = _verify_one(_target(), [], 30.0)
    assert any("com.example.app" in d.message for d in outcome.diagnostics)


def test_present_package_produces_no_diagnostic(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        command = argv[-1]
        if "pm list packages" in command:
            return subprocess.CompletedProcess(
                argv, 0, "package:com.example.app\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "running", "")

    monkeypatch.setattr(verify_mod.subprocess, "run", fake_run)
    outcome = _verify_one(_target(), [], 30.0)
    assert outcome.diagnostics == []


def test_vm_with_no_declared_packages_skips_the_check(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(argv, **kwargs):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, "running", "")

    monkeypatch.setattr(verify_mod.subprocess, "run", fake_run)
    _verify_one(_target(android_packages=()), [], 30.0)
    assert not any("pm list packages" in c for c in calls)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/backend/local_libvirt/test_verify_android.py -v`
Expected: FAIL — `VmTarget` has no `android_packages` field.

First confirm `_verify_one`'s real signature:
```bash
grep -n "def _verify_one" -A 6 src/playground/backend/local_libvirt/verify.py
```
and match the test call to it.

- [ ] **Step 3: Implement**

Add `android_packages: tuple[str, ...] = ()` to `VmTarget` with a
docstring explaining it comes from scheduled `android_app` workloads.

In `_build_targets`, compute the schedule and derive per-VM packages:

```python
    schedule, _ = schedule_workloads(resolved)
    android_by_vm = {
        vm_name: tuple(
            wl.android.package
            for wl in workloads
            if wl.type == "android_app" and wl.android is not None
        )
        for vm_name, workloads in schedule.items()
    }
```

then pass `android_packages=android_by_vm.get(vm.name, ())` when building
each `VmTarget`.

In `_verify_one`, after the docker sub-check, add:

```python
    if target.android_packages:
        installed = _ssh(
            target,
            "adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true; "
            "adb -s 127.0.0.1:5555 shell pm list packages",
            timeout=timeout,
        )
        for package in target.android_packages:
            if f"package:{package}" not in installed.stdout:
                outcome.log_lines.append(
                    f"[{target.name}] android package {package} NOT installed"
                )
                outcome.diagnostics.append(
                    Diagnostic(
                        id="runtime.apply.verify_failed",
                        severity="error",
                        message=(
                            f"VM {target.name!r}: declared android_app "
                            f"package {package!r} is not installed"
                        ),
                        source=SourceLocation(path=target.ip),
                        suggestion=(
                            "check the workload_android_app role output in "
                            "the ansible log for this host"
                        ),
                    )
                )
            else:
                outcome.log_lines.append(
                    f"[{target.name}] android package {package} installed"
                )
```

Use the same `Diagnostic` id and severity as the existing sub-checks so the
runner's warning-only downgrade still applies.

- [ ] **Step 4: Run, lint, typecheck, commit**

```bash
uv run pytest tests/unit/backend/local_libvirt/ -v
uv run ruff check src tests && uv run mypy src
uv run pytest -q 2>&1 | tail -8
git add src/playground/backend/local_libvirt/verify.py \
        tests/unit/backend/local_libvirt/test_verify_android.py
git commit -m "verify: assert declared android_app packages are installed

Gated like the existing docker check, warning-only so a failed app check
never fails the run. verify.py now computes the workload schedule, which
it previously did not need."
```

---

### Task 10: Live validation on DigitalOcean

**Files:** none — this verifies. Any bug becomes a fix against the task
that owns the file.

**Prerequisites:** `DIGITALOCEAN_TOKEN` exported; `doctl` on PATH.
**This spends real money (~$0.07/hr).** Destroy the lab even if the run
fails.

- [ ] **Step 1: Record the pre-existing inventory**

```bash
export DIGITALOCEAN_ACCESS_TOKEN="$DIGITALOCEAN_TOKEN"
doctl compute droplet list --format ID,Name,Status --no-header
doctl compute firewall list --format ID,Name --no-header
```
Save it. An unrelated `lab-scheduler-scheduler` Droplet and
`lab-scheduler-fw` firewall must still exist at the end. Never destroy
anything you did not create.

- [ ] **Step 2: Apply and confirm the imperative layer**

```bash
uv run playground apply redroid-cloud
uv run playground app list --lab redroid-cloud --third-party
uv run playground app screenshot --lab redroid-cloud --out /tmp/shots
uv run playground app ui-dump --lab redroid-cloud --out /tmp/ui.xml
uv run playground app logcat --lab redroid-cloud --lines 20
```
Expected: exit 0 throughout; a real PNG in `/tmp/shots`; XML in
`/tmp/ui.xml`.

- [ ] **Step 3: Build the test APK by round-tripping one off the device**

```bash
uv run playground app shell --lab redroid-cloud -- pm list packages -3
uv run playground app shell --lab redroid-cloud -- pm path <chosen.package>
```
Pick a NORMAL app, never a system service — reinstalling a system
component can brick the device. Then pull it to the controller with
`playground app pull`, giving a real `.apk` for install testing.

If `pm list packages -3` returns nothing, fall back to a non-`/system`
path from `pm list packages -f`. If no safe candidate exists, say so and
skip the live install test rather than reinstalling a system package.

- [ ] **Step 4: Exercise the imperative install path**

```bash
uv run playground app install <pulled.apk> --lab redroid-cloud
uv run playground app launch <package> --lab redroid-cloud
uv run playground app stop <package> --lab redroid-cloud
uv run playground app clear <package> --lab redroid-cloud
```
Expected: exit 0 each time.

- [ ] **Step 5: Exercise the declarative path**

Create a scratch lab (do NOT commit it) copying `redroid-cloud.yaml` and
adding an `android_app` workload pointing at the pulled APK, with
`launch: true`. Apply it, and confirm the app installs and runs.

- [ ] **Step 6: Prove idempotency — the check that matters most**

```bash
uv run playground apply <scratch-lab>
```
Expected: the Ansible recap reports `changed=0`. **A non-zero `changed`
means `launch` is starting the app every run instead of ensuring it is
running** — that is the failure this design most expects, and it is a bug
in Task 8, not an acceptable result. Name the offending task.

- [ ] **Step 7: Confirm verify-lab caught it**

Check the run's `verify-lab` log for the `android package <pkg> installed`
line.

- [ ] **Step 8: Destroy and confirm no leak**

```bash
uv run playground destroy redroid-cloud
uv run playground destroy <scratch-lab>
sleep 20
doctl compute droplet list --format ID,Name,Status --no-header
doctl compute firewall list --format ID,Name --no-header
```
Expected: identical to Step 1.

- [ ] **Step 9: Record results in the spec and commit**

Append a `## Live validation` section to
`docs/superpowers/specs/2026-08-31-android-app-lifecycle-design.md` with
the real outcome of each step, including anything that failed and how it
was fixed. State results plainly; do not call a step passed unless you ran
it and saw it pass. Note explicitly whether the split-APK
(`install-multiple`) path was exercised live or only unit-tested — spec
open question 3 predicts it may not be coverable.

```bash
git add docs/superpowers/specs/2026-08-31-android-app-lifecycle-design.md
git commit -m "docs: record live validation of the Android app lifecycle"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| 1. Foundation — adb on the guest | 1 |
| 2. Declarative `type: android_app` | 5 (model), 6 (validation), 7 (placement/staging), 8 (role) |
| Artifacts: single / split / bundle rejection | 7 |
| 3. Imperative `playground app`, 11 verbs | 3 (builders), 4 (CLI) |
| Fan-out, `--all` = redroid-capable only | 3 (`resolve_targets`), 4 (dispatch) |
| `logcat --follow` single-device limit | 4 |
| screenshot per-device filenames | 4 |
| 4. One shared ssh argv helper | 2 |
| 5. verify-lab package check | 9 |
| Idempotency (install skip, ensure-running launch) | 8, asserted live in 10 |
| Testing: unit / ansible / CLI / live | 1-9 inline, 10 |
| Risk: adb races a booting Android | 3 (`wait_booted_cmd`), 8 (step 5.3) |
| Risk: split installs are device-aware | 7, and 10 step 9 records live coverage honestly |
| Open Q1: is adb in universe? | **Closed before planning** — see "Known facts" |
| Open Q2: safe round-trip package | 10 step 3 |
| Open Q3: can split install be live-tested? | 10 step 9 |

Out-of-scope items in the spec (network/proxy/capture, `adb forward`/`reverse`,
bundletool, Appium/Maestro) intentionally have no task; `.aab` produces an
explicit rejection diagnostic in Task 7 rather than silence.

**Placeholder scan:** clean. Four steps deliberately require the
implementer to check reality before coding — Task 3 Step 6 (`LabStatus`
fields), Task 4 Step 1 (whether `query_status` resolves `droid1` under the
shims), Task 7 Step 2 (`ResolvedVm` required fields), and Task 9 Step 2
(`_verify_one` signature). Each names the exact command to run and says to
fix the test to match reality rather than bend the code.

**Type consistency:** `AndroidAppOptions` fields (`package`, `launch`,
`activity`, `permissions`, `reinstall`) are spelled identically in Tasks 5,
6, 7, 8, 9. `AndroidTarget` (`vm_name`, `ssh_host`, `ssh_port`, `ssh_user`)
is consistent across Tasks 3 and 4. `build_ssh_argv` / `build_scp_argv`
signatures match between Task 2's definition and their uses in Tasks 2, 3,
4. `ADB_PORT` is defined once in `commands.py`; `main.py` keeps its own
`ADB_REMOTE_PORT` for the pre-existing `adb` verb — these are separate
constants with the same value, which is deliberate: Task 2 must not change
`main.py`'s behavior.
