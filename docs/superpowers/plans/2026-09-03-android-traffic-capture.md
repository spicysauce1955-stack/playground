# Android Device Traffic Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the full network traffic of a Redroid Android device to a `.pcap` artifact, started and stopped by an explicit CLI verb.

**Architecture:** An Ansible `capture` role installs `tcpdump` and an instanced systemd unit on every `redroid-host`. Starting a session runs `nsenter --target <redroid-pid> --net tcpdump -i any`, so `-i any` means exactly one device's traffic; only the network namespace is entered, so the pcap lands on the VM's own disk. `spec.capture` in the lab YAML bounds the capture ring; `playground capture start|stop|status|fetch` drives sessions and lands pcaps under `.playground/runs/<run-id>/artifacts/`.

**Tech Stack:** Python 3.12+ / Pydantic v2 / Typer / pytest, Ansible (community.docker, ansible.posix), tcpdump + nsenter + systemd on Ubuntu Noble guests.

**Spec:** `docs/superpowers/specs/2026-09-03-android-traffic-capture-design.md` — read it first; this plan argues from it.

## Global Constraints

- **No hardcoded secrets.** PRD constraint. Nothing in this slice handles a secret, but do not introduce one (phase 2's CA key is explicitly out of scope).
- **Idempotency is non-negotiable.** The `capture` role must converge to `changed=0` on re-run, and must never start, stop, or restart a capture session. A re-apply during a live capture leaves it running.
- **Air-gap readiness.** `tcpdump` comes from Ubuntu main. Do not add a new Docker image, a new Ansible collection, or a new Python dependency. `spec.capture.enabled: false` must skip provisioning entirely.
- **`tcpdump -C` counts units of 1,000,000 bytes, not MiB.** `max_file_mb: 100` means 100 MB. Do not "fix" this to MiB.
- **`-C` with `-W` is a ring buffer** — it overwrites the oldest file. This is intended; do not remove `-W`.
- **`-Z root` is required.** Debian/Ubuntu `tcpdump` drops privileges to the `tcpdump` user by default and then cannot write to a root-owned directory.
- **AppArmor confines `tcpdump` writes.** `/var/lib/playground/capture/**` needs a rule in `/etc/apparmor.d/local/usr.sbin.tcpdump`.
- **The Redroid container PID is resolved at unit start, never at provision time** (`restart_policy: unless-stopped` changes it across restarts).
- **Backends:** `local-libvirt` and `cloud-digitalocean` only. `local-vbox` declares `redroid: false` and must return `runtime.backend.verb_not_supported`.
- **`LabSpec` is `BaseModel` with `ConfigDict(extra="forbid", frozen=True)`**, not `StrictModel`. New nested models use `StrictModel`.
- Keep `__all__` lists alphabetically sorted in `models/kinds.py` and `models/resolved.py`.
- **Running tests.** This repo has no CI, no `lint`/`typecheck` Make target,
  and no installed venv by convention. Every command is a disposable
  `uv run --no-project`. Throughout this plan, `$PYTEST` means:

  ```bash
  PYTEST='PYTHONPATH=src uv run --no-project --with pytest --with pytest-asyncio \
    --with pydantic --with ruamel.yaml --with jsonschema --with typer --with textual pytest'
  ```

  Lint and type-check (run both before each commit):

  ```bash
  uv run --no-project --with ruff ruff check src tests
  uv run --no-project --with mypy --with pydantic --with ruamel.yaml \
    --with jsonschema --with typer mypy src
  ```

  `mypy` is `strict = true` and `ruff` is `line-length = 100` with
  `select = ["E","F","I","B","UP","W"]`. Note there are 6 pre-existing ruff
  errors in `src/playground/__init__.py` and
  `tests/unit/models/test_kinds.py` — unrelated tech debt, leave them alone
  and do not fold them into these commits.
- **`tests/unit/config/`, `tests/unit/models/`, and `tests/unit/validation/`
  have no `__init__.py`; `tests/unit/android/` and `tests/unit/ansible/` do.**
  New package `tests/unit/capture/` gets one, matching its closest siblings.

---

## The spec's open questions, resolved

The design doc left three open. Answering them here rather than
mid-implementation, because each one changes a file boundary:

1. **Where do the pure command builders live?** A new
   `src/playground/capture/commands.py`, not
   `playground/android/commands.py`. `android/` is about adb; this is
   about `systemctl` and `nsenter`, which are not Android concepts. The
   capture module does import `AndroidTarget` and `run_on_targets` from
   `android/` (Task 3 explains why), so the dependency runs one way only:
   capture → android, never back.

2. **Does `fetch` remove the guest-side pcaps?** No — it leaves them, with
   `--clean` to opt in (Task 7). A failed `scp` must never be able to
   destroy the only copy of a capture.

3. **Does `verify-lab` grow a capture assertion?** **No, and this is
   deliberate — there is no task for it.** `verify-lab` runs on every
   apply, and the only capture state worth asserting is "the unit file is
   loadable", which `systemd-analyze verify` answers without telling you
   anything an operator would act on. Anything stronger means starting a
   capture, and provisioning must never do that (Task 4's
   `test_role_does_not_start_capture`). The role's own probe-and-abort on
   missing `tcpdump`/`nsenter` already fails the apply loudly at the point
   where the problem is fixable.

---

## File Structure

**Created:**

| Path | Responsibility |
| --- | --- |
| `src/playground/capture/__init__.py` | Package marker |
| `src/playground/capture/commands.py` | Pure builders for the guest-side shell strings (`systemctl`, accounting). No I/O. |
| `src/playground/capture/targets.py` | "Which VMs can be captured" — capability filter + `config.capture.*` diagnostics |
| `src/playground/capture/state.py` | Session-record read/write under `.playground/state/capture/<lab>/` |
| `src/playground/cli/capture_commands.py` | The `playground capture` Typer group (4 verbs) |
| `ansible/roles/capture/defaults/main.yml` | Role defaults (limits, paths, install toggle) |
| `ansible/roles/capture/tasks/main.yml` | Package + templates + unit enable |
| `ansible/roles/capture/templates/capture-start.j2` | PID-resolving wrapper that execs `nsenter`+`tcpdump` |
| `ansible/roles/capture/templates/playground-capture@.service.j2` | Instanced systemd unit |
| `ansible/roles/capture/templates/apparmor-local-tcpdump.j2` | AppArmor write rule |
| `tests/unit/capture/test_commands.py` | Command-builder tests |
| `tests/unit/capture/test_targets.py` | Targeting + diagnostic tests |
| `tests/unit/capture/test_state.py` | Session-record round-trip |
| `tests/unit/ansible/test_capture_role.py` | Role-content assertions (the `-Z root` / AppArmor / late-PID guards) |
| `config/labs/redroid-capture.yaml` | Example lab |

**Modified:**

| Path | Change |
| --- | --- |
| `src/playground/models/kinds.py` | `CaptureOptions`; `DefaultsSpec.capture`; `LabSpec.capture`; `__all__` |
| `src/playground/models/resolved.py` | `ResolvedLab.capture`; import; `__all__` |
| `src/playground/config/resolver.py` | Thread `capture` into the `ResolvedLab(...)` call |
| `config/defaults.yaml` | `capture:` block next to `retention:` |
| `config/roles/redroid-host.yaml` | `capture: true` capability + `ansible_role: capture` provisioner |
| `ansible/site.yml` | One play, `hosts: needs_capture`, after the `redroid` play |
| `src/playground/cli/main.py` | `app.add_typer(capture_app, name="capture")` |
| `docs/architecture/CONTRACTS.md` | Capture-layer contract + the two tcpdump gotchas |
| `CLAUDE.md` | Architecture bullet |

---

## Task 1: `spec.capture` schema and resolver threading

**Files:**
- Modify: `src/playground/models/kinds.py` (insert after line 79; `DefaultsSpec` at 97-103; `LabSpec` at 417-442; `__all__` at 522-540)
- Modify: `src/playground/models/resolved.py` (import block lines 10-19; `ResolvedLab` lines 109-137; `__all__` lines 140-149)
- Modify: `src/playground/config/resolver.py` (the `ResolvedLab(...)` call, lines 70-90)
- Modify: `config/defaults.yaml` (append after line 38)
- Test: `tests/unit/models/test_kinds.py`, `tests/unit/config/test_resolver.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `playground.models.kinds.CaptureOptions` with fields `enabled: bool`, `max_file_mb: int`, `max_files: int`, `snaplen: int`; and `ResolvedLab.capture: CaptureOptions` (always populated, never `None`). Every later task reads limits from `resolved.capture`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/models/test_kinds.py`:

```python
def test_capture_options_defaults() -> None:
    from playground.models.kinds import CaptureOptions

    options = CaptureOptions()
    assert options.enabled is True
    assert options.max_file_mb == 100
    assert options.max_files == 10
    assert options.snaplen == 0


def test_capture_options_reject_unknown_field() -> None:
    from playground.models.kinds import CaptureOptions

    with pytest.raises(ValidationError):
        CaptureOptions(max_file_gb=1)


def test_capture_options_reject_zero_files() -> None:
    from playground.models.kinds import CaptureOptions

    with pytest.raises(ValidationError):
        CaptureOptions(max_files=0)


def test_lab_capture_defaults_to_none_at_parse_time() -> None:
    raw = _load_yaml(CONFIG_DIR / "labs" / "generic-infra.yaml")
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    assert lab.spec.capture is None


def test_lab_capture_parses_when_set() -> None:
    raw = _load_yaml(CONFIG_DIR / "labs" / "generic-infra.yaml")
    raw["spec"]["capture"] = {"enabled": False, "max_files": 3}
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    assert lab.spec.capture.enabled is False
    assert lab.spec.capture.max_files == 3
    assert lab.spec.capture.max_file_mb == 100
```

Append to `tests/unit/config/test_resolver.py`:

```python
def test_resolver_capture_falls_back_to_defaults(resolved_generic_infra) -> None:
    lab = resolved_generic_infra
    assert lab.capture.enabled is True
    assert lab.capture.max_file_mb == 100
    assert lab.capture.max_files == 10
    assert lab.capture.snaplen == 0


def test_resolver_capture_respects_lab_override(tmp_path) -> None:
    from textwrap import dedent

    config_dir = tmp_path / "config"
    for sub in ("artifacts", "commands", "labs", "networks", "providers", "roles"):
        (config_dir / sub).mkdir(parents=True, exist_ok=True)
    import shutil as _shutil
    for sub in ("artifacts", "commands", "networks", "providers", "roles"):
        for f in (CONFIG_DIR / sub).iterdir():
            _shutil.copy(f, config_dir / sub / f.name)
    _shutil.copy(CONFIG_DIR / "defaults.yaml", config_dir / "defaults.yaml")
    (config_dir / "labs" / "custom-capture.yaml").write_text(
        dedent(
            """
            apiVersion: playground/v1
            kind: Lab
            metadata:
              name: custom-capture
            spec:
              backend: local-libvirt
              capture:
                enabled: false
                max_file_mb: 25
                max_files: 4
                snaplen: 96
              networks:
                - name: net-a
                  profile: isolated
                  cidr: 10.20.40.0/24
              vms:
                - name: vm-a
                  role: generic-node
                  networks: [net-a]
            """
        ).lstrip("\n")
    )

    loaded, diagnostics = load_config(config_dir)
    assert diagnostics == []
    resolved = resolve_lab(loaded, "custom-capture")
    assert resolved.capture.enabled is False
    assert resolved.capture.max_file_mb == 25
    assert resolved.capture.max_files == 4
    assert resolved.capture.snaplen == 96
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/unit/models/test_kinds.py tests/unit/config/test_resolver.py -k capture -v`
Expected: FAIL — `ImportError: cannot import name 'CaptureOptions'` / `AttributeError` on `lab.spec.capture`.

- [ ] **Step 3: Add `CaptureOptions` to `models/kinds.py`**

Insert after line 79 (the end of `RetentionPolicy`), keeping two blank lines either side:

```python
# ---------------------------------------------------------------------------
# Capture (also used by Defaults.spec.capture)
# ---------------------------------------------------------------------------


class CaptureOptions(StrictModel):
    """Bounds on packet capture for one device, per session.

    ``max_file_mb * max_files`` is a hard ceiling on disk: 1 GB at these
    defaults. It is not optional. pcaps grow without bound, `redroid-host`
    asks for a 60 GB disk, and filling a guest's root filesystem takes
    sshd down and therefore every other verb with it.

    Two properties of that ceiling are deliberate and must not be
    "corrected":

    * ``max_file_mb`` maps to ``tcpdump -C``, which counts units of
      1,000,000 bytes -- not MiB. The field name says ``mb`` and means
      exactly what tcpdump does.
    * ``-C`` with ``-W`` is a ring, so the OLDEST file is overwritten once
      the ceiling is reached. A session that outlives its budget keeps the
      most recent 1 GB and silently discards the beginning. That is better
      than filling the disk, and `capture status` reports the file count so
      a wrapping session is visible.

    ``enabled: false`` is a PROVISIONING switch, not just a CLI gate: the
    `needs_capture` play is skipped, so no package is installed and no unit
    exists. It is the air-gap / lean-guest opt-out.
    """

    enabled: bool = True
    max_file_mb: int = Field(default=100, ge=1)
    max_files: int = Field(default=10, ge=1)
    snaplen: int = Field(default=0, ge=0)
    """``tcpdump -s``. 0 means the whole packet."""
```

- [ ] **Step 4: Wire it into `DefaultsSpec` and `LabSpec`**

In `DefaultsSpec` (line 103, after `retention: RetentionPolicy`) add:

```python
    capture: CaptureOptions = Field(default_factory=CaptureOptions)
```

In `LabSpec` (after line 433, `providers`) add:

```python
    capture: CaptureOptions | None = None
    """Per-lab capture bounds. When unset the resolver falls back to
    ``Defaults.spec.capture``."""
```

Add `"CaptureOptions",` to `__all__` in alphabetical position (before `"CommandBody"`).

- [ ] **Step 5: Add `ResolvedLab.capture`**

In `src/playground/models/resolved.py`, add `CaptureOptions` to the `playground.models.kinds` import (alphabetically, after `Budget`), then add to `ResolvedLab` immediately after `budget: Budget` (line 124):

```python
    capture: CaptureOptions
    """Always populated -- falls back to ``Defaults.spec.capture`` when the
    lab omits ``spec.capture``."""
```

`ResolvedLab` is `extra="forbid"` and frozen, so this must be a real field. Do NOT add `CaptureOptions` to `resolved.py`'s `__all__` — it is re-exported from `kinds`.

- [ ] **Step 6: Thread it through the resolver**

In `src/playground/config/resolver.py`, in the `ResolvedLab(...)` call, immediately after the `budget=` kwarg (line 76):

```python
        capture=lab.spec.capture or defaults.spec.capture,
```

This matches the existing `budget=lab.spec.budget or defaults.spec.budget` idiom. Truthiness is safe here: a Pydantic model instance is always truthy, so `capture: {enabled: false}` is honored rather than silently replaced by the default.

- [ ] **Step 7: Add the defaults block**

Append to `config/defaults.yaml` (after line 38):

```yaml

  capture:
    enabled: true
    # max_file_mb * max_files is a hard 1 GB ceiling per device per
    # session. tcpdump -C counts 1,000,000-byte units, not MiB, and -C
    # with -W is a RING: the oldest file is overwritten, so a long session
    # keeps the most recent 1 GB. See CaptureOptions.
    max_file_mb: 100
    max_files: 10
    snaplen: 0
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `$PYTEST tests/unit/models/ tests/unit/config/ -v`
Expected: PASS, including the pre-existing `test_every_committed_yaml_parses` (it walks `config/` and will parse the new `defaults.yaml` block).

- [ ] **Step 9: Commit**

```bash
git add src/playground/models/kinds.py src/playground/models/resolved.py \
        src/playground/config/resolver.py config/defaults.yaml \
        tests/unit/models/test_kinds.py tests/unit/config/test_resolver.py
git commit -m "feat(capture): add spec.capture schema with bounded pcap ring

CaptureOptions bounds one device's capture to max_file_mb * max_files.
Documents the two tcpdump semantics the field names inherit: -C counts
1,000,000-byte units (not MiB), and -C with -W overwrites the oldest
file rather than stopping."
```

---
## Task 2: Pure guest-command builders

**Files:**
- Create: `src/playground/capture/__init__.py`
- Create: `src/playground/capture/commands.py`
- Test: `tests/unit/capture/__init__.py`, `tests/unit/capture/test_commands.py`

**Interfaces:**
- Consumes: nothing at runtime.
- Produces, and later tasks call exactly these:
  - `UNIT_TEMPLATE: str` = `"playground-capture@"`
  - `CAPTURE_DIR: str` = `"/var/lib/playground/capture"`
  - `unit_name(vm: str) -> str`
  - `remote_capture_dir(vm: str) -> str`
  - `start_cmd(vm: str) -> str`
  - `stop_cmd(vm: str) -> str`
  - `status_cmd(vm: str) -> str`

The house style for this module is `src/playground/android/commands.py`:
every function returns a shell string, nothing does I/O, and every
interpolated value goes through `shlex.quote`. That purity is what makes
the whole guest contract testable without a VM.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/capture/__init__.py` (empty) and
`tests/unit/capture/test_commands.py`:

```python
"""Pure builders for the guest-side capture commands.

No device, no guest, no ssh: these pin the exact strings the CLI sends,
which is where the load-bearing details live (passwordless-sudo
behavior, the unit's instance name, and NOT disabling the unit on stop).
"""

from __future__ import annotations

from playground.capture.commands import (
    CAPTURE_DIR,
    remote_capture_dir,
    start_cmd,
    status_cmd,
    stop_cmd,
    unit_name,
)


def test_unit_name_is_the_systemd_instance() -> None:
    assert unit_name("droid1") == "playground-capture@droid1.service"


def test_remote_capture_dir_is_per_vm() -> None:
    assert remote_capture_dir("droid1") == f"{CAPTURE_DIR}/droid1"


def test_start_uses_non_interactive_sudo() -> None:
    """`ubuntu` needs sudo for systemctl. -n makes a misconfigured
    sudoers fail immediately instead of hanging on a password prompt
    that no one is there to answer -- the ssh call has no tty."""
    cmd = start_cmd("droid1")
    assert "sudo -n systemctl start" in cmd
    assert "playground-capture@droid1.service" in cmd


def test_stop_stops_but_does_not_disable() -> None:
    """Disabling is a provisioning concern. `stop` ends one session; it
    must not change whether the unit would come back."""
    cmd = stop_cmd("droid1")
    assert "sudo -n systemctl stop" in cmd
    assert "disable" not in cmd


def test_status_reports_state_files_and_bytes_in_one_line() -> None:
    cmd = status_cmd("droid1")
    assert "is-active" in cmd
    # A wrapping ring is only visible if the file COUNT is reported, and
    # disk pressure only if bytes are.
    assert "state=" in cmd
    assert "files=" in cmd
    assert "bytes=" in cmd


def test_status_survives_a_missing_capture_directory() -> None:
    """Before the first session the directory does not exist. status must
    report zeros, not a shell error."""
    cmd = status_cmd("droid1")
    assert "2>/dev/null" in cmd


def test_every_builder_quotes_the_vm_name() -> None:
    """VM names are schema-validated, but these strings cross two shells;
    quoting is the invariant, not the current validator."""
    nasty = "a b;rm -rf /"
    for build in (start_cmd, stop_cmd, status_cmd):
        cmd = build(nasty)
        assert "rm -rf /" not in cmd.replace("'a b;rm -rf /'", "")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/unit/capture/test_commands.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'playground.capture'`.

- [ ] **Step 3: Write `src/playground/capture/__init__.py`**

```python
"""Packet capture for Redroid devices, from the host VM's side.

`commands` builds the guest-side shell strings, `targets` answers "which
VMs can be captured", and `state` records the active session per VM. The
CLI surface lives in `playground.cli.capture_commands`.
"""
```

- [ ] **Step 4: Write `src/playground/capture/commands.py`**

```python
"""Pure builders for the guest-side capture commands.

Everything here returns a shell string to be handed to `ssh`. Keeping
them pure means every command shape is unit-testable without a guest,
and the CLI layer stays a thin argument-parsing shell -- the same split
`playground.android.commands` uses for adb.

The heavy lifting is NOT here: entering the Redroid container's network
namespace is the `capture` Ansible role's wrapper script
(`/usr/local/lib/playground/capture-start`), invoked by an instanced
systemd unit. This module only starts, stops, and interrogates that
unit. That division is deliberate -- a capture must outlive the ssh
session that started it, which rules out running tcpdump directly over
ssh.
"""

from __future__ import annotations

import shlex

UNIT_TEMPLATE = "playground-capture@"
"""Instanced unit installed by the `capture` Ansible role. `%i` is the
lab's VM name."""

CAPTURE_DIR = "/var/lib/playground/capture"
"""Guest-side pcap root. Must match `capture_dir` in
`ansible/roles/capture/defaults/main.yml` -- the two are one contract."""


def unit_name(vm: str) -> str:
    return f"{UNIT_TEMPLATE}{vm}.service"


def remote_capture_dir(vm: str) -> str:
    return f"{CAPTURE_DIR}/{vm}"


def start_cmd(vm: str) -> str:
    # -n (non-interactive) rather than a bare `sudo`: the ssh call has no
    # tty, so a sudoers misconfiguration would otherwise hang until the
    # subprocess timeout instead of failing with a readable message.
    return f"sudo -n systemctl start {shlex.quote(unit_name(vm))}"


def stop_cmd(vm: str) -> str:
    # `stop`, never `disable`. Whether the unit is enabled is the
    # provisioning layer's business; this ends one session.
    return f"sudo -n systemctl stop {shlex.quote(unit_name(vm))}"


def status_cmd(vm: str) -> str:
    """One parseable line: unit state, pcap count, total bytes.

    File count is what makes a WRAPPING ring visible (`files=` pinned at
    `max_files` means the oldest data is being overwritten), and bytes is
    what makes disk pressure visible. Both matter enough to be in the
    default output rather than behind a flag.

    Every command is `2>/dev/null`-guarded and `|| true`-terminated
    because none of this exists before the first session: `is-active`
    exits non-zero for a dead unit (which is a normal, reportable state,
    not an error), and the capture directory is absent until the wrapper
    creates it.
    """
    unit = shlex.quote(unit_name(vm))
    directory = shlex.quote(remote_capture_dir(vm))
    return (
        f"state=$(systemctl is-active {unit} 2>/dev/null || true); "
        f"files=$(find {directory} -maxdepth 1 -name '*.pcap' 2>/dev/null | wc -l); "
        f"bytes=$(find {directory} -maxdepth 1 -name '*.pcap' -printf '%s\\n' "
        "2>/dev/null | awk '{t+=$1} END {print t+0}'); "
        'printf "state=%s files=%s bytes=%s\\n" "$state" "$files" "$bytes"'
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$PYTEST tests/unit/capture/test_commands.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add src/playground/capture tests/unit/capture
git commit -m "feat(capture): pure builders for the guest-side capture commands

Starts/stops an instanced systemd unit rather than running tcpdump over
ssh, so a capture outlives the session that started it. sudo -n so a
sudoers misconfiguration fails readably instead of hanging on a prompt
with no tty to answer it."
```

---

## Task 3: Capture targeting and diagnostics

**Files:**
- Create: `src/playground/capture/targets.py`
- Test: `tests/unit/capture/test_targets.py`

**Interfaces:**
- Consumes: `ResolvedLab.capture` (Task 1); `AndroidTarget` and `LabStatus`.
- Produces:
  - `capture_vm_names(resolved: ResolvedLab) -> list[str]`
  - `resolve_capture_targets(resolved: ResolvedLab, status: LabStatus, *, on: str | None, role: str | None, all_devices: bool, user: str) -> tuple[list[AndroidTarget], list[Diagnostic]]`

This reuses `playground.android.targets.AndroidTarget` rather than
declaring a parallel dataclass, so `android.runner.run_on_targets` can
fan out capture commands unchanged. `run_on_targets` is typed
`list[AndroidTarget]` and mypy is strict, so a look-alike dataclass would
force either a second runner or a cast. In this slice every capturable VM
IS an Android device, so the reuse is honest rather than a shortcut.

**Deviation from the spec, deliberate:** the design said `local-vbox`
should be rejected with the existing `runtime.backend.verb_not_supported`.
On reading `dispatch.py:64-83`, that helper's message and suggestion are
hardcoded to suspend/resume billing framing ("only cloud backends do",
"charge for idle compute"), which would be actively misleading here. This
task adds `config.capture.backend_unsupported` instead. Same outcome, and
the operator gets a sentence that is true.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/capture/test_targets.py`:

```python
"""Which VMs can be captured, and how the refusals read.

Mirrors tests/unit/android/test_targets.py: build a ResolvedLab from the
committed config, synthesize a LabStatus, and assert on diagnostic ids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.capture.targets import capture_vm_names, resolve_capture_targets
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.models.status import LabStatus, VmStatus

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def redroid_lab():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "redroid-cloud")


@pytest.fixture
def generic_lab():
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return resolve_lab(loaded, "generic-infra")


def _status(resolved, *, reachable: bool = True) -> LabStatus:
    return LabStatus(
        lab=resolved.lab_name,
        backend=resolved.backend,
        expected_vms=len(resolved.vms),
        provisioned_vms=len(resolved.vms),
        vms=[
            VmStatus(
                name=vm.name,
                role=vm.role,
                state="running",
                ssh_host="127.0.0.1" if reachable else "",
                ssh_port=22,
            )
            for vm in resolved.vms
        ],
    )


def test_capture_vm_names_follows_the_capability(redroid_lab) -> None:
    assert capture_vm_names(redroid_lab) == ["droid1"]


def test_a_lab_with_no_capturable_vm_is_a_clean_error(generic_lab) -> None:
    targets, diagnostics = resolve_capture_targets(
        generic_lab, _status(generic_lab),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.capture.no_capture_vms"]


def test_single_device_needs_no_targeting_flag(redroid_lab) -> None:
    targets, diagnostics = resolve_capture_targets(
        redroid_lab, _status(redroid_lab),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert diagnostics == []
    assert [t.vm_name for t in targets] == ["droid1"]
    assert targets[0].ssh_user == "ubuntu"


def test_unknown_vm_names_the_candidates(redroid_lab) -> None:
    targets, diagnostics = resolve_capture_targets(
        redroid_lab, _status(redroid_lab),
        on="nope", role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.capture.unknown_vm"]
    assert "droid1" in diagnostics[0].message


def test_capture_disabled_in_the_lab_is_refused(redroid_lab) -> None:
    """enabled: false skips provisioning entirely, so there is no unit to
    start. Say that, rather than failing later on a missing unit."""
    disabled = redroid_lab.model_copy(
        update={"capture": redroid_lab.capture.model_copy(update={"enabled": False})}
    )
    targets, diagnostics = resolve_capture_targets(
        disabled, _status(disabled),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.capture.disabled"]


def test_vbox_is_refused_with_capture_specific_wording(redroid_lab) -> None:
    """Redroid is unverified on local-vbox, so there is no device to
    capture. The message must not talk about idle-compute billing, which
    is what the shared verb_not_supported helper says."""
    vbox = redroid_lab.model_copy(update={"backend": "local-vbox"})
    targets, diagnostics = resolve_capture_targets(
        vbox, _status(vbox),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.capture.backend_unsupported"]
    assert "billing" not in diagnostics[0].message.lower()


def test_unreachable_vm_says_apply_first(redroid_lab) -> None:
    targets, diagnostics = resolve_capture_targets(
        redroid_lab, _status(redroid_lab, reachable=False),
        on=None, role=None, all_devices=False, user="ubuntu",
    )
    assert targets == []
    assert [d.id for d in diagnostics] == ["config.capture.vm_not_reachable"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/unit/capture/test_targets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'playground.capture.targets'`.

- [ ] **Step 3: Write `src/playground/capture/targets.py`**

```python
"""Resolve `playground capture` targeting flags to concrete devices.

"Which VMs can be captured?" is answered from resolved role
capabilities: a VM is capturable when its role chain declares
`capture: true` (config/roles/redroid-host.yaml). Same mechanism
`playground.android.targets` uses for `redroid`.

Returns `AndroidTarget` rather than a look-alike of its own so
`playground.android.runner.run_on_targets` fans capture commands out
unchanged -- that runner is typed `list[AndroidTarget]`, and in this
slice every capturable VM is an Android device.
"""

from __future__ import annotations

from playground.android.targets import AndroidTarget
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.models.status import LabStatus

_CAPTURE_BACKENDS = ("local-libvirt", "cloud-digitalocean")
"""`local-vbox` declares `redroid: false` in its ProviderConfig -- Redroid
is unverified there, so there is no device whose traffic could be
captured."""


def capture_vm_names(resolved: ResolvedLab) -> list[str]:
    """VMs whose role declares the `capture` capability, in lab order."""
    return [vm.name for vm in resolved.vms if vm.capabilities.get("capture")]


def resolve_capture_targets(
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

    if resolved.backend not in _CAPTURE_BACKENDS:
        return [], [
            Diagnostic(
                id="config.capture.backend_unsupported",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} uses backend "
                    f"{resolved.backend!r}, which has no Redroid device to "
                    "capture"
                ),
                source=source,
                key_path="spec.backend",
                suggestion=(
                    "capture is available on local-libvirt and "
                    "cloud-digitalocean; local-vbox declares redroid: false"
                ),
            )
        ]

    if not resolved.capture.enabled:
        return [], [
            Diagnostic(
                id="config.capture.disabled",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} sets spec.capture.enabled: "
                    "false, so no capture tooling was installed"
                ),
                source=source,
                key_path="spec.capture.enabled",
                suggestion=(
                    "set spec.capture.enabled: true and re-run "
                    f"`playground apply {resolved.lab_name}`"
                ),
            )
        ]

    capturable = capture_vm_names(resolved)
    if not capturable:
        return [], [
            Diagnostic(
                id="config.capture.no_capture_vms",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} has no VM with the `capture` "
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
        if on not in capturable:
            return [], [
                Diagnostic(
                    id="config.capture.unknown_vm",
                    severity="error",
                    message=(
                        f"VM {on!r} is not a capturable device in lab "
                        f"{resolved.lab_name!r} (devices: {capturable})"
                    ),
                    source=source,
                    key_path="spec.vms",
                )
            ]
        wanted = [on]
    elif role is not None:
        wanted = [
            vm.name for vm in resolved.vms
            if role in vm.roles and vm.capabilities.get("capture")
        ]
        if not wanted:
            return [], [
                Diagnostic(
                    id="config.capture.unknown_vm",
                    severity="error",
                    message=(
                        f"no capturable device in lab {resolved.lab_name!r} "
                        f"has role {role!r}"
                    ),
                    source=source,
                )
            ]
    elif all_devices or len(capturable) == 1:
        wanted = capturable
    else:
        return [], [
            Diagnostic(
                id="config.capture.target_required",
                severity="error",
                message=(
                    f"lab {resolved.lab_name!r} has {len(capturable)} "
                    "capturable devices; pass --on, --role, or --all"
                ),
                source=source,
                suggestion=f"devices: {', '.join(capturable)}",
            )
        ]

    by_name = {vm.name: vm for vm in status.vms}
    targets: list[AndroidTarget] = []
    diagnostics: list[Diagnostic] = []
    for name in wanted:
        vm_status = by_name.get(name)
        if vm_status is None or not vm_status.ssh_host:
            diagnostics.append(
                Diagnostic(
                    id="config.capture.vm_not_reachable",
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
                ssh_host=vm_status.ssh_host,
                ssh_port=vm_status.ssh_port or 22,
                ssh_user=user,
            )
        )
    return targets, diagnostics
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/unit/capture/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/playground/capture/targets.py tests/unit/capture/test_targets.py
git commit -m "feat(capture): capability-based targeting with capture-specific diagnostics

Adds config.capture.backend_unsupported rather than reusing
runtime.backend.verb_not_supported, whose message and suggestion are
hardcoded to suspend/resume billing framing and would misinform here."
```

---

## Task 4: The `capture` Ansible role

**Files:**
- Create: `ansible/roles/capture/defaults/main.yml`
- Create: `ansible/roles/capture/tasks/main.yml`
- Create: `ansible/roles/capture/templates/capture-start.j2`
- Create: `ansible/roles/capture/templates/playground-capture@.service.j2`
- Create: `ansible/roles/capture/templates/apparmor-local-tcpdump.j2`
- Modify: `ansible/site.yml` (insert a play after line 53, `    - redroid`)
- Modify: `config/roles/redroid-host.yaml`
- Modify: `src/playground/backend/local_libvirt/inventory.py` (the `[playground:vars]` block at lines 334-335)
- Test: `tests/unit/ansible/test_capture_role.py`
- Test: `tests/unit/backend/local_libvirt/test_inventory.py` (append)

**Interfaces:**
- Consumes: `ResolvedLab.capture` (Task 1).
- Produces: the guest contract every CLI verb depends on — unit name
  `playground-capture@<vm>.service`, pcap directory
  `/var/lib/playground/capture/<vm>/`. Tasks 2, 6, and 7 hardcode nothing
  else about the guest.

House style this role must follow (enforced only by pytest — the repo has
no ansible-lint and no `.yamllint`):

- No `handlers/` anywhere in this repo. Change propagation is
  `register:` + `when: x.changed`.
- `daemon_reload: yes` inline on the `systemd` module, not a separate task.
- Module booleans are `yes`/`no`, not `true`/`false`. `mode:` is always a
  quoted string.
- Every template starts `# Managed by the playground <role> Ansible role.`
- Every `apt` task sets `failed_when: false` and is gated by a real probe
  that `fail`s with a message naming the opt-out variable. This is the
  convention `tests/unit/ansible/test_redroid_adb.py` enforces on the
  redroid role; mirror it here.
- New collections are forbidden: use `ansible.builtin` only. Do NOT add to
  `ansible/requirements.yml`.

- [ ] **Step 1: Write the failing role tests**

Create `tests/unit/ansible/test_capture_role.py`:

```python
"""The capture role's guest contract, and the three defaults that break it.

Every assertion here encodes a failure that is invisible without a live
guest, which is why they are pinned statically instead:

* `tcpdump` on Debian/Ubuntu drops privileges to the `tcpdump` user by
  default and then cannot write to a root-owned directory (`-Z root`).
* Ubuntu's AppArmor profile confines where `tcpdump` may write, and
  /var/lib/playground is outside it.
* The Redroid container's PID changes across restarts
  (`restart_policy: unless-stopped`), so it must be resolved when the
  unit starts -- never baked in at provision time.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[3]
ROLE = REPO_ROOT / "ansible" / "roles" / "capture"
TASKS = ROLE / "tasks" / "main.yml"
DEFAULTS = ROLE / "defaults" / "main.yml"
WRAPPER = ROLE / "templates" / "capture-start.j2"
UNIT = ROLE / "templates" / "playground-capture@.service.j2"
APPARMOR = ROLE / "templates" / "apparmor-local-tcpdump.j2"

_yaml = YAML(typ="safe")


def test_tcpdump_drops_no_privileges() -> None:
    """Without -Z root, tcpdump setuid()s to the `tcpdump` user and then
    cannot write into a root-owned capture directory."""
    assert "-Z root" in WRAPPER.read_text()


def test_apparmor_override_grants_the_capture_path() -> None:
    """Ubuntu's usr.sbin.tcpdump profile confines writes. The shipped
    profile includes <local/usr.sbin.tcpdump>, so a local override is the
    supported way to widen it."""
    text = APPARMOR.read_text()
    assert "/var/lib/playground/capture/" in text
    tasks = _yaml.load(TASKS.read_text())
    dests = [
        str(t.get("ansible.builtin.template", {}).get("dest", "")) for t in tasks
    ]
    assert "/etc/apparmor.d/local/usr.sbin.tcpdump" in dests
    assert "apparmor_parser" in TASKS.read_text(), "the override must be reloaded"


def test_container_pid_is_resolved_at_run_time_not_provision_time() -> None:
    """A PID baked into the unit is stale after the first container
    restart. The wrapper must resolve it itself, and the unit must call
    the wrapper rather than tcpdump directly."""
    wrapper = WRAPPER.read_text()
    assert "docker inspect" in wrapper
    assert "State.Pid" in wrapper
    unit = UNIT.read_text()
    assert "capture-start" in unit
    assert "nsenter" not in unit, "nsenter belongs in the wrapper, not the unit"


def test_wrapper_fails_loudly_on_ambiguous_or_absent_container() -> None:
    """Discovery, not a duplicated naming convention. If it finds zero or
    more than one candidate it must exit nonzero with a message, never
    capture the wrong namespace."""
    wrapper = WRAPPER.read_text()
    assert "exit 1" in wrapper
    for token in ("no redroid container", "more than one"):
        assert token in wrapper.lower()


def test_unit_is_instanced_and_restarts() -> None:
    unit = UNIT.read_text()
    assert "%i" in unit, "must be an instanced unit (one session per VM)"
    assert "After=docker.service" in unit
    assert "Restart=always" in unit


def test_role_does_not_start_capture() -> None:
    """A re-apply must never begin recording, and must never interrupt a
    session already in progress. The unit is enabled-but-stopped."""
    tasks = _yaml.load(TASKS.read_text())
    for task in tasks:
        systemd = task.get("ansible.builtin.systemd", {})
        state = str(systemd.get("state", ""))
        assert state not in ("started", "restarted"), (
            f"task {task['name']!r} sets state={state!r}; provisioning must "
            "not start or restart a capture session"
        )


def test_every_apt_task_tolerates_an_unreachable_archive() -> None:
    tasks = _yaml.load(TASKS.read_text())
    apt_tasks = [t for t in tasks if "ansible.builtin.apt" in t]
    assert apt_tasks, "expected an apt task installing tcpdump"
    for t in apt_tasks:
        assert t.get("failed_when") is False, (
            f"apt task {t['name']!r} can abort the play on an unreachable "
            "archive; gate on a probe instead"
        )


def test_a_probe_gates_on_tcpdump_actually_being_present() -> None:
    text = TASKS.read_text()
    assert "tcpdump --version" in text
    fails = [t for t in _yaml.load(text) if "ansible.builtin.fail" in t]
    assert fails, "expected an abort when tcpdump is missing"
    assert any(
        "capture_install_tcpdump" in str(f["ansible.builtin.fail"]["msg"])
        for f in fails
    ), "the abort must name the opt-out variable"


def test_defaults_declare_the_opt_out_and_paths() -> None:
    defaults = _yaml.load(DEFAULTS.read_text())
    assert defaults["capture_install_tcpdump"] is True
    assert defaults["capture_dir"] == "/var/lib/playground/capture"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/unit/ansible/test_capture_role.py -v`
Expected: FAIL — every test errors with `FileNotFoundError` (the role does not exist).

- [ ] **Step 3: Write `ansible/roles/capture/defaults/main.yml`**

```yaml
---
# Packet capture for a Redroid device, from the host VM's side.
#
# Limits arrive from the lab as the `pg_capture` inventory var (see
# src/playground/backend/local_libvirt/inventory.py); these are the
# fallbacks used when a lab predates that var or capture is driven by
# hand. Keep them in step with CaptureOptions' defaults.

capture_dir: /var/lib/playground/capture

# Ubuntu ships tcpdump in main. Set false for an air-gapped host whose
# package source lacks it -- the role then probes and aborts rather than
# reaching for apt.
capture_install_tcpdump: true

# tcpdump -C counts units of 1,000,000 bytes, NOT MiB. -C with -W is a
# ring: the oldest file is overwritten once max_files is reached.
capture_max_file_mb: 100
capture_max_files: 10
capture_snaplen: 0

# Container-name prefix used to DISCOVER the device's netns. Deliberately
# a prefix match rather than a copy of the redroid role's
# `redroid_{{ inventory_hostname | regex_replace(...) }}` expression --
# duplicating that expression across two roles is exactly the
# "implicit cross-layer dependency hidden by a hardcoded value" shape
# docs/architecture/CONTRACTS.md warns about. The wrapper fails loudly if
# the prefix matches zero or several containers.
capture_container_prefix: redroid_

# Wrapper the instanced unit execs. Referenced by the unit template and
# by tasks/main.yml.
capture_bin: /usr/local/lib/playground/capture-start

# Lab-level bounds, injected by `playground inventory render` as a group
# var under [playground:vars]. Defaulted so a hand-run play without the
# var is a no-op rather than an undefined-variable failure -- same reason
# workload_container defaults pg_workloads: '[]'.
pg_capture: '{}'
```

- [ ] **Step 4: Write `ansible/roles/capture/templates/capture-start.j2`**

```jinja
#!/bin/sh
# Managed by the playground capture Ansible role.
#
# Resolve the Redroid container's network namespace and run tcpdump
# inside it. Only --net is entered: the mount namespace stays the
# guest's, so tcpdump is the guest's binary and the pcap lands on the
# VM's disk rather than inside a container whose writes vanish when it
# is recreated.
#
# The PID is resolved HERE, at every start, not baked in by Ansible: the
# Redroid container runs `restart_policy: unless-stopped`, so its PID
# changes across restarts and a provision-time PID goes stale.
set -eu

vm="$1"
dir="{{ capture_dir }}/${vm}"

ids=$(docker ps --filter "name=^{{ capture_container_prefix }}" --format '{% raw %}{{.ID}}{% endraw %}')
count=$(printf '%s\n' "$ids" | grep -c . || true)

if [ "$count" -eq 0 ]; then
    echo "capture: no redroid container matching '{{ capture_container_prefix }}*' on $(hostname); nothing to capture" >&2
    exit 1
fi
if [ "$count" -gt 1 ]; then
    echo "capture: found more than one container matching '{{ capture_container_prefix }}*' on $(hostname); refusing to guess which namespace to capture" >&2
    exit 1
fi

pid=$(docker inspect -f '{% raw %}{{.State.Pid}}{% endraw %}' "$ids")
if [ -z "$pid" ] || [ "$pid" = "0" ]; then
    echo "capture: redroid container $ids has no running PID; is it up?" >&2
    exit 1
fi

mkdir -p "$dir"

# -Z root: Debian/Ubuntu tcpdump drops privileges to the `tcpdump` user
#   by default and then cannot write into this root-owned directory.
# -i any: inside the container's netns this is EXACTLY the device's
#   traffic. Note it yields the Linux "cooked" (SLL) link type, not
#   Ethernet -- Wireshark and tshark read it natively.
# -U: pack-per-write, so a pcap is readable while the session is live.
# -C/-W: a bounded ring. The OLDEST file is overwritten once -W is hit.
exec nsenter --target "$pid" --net \
    tcpdump -i any -U -Z root \
        -s "{{ capture_snaplen }}" \
        -w "${dir}/%Y%m%d-%H%M%S.pcap" \
        -C "{{ capture_max_file_mb }}" \
        -W "{{ capture_max_files }}"
```

- [ ] **Step 5: Write `ansible/roles/capture/templates/playground-capture@.service.j2`**

```jinja
# Managed by the playground capture Ansible role.
#
# Instanced unit: %i is the lab's VM name, so `systemctl start
# playground-capture@droid1` captures droid1's device.
#
# Restart=always is load-bearing. If the Redroid container restarts it
# takes its network namespace with it and tcpdump dies; without a restart
# the capture would end silently in the middle of an experiment. The
# wrapper re-resolves the new PID on each start. Timestamped filenames
# make the discontinuity visible rather than hiding it.
[Unit]
Description=playground packet capture for Redroid device %i
After=docker.service
Requires=docker.service

[Service]
Type=exec
ExecStart={{ capture_bin }} %i
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 6: Write `ansible/roles/capture/templates/apparmor-local-tcpdump.j2`**

```jinja
# Managed by the playground capture Ansible role.
#
# Ubuntu's /etc/apparmor.d/usr.sbin.tcpdump confines where tcpdump may
# write, and {{ capture_dir }} is outside it: without this rule the
# capture fails as "Permission denied" on a directory that looks
# correctly root-owned, which is a genuinely confusing way to lose an
# afternoon. The shipped profile ends with
# `#include <local/usr.sbin.tcpdump>`, so widening it here is the
# supported route -- do not disable confinement instead.
{{ capture_dir }}/ rw,
{{ capture_dir }}/** rw,
```

- [ ] **Step 7: Write `ansible/roles/capture/tasks/main.yml`**

```yaml
---
# Idempotent: package present, three templated files unchanged, unit
# enabled but STOPPED. A converged host reports changed=0.
#
# This role must never start, stop, or restart a capture session --
# `playground apply` on a lab that is mid-capture leaves the recording
# untouched. Sessions are started only by `playground capture start`.

- name: Install tcpdump
  ansible.builtin.apt:
    name: tcpdump
    state: present
    update_cache: yes
    cache_valid_time: 3600
  when: capture_install_tcpdump | bool
  # Tolerated for the same reason every apt task in the redroid role is:
  # an unreachable archive must not abort the play, and `update_cache`
  # fails before package state is even examined -- so this would fail
  # even on a host where tcpdump is ALREADY installed. The probe below is
  # the honest gate.
  failed_when: false

- name: Confirm tcpdump is on the guest
  ansible.builtin.command: tcpdump --version
  register: tcpdump_present
  changed_when: false
  failed_when: false

- name: Abort when tcpdump is missing (capture needs it)
  ansible.builtin.fail:
    msg: |
      Host '{{ inventory_hostname }}' has no working `tcpdump` on PATH, so
      `playground capture start` would fail later with a bare exit code
      and no message.

      capture_install_tcpdump is currently
      {{ capture_install_tcpdump | default(true) }}.

      To resolve:
        - Leave capture_install_tcpdump: true and make the apt archive
          reachable (Ubuntu ships tcpdump in main), OR
        - Install tcpdump by another route if this host is air-gapped, OR
        - Set `capture: {enabled: false}` in the lab's spec, which drops
          this host from the capture play entirely.
  when: tcpdump_present.rc != 0

- name: Confirm nsenter is present (util-linux; needed to enter the device netns)
  ansible.builtin.command: nsenter --version
  register: nsenter_present
  changed_when: false
  failed_when: false

- name: Abort when nsenter is missing
  ansible.builtin.fail:
    msg: |
      Host '{{ inventory_hostname }}' has no `nsenter` (normally shipped by
      util-linux on every Ubuntu image). Capture enters the Redroid
      container's network namespace with it; without it there is no way to
      aim tcpdump at exactly one device.
  when: nsenter_present.rc != 0

- name: Create the capture directory
  ansible.builtin.file:
    path: "{{ capture_dir }}"
    state: directory
    owner: root
    group: root
    mode: "0750"

- name: Create the playground lib directory
  ansible.builtin.file:
    path: "{{ capture_bin | dirname }}"
    state: directory
    owner: root
    group: root
    mode: "0755"

- name: Install the capture wrapper (resolves the container PID at run time)
  ansible.builtin.template:
    src: capture-start.j2
    dest: "{{ capture_bin }}"
    owner: root
    group: root
    mode: "0755"

- name: Widen the tcpdump AppArmor profile for the capture directory
  ansible.builtin.template:
    src: apparmor-local-tcpdump.j2
    dest: /etc/apparmor.d/local/usr.sbin.tcpdump
    owner: root
    group: root
    mode: "0644"
  register: apparmor_local

- name: Reload the tcpdump AppArmor profile
  ansible.builtin.command: apparmor_parser -r /etc/apparmor.d/usr.sbin.tcpdump
  when: apparmor_local.changed
  changed_when: apparmor_local.changed
  # A host with AppArmor disabled has no profile to reload; that is not a
  # capture failure, and the wrapper's own write is the real gate.
  failed_when: false

- name: Install the instanced capture unit
  ansible.builtin.template:
    src: playground-capture@.service.j2
    dest: /etc/systemd/system/playground-capture@.service
    owner: root
    group: root
    mode: "0644"
  register: capture_unit

- name: Reload systemd when the unit definition changed
  ansible.builtin.systemd:
    daemon_reload: yes
  when: capture_unit.changed
```

Note there is deliberately no `enabled: yes`/`state: started` task: an
instanced unit is enabled per-instance, and enabling an instance would
start capture on boot. `playground capture start` is the only thing that
starts a session.

- [ ] **Step 8: Add the `site.yml` play**

Insert after line 53 (`    - redroid`), leaving one blank line either side:

```yaml
- name: Install packet-capture tooling
  hosts: needs_capture
  become: yes
  gather_facts: no
  roles:
    # Opt-in via the VmRole provisioner list, like redroid above. Runs
    # AFTER redroid because the wrapper it installs discovers a running
    # Redroid container. Installs tooling only -- it never starts a
    # capture session, so a re-apply cannot disturb one in progress.
    - capture
```

- [ ] **Step 9: Give `redroid-host` the capability and provisioner**

In `config/roles/redroid-host.yaml`, add `capture: true` under
`capabilities:` and `- ansible_role: capture` as the last entry of
`provisioners:`. The resolver list-REPLACES `provisioners` on `extends`,
which is why all three must be listed:

```yaml
  capabilities:
    docker: true
    compose: true
    swarm: true
    redroid: true
    capture: true
  provisioners:
    # The resolver list-replaces provisioners on `extends`, so we
    # re-list docker explicitly. Order matters: docker first (redroid
    # talks to dockerd), redroid second, capture last (its wrapper
    # discovers a running Redroid container).
    - ansible_role: docker
    - ansible_role: redroid
    - ansible_role: capture
```

`_provisioner_group()` derives `[needs_capture]` from this with no
renderer change.

- [ ] **Step 10: Carry the lab's limits into the inventory**

`ResolvedVm.capabilities` is NOT emitted into the inventory today, and
there is no existing path for lab-level settings other than the
`[playground:vars]` block. Add `pg_capture` there, next to `pg_lab`
(inventory.py lines 334-335):

```python
        "[playground:vars]",
        f"pg_lab={resolved.lab_name}",
        # Lab-level capture bounds for the `capture` role. A group var,
        # not a host var: spec.capture is lab-scoped, so every capturable
        # VM in the lab shares it.
        f"pg_capture='{_capture_group_var(resolved)}'",
```

with the helper, mirroring the `pg_extra_hosts` JSON + shell-escape idiom:

```python
def _capture_group_var(resolved: ResolvedLab) -> str:
    """JSON-encode the lab's capture bounds for the `capture` role.

    Same escape as `pg_workloads` / `pg_extra_hosts`: an embedded single
    quote would end the `.ini` value early.
    """
    payload = json.dumps(
        resolved.capture.model_dump(), separators=(",", ":"), sort_keys=True
    )
    return payload.replace("'", "'\\''")
```

Then map it onto the role's variables in `ansible/roles/capture/tasks/main.yml`,
as the FIRST tasks in the file, using the BUG-6 guard every other
JSON-hostvar consumer opens with (Ansible may hand a single-quoted `.ini`
value back already-decoded, and `from_json` only accepts a string):

```yaml
- name: Parse pg_capture JSON payload
  ansible.builtin.set_fact:
    # May arrive as a JSON STRING, or as an already-decoded DICT when
    # Ansible auto-parses the single-quoted .ini value. `from_json` only
    # accepts a string, so guard it (BUG-6): parse a string, pass a dict
    # through unchanged.
    pg_capture_parsed: >-
      {{ pg_capture if (pg_capture is not string)
         else (pg_capture | from_json) }}
  when: pg_capture is defined

- name: Apply the lab's capture bounds over the role defaults
  ansible.builtin.set_fact:
    capture_max_file_mb: "{{ _pgc.max_file_mb | default(capture_max_file_mb) }}"
    capture_max_files: "{{ _pgc.max_files | default(capture_max_files) }}"
    capture_snaplen: "{{ _pgc.snaplen | default(capture_snaplen) }}"
  vars:
    _pgc: "{{ pg_capture_parsed | default({}) }}"

- name: Skip the host entirely when the lab disabled capture
  ansible.builtin.meta: end_host
  when: not ((pg_capture_parsed | default({})).get('enabled', true) | bool)
```

Both `pg_capture` and `capture_bin` are already declared in Step 3's
`defaults/main.yml`.

- [ ] **Step 11: Add the inventory test**

Append to `tests/unit/backend/local_libvirt/test_inventory.py`:

```python
def test_render_inventory_emits_capture_bounds_as_a_group_var(
    resolved_generic_infra, lab_ips: dict[str, str]
) -> None:
    body, diagnostics = render_inventory(resolved_generic_infra, lab_ips)

    assert diagnostics == []
    assert "pg_capture=" in body
    # A group var under [playground:vars], not a per-host var: spec.capture
    # is lab-scoped.
    vars_block = body.split("[playground:vars]", 1)[1]
    assert "pg_capture=" in vars_block
    assert '"max_file_mb":100' in vars_block
    assert '"max_files":10' in vars_block
```

- [ ] **Step 12: Run the tests to verify they pass**

Run: `$PYTEST tests/unit/ansible/ tests/unit/backend/local_libvirt/test_inventory.py -v`
Expected: PASS. Also re-run `$PYTEST tests/unit/models tests/unit/config` to confirm the `redroid-host.yaml` edit still parses.

- [ ] **Step 13: Commit**

```bash
git add ansible/roles/capture ansible/site.yml config/roles/redroid-host.yaml \
        src/playground/backend/local_libvirt/inventory.py \
        tests/unit/ansible/test_capture_role.py \
        tests/unit/backend/local_libvirt/test_inventory.py
git commit -m "feat(capture): capture role, instanced unit, and needs_capture play

Captures in the Redroid container's own netns via nsenter, resolving the
container PID at unit start (restart_policy: unless-stopped changes it).
Encodes the two Ubuntu defaults that break a fresh guest: tcpdump's
privilege drop (-Z root) and AppArmor write confinement.

Provisioning never starts a session, so a re-apply cannot disturb a
capture in progress."
```

---

## Task 5: Session-record state store

**Files:**
- Create: `src/playground/capture/state.py`
- Test: `tests/unit/capture/test_state.py`

**Interfaces:**
- Consumes: `CaptureOptions` (Task 1) for the recorded limits. Imports
  nothing from `commands` — the unit name is stored as a plain string,
  so this task can be built in parallel with Task 2.
- Produces:
  - `CaptureSession` (StrictModel: `vm`, `unit`, `started_at`, `max_file_mb`, `max_files`, `snaplen`)
  - `session_path(state_dir: Path, lab: str, vm: str) -> Path`
  - `write_session(state_dir: Path, lab: str, session: CaptureSession) -> Path`
  - `read_session(state_dir: Path, lab: str, vm: str) -> CaptureSession | None`
  - `clear_session(state_dir: Path, lab: str, vm: str) -> None`

`.playground/state/` currently holds exactly five subdirectories —
`tofu/`, `inventory/`, `workloads/`, `vbox/`, `cloud-digitalocean/`. Per-lab
*files* are `<subdir>/<lab>.<ext>`; per-lab *directories* are
`<subdir>/<lab>/`. Capture needs one record per VM, so it follows the
directory precedent: `state/capture/<lab>/<vm>.json`.

Note `src/playground/state/__init__.py` is a 5-line docstring promising a
`StateStore` that does not exist; every state path in the repo is built ad
hoc at the call site. Do **not** build `StateStore` here — that is a
refactor of nine other call sites and is not this slice's job. Follow the
existing convention.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/capture/test_state.py`:

```python
"""Session records under .playground/state/capture/<lab>/<vm>.json."""

from __future__ import annotations

from pathlib import Path

from playground.capture.state import (
    CaptureSession,
    clear_session,
    read_session,
    session_path,
    write_session,
)


def _session() -> CaptureSession:
    return CaptureSession(
        vm="droid1",
        unit="playground-capture@droid1.service",
        started_at="2026-09-03T12:00:00+00:00",
        max_file_mb=100,
        max_files=10,
        snaplen=0,
    )


def test_session_path_follows_the_per_lab_directory_precedent(tmp_path: Path) -> None:
    path = session_path(tmp_path, "redroid-cloud", "droid1")
    assert path == tmp_path / "state" / "capture" / "redroid-cloud" / "droid1.json"


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    write_session(tmp_path, "redroid-cloud", _session())
    loaded = read_session(tmp_path, "redroid-cloud", "droid1")
    assert loaded == _session()


def test_write_creates_missing_parents(tmp_path: Path) -> None:
    written = write_session(tmp_path, "redroid-cloud", _session())
    assert written.is_file()


def test_read_of_an_absent_session_is_none_not_an_error(tmp_path: Path) -> None:
    assert read_session(tmp_path, "redroid-cloud", "droid1") is None


def test_read_of_a_corrupt_record_is_none(tmp_path: Path) -> None:
    """A truncated write must not wedge `capture start` forever -- the
    operator can always start a new session over a broken record."""
    path = session_path(tmp_path, "redroid-cloud", "droid1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert read_session(tmp_path, "redroid-cloud", "droid1") is None


def test_clear_is_idempotent(tmp_path: Path) -> None:
    write_session(tmp_path, "redroid-cloud", _session())
    clear_session(tmp_path, "redroid-cloud", "droid1")
    clear_session(tmp_path, "redroid-cloud", "droid1")
    assert read_session(tmp_path, "redroid-cloud", "droid1") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/unit/capture/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'playground.capture.state'`.

- [ ] **Step 3: Write `src/playground/capture/state.py`**

```python
"""Per-VM capture session records under `.playground/state/capture/`.

Layout: ``<state_dir>/state/capture/<lab>/<vm>.json``. `.playground/state/`
holds per-lab FILES as ``<subdir>/<lab>.<ext>`` and per-lab DIRECTORIES as
``<subdir>/<lab>/``; capture needs one record per VM, so it takes the
directory form.

Paths are built here rather than through `playground.state`, whose
module docstring promises a `StateStore` that was never written -- every
other state path in the repo is likewise built at its call site.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from playground.models.base import StrictModel


class CaptureSession(StrictModel):
    """One capture session, as recorded on the operator's machine.

    The limits are copied in rather than re-read from the lab at
    `stop`/`fetch` time: they describe the pcaps that were ACTUALLY
    produced. Editing `spec.capture` mid-session must not retroactively
    change what a finished session claims about itself.
    """

    vm: str
    unit: str
    started_at: str
    max_file_mb: int
    max_files: int
    snaplen: int


def session_path(state_dir: Path, lab: str, vm: str) -> Path:
    return state_dir / "state" / "capture" / lab / f"{vm}.json"


def write_session(state_dir: Path, lab: str, session: CaptureSession) -> Path:
    path = session_path(state_dir, lab, session.vm)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session.model_dump_json(indent=2) + "\n")
    return path


def read_session(state_dir: Path, lab: str, vm: str) -> CaptureSession | None:
    """The recorded session, or None when there is none to read.

    A corrupt or truncated record reads as None rather than raising: the
    only thing a caller does with "no valid session" is start a fresh
    one, and a half-written file must not wedge that forever.
    """
    path = session_path(state_dir, lab, vm)
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        return CaptureSession.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError):
        return None


def clear_session(state_dir: Path, lab: str, vm: str) -> None:
    """Remove the record. Absent is success -- the goal is "be gone"."""
    session_path(state_dir, lab, vm).unlink(missing_ok=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$PYTEST tests/unit/capture/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/playground/capture/state.py tests/unit/capture/test_state.py
git commit -m "feat(capture): per-VM session records under state/capture/<lab>/

Limits are copied into the record rather than re-read from the lab, so
editing spec.capture mid-session cannot retroactively change what a
finished session claims about the pcaps it produced."
```

---
## Task 6: `playground capture start | stop | status`

**Files:**
- Create: `src/playground/cli/capture_commands.py`
- Modify: `src/playground/cli/main.py` (import near line 34; `add_typer` near line 68)
- Test: `tests/cli/test_capture.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 3, 5.
- Produces: `capture_app` (a `typer.Typer`), and `_resolve(...)` /
  `_fail(...)` module-private helpers Task 7 reuses for `fetch`.

Mirror `app_commands.py` exactly, including the deferred `_main()` import:
`main.py` imports `capture_app` from this module, so a top-level
`from playground.cli.main import ...` here would be circular — `main` has
not yet defined `OutputFormat` / `_exit_with_diagnostic` at the point it
imports us.

Every verb takes the same trailing parameters in the same order as every
`app` verb (`lab`, `on`, `role`, `all_devices`, `user`, `config_dir`,
`tofu_dir`), plus `state_dir` because capture persists session records.

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_capture.py`:

```python
"""`playground capture` — start/stop/status against a shimmed guest.

`redroid-cloud` is a cloud-digitalocean lab, so `query_status` is
monkeypatched to a canned LabStatus exactly as tests/cli/test_app.py
does; these tests are about the commands sent and the exit codes, not
about reaching DigitalOcean.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import playground.cli.capture_commands as capture_commands
from playground.capture.state import read_session
from playground.cli.main import app
from playground.models.status import LabStatus, VmStatus

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _stub_status(resolved, tofu_dir):
    return (
        LabStatus(
            lab=resolved.lab_name,
            backend=resolved.backend,
            expected_vms=len(resolved.vms),
            provisioned_vms=len(resolved.vms),
            vms=[
                VmStatus(
                    name=vm.name, role=vm.role, state="running",
                    ssh_host="127.0.0.1", ssh_port=22,
                )
                for vm in resolved.vms
            ],
        ),
        [],
    )


def _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, *args, ssh_exit=0):
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=ssh_exit)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    monkeypatch.setattr(capture_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    return CliRunner().invoke(
        app,
        ["capture", *args, "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir),
         "--state-dir", str(tmp_path / ".playground")],
    )


def test_start_starts_the_instanced_unit(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start")
    assert result.exit_code == 0, result.output
    log = (tmp_path / "ssh.log").read_text()
    assert "sudo -n systemctl start" in log
    assert "playground-capture@droid1.service" in log


def test_start_writes_a_session_record(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start")
    assert result.exit_code == 0
    session = read_session(tmp_path / ".playground", "redroid-cloud", "droid1")
    assert session is not None
    assert session.unit == "playground-capture@droid1.service"
    # Limits are recorded from the lab's resolved spec.capture.
    assert session.max_file_mb == 100
    assert session.max_files == 10


def test_start_twice_is_refused(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """"Already capturing" and "just started capturing" produce
    differently attributable pcaps, so this is an error, not a no-op."""
    first = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start")
    assert first.exit_code == 0
    second = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start")
    assert second.exit_code == 1
    assert "runtime.capture.already_running" in second.output + str(second.stderr)


def test_start_does_not_record_a_session_when_the_guest_fails(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """A recorded session that never started would make `start` refuse
    forever while nothing was capturing."""
    result = _run(
        tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start", ssh_exit=1
    )
    assert result.exit_code == 1
    assert read_session(tmp_path / ".playground", "redroid-cloud", "droid1") is None


def test_stop_stops_the_unit_and_clears_the_record(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start")
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "stop")
    assert result.exit_code == 0, result.output
    assert "sudo -n systemctl stop" in (tmp_path / "ssh.log").read_text()
    assert read_session(tmp_path / ".playground", "redroid-cloud", "droid1") is None


def test_stop_without_a_session_is_a_clean_error(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "stop")
    assert result.exit_code == 1
    assert "runtime.capture.not_running" in result.output + str(result.stderr)


def test_status_needs_no_session_record(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    """status asks the guest, so it must work on a lab this machine has
    never started a session for."""
    result = _run(tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "status")
    assert result.exit_code == 0, result.output
    assert "is-active" in (tmp_path / "ssh.log").read_text()


def test_unknown_vm_is_rejected_before_any_ssh(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim
) -> None:
    result = _run(
        tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, "start", "--on", "nope"
    )
    assert result.exit_code == 1
    assert "config.capture.unknown_vm" in result.output + str(result.stderr)
    assert not (tmp_path / "ssh.log").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/cli/test_capture.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'playground.cli.capture_commands'`.

- [ ] **Step 3: Write `src/playground/cli/capture_commands.py`**

```python
"""`playground capture` — record a Redroid device's traffic to a pcap.

Sessions are systemd units on the guest, not processes this CLI owns: a
capture has to outlive the ssh call that started it, and has to survive
the operator closing their laptop. Every verb here is therefore a thin
`systemctl` driver plus a session record on this machine.

Lives in its own module rather than growing `main.py` (2000+ lines)
further, following `app_commands.py`. `main.py` imports `capture_app`
from here to register it, so importing `main.py`'s config/diagnostic
helpers back at THIS module's top level would be circular: `main` would
not yet have defined them at the point it imports `capture_app`.
`_main()` below defers that import to call time instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from playground.android.runner import TargetResult, run_on_targets
from playground.android.targets import AndroidTarget
from playground.backend.dispatch import query_status
from playground.capture.commands import start_cmd, status_cmd, stop_cmd, unit_name
from playground.capture.state import (
    CaptureSession,
    clear_session,
    read_session,
    write_session,
)
from playground.capture.targets import resolve_capture_targets
from playground.models.diagnostic import Diagnostic, SourceLocation
from playground.models.resolved import ResolvedLab
from playground.validation import validate as validate_loaded_config


def _main() -> Any:  # noqa: ANN401 - deferred cross-module import, see module docstring
    """Deferred import of `playground.cli.main` — see module docstring."""
    from playground.cli import main as cli_main

    return cli_main


def _fail(diagnostic: Diagnostic) -> NoReturn:
    """Exit with one diagnostic, human-formatted, matching every other
    CLI command's error gate."""
    _main()._exit_with_diagnostic(
        diagnostic, _main().OutputFormat.human, json_errors=False
    )
    raise typer.Exit(code=1)  # unreachable: _exit_with_diagnostic always raises


capture_app = typer.Typer(
    no_args_is_help=True,
    help="Capture the network traffic of Redroid devices to pcap files.",
)

_DEFAULT_TIMEOUT = 30.0

LabOpt = Annotated[
    str | None,
    typer.Option(
        "--lab",
        help=(
            "Lab name. Defaults to the only configured lab; required when "
            "multiple labs are configured."
        ),
    ),
]
OnOpt = Annotated[str | None, typer.Option("--on", help="Target one VM by name.")]
RoleOpt = Annotated[
    str | None,
    typer.Option("--role", help="Target every capturable VM that has this role."),
]
AllOpt = Annotated[
    bool, typer.Option("--all", help="Target every capturable VM in the lab.")
]
UserOpt = Annotated[str, typer.Option("--user", help="SSH user (default: ubuntu).")]
ConfigDirOpt = Annotated[
    Path, typer.Option("--config-dir", "-c", help="Config directory to load.")
]
TofuDirOpt = Annotated[
    Path, typer.Option("--tofu-dir", help="OpenTofu working directory.")
]
StateDirOpt = Annotated[
    Path,
    typer.Option(
        "--state-dir",
        help="Where generated state lives. Defaults to `.playground/`.",
    ),
]


def _resolve(
    *,
    lab: str | None,
    on: str | None,
    role: str | None,
    all_devices: bool,
    user: str,
    config_dir: Path,
    tofu_dir: Path,
) -> tuple[str, ResolvedLab, list[AndroidTarget]]:
    """Load, validate, default the lab, and pick capturable devices.

    Returns the lab NAME alongside the resolved lab and targets because
    every verb needs it for the session-record path, and `--lab` may have
    been defaulted here rather than passed.
    """
    cli_main = _main()
    loaded, diagnostics = cli_main._load_config_or_exit(
        config_dir, cli_main.OutputFormat.human
    )
    if not cli_main._has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded, lab=lab))
    cli_main._exit_on_errors(diagnostics, cli_main.OutputFormat.human, json_errors=False)
    cli_main._print_warnings(diagnostics)

    if lab is None:
        if len(loaded.labs) == 1:
            lab = next(iter(loaded.labs))
        else:
            _fail(
                Diagnostic(
                    id="config.capture.lab_required",
                    severity="error",
                    message=(
                        f"--lab required when {len(loaded.labs)} labs are "
                        "configured; pass --lab <name>"
                    ),
                    source=SourceLocation(path=str(config_dir / "labs")),
                    suggestion="run `playground lab list` and pass --lab <name>",
                )
            )

    resolved = cli_main._resolve_lab_or_exit(
        loaded, lab, config_dir, cli_main.OutputFormat.human
    )
    status, query_diagnostics = query_status(resolved, tofu_dir)
    cli_main._exit_on_errors(
        query_diagnostics, cli_main.OutputFormat.human, json_errors=False
    )
    targets, target_diagnostics = resolve_capture_targets(
        resolved, status, on=on, role=role, all_devices=all_devices, user=user,
    )
    cli_main._exit_on_errors(
        target_diagnostics, cli_main.OutputFormat.human, json_errors=False
    )
    return lab, resolved, targets


def _echo(result: TargetResult) -> None:
    prefix = f"[{result.target.vm_name}]"
    for line in result.stdout.splitlines():
        typer.echo(f"{prefix} {line}")
    for line in result.stderr.splitlines():
        typer.echo(f"{prefix} {line}", err=True)
    if not result.ok:
        typer.echo(f"{prefix} exited {result.returncode}", err=True)


def _exit_for_failures(failed: list[str], total: int) -> None:
    if failed:
        typer.echo(
            f"failed on {len(failed)}/{total} device(s): {', '.join(failed)}",
            err=True,
        )
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@capture_app.command("start", help="Start capturing a device's traffic.")
def start_command(
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    lab_name, resolved, targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
    )

    # Refuse BEFORE touching any guest: a second start against a live
    # session produces two overlapping pcap sets that cannot be
    # attributed to either experiment.
    for target in targets:
        if read_session(state_dir, lab_name, target.vm_name) is not None:
            _fail(
                Diagnostic(
                    id="runtime.capture.already_running",
                    severity="error",
                    message=(
                        f"a capture session is already recorded for "
                        f"{target.vm_name!r} in lab {lab_name!r}"
                    ),
                    source=SourceLocation(
                        path=str(state_dir / "state" / "capture" / lab_name)
                    ),
                    suggestion=(
                        f"run `playground capture stop --lab {lab_name} --on "
                        f"{target.vm_name}` first"
                    ),
                )
            )

    failed: list[str] = []
    # One command per target: `run_on_targets` sends ONE command string to
    # every target, but each device's command names its own unit instance,
    # so capture calls it per-target. Identical for a single device (the
    # common case); with --all it serializes. A parallel variant belongs in
    # `android/runner.py`, and only once something needs it.
    results = [
        run_on_targets([t], start_cmd(t.vm_name), timeout=_DEFAULT_TIMEOUT)[0]
        for t in targets
    ]
    for result in results:
        _echo(result)
        if result.ok:
            # Only record a session the guest actually started. A record
            # written on failure would make `start` refuse forever while
            # nothing was capturing.
            write_session(
                state_dir,
                lab_name,
                CaptureSession(
                    vm=result.target.vm_name,
                    unit=unit_name(result.target.vm_name),
                    started_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                    max_file_mb=resolved.capture.max_file_mb,
                    max_files=resolved.capture.max_files,
                    snaplen=resolved.capture.snaplen,
                ),
            )
            typer.echo(
                f"[{result.target.vm_name}] capturing — "
                f"stop with `playground capture stop --lab {lab_name} "
                f"--on {result.target.vm_name}`"
            )
        else:
            failed.append(result.target.vm_name)
    _exit_for_failures(failed, len(targets))


@capture_app.command("stop", help="Stop capturing and seal the session.")
def stop_command(
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    lab_name, _resolved, targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
    )

    for target in targets:
        if read_session(state_dir, lab_name, target.vm_name) is None:
            _fail(
                Diagnostic(
                    id="runtime.capture.not_running",
                    severity="error",
                    message=(
                        f"no capture session recorded for {target.vm_name!r} "
                        f"in lab {lab_name!r}"
                    ),
                    source=SourceLocation(
                        path=str(state_dir / "state" / "capture" / lab_name)
                    ),
                    suggestion=(
                        f"run `playground capture status --lab {lab_name}` to "
                        "see what the guest reports"
                    ),
                )
            )

    failed: list[str] = []
    for target in targets:
        result = run_on_targets(
            [target], stop_cmd(target.vm_name), timeout=_DEFAULT_TIMEOUT
        )[0]
        _echo(result)
        if result.ok:
            # Clear only on success: leaving the record in place after a
            # failed stop keeps `fetch` pointed at a session that may
            # still be writing.
            clear_session(state_dir, lab_name, target.vm_name)
            typer.echo(f"[{target.vm_name}] stopped")
        else:
            failed.append(target.vm_name)
    _exit_for_failures(failed, len(targets))


@capture_app.command("status", help="Report capture state, file count, and bytes.")
def status_command(
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    """Ask the GUEST, not the session record.

    The record says what this machine started; the guest says what is
    actually running. They can disagree — another operator's stop, a
    reboot, a container restart — and the guest is the truth.
    """
    lab_name, _resolved, targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
    )
    failed: list[str] = []
    for target in targets:
        result = run_on_targets(
            [target], status_cmd(target.vm_name), timeout=_DEFAULT_TIMEOUT
        )[0]
        _echo(result)
        recorded = read_session(state_dir, lab_name, target.vm_name)
        if recorded is not None:
            typer.echo(
                f"[{target.vm_name}] session recorded here since "
                f"{recorded.started_at}"
            )
        if not result.ok:
            failed.append(target.vm_name)
    _exit_for_failures(failed, len(targets))
```

- [ ] **Step 4: Register the group in `main.py`**

Add the import beside the existing `app_commands` import (line 34):

```python
from playground.cli.capture_commands import capture_app
```

and the registration after `app.add_typer(app_app, name="app")` (line 68):

```python
app.add_typer(capture_app, name="capture")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$PYTEST tests/cli/test_capture.py tests/unit/capture/ -v`
Expected: PASS.

- [ ] **Step 6: Lint, type-check, and commit**

```bash
uv run --no-project --with ruff ruff check src tests
uv run --no-project --with mypy --with pydantic --with ruamel.yaml \
  --with jsonschema --with typer mypy src
git add src/playground/cli/capture_commands.py src/playground/cli/main.py \
        tests/cli/test_capture.py
git commit -m "feat(capture): playground capture start|stop|status

Sessions are systemd units on the guest, so a capture outlives the ssh
call that started it. start refuses when a session is already recorded
(two overlapping pcap sets cannot be attributed to either experiment)
and records nothing when the guest refuses to start."
```

---

## Task 7: `playground capture fetch`

**Files:**
- Modify: `src/playground/cli/capture_commands.py` (add the verb)
- Modify: `src/playground/runs/operation.py` (the `operation` Literal, lines 46 and 68)
- Test: `tests/cli/test_capture.py` (append), `tests/unit/runs/test_operation.py` (append)

**Interfaces:**
- Consumes: Task 6's `_resolve` / `_fail`; `runs.operation.start_run` / `finish_run`.
- Produces: pcaps at
  `<state_dir>/runs/<run-id>/artifacts/capture/<vm>/`.

`OperationRun.operation` is a closed
`Literal["apply","destroy","reset","stop","suspend","resume"]` in **two**
places — the model field and `start_run`'s parameter annotation. Both
need `"capture"` added, or the run record cannot be written and mypy
strict will reject the call.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/runs/test_operation.py`:

```python
def test_capture_is_an_allowed_operation(tmp_path) -> None:
    """`capture fetch` writes artifacts, so it gets a run record and
    shows up in `playground runs list` like every other operation."""
    from playground.runs.operation import start_run

    run, run_dir = start_run(tmp_path / "runs", "capture", "redroid-cloud")
    assert run.operation == "capture"
    assert (run_dir / "run.json").is_file()
```

Append to `tests/cli/test_capture.py`:

```python
def test_fetch_scps_the_guest_capture_directory(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, write_scp_shim
) -> None:
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)
    scp_bin = write_scp_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH",
        f"{scp_bin}{os.pathsep}{ssh_bin}{os.pathsep}{bin_dir}"
        f"{os.pathsep}{os.environ['PATH']}",
    )
    monkeypatch.setattr(capture_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    result = CliRunner().invoke(
        app,
        ["capture", "fetch", "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir),
         "--state-dir", str(tmp_path / ".playground")],
    )
    assert result.exit_code == 0, result.output
    scp_log = (tmp_path / "scp.log").read_text()
    assert "-r" in scp_log, "the guest capture directory is a directory"
    assert "/var/lib/playground/capture/droid1" in scp_log
    # The pcaps land as a run artifact, so `runs list` can find them.
    runs = list((tmp_path / ".playground" / "runs").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "run.json").is_file()
    assert (runs[0] / "artifacts" / "capture" / "droid1").is_dir()


def test_fetch_leaves_the_guest_copy_in_place_by_default(
    tmp_path, monkeypatch, write_apply_shims, write_ssh_shim, write_scp_shim
) -> None:
    """A failed transfer must not be able to destroy the only copy, so
    removal is opt-in via --clean."""
    bin_dir = write_apply_shims(tmp_path)
    ssh_bin = write_ssh_shim(tmp_path, exit_code=0)
    scp_bin = write_scp_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH",
        f"{scp_bin}{os.pathsep}{ssh_bin}{os.pathsep}{bin_dir}"
        f"{os.pathsep}{os.environ['PATH']}",
    )
    monkeypatch.setattr(capture_commands, "query_status", _stub_status)
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    CliRunner().invoke(
        app,
        ["capture", "fetch", "--lab", "redroid-cloud",
         "--config-dir", str(CONFIG_DIR), "--tofu-dir", str(tofu_dir),
         "--state-dir", str(tmp_path / ".playground")],
    )
    ssh_log = tmp_path / "ssh.log"
    assert not ssh_log.exists() or "rm -rf" not in ssh_log.read_text()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$PYTEST tests/cli/test_capture.py tests/unit/runs/test_operation.py -k capture -v`
Expected: FAIL — `ValidationError` on `operation="capture"`, and
`No such command 'fetch'`.

- [ ] **Step 3: Widen the `operation` Literal**

In `src/playground/runs/operation.py`, in **both** places (the
`OperationRun.operation` field near line 46 and `start_run`'s
`operation` parameter near line 68), change:

```python
Literal["apply", "destroy", "reset", "stop", "suspend", "resume"]
```

to:

```python
Literal["apply", "destroy", "reset", "stop", "suspend", "resume", "capture"]
```

- [ ] **Step 4: Add the `fetch` verb to `capture_commands.py`**

Add these imports at the top of the module:

```python
import subprocess

from playground.runs.operation import StepResult, finish_run, start_run
from playground.ssh.argv import build_scp_argv
```

and the verb:

```python
_FETCH_TIMEOUT = 300.0
"""A 1 GB ring over a cloud VM's uplink is not a 30-second transfer."""


@capture_app.command("fetch", help="Copy captured pcaps into a run artifact directory.")
def fetch_command(
    lab: LabOpt = None,
    on: OnOpt = None,
    role: RoleOpt = None,
    all_devices: AllOpt = False,
    user: UserOpt = "ubuntu",
    clean: Annotated[
        bool,
        typer.Option(
            "--clean",
            help="Remove the guest-side pcaps after a successful transfer.",
        ),
    ] = False,
    config_dir: ConfigDirOpt = Path("config"),
    tofu_dir: TofuDirOpt = Path("tofu"),
    state_dir: StateDirOpt = Path(".playground"),
) -> None:
    """Pull each device's pcap directory into this run's artifacts.

    Recorded as an operation run so the pcaps are addressable later
    (`playground runs list`) rather than landing in whatever directory
    the operator happened to be standing in.

    The guest copy is left in place unless `--clean`: a failed scp must
    never be able to destroy the only copy of a capture.
    """
    lab_name, _resolved, targets = _resolve(
        lab=lab, on=on, role=role, all_devices=all_devices, user=user,
        config_dir=config_dir, tofu_dir=tofu_dir,
    )

    run, run_dir = start_run(state_dir / "runs", "capture", lab_name)
    artifacts = run_dir / "artifacts" / "capture"
    steps: list[StepResult] = []
    failed: list[str] = []

    for target in targets:
        destination = artifacts / target.vm_name
        destination.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        source = (
            f"{target.ssh_user}@{target.ssh_host}:"
            f"{remote_capture_dir(target.vm_name)}/."
        )
        argv = build_scp_argv(
            source, str(destination), port=target.ssh_port, recursive=True
        )
        try:
            completed = subprocess.run(  # noqa: S603
                argv, capture_output=True, text=True, check=False,
                timeout=_FETCH_TIMEOUT,
            )
            code = completed.returncode
            stderr = completed.stderr.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            code, stderr = 1, str(exc)

        steps.append(
            StepResult(
                name=f"capture-fetch:{target.vm_name}",
                command=list(argv),
                exit_code=code,
                log_path=str(destination),
                started_at=started_at,
                finished_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            )
        )
        if code != 0:
            typer.echo(f"[{target.vm_name}] fetch failed: {stderr}", err=True)
            failed.append(target.vm_name)
            continue
        typer.echo(f"[{target.vm_name}] pcaps → {destination}")
        if clean:
            removal = run_on_targets(
                [target],
                f"sudo -n rm -f {remote_capture_dir(target.vm_name)}/*.pcap",
                timeout=_DEFAULT_TIMEOUT,
            )[0]
            _echo(removal)

    finish_run(
        run,
        run_dir,
        status="failed" if failed else "succeeded",
        steps=steps,
        summary=(
            f"fetched capture from {len(targets) - len(failed)}/{len(targets)} "
            "device(s)"
        ),
    )
    typer.echo(f"  run: {run.run_id}")
    _exit_for_failures(failed, len(targets))
```

Add `remote_capture_dir` to the existing
`from playground.capture.commands import ...` line, keeping it
alphabetical:

```python
from playground.capture.commands import (
    remote_capture_dir,
    start_cmd,
    status_cmd,
    stop_cmd,
    unit_name,
)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$PYTEST tests/cli/ tests/unit/capture/ tests/unit/runs/ -v`
Expected: PASS.

- [ ] **Step 6: Lint, type-check, and commit**

```bash
uv run --no-project --with ruff ruff check src tests
uv run --no-project --with mypy --with pydantic --with ruamel.yaml \
  --with jsonschema --with typer mypy src
git add src/playground/cli/capture_commands.py src/playground/runs/operation.py \
        tests/cli/test_capture.py tests/unit/runs/test_operation.py
git commit -m "feat(capture): fetch pcaps into a run artifact directory

Adds 'capture' to OperationRun.operation so a fetch is addressable via
\`playground runs list\` instead of landing in the operator's cwd. The
guest copy survives unless --clean, so a failed scp cannot destroy the
only copy."
```

---

## Task 8: Scrub capture state on `reset`

**Files:**
- Modify: `src/playground/backend/local_libvirt/runner.py` (paths near 388-390; `targets=[...]` at 475; docstring 371-375)
- Modify: `src/playground/backend/local_vbox/runner.py` (paths near 310-312; `targets=[...]` at 316)
- Modify: `src/playground/backend/cloud_digitalocean/runner.py` (paths near 245-246; `targets=[...]` at 250)
- Test: `tests/unit/backend/local_libvirt/test_scrub.py` (append)

**Interfaces:**
- Consumes: `capture.state.session_path`'s layout (Task 5).
- Produces: nothing new.

**Deviation from the spec, deliberate:** the design said "`playground
reset` and `destroy` learn to scrub `state/capture/<lab>/`".
`_clean_state_files` is called *only* from the three `execute_reset`
paths; `execute_destroy` has never removed state files for any subsystem.
Making `destroy` remove state would be a new behavior for capture alone
and inconsistent with `tofu/`, `inventory/`, `workloads/`, `vbox/`, and
`cloud-digitalocean/`. So: `reset` scrubs, `destroy` does not, and the
spec gets a correction in Task 9.

This matters for correctness, not tidiness. A stale session record makes
`capture start` refuse forever with `runtime.capture.already_running`
after a lab is torn down and re-applied, because the record is on the
operator's machine and the tear-down took the guest's unit with it.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/backend/local_libvirt/test_scrub.py`:

```python
def test_reset_removes_the_labs_capture_session_records(tmp_path) -> None:
    """A stale record makes `capture start` refuse forever after a lab is
    destroyed and re-applied: the record lives on the operator's machine,
    but the unit it names went away with the guest."""
    from playground.backend.local_libvirt.runner import _clean_state_files

    capture_dir = tmp_path / "state" / "capture" / "redroid-cloud"
    capture_dir.mkdir(parents=True)
    (capture_dir / "droid1.json").write_text("{}")
    other = tmp_path / "state" / "capture" / "other-lab"
    other.mkdir(parents=True)
    (other / "droid1.json").write_text("{}")

    step, diagnostics = _clean_state_files(
        lab="redroid-cloud",
        targets=[capture_dir],
        log_path=tmp_path / "clean.log",
    )

    assert step.exit_code == 0
    assert diagnostics == []
    assert not capture_dir.exists()
    # Never another lab's state.
    assert (other / "droid1.json").is_file()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `$PYTEST tests/unit/backend/local_libvirt/test_scrub.py -k capture -v`
Expected: FAIL — the test itself passes trivially only once
`_clean_state_files` is handed the directory; it fails now because
nothing constructs that path. Confirm the failure is the missing
`capture_dir` wiring below, not an import error.

- [ ] **Step 3: Wire the path in the libvirt runner**

In `execute_reset`, beside the existing path variables (lines 388-390):

```python
    capture_dir = state_dir / "state" / "capture" / lab
```

and add it to the step-4 target list (line 475):

```python
        targets=[tfvars_path, inventory_path, workload_dir, capture_dir],
```

Update the docstring (lines 371-375) to name the fourth directory:

```python
    4. **clean-state-files**: remove per-lab artifacts under
       ``.playground/state/{tofu,inventory,workloads,capture}/`` so the
       next ``playground apply`` starts from a clean slate. Shared
       artifacts (tofu/terraform.tfstate, ubuntu-noble.qcow2 base
       image) are never touched.
```

- [ ] **Step 4: Do the same for the other two backends**

`src/playground/backend/local_vbox/runner.py`, beside lines 310-312:

```python
    capture_dir = state_dir / "state" / "capture" / lab
```

```python
        targets=[vbox_state, inventory_path, workload_dir, capture_dir],
```

`src/playground/backend/cloud_digitalocean/runner.py`, beside lines 245-246:

```python
    capture_dir = state_dir / "state" / "capture" / lab
```

```python
        targets=[per_lab_dir, inventory_path, workload_dir, capture_dir],
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$PYTEST tests/unit/backend/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/playground/backend/local_libvirt/runner.py \
        src/playground/backend/local_vbox/runner.py \
        src/playground/backend/cloud_digitalocean/runner.py \
        tests/unit/backend/local_libvirt/test_scrub.py
git commit -m "fix(capture): reset scrubs state/capture/<lab>/ on all three backends

Without this a stale session record survives teardown and makes
\`capture start\` refuse forever with already_running: the record is on
the operator's machine, but the unit it names left with the guest."
```

---

## Task 9: Documentation, example lab, and spec corrections

**Files:**
- Create: `config/labs/redroid-capture.yaml`
- Modify: `docs/architecture/CONTRACTS.md`
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-09-03-android-traffic-capture-design.md`
- Modify: `docs/roadmap.md`

**Interfaces:**
- Consumes: everything above.
- Produces: no code.

- [ ] **Step 1: Add the example lab**

Create `config/labs/redroid-capture.yaml`. Note
`tests/unit/models/test_kinds.py::test_every_committed_yaml_parses` walks
`config/` and will parse this, so it is a test as much as an example:

```yaml
# redroid-capture — containerized Android with traffic capture, on libvirt
#
# Captures the device's own network namespace from the host VM, so the
# pcap is exactly this device's traffic:
#
#     playground apply redroid-capture
#     playground capture start --lab redroid-capture
#     playground app install --lab redroid-capture ./some.apk
#     playground capture stop  --lab redroid-capture
#     playground capture fetch --lab redroid-capture
#
# The ring below is 40 x 25 MB = 1 GB per session. tcpdump -C counts
# 1,000,000-byte units, not MiB, and -C with -W OVERWRITES the oldest
# file once max_files is reached — a long session keeps the most recent
# 1 GB, not the first.
apiVersion: playground/v1
kind: Lab
metadata:
  name: redroid-capture
  description: |
    Single-VM libvirt lab running Redroid with packet capture enabled,
    reachable over ADB through an SSH tunnel.
  tags: [local, libvirt, redroid, android, capture]

spec:
  backend: local-libvirt
  offline: false

  capture:
    enabled: true
    max_file_mb: 25
    max_files: 40
    snaplen: 0

  budget:
    mode: permissive
    max_vcpu: 8
    max_memory_mb: 16384
    max_disk_gb: 120
    max_vms: 2
    max_containers: 10

  networks:
    - name: lab-net
      profile: nat
      cidr: 10.75.0.0/24

  vms:
    - name: droid1
      role: redroid-host
      networks: [lab-net]

  providers:
    local-libvirt:
      uri: qemu:///system
      pool: default
```

- [ ] **Step 2: Add the `CONTRACTS.md` layer contract**

Append a section after "Workload: `android_app`" (which ends at line 490,
before "Cross-layer pitfalls" at 491):

```markdown
## Capture: Redroid device traffic

**Input**: `ResolvedLab.capture` (a `CaptureOptions`), carried to Ansible
as the `pg_capture` group var under `[playground:vars]`.

**Output**: `.pcap` files at `/var/lib/playground/capture/<vm>/` on the
guest; fetched to `.playground/runs/<run-id>/artifacts/capture/<vm>/`.

**Contract**:
- The capture point is the Redroid container's OWN network namespace,
  entered with `nsenter --target <pid> --net`. Only `--net` is entered,
  so `tcpdump` is the guest's binary and the pcap lands on the guest's
  disk. `-i any` inside that namespace is exactly one device's traffic.
- **The container PID is resolved at unit-start time, never at provision
  time.** The Redroid container runs `restart_policy: unless-stopped`, so
  its PID changes across restarts.
- The wrapper DISCOVERS the container by the `redroid_` name prefix and
  fails loudly on zero or multiple matches. It deliberately does not
  duplicate the redroid role's
  `redroid_{{ inventory_hostname | regex_replace(...) }}` expression —
  that would be a second copy of a naming convention in a second role.
- Provisioning installs tooling and never starts a session, so
  `playground apply` on a lab that is mid-capture leaves the recording
  untouched.
- `spec.capture.enabled: false` skips the `needs_capture` play entirely
  (no package, no unit) — it is the air-gap opt-out, not just a CLI gate.
- `local-vbox` has no capture: its ProviderConfig declares
  `redroid: false`, so there is no device. `config.capture.backend_unsupported`.

### Two Ubuntu tcpdump defaults that break a fresh guest

Both are the recurring "library default wrong for fresh state" shape.

1. **Privilege drop.** Debian/Ubuntu `tcpdump` setuid()s to the
   unprivileged `tcpdump` user unless told otherwise, and then cannot
   write into the root-owned capture directory. `-Z root` is required.
2. **AppArmor confinement.** Ubuntu ships
   `/etc/apparmor.d/usr.sbin.tcpdump`, which confines where tcpdump may
   write; `/var/lib/playground/` is outside it. The failure presents as
   `Permission denied` on a directory whose ownership and mode look
   correct. The shipped profile ends with
   `#include <local/usr.sbin.tcpdump>`, so the role writes a local
   override and reloads with `apparmor_parser -r`. Do not disable
   confinement instead.

### `-C` semantics are inherited, not invented

`max_file_mb` maps to `tcpdump -C`, which counts units of **1,000,000
bytes, not MiB**. `-C` with `-W` is a **ring**: the oldest file is
overwritten once `max_files` is reached, so a session that outlives its
budget keeps the most recent `max_file_mb * max_files` and discards the
beginning. `capture status` reports the file count so a wrapping session
is visible.

### Android 14 moves the CA store — a constraint on the image tag

Not needed for passive capture, but load-bearing for the TLS-interception
follow-up: bind-mounting a MITM CA into `/system/etc/security/cacerts`
works because the pinned image is Android 11. **Android 14+ moved the
trust store into the Conscrypt APEX**, so raising the tag past 13
invalidates that approach entirely. This is a second constraint on
`redroid_image`, alongside "must stay date-stamped".
```

- [ ] **Step 3: Add the `CLAUDE.md` bullet**

Add after the Redroid bullet in "Architecture notes that aren't obvious
from a single file":

```markdown
- **Traffic capture enters the device's netns, it does not sniff the
  bridge.** `ansible/roles/capture/` installs `tcpdump` plus an instanced
  `playground-capture@<vm>.service` whose wrapper resolves the Redroid
  container's PID *at start time* (`restart_policy: unless-stopped`
  changes it) and runs `nsenter --target <pid> --net tcpdump -i any`.
  Only `--net` is entered, so the pcap lands on the VM's disk, not inside
  the container. Two Ubuntu defaults break the naive version: tcpdump
  drops privileges to the `tcpdump` user (hence `-Z root`) and AppArmor
  confines its writes (hence the `/etc/apparmor.d/local/` override).
  `spec.capture` bounds the ring — and `-C` counts 1,000,000-byte units
  while `-C`+`-W` OVERWRITES the oldest file, so a long session keeps the
  most recent GB, not the first. Provisioning never starts a session;
  `playground capture start|stop|status|fetch` does. TLS interception is
  not implemented — see the spec's "Phase 2 seam".
```

- [ ] **Step 4: Correct the spec's two now-known-wrong claims**

In `docs/superpowers/specs/2026-09-03-android-traffic-capture-design.md`:

- In the Diagnostics section, replace the
  `runtime.backend.verb_not_supported` line with:

  ```markdown
  - `config.capture.backend_unsupported` — `local-vbox`. Note this
    replaces the originally-planned reuse of
    `runtime.backend.verb_not_supported`: that helper's message and
    suggestion are hardcoded to suspend/resume billing framing
    ("charge for idle compute"), which would misinform here.
  ```

- In "State and artifacts", replace "`playground reset` and `destroy`
  scrub `state/capture/<lab>/`" with:

  ```markdown
  `playground reset` scrubs `state/capture/<lab>/`. `destroy` does not —
  `_clean_state_files` is wired only into the three `execute_reset`
  paths, and no subsystem's state (`tofu/`, `inventory/`, `workloads/`,
  `vbox/`, `cloud-digitalocean/`) is removed by `destroy` either.
  Scrubbing matters because a stale record makes `capture start` refuse
  with `already_running` after a teardown: the record is on the
  operator's machine, but the unit it names left with the guest.
  ```

- [ ] **Step 5: Note the shipped slice in the roadmap**

Add to `docs/roadmap.md`'s backlog section, so the deferred half is
recorded rather than lost:

```markdown
- Redroid TLS interception (phase 2 of traffic capture). Passive pcap
  shipped; see
  `docs/superpowers/specs/2026-09-03-android-traffic-capture-design.md`
  → "Phase 2 seam". Needs mitmproxy on the guest, an `iptables REDIRECT`
  in the device netns the capture role already enters, and a per-lab CA
  under `.playground/state/capture/<lab>/` (gitignored — the private key
  is a secret under the PRD). Blocked from bumping `redroid_image` past
  Android 13: the system CA store moves into the Conscrypt APEX.
```

- [ ] **Step 6: Verify the whole suite and both linters**

```bash
$PYTEST tests -q
uv run --no-project --with ruff ruff check src tests
uv run --no-project --with mypy --with pydantic --with ruamel.yaml \
  --with jsonschema --with typer mypy src
cd ansible && ansible-playbook -i inventory.ini site.yml --syntax-check; cd ..
```

Expected: the full suite green; ruff reporting only the 6 pre-existing
errors named in the Global Constraints; mypy clean; the playbook parsing.

- [ ] **Step 7: Commit**

```bash
git add config/labs/redroid-capture.yaml docs/architecture/CONTRACTS.md \
        CLAUDE.md docs/superpowers/specs/2026-09-03-android-traffic-capture-design.md \
        docs/roadmap.md
git commit -m "docs(capture): CONTRACTS entry, example lab, and spec corrections

Records the two Ubuntu tcpdump defaults, the inherited -C ring
semantics, and the Android 14 Conscrypt APEX constraint on the image
tag. Corrects two spec claims the implementation disproved: the shared
verb_not_supported diagnostic reads as billing advice, and destroy has
never scrubbed per-lab state for any subsystem."
```

---

## Live validation (not a task — do this before claiming the slice works)

Every bug this repo has found in the Redroid path was live-only. The
static suite cannot see AppArmor, a privilege drop, or a container
restart. After Task 9:

1. `playground apply redroid-capture` on real libvirt.
2. `playground capture start --lab redroid-capture`; confirm
   `systemctl is-active` reports `active` and a `.pcap` appears.
3. Generate traffic (`playground app install`, launch something) and
   confirm the pcap grows and `tshark -r <file>` parses it. **This is the
   step that proves the AppArmor override and `-Z root` work** — both fail
   as an empty or absent file, not as a start failure.
4. `playground apply redroid-capture` again mid-session: assert
   `changed=0` and that the capture is still running and still the same
   session.
5. `docker restart redroid_droid1`: assert the unit re-resolves the new
   PID and a new timestamped pcap appears.
6. `playground capture stop`, `fetch`, then `playground runs list`.
7. `playground reset redroid-capture`, then `capture start` again:
   assert it does NOT report `already_running`.
8. Repeat 1-3 on `cloud-digitalocean` (`redroid-cloud` plus a `capture`
   block). Every live-only Redroid bug so far surfaced there first.
