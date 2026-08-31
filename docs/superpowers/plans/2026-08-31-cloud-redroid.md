# Redroid on Cloud Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `redroid-host` labs run on `cloud-digitalocean` the same way they run on `local-libvirt`, by teaching the `redroid` Ansible role to provide binder itself, adding a real capability cross-check, shipping an example cloud lab, and adding an SSH-tunnel `adb` verb.

**Architecture:** Binder remediation lives in the existing `redroid` Ansible role so one backend-neutral code path serves every backend (principle 5). Provider configs gain a `redroid` capability key so the validator can warn — never block (principle 10) — when a lab asks a backend for something it cannot do. ADB is reached only through an SSH tunnel; the DigitalOcean firewall stays SSH-only because ADB has no authentication.

**Tech Stack:** Python 3.12 (Typer CLI, Pydantic v2, pytest), Ansible (collections `community.general`, `community.docker`, `ansible.posix`), OpenTofu, DigitalOcean.

**Spec:** `docs/superpowers/specs/2026-08-31-cloud-redroid-design.md`

## Global Constraints

- Python >= 3.12. Ruff `line-length = 100`, lint rules `["E", "F", "I", "B", "UP", "W"]`. Mypy `strict = true`.
- Run tests with `uv run pytest` (or `make test`). `testpaths = ["tests"]`, `pythonpath = ["src"]`.
- Diagnostic ID convention: `config.<area>.<condition>` for config-input faults, `runtime.<subsystem>.<reason>` for execution-time faults.
- `Diagnostic.severity` is `Literal["error", "warning", "info"]`. All new diagnostics in this plan are `"warning"` except CLI input errors, which are `"error"`.
- Engineering principle 7: idempotency is mandatory — a second `apply` on a converged host must report `changed=0`.
- Engineering principle 10: warn clearly, block only hard errors.
- Ansible convention: `become` is set at the PLAY level in `ansible/site.yml`, never inside a role's tasks. This repo has no `handlers/` anywhere — the substitute is `register: _name` plus a later task gated `when: _name.changed`. No role uses `retries:`/`until:`; do not introduce that idiom.
- Ansible apt idiom: `ansible.builtin.apt` with `state: present`, `update_cache: yes`, `cache_valid_time: 3600`.
- Never commit or log secrets. `DIGITALOCEAN_TOKEN` is env-only.

## Baseline: the test suite is ALREADY RED on `main`

Before you start, know this. `uv run pytest` currently reports **4 failed, 734 passed, 7 skipped**:

```
FAILED tests/cli/test_cli.py::test_validate_committed_config_succeeds
FAILED tests/unit/validation/test_validator.py::test_budget_exceeded_is_error_in_strict_mode
FAILED tests/unit/validation/test_validator.py::test_budget_inherits_from_defaults_when_lab_omits_it
FAILED tests/unit/validation/test_validator.py::test_budget_exceeded_warns_in_permissive_mode
```

**Cause:** three UNTRACKED lab files sit in `config/labs/` (`barak-deploy-cross-vm.yaml`, `barak-dev-fleet-cloud.yaml`, `barak-fleet-airgap.yaml`). Several tests load the real `config/` tree and assert exact diagnostic counts — `tests/cli/test_cli.py:508` asserts the literal string `"0 errors, 1 warnings"`. The untracked labs add their own diagnostics and break those counts. Verified: moving the three files aside makes `tests/cli/test_cli.py` and `tests/unit/validation/test_validator.py` pass with `130 passed`.

**Do NOT delete those files** — they are the operator's local work. **Do NOT "fix" the failures** — they are out of scope.

**How to verify your work:** the failure set must be UNCHANGED by your task. Compare against the four failures above. To check a config-tree-sensitive assertion in isolation, temporarily move the three files aside, run, then move them back:

```bash
STASH=/tmp/pg-untracked-labs && mkdir -p $STASH
mv config/labs/barak-deploy-cross-vm.yaml config/labs/barak-dev-fleet-cloud.yaml config/labs/barak-fleet-airgap.yaml $STASH/
uv run pytest -q tests/cli/test_cli.py tests/unit/validation/test_validator.py
mv $STASH/*.yaml config/labs/
```

Task 3 adds a committed lab to `config/labs/`, so it is the task most exposed to this. It has explicit steps for it.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `ansible/roles/redroid/tasks/main.yml` | Modify: probe -> remediate -> persist -> re-probe | 1 |
| `ansible/roles/redroid/defaults/main.yml` | Modify: add `redroid_install_kernel_modules` | 1 |
| `tests/unit/ansible/test_redroid_binder_remediation.py` | Create: structural guard on the role YAML | 1 |
| `config/providers/*.yaml` (3 files) | Modify: add the `redroid` capability key | 2 |
| `src/playground/validation/validator.py` | Modify: new capability cross-check + extend backend-capability check | 2 |
| `tests/unit/validation/test_capabilities.py` | Create: capability cross-check tests | 2 |
| `config/labs/redroid-cloud.yaml` | Create: the shipped cloud Redroid lab | 3 |
| `tests/unit/config/test_loader.py` | Modify: assert the new lab loads | 3 |
| `src/playground/cli/main.py` | Modify: new `adb` verb | 4 |
| `tests/cli/test_adb.py` | Create: `adb` verb tests | 4 |
| `src/playground/preflight/doctor.py` | Modify: stop citing redroid as the nested-virt reason | 5 |
| `docs/architecture/CONTRACTS.md`, `docs/product/mvp_scope.md`, `CLAUDE.md` | Modify: corrected framing | 5 |

## Dependencies and parallelism

- **Wave 1 (parallel, no shared files):** Task 1, Task 2, Task 4.
- **Wave 2 (after Task 2):** Task 3 depends on Task 2 because the new lab must not emit a capability warning — that requires `cloud-digitalocean.yaml` to declare `redroid: true`. Task 5 may run in parallel with Task 3.
- **Wave 3 (after all):** Task 6, live validation on DigitalOcean.

---

### Task 1: Self-healing binder in the `redroid` Ansible role

**Files:**
- Modify: `ansible/roles/redroid/defaults/main.yml`
- Modify: `ansible/roles/redroid/tasks/main.yml`
- Test: `tests/unit/ansible/test_redroid_binder_remediation.py` (create)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: the role variable `redroid_install_kernel_modules` (bool, default `true`). Task 5 references it in documentation. No Python symbols.

**Background you need:** Ubuntu Noble builds binder as a MODULE (`CONFIG_ANDROID_BINDER_IPC=m`, `CONFIG_ANDROID_BINDERFS=m`) and ships it in the `linux-modules-extra-$(uname -r)` package, which cloud images do not install. `ashmem_linux` does NOT exist on kernels >= 5.18 — it was removed from staging and Redroid uses memfd instead, so its modprobe must stay best-effort. `ansible_kernel` is available without a `setup` call because the `extra_hosts` play in `ansible/site.yml` gathers facts for all hosts first.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/ansible/test_redroid_binder_remediation.py`:

```python
"""Guards for the redroid role's binder remediation.

Ubuntu cloud images build binder as a module in linux-modules-extra, which
is not installed by default. Without remediation the role's binder
assertion fails on every stock cloud image (and on the repo's own Noble
libvirt guests). These tests pin the remediation and its persistence so a
refactor cannot silently drop them.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKS = REPO_ROOT / "ansible" / "roles" / "redroid" / "tasks" / "main.yml"
DEFAULTS = REPO_ROOT / "ansible" / "roles" / "redroid" / "defaults" / "main.yml"

_yaml = YAML(typ="safe")


def _tasks() -> list[dict]:
    return _yaml.load(TASKS.read_text())


def test_opt_out_variable_defaults_to_true() -> None:
    defaults = _yaml.load(DEFAULTS.read_text())
    assert defaults["redroid_install_kernel_modules"] is True


def test_installs_modules_extra_for_the_running_kernel() -> None:
    """The package must be pinned to ansible_kernel, not 'latest'."""
    text = TASKS.read_text()
    assert "linux-modules-extra-{{ ansible_kernel }}" in text


def test_modules_extra_install_is_gated_on_the_opt_out_and_a_missing_binder() -> None:
    install = [
        t for t in _tasks()
        if "linux-modules-extra" in str(t.get("ansible.builtin.apt", ""))
    ]
    assert len(install) == 1, "expected exactly one modules-extra install task"
    when = str(install[0]["when"])
    assert "redroid_install_kernel_modules" in when, "must honor the air-gap opt-out"
    assert "binder_check" in when, "must only run when binder is actually missing"


def test_module_load_is_persisted_across_reboot() -> None:
    text = TASKS.read_text()
    assert "/etc/modules-load.d/" in text, "modprobe must survive a reboot"
    assert "/etc/modprobe.d/" in text, "the devices= parameter must survive a reboot"


def test_binder_is_reprobed_after_remediation() -> None:
    """A single probe cannot prove remediation worked."""
    probes = [
        t for t in _tasks()
        if "grep -qw binder /proc/filesystems" in str(t.get("ansible.builtin.command", ""))
    ]
    assert len(probes) == 2, "expected a probe before and after remediation"


def test_abort_message_mentions_the_attempted_remediation() -> None:
    text = TASKS.read_text()
    assert "linux-modules-extra" in text
    fail_tasks = [t for t in _tasks() if "ansible.builtin.fail" in t]
    assert len(fail_tasks) == 1
    msg = fail_tasks[0]["ansible.builtin.fail"]["msg"]
    assert "modules-extra" in msg, "the abort must say remediation was already tried"


def test_ashmem_modprobe_stays_best_effort() -> None:
    """ashmem_linux does not exist on kernels >= 5.18; Redroid uses memfd."""
    text = TASKS.read_text()
    assert "ashmem_linux" in text
    ashmem_tasks = [t for t in _tasks() if "ashmem" in str(t)]
    assert any(t.get("ignore_errors") is True for t in ashmem_tasks)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/ansible/test_redroid_binder_remediation.py -v`
Expected: FAIL — `test_opt_out_variable_defaults_to_true` raises `KeyError: 'redroid_install_kernel_modules'`, and the remediation/persistence/re-probe tests fail on missing strings.

- [ ] **Step 3: Add the opt-out default**

Replace `ansible/roles/redroid/defaults/main.yml` with:

```yaml
---
redroid_image: redroid/redroid:11.0.0-latest

# Ubuntu cloud images build binder as a module and ship it in
# linux-modules-extra-<kernel>, which is not installed by default. Set
# this to false for air-gapped hosts whose kernel already provides binder
# (or whose packages come from a local mirror) — the role then probes and
# aborts rather than reaching for apt.
redroid_install_kernel_modules: true
```

- [ ] **Step 4: Rewrite the role tasks**

Replace `ansible/roles/redroid/tasks/main.yml` with:

```yaml
---
# Idempotent: the role probes for binder FIRST and does nothing when the
# kernel already provides it, so a converged host reports changed=0.
#
# Ubuntu Noble (and every Ubuntu cloud image) builds binder as a module
# (CONFIG_ANDROID_BINDER_IPC=m) shipped in linux-modules-extra-<kernel>,
# which cloud-init does not install. Redroid needs binder in
# /proc/filesystems; it does NOT need nested virtualization.
#
# ashmem_linux was removed from staging in kernels >= 5.18 and Redroid
# uses memfd instead, so its modprobe is best-effort forever.

- name: Probe binder filesystem support in the running kernel
  ansible.builtin.command: grep -qw binder /proc/filesystems
  register: binder_check
  changed_when: false
  # Handle the rc explicitly below so the failure message can name the
  # VmRole opt-out path instead of a bare grep exit-1.
  failed_when: false

- name: Install kernel modules-extra for the running kernel
  ansible.builtin.apt:
    name: "linux-modules-extra-{{ ansible_kernel }}"
    state: present
    update_cache: yes
    cache_valid_time: 3600
  when:
    - binder_check.rc != 0
    - redroid_install_kernel_modules | bool

- name: Load binder_linux with the three standard device nodes
  community.general.modprobe:
    name: binder_linux
    params: devices=binder,hwbinder,vndbinder
    state: present
  when: binder_check.rc != 0
  register: binder_modprobe
  failed_when: false

- name: Load ashmem_linux (best-effort; absent on kernels >= 5.18)
  community.general.modprobe:
    name: ashmem_linux
    state: present
  ignore_errors: true

- name: Persist binder_linux across reboots
  ansible.builtin.copy:
    content: |
      # Managed by the playground redroid Ansible role.
      binder_linux
    dest: /etc/modules-load.d/redroid-binder.conf
    owner: root
    group: root
    mode: "0644"

- name: Persist the binder_linux devices parameter across reboots
  ansible.builtin.copy:
    content: |
      # Managed by the playground redroid Ansible role.
      options binder_linux devices=binder,hwbinder,vndbinder
    dest: /etc/modprobe.d/redroid-binder.conf
    owner: root
    group: root
    mode: "0644"

- name: Re-probe binder after remediation
  ansible.builtin.command: grep -qw binder /proc/filesystems
  register: binder_recheck
  changed_when: false
  failed_when: false

- name: Gather kernel facts for the abort message
  ansible.builtin.setup:
    filter: ansible_kernel
  when: binder_recheck.rc != 0

- name: Abort cleanly when the kernel still lacks binderfs (with opt-out hint)
  ansible.builtin.fail:
    msg: |
      Host '{{ inventory_hostname }}' is running kernel
      {{ ansible_kernel | default('<unknown>') }}, which does NOT expose
      binder in /proc/filesystems. Redroid requires binder; without it the
      rest of this role would create a non-functional container.

      Already attempted: installing linux-modules-extra for this kernel
      (redroid_install_kernel_modules =
      {{ redroid_install_kernel_modules | default(true) }}) and
      `modprobe binder_linux`.

      To resolve:
        - Confirm the package exists for THIS kernel:
          `apt-cache policy linux-modules-extra-{{ ansible_kernel | default('$(uname -r)') }}`.
          A kernel upgraded by cloud-init but not yet booted is the usual
          cause — reboot the host and re-run, OR
        - Run on an Android-capable kernel (mainline >= 5.10 with
          CONFIG_ANDROID_BINDER_IPC built in or available as a module), OR
        - Remove `redroid` from this VM's VmRole. The conventional opt-out
          is to use `docker-host` instead of `redroid-host` under
          config/roles/, or to drop `ansible_role: redroid` from a custom
          VmRole's `spec.provisioners` list.

      site.yml dispatches the redroid play only on the `[needs_redroid]`
      inventory group, so removing the provisioner drops this host from the
      play entirely on the next apply.
  when: binder_recheck.rc != 0

- name: Create binderfs mountpoint
  ansible.builtin.file:
    path: /dev/binderfs
    state: directory
    mode: '0755'

- name: Mount binderfs
  ansible.posix.mount:
    path: /dev/binderfs
    src: binder
    fstype: binder
    state: mounted

- name: Pull redroid image
  community.docker.docker_image:
    name: "{{ redroid_image }}"
    source: pull

- name: Run redroid container
  community.docker.docker_container:
    name: "redroid_{{ inventory_hostname | regex_replace('[^A-Za-z0-9_.-]', '_') }}"
    image: "{{ redroid_image }}"
    privileged: yes
    restart_policy: unless-stopped
    volumes:
      - /dev/binderfs:/dev/binderfs
    ports:
      - "5555:5555"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/ansible/test_redroid_binder_remediation.py -v`
Expected: 7 passed.

- [ ] **Step 6: Syntax-check the playbook**

Run: `cd ansible && ansible-playbook -i /dev/null site.yml --syntax-check`
Expected: exit 0. If `ansible-playbook` is not installed, skip this step and say so in your report — do not install it.

- [ ] **Step 7: Confirm the baseline failure set is unchanged**

Run: `uv run pytest -q 2>&1 | tail -8`
Expected: the SAME 4 failures listed in the Baseline section, plus your 7 new passes. If a fifth failure appears, you caused it — fix it before committing.

- [ ] **Step 8: Commit**

```bash
git add ansible/roles/redroid tests/unit/ansible/test_redroid_binder_remediation.py
git commit -m "redroid: install binder modules-extra and persist the module load

Ubuntu cloud images build binder as a module in linux-modules-extra and do
not install it, so the role's binder assertion failed on stock images. Probe
first, install for the running kernel, load with the standard device nodes,
persist via modules-load.d/modprobe.d, then re-probe before continuing.

Also fixes a latent gap on every backend: the modprobe was never persisted,
so binder disappeared on reboot."
```

---

### Task 2: Provider capability vocabulary and the validator cross-check

**Files:**
- Modify: `config/providers/local-libvirt.yaml`, `config/providers/local-vbox.yaml`, `config/providers/cloud-digitalocean.yaml`
- Modify: `src/playground/validation/validator.py`
- Test: `tests/unit/validation/test_capabilities.py` (create)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `_capabilities_for_vm(loaded: LoadedConfig, vm: LabVm) -> dict[str, Any]` in `validator.py`
  - `_check_role_provider_capabilities(lab: Lab, loaded: LoadedConfig, source: SourceLocation) -> list[Diagnostic]` in `validator.py`
  - Diagnostic ID `config.backend.capability_unsupported`
  - Provider capability key `redroid` on all three provider configs. **Task 3 depends on `cloud-digitalocean` declaring `redroid: true`.**

**Background you need:** Role capabilities and provider capabilities currently use DISJOINT vocabularies. Roles declare `docker`/`compose`/`swarm`/`redroid` (verified: `redroid-host` -> `{'docker': True, 'compose': True, 'swarm': True, 'redroid': True}`); providers declare `nested_virtualization`/`privileged_containers`. There is no overlapping key today, so a same-key comparison finds nothing until you add `redroid` to the provider side. That is the point of this task.

`ProviderConfigSpec` declares only `driver`; everything else rides on `model_config = ConfigDict(extra="allow")`. So `capabilities` is NOT a typed attribute and may be absent entirely. You MUST use `getattr(provider.spec, "capabilities", None) or {}` — a direct attribute access raises `AttributeError` on a provider config that omits the block.

Role capability inheritance merges root -> leaf (leaf wins), matching `_deep_merge_spec` in `config/resolver.py`. `_role_ancestors(loaded, role_name)` returns leaf -> root, so you must reverse it.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/validation/test_capabilities.py`:

```python
"""Tests for the VmRole-vs-ProviderConfig capability cross-check."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from ruamel.yaml import YAML

from playground.config.loader import LoadedConfig, load_config
from playground.models.kinds import Lab, parse_resource
from playground.validation.validator import _capabilities_for_vm, validate

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

_yaml = YAML(typ="safe")

CAPABILITY_UNSUPPORTED = "config.backend.capability_unsupported"


@pytest.fixture
def committed_load() -> LoadedConfig:
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    return loaded


def _yaml_to_lab(text: str) -> Lab:
    raw = _yaml.load(dedent(text).lstrip("\n"))
    lab = parse_resource(raw)
    assert isinstance(lab, Lab)
    return lab


def _redroid_lab(backend: str, name: str) -> Lab:
    return _yaml_to_lab(
        f"""
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: {name}
        spec:
          backend: {backend}
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: droid1
              role: redroid-host
              networks: [net]
        """
    )


def test_capabilities_for_vm_unions_the_inheritance_chain(
    committed_load: LoadedConfig,
) -> None:
    """redroid-host extends docker-host; the child must inherit and add."""
    lab = _redroid_lab("local-libvirt", "cap-union")
    caps = _capabilities_for_vm(committed_load, lab.spec.vms[0])
    assert caps["redroid"] is True
    assert caps["docker"] is True  # inherited from docker-host
    assert caps["swarm"] is True


def test_warns_when_backend_explicitly_declares_the_capability_false(
    committed_load: LoadedConfig,
) -> None:
    bad = _redroid_lab("local-vbox", "redroid-on-vbox")
    committed_load.labs[bad.metadata.name] = bad

    diagnostics = validate(committed_load, lab=bad.metadata.name)

    matching = [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]
    assert len(matching) == 1
    assert matching[0].severity == "warning"
    assert "redroid" in matching[0].message
    assert "local-vbox" in matching[0].message
    assert matching[0].key_path == "spec.vms[0].role"


def test_no_warning_when_backend_supports_the_capability(
    committed_load: LoadedConfig,
) -> None:
    good = _redroid_lab("cloud-digitalocean", "redroid-on-do")
    committed_load.labs[good.metadata.name] = good

    diagnostics = validate(committed_load, lab=good.metadata.name)

    assert not [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]


def test_no_warning_when_provider_is_silent_about_the_capability(
    committed_load: LoadedConfig,
) -> None:
    """An undeclared capability is not the same as an unsupported one."""
    provider = committed_load.providers["local-libvirt"]
    stripped = provider.spec.model_copy(update={"capabilities": {}})
    committed_load.providers["local-libvirt"] = provider.model_copy(
        update={"spec": stripped}
    )
    lab = _redroid_lab("local-libvirt", "silent-provider")
    committed_load.labs[lab.metadata.name] = lab

    diagnostics = validate(committed_load, lab=lab.metadata.name)

    assert not [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]


def test_no_warning_when_provider_omits_capabilities_entirely(
    committed_load: LoadedConfig,
) -> None:
    """ProviderConfigSpec only declares `driver`; capabilities may be absent."""
    provider = committed_load.providers["local-libvirt"]
    bare = provider.spec.model_copy(update={"capabilities": None})
    object.__delattr__  # documentation: pydantic extras cannot be deleted cleanly
    committed_load.providers["local-libvirt"] = provider.model_copy(
        update={"spec": bare}
    )
    lab = _redroid_lab("local-libvirt", "bare-provider")
    committed_load.labs[lab.metadata.name] = lab

    diagnostics = validate(committed_load, lab=lab.metadata.name)

    assert not [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]


def test_false_role_capability_never_warns(committed_load: LoadedConfig) -> None:
    """Only capabilities the role actually asks for are checked."""
    lab = _yaml_to_lab(
        """
        apiVersion: playground/v1
        kind: Lab
        metadata:
          name: plain-node-on-vbox
        spec:
          backend: local-vbox
          networks:
            - name: net
              profile: nat
              cidr: 10.99.0.0/24
          vms:
            - name: plain
              role: generic-node
              networks: [net]
        """
    )
    committed_load.labs[lab.metadata.name] = lab

    diagnostics = validate(committed_load, lab=lab.metadata.name)

    assert not [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]


def test_committed_config_emits_no_capability_warnings(
    committed_load: LoadedConfig,
) -> None:
    """Every committed lab must run on a backend that supports its roles."""
    diagnostics = validate(committed_load)
    matching = [d for d in diagnostics if d.id == CAPABILITY_UNSUPPORTED]
    assert matching == [], f"committed config has capability mismatches: {matching}"
```

Note on `test_no_warning_when_provider_omits_capabilities_entirely`: the stray `object.__delattr__` line is a no-op reference left as a marker. **Delete that line** when you implement — it is there only to flag that pydantic extras cannot be removed by `model_copy`. If setting `capabilities` to `None` does not exercise the absent-attribute path, replace the body with a hand-built `ProviderConfig` parsed from YAML that has no `capabilities:` key at all, e.g.:

```python
    from playground.models.kinds import ProviderConfig
    bare = parse_resource(
        _yaml.load(dedent("""
            apiVersion: playground/v1
            kind: ProviderConfig
            metadata:
              name: local-libvirt
            spec:
              driver: local-libvirt
        """).lstrip("\n"))
    )
    assert isinstance(bare, ProviderConfig)
    committed_load.providers["local-libvirt"] = bare
```

Use whichever form actually exercises "attribute absent"; verify with `getattr(spec, "capabilities", "MISSING")`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/validation/test_capabilities.py -v`
Expected: FAIL — `ImportError: cannot import name '_capabilities_for_vm' from 'playground.validation.validator'`.

- [ ] **Step 3: Add the `redroid` capability to the three provider configs**

In `config/providers/cloud-digitalocean.yaml`, replace the `capabilities:` block with:

```yaml
  capabilities:
    nested_virtualization: false
    privileged_containers: true
    # Redroid needs binder in the Droplet's own kernel, not nested virt —
    # there is no nesting on a cloud VM. Ubuntu images ship binder in
    # linux-modules-extra, which the redroid Ansible role installs.
    # Verified live on ubuntu-24-04-x64 / kernel 6.8.0-124-generic.
    redroid: true
```

In `config/providers/local-libvirt.yaml`, replace the `capabilities:` block with:

```yaml
  capabilities:
    nested_virtualization: true
    privileged_containers: true
    # cpu mode host-passthrough in tofu/main.tf lets the guest kernel
    # provide binder.
    redroid: true
```

In `config/providers/local-vbox.yaml`, replace the `capabilities:` block with:

```yaml
  capabilities:
    nested_virtualization: false
    privileged_containers: true
    # VirtualBox does not pass through the CPU features the guest kernel
    # needs for binderfs — see docs/architecture/CONTRACTS.md, "vbox: no
    # nested virt -> Redroid won't work there".
    redroid: false
```

Keep every other key in those files untouched.

- [ ] **Step 4: Add the two helpers to `validator.py`**

Append near `_resources_for_vm` (around line 760):

```python
def _capabilities_for_vm(loaded: LoadedConfig, vm: LabVm) -> dict[str, Any]:
    """Union a VM's role capabilities across the whole ``extends`` chain.

    Unlike ``image`` / ``resources`` — where the first non-``None`` value
    along the chain wins — capabilities MERGE, with the leaf overriding
    same-named ancestor keys. This mirrors ``_deep_merge_spec`` in
    ``config/resolver.py``, which merges root -> leaf. ``_role_ancestors``
    returns leaf -> root, hence the ``reversed``.
    """
    merged: dict[str, Any] = {}
    for ancestor in reversed(_role_ancestors(loaded, vm.role)):
        role = loaded.roles.get(ancestor)
        if role is not None:
            merged.update(role.spec.capabilities)
    return merged


def _check_role_provider_capabilities(
    lab: Lab,
    loaded: LoadedConfig,
    source: SourceLocation,
) -> list[Diagnostic]:
    """Warn when a VM's role wants a capability its backend declares false.

    Permissive per engineering principle #10: the operator may know
    something the provider config does not, so this warns rather than
    blocking. A capability the provider does not mention produces nothing —
    ``ProviderConfigSpec`` is an open model and "undeclared" is not
    "unsupported".
    """
    provider = loaded.providers.get(lab.spec.backend)
    if provider is None:
        # config.reference.unknown_provider already covers this.
        return []
    provider_caps = getattr(provider.spec, "capabilities", None) or {}
    if not isinstance(provider_caps, dict):
        return []

    diagnostics: list[Diagnostic] = []
    for idx, vm in enumerate(lab.spec.vms):
        for capability, wanted in sorted(_capabilities_for_vm(loaded, vm).items()):
            if not wanted:
                continue
            if provider_caps.get(capability) is not False:
                continue
            diagnostics.append(
                Diagnostic(
                    id="config.backend.capability_unsupported",
                    severity="warning",
                    message=(
                        f"VM {vm.name!r} in lab {lab.metadata.name!r} uses role "
                        f"{vm.role!r}, which declares capability "
                        f"{capability!r}, but backend {lab.spec.backend!r} "
                        f"declares {capability}: false"
                    ),
                    source=source,
                    key_path=f"spec.vms[{idx}].role",
                    suggestion=(
                        f"choose a backend whose ProviderConfig declares "
                        f"{capability}: true, use a VmRole without that "
                        f"capability, or set {capability}: true in "
                        f"config/providers/{lab.spec.backend}.yaml if the "
                        "backend really does support it"
                    ),
                )
            )
    return diagnostics
```

If `Any` or `LabVm` are not already imported in `validator.py`, add them (`from typing import Any`, and `LabVm` from `playground.models.kinds`). Check the existing imports first — `_resources_for_vm` already takes a `LabVm`, so it is almost certainly imported.

- [ ] **Step 5: Register the check**

In `_check_lab`, next to the other per-lab helper calls (around line 334), add the new line so the block reads:

```python
    diagnostics.extend(_check_budget(lab, loaded, source))
    diagnostics.extend(_check_offline_artifacts(lab, loaded, source))
    diagnostics.extend(_check_backend_capability(lab, loaded, source))
    diagnostics.extend(_check_role_provider_capabilities(lab, loaded, source))
    diagnostics.extend(_check_network_ips(lab, source))
    diagnostics.extend(_check_dns_domain(lab, source))
```

- [ ] **Step 6: Add the new ID to the module docstring**

`validator.py`'s module docstring (lines 9-32) lists every diagnostic ID. Add `config.backend.capability_unsupported` next to `config.backend.per_vm_resources_unsupported`, matching the surrounding format exactly.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/validation/test_capabilities.py -v`
Expected: 7 passed.

- [ ] **Step 8: Extend the per-VM-resources warning to cloud-digitalocean**

`_check_backend_capability` currently early-returns for every backend but `local-libvirt`. The DigitalOcean backend has the same limitation for a different reason: `_TFVARS_KEYS` in `src/playground/backend/cloud_digitalocean/tfvars.py` carries a single scalar `size`, so one Droplet size applies to every VM in the lab. Change the guard and the message:

```python
    if lab.spec.backend not in ("local-libvirt", "cloud-digitalocean"):
        return []
```

and make the message and suggestion backend-specific:

```python
    if lab.spec.backend == "cloud-digitalocean":
        detail = (
            "the cloud-digitalocean backend applies a single Droplet `size` "
            "slug to every VM in the lab"
        )
        suggestion = (
            "set spec.providers.cloud-digitalocean.size to a slug that fits "
            "the largest VM; per-VM resources are advisory on this backend"
        )
    else:
        detail = (
            "the local-libvirt backend applies global "
            "var.vm_memory/var.vm_vcpu uniformly"
        )
        suggestion = (
            "tune var.vm_memory and var.vm_vcpu in tofu/terraform.tfvars "
            "to fit the largest VM, or wait for tofu support for per-VM "
            "resources"
        )
```

Then use `detail` and `suggestion` in the returned `Diagnostic`, keeping the ID `config.backend.per_vm_resources_unsupported` and severity `warning`. Keep the message prefix `f"lab {lab.metadata.name!r} declares heterogeneous per-VM resources, but "` so existing assertions on the ID and severity still hold.

- [ ] **Step 9: Verify no committed lab newly warns**

Run:
```bash
STASH=/tmp/pg-untracked-labs && mkdir -p $STASH
mv config/labs/barak-deploy-cross-vm.yaml config/labs/barak-dev-fleet-cloud.yaml config/labs/barak-fleet-airgap.yaml $STASH/
uv run pytest -q tests/cli/test_cli.py tests/unit/validation/
mv $STASH/*.yaml config/labs/
```
Expected: all pass. `cloud-smoke` has one VM, so the extended heterogeneity check cannot fire on it. If `test_validate_committed_config_succeeds` now reports a different warning count, you have introduced a warning on a committed lab — find it with `uv run playground validate --config-dir config` and fix the cause, do not edit the assertion.

- [ ] **Step 10: Lint and typecheck**

Run: `uv run ruff check src tests && uv run mypy src`
Expected: clean. `mypy` is `strict = true`; `getattr(provider.spec, "capabilities", None)` returns `Any`, which is why the `isinstance(provider_caps, dict)` guard is there — keep it.

- [ ] **Step 11: Confirm the baseline failure set is unchanged, then commit**

```bash
uv run pytest -q 2>&1 | tail -8
git add config/providers src/playground/validation/validator.py tests/unit/validation/test_capabilities.py
git commit -m "validation: cross-check VmRole capabilities against the backend

Provider capability blocks were declarative only — nothing read them. Add a
shared 'redroid' key to the role and provider vocabularies and warn when a
lab asks a backend for a capability it declares false.

cloud-digitalocean declares redroid: true (verified live); local-vbox
declares false. Warning, not error, per principle 10.

Also extends the per-VM-resources warning to cloud-digitalocean, which
applies one Droplet size slug to every VM."
```

---

### Task 3: The `redroid-cloud` lab

**Files:**
- Create: `config/labs/redroid-cloud.yaml`
- Modify: `tests/unit/config/test_loader.py`

**Interfaces:**
- Consumes: Task 2's `redroid: true` on `config/providers/cloud-digitalocean.yaml`. **Do not start this task until Task 2 is committed** — without it the new lab emits a `config.backend.capability_unsupported` warning and Step 4 fails.
- Produces: lab name `redroid-cloud`, used by Task 6's live validation.

- [ ] **Step 1: Write the failing test**

In `tests/unit/config/test_loader.py`, add after `test_cloud_smoke_lab_loads_in_committed_config`:

```python
def test_redroid_cloud_lab_loads_in_committed_config() -> None:
    """The shipped cloud Redroid lab parses and resolves cleanly."""
    loaded, diagnostics = load_config(CONFIG_DIR)
    assert diagnostics == []
    assert "redroid-cloud" in loaded.labs

    lab = loaded.labs["redroid-cloud"]
    assert lab.spec.backend == "cloud-digitalocean"
    assert [vm.role for vm in lab.spec.vms] == ["redroid-host"]
    # DigitalOcean applies ONE size slug to every Droplet in the lab, so the
    # slug — not the role's resources block — is the real control. It must
    # be big enough for an Android container.
    assert lab.spec.providers["cloud-digitalocean"]["size"] == "s-4vcpu-8gb"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/config/test_loader.py::test_redroid_cloud_lab_loads_in_committed_config -v`
Expected: FAIL — `AssertionError: assert 'redroid-cloud' in {...}`.

- [ ] **Step 3: Create the lab**

Create `config/labs/redroid-cloud.yaml`:

```yaml
# redroid-cloud — containerized Android on a DigitalOcean Droplet
#
# Requires:  export DIGITALOCEAN_TOKEN=<your-token>   before apply.
#
# Redroid needs `binder` in the Droplet's own kernel — NOT nested
# virtualization; there is no nesting on a cloud VM. Ubuntu images build
# binder as a module in linux-modules-extra, which the `redroid` Ansible
# role installs and persists.
#
# ADB has no authentication, so the DigitalOcean firewall opens port 22
# only. Reach the device through an SSH tunnel:
#
#     playground apply redroid-cloud
#     playground adb --lab redroid-cloud --on droid1
#     adb connect 127.0.0.1:5555
#
# This Droplet size bills at roughly $0.07/hr. Run
# `playground destroy redroid-cloud` when you are done.
apiVersion: playground/v1
kind: Lab
metadata:
  name: redroid-cloud
  description: |
    Single-VM DigitalOcean lab running Redroid (containerized Android 11)
    reachable over ADB through an SSH tunnel.
  tags: [cloud, digitalocean, redroid, android]

spec:
  backend: cloud-digitalocean
  offline: false

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
      cidr: 10.70.0.0/24

  vms:
    - name: droid1
      role: redroid-host
      networks: [lab-net]

  providers:
    cloud-digitalocean:
      region: nyc3
      # The redroid-host role asks for 4 vCPU / 8 GB / 60 GB. DigitalOcean
      # applies ONE size slug to every Droplet in the lab, so this slug —
      # not the role's resources block — is what actually provisions.
      size: s-4vcpu-8gb
```

- [ ] **Step 4: Run the test to verify it passes, with a clean config tree**

```bash
STASH=/tmp/pg-untracked-labs && mkdir -p $STASH
mv config/labs/barak-deploy-cross-vm.yaml config/labs/barak-dev-fleet-cloud.yaml config/labs/barak-fleet-airgap.yaml $STASH/
uv run pytest -q tests/unit/config/ tests/unit/validation/ tests/cli/test_cli.py
mv $STASH/*.yaml config/labs/
```
Expected: all pass, INCLUDING `test_validate_committed_config_succeeds`, which asserts the literal `"0 errors, 1 warnings"`.

**If that CLI test now fails on the warning count, your new lab emitted a warning.** Diagnose with:
```bash
uv run playground validate --config-dir config
```
(NOTE: `validate` has no `--lab` flag. To scope to one lab, call the API:
`validate(loaded, Path('ansible/roles'), lab='redroid-cloud')`.)
Fix the lab (most likely the budget is too tight — raise it), not the assertion. The lab is expected to validate with ZERO diagnostics.

- [ ] **Step 5: Confirm the baseline failure set is unchanged, then commit**

```bash
uv run pytest -q 2>&1 | tail -8
git add config/labs/redroid-cloud.yaml tests/unit/config/test_loader.py
git commit -m "labs: add redroid-cloud, Redroid on a DigitalOcean Droplet

One redroid-host Droplet at s-4vcpu-8gb. ADB is reached over an SSH tunnel
(playground adb) because the firewall opens port 22 only and ADB has no
authentication."
```

---

### Task 4: The `playground adb` verb

**Files:**
- Modify: `src/playground/cli/main.py`
- Test: `tests/cli/test_adb.py` (create)

**Interfaces:**
- Consumes: nothing from other tasks. Uses existing helpers `_load_config_or_exit`, `_resolve_lab_or_exit`, `_exit_on_errors`, `_exit_with_diagnostic`, `_has_errors`, `_print_warnings`, and `query_status` from `playground.backend.dispatch`.
- Produces: CLI verb `adb`. Diagnostic IDs `config.adb.lab_required`, `config.adb.unknown_vm`, `config.adb.vm_ip_not_found`, `runtime.adb.ssh_binary_missing`, `runtime.adb.no_free_port`.

**Background you need:** `exec` (at `src/playground/cli/main.py:847`) is the model to copy. It resolves the VM's SSH endpoint through `query_status(resolved, tofu_dir)`, which is backend-neutral: `VmStatus.ssh_host` / `VmStatus.ssh_port` are populated for libvirt (IP, 22), vbox (127.0.0.1, NAT port) and DigitalOcean (public IP, 22). Do NOT read `.playground/state` directly. The repo's ssh invocations pass NO identity file — ambient ssh-agent / `~/.ssh/config` resolution is the convention. The three baked-in options are `-o StrictHostKeyChecking=accept-new`, `-o UserKnownHostsFile=/dev/null`, `-o LogLevel=ERROR`, and `-p <port>` is added only when `ssh_port` is truthy and `!= 22`.

The Redroid container publishes 5555 on the VM (`ansible/roles/redroid/tasks/main.yml`). This verb forwards a local port to `127.0.0.1:5555` on the VM and blocks until Ctrl-C.

- [ ] **Step 1: Write the failing test**

Create `tests/cli/test_adb.py`:

```python
"""Tests for the `playground adb` SSH-tunnel verb."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from playground.cli.main import app

from .test_cli import _write_apply_shims, _write_ssh_shim

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _invoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    bin_dir = _write_apply_shims(tmp_path)
    ssh_bin = _write_ssh_shim(tmp_path, exit_code=0)
    monkeypatch.setenv(
        "PATH", f"{ssh_bin}{os.pathsep}{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )
    tofu_dir = tmp_path / "tofu"
    tofu_dir.mkdir(exist_ok=True)
    return CliRunner().invoke(
        app,
        [
            "adb",
            "--lab", "generic-infra",
            "--config-dir", str(CONFIG_DIR),
            "--tofu-dir", str(tofu_dir),
            *args,
        ],
    )


def test_adb_builds_a_local_forward_to_5555(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(tmp_path, monkeypatch, "--on", "docker1")

    assert result.exit_code == 0, result.stderr
    ssh_log = (tmp_path / "ssh.log").read_text()
    assert "-L" in ssh_log
    assert "5555:127.0.0.1:5555" in ssh_log
    assert "ubuntu@10.0.10.43" in ssh_log  # docker1's IP from the tofu shim
    assert "-N" in ssh_log  # no remote command; forwarding only


def test_adb_prints_the_connect_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(tmp_path, monkeypatch, "--on", "docker1")

    assert result.exit_code == 0
    assert "adb connect 127.0.0.1:5555" in result.output


def test_adb_honors_an_explicit_local_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(tmp_path, monkeypatch, "--on", "docker1", "--local-port", "5599")

    assert result.exit_code == 0
    ssh_log = (tmp_path / "ssh.log").read_text()
    assert "5599:127.0.0.1:5555" in ssh_log
    assert "adb connect 127.0.0.1:5599" in result.output


def test_adb_picks_the_next_free_port_when_the_default_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 5555))
    holder.listen(1)
    try:
        result = _invoke(tmp_path, monkeypatch, "--on", "docker1")
    finally:
        holder.close()

    assert result.exit_code == 0, result.stderr
    ssh_log = (tmp_path / "ssh.log").read_text()
    assert "5555:127.0.0.1:5555" not in ssh_log
    assert ":127.0.0.1:5555" in ssh_log  # some other local port was chosen
    assert "adb connect 127.0.0.1:" in result.output


def test_adb_unknown_vm_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke(tmp_path, monkeypatch, "--on", "nope")

    assert result.exit_code == 1
    assert "config.adb.unknown_vm" in result.output + result.stderr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/cli/test_adb.py -v`
Expected: FAIL — every test exits non-zero because Typer reports `No such command 'adb'`.

First confirm the two helpers are importable from `tests/cli/test_cli.py`. Run:
```bash
uv run python -c "from tests.cli.test_cli import _write_apply_shims, _write_ssh_shim; print('ok')"
```
If that fails because `tests/cli/` has no `__init__.py`, copy the two helper functions into `tests/cli/test_adb.py` verbatim from `tests/cli/test_cli.py:90-127` and `tests/cli/test_cli.py:1215-1230` instead of importing them, and drop the `from .test_cli import ...` line.

- [ ] **Step 3: Add the port helper**

Add near the other private helpers at the bottom of `src/playground/cli/main.py`:

```python
def _first_free_local_port(preferred: int, attempts: int = 20) -> int | None:
    """Return ``preferred`` if bindable on loopback, else the next free port.

    Returns ``None`` when no port in the scan window is free. Binding is the
    only reliable probe — checking a listener table races with every other
    process on the box.
    """
    for candidate in range(preferred, preferred + attempts):
        if candidate > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    return None
```

Add `import socket` to the imports at the top of the file if it is not already there.

- [ ] **Step 4: Add the `adb` command**

Add after the `exec` command (which ends around line 1006):

```python
ADB_REMOTE_PORT = 5555
"""Port the redroid container publishes on the VM (ansible/roles/redroid)."""


@app.command(
    "adb",
    help=(
        "Open an SSH tunnel to a lab VM's Redroid ADB port and print the "
        "`adb connect` line. Blocks until interrupted."
    ),
)
def adb_command(
    on: Annotated[
        str,
        typer.Option("--on", "--host", help="VM name within the lab (alias: --host)."),
    ],
    lab: Annotated[
        str | None,
        typer.Option(
            "--lab",
            help=(
                "Lab name. Defaults to the only configured lab; required "
                "when multiple labs are configured."
            ),
        ),
    ] = None,
    local_port: Annotated[
        int | None,
        typer.Option(
            "--local-port",
            help=(
                f"Local port to forward from (default: {ADB_REMOTE_PORT}, or "
                "the next free port when it is taken)."
            ),
        ),
    ] = None,
    user: Annotated[
        str,
        typer.Option("--user", help="SSH user (default: ubuntu)."),
    ] = "ubuntu",
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", "-c", help="Config directory to load."),
    ] = Path("config"),
    tofu_dir: Annotated[
        Path,
        typer.Option("--tofu-dir", help="OpenTofu working directory."),
    ] = Path("tofu"),
) -> None:
    # ADB has NO authentication, which is why this is a tunnel and not a
    # firewall rule: the cloud backend deliberately opens port 22 only.
    loaded, diagnostics = _load_config_or_exit(config_dir, OutputFormat.human)
    if not _has_errors(diagnostics):
        diagnostics.extend(validate_loaded_config(loaded, lab=lab))
    _exit_on_errors(diagnostics, OutputFormat.human, json_errors=False)
    _print_warnings(diagnostics)

    if lab is None:
        if len(loaded.labs) == 1:
            lab = next(iter(loaded.labs))
        else:
            _exit_with_diagnostic(
                Diagnostic(
                    id="config.adb.lab_required",
                    severity="error",
                    message=(
                        f"--lab required when {len(loaded.labs)} labs are "
                        "configured; pass --lab <name>"
                    ),
                    source=SourceLocation(path=str(config_dir / "labs")),
                    suggestion="run `playground lab list` and pass --lab <name>",
                ),
                OutputFormat.human,
                json_errors=False,
            )

    resolved = _resolve_lab_or_exit(loaded, lab, config_dir, OutputFormat.human)

    vm_names = {vm.name for vm in resolved.vms}
    if on not in vm_names:
        _exit_with_diagnostic(
            Diagnostic(
                id="config.adb.unknown_vm",
                severity="error",
                message=(
                    f"VM {on!r} is not declared in lab {lab!r} "
                    f"(known VMs: {sorted(vm_names) or '<none>'})"
                ),
                source=SourceLocation(path=f"config/labs/{lab}.yaml"),
                key_path="spec.vms",
            ),
            OutputFormat.human,
            json_errors=False,
        )

    status, query_diagnostics = query_status(resolved, tofu_dir)
    _exit_on_errors(query_diagnostics, OutputFormat.human, json_errors=False)

    vm_status = next((v for v in status.vms if v.name == on), None)
    ssh_host = vm_status.ssh_host if vm_status else None
    ssh_port = vm_status.ssh_port if vm_status else None
    if not ssh_host:
        _exit_with_diagnostic(
            Diagnostic(
                id="config.adb.vm_ip_not_found",
                severity="error",
                message=(
                    f"VM {on!r} has no reachable SSH endpoint — "
                    "has the lab been applied?"
                ),
                source=SourceLocation(path=str(config_dir / "labs" / f"{lab}.yaml")),
                suggestion=f"run `playground apply {lab}` first",
            ),
            OutputFormat.human,
            json_errors=False,
        )

    chosen = local_port if local_port is not None else _first_free_local_port(
        ADB_REMOTE_PORT
    )
    if chosen is None:
        _exit_with_diagnostic(
            Diagnostic(
                id="runtime.adb.no_free_port",
                severity="error",
                message=(
                    f"no free local port found in "
                    f"{ADB_REMOTE_PORT}-{ADB_REMOTE_PORT + 19}"
                ),
                source=SourceLocation(path="<local>"),
                suggestion="pass --local-port <port> with a port you know is free",
            ),
            OutputFormat.human,
            json_errors=False,
        )

    ssh_argv = [
        "ssh",
        *(["-p", str(ssh_port)] if ssh_port and ssh_port != 22 else []),
        "-N",
        "-L", f"{chosen}:127.0.0.1:{ADB_REMOTE_PORT}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        f"{user}@{ssh_host}",
    ]

    typer.echo(f"tunnel: 127.0.0.1:{chosen} -> {on}:{ADB_REMOTE_PORT} (via {ssh_host})")
    typer.echo(f"adb connect 127.0.0.1:{chosen}")
    typer.echo("Ctrl-C to close the tunnel.")

    try:
        completed = subprocess.run(ssh_argv, check=False)  # noqa: S603
    except FileNotFoundError as exc:
        _exit_with_diagnostic(
            Diagnostic(
                id="runtime.adb.ssh_binary_missing",
                severity="error",
                message=f"failed to launch ssh: {exc}",
                source=SourceLocation(path="ssh"),
                suggestion="install openssh-client",
            ),
            OutputFormat.human,
            json_errors=False,
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=0) from None
    raise typer.Exit(code=completed.returncode)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/cli/test_adb.py -v`
Expected: 5 passed.

If `test_adb_picks_the_next_free_port_when_the_default_is_taken` is flaky because port 5555 is genuinely in use on the machine, that is a real signal — the helper is working. Keep the test; it binds and releases its own socket.

- [ ] **Step 6: Lint and typecheck**

Run: `uv run ruff check src tests && uv run mypy src`
Expected: clean.

- [ ] **Step 7: Confirm the baseline failure set is unchanged, then commit**

```bash
uv run pytest -q 2>&1 | tail -8
git add src/playground/cli/main.py tests/cli/test_adb.py
git commit -m "cli: add \`playground adb\` SSH-tunnel verb

ADB has no authentication, so cloud labs must never expose 5555. Forward a
local port to the VM's 5555 over SSH and print the adb connect line.
Resolves the endpoint through the backend-neutral query_status, so it works
on libvirt, vbox NAT ports, and DigitalOcean public IPs alike."
```

---

### Task 5: Correct the nested-virt framing

**Files:**
- Modify: `src/playground/preflight/doctor.py` (around lines 231-270)
- Modify: `docs/architecture/CONTRACTS.md`
- Modify: `docs/product/mvp_scope.md`
- Modify: `CLAUDE.md:57`

**Interfaces:**
- Consumes: Task 1's `redroid_install_kernel_modules` variable name (referenced in prose).
- Produces: documentation only. No code symbols.

**Background you need:** `check_kvm_nested_enabled` in `doctor.py` is a legitimate check — libvirt guests DO need nested virt for some workloads. What is wrong is its stated JUSTIFICATION: it names `redroid-host` as the reason, and the docstring says redroid "containers need nested-virt features". They do not. They need binder. Keep the check; fix the reason.

- [ ] **Step 1: Check whether any test asserts the current message text**

Run: `grep -rn "nested_disabled\|nested-virt\|redroid-host" tests/`
Note every hit. If a test asserts the literal message string you are about to change, update that test in the same commit.

- [ ] **Step 2: Fix the docstring**

In `src/playground/preflight/doctor.py`, in `check_kvm_nested_enabled`, replace the sentence `both relevant to the redroid-host lab, whose containers need nested-virt features.` with:

```
    both relevant to any lab whose guests run nested hypervisors or
    virt-accelerated workloads. (Redroid is NOT such a case: it is a
    container needing `binder` in the guest kernel, which the redroid
    Ansible role installs. Do not cite it as the reason for this check.)
```

- [ ] **Step 3: Fix the user-facing message**

In the same function, replace:

```python
                "Labs that require nested-virt features (e.g. redroid-host) "
                "will fail to start their guest workloads."
```

with:

```python
                "Labs whose guests run nested hypervisors or virt-accelerated "
                "workloads will fail to start them."
```

- [ ] **Step 4: Run the doctor tests**

Run: `uv run pytest tests/unit/preflight/ -v`
Expected: pass (one environment-specific skip is normal — see the Baseline section).

- [ ] **Step 5: Add a CONTRACTS.md section**

`docs/architecture/CONTRACTS.md` has a "Cross-layer pitfalls" section starting at line 387. Insert a new subsection immediately BEFORE `### vbox: no nested virt → Redroid won't work there` (line 509), so the two read together:

```markdown
### Redroid needs binder, not nested virtualization

The recurring misstatement in this repo was that Redroid requires nested
virt. It does not. Redroid is a container: it needs `binder` in
`/proc/filesystems` on the kernel it runs on, plus a mounted binderfs.

Nested virt matters on `local-libvirt` only, and only indirectly — Redroid
runs inside a libvirt *guest*, so that guest's kernel must expose binder,
which is what `cpu { mode = "host-passthrough" }` in `tofu/main.tf` buys.
On a cloud VM there is no nesting at all: Redroid runs directly on the
instance kernel.

Ubuntu builds binder as a MODULE (`CONFIG_ANDROID_BINDER_IPC=m`,
`CONFIG_ANDROID_BINDERFS=m`) and ships it in
`linux-modules-extra-$(uname -r)`, which cloud images do not install. The
`redroid` Ansible role installs it for the running kernel, loads
`binder_linux` with `devices=binder,hwbinder,vndbinder`, and persists both
via `/etc/modules-load.d/` and `/etc/modprobe.d/`. Set
`redroid_install_kernel_modules: false` to opt out on air-gapped hosts.

Verified live on DigitalOcean `ubuntu-24-04-x64`, kernel
`6.8.0-124-generic`: Redroid 11 booted (`sys.boot_completed=1`) and served
ADB.

**The failure mode to expect** is kernel/package skew. The package must
match the RUNNING kernel. DigitalOcean's cloud-init sets
`package_upgrade: true`, so an upgrade can install a newer kernel whose
modules only take effect after a reboot. The role aborts with both versions
named rather than building a non-functional container.

**ADB has no authentication.** Never open 5555 to a network. The
`cloud-digitalocean` firewall (`tofu/cloud_digitalocean/main.tf`) opens port
22 only; reach the device with `playground adb --lab <lab> --on <vm>`,
which forwards a local port over SSH.
```

- [ ] **Step 6: Update `docs/product/mvp_scope.md`**

The "Deferred But Designed For" list has `- Redroid/Android device lifecycle.` at line 61, and the file already carries a dated update note for the DigitalOcean backend at the end of that section. Add a matching note in the same style:

```markdown
  > Update (2026-08-31): Redroid now runs on `cloud-digitalocean` as well as
  > `local-libvirt` — see `config/labs/redroid-cloud.yaml` and
  > `docs/superpowers/specs/2026-08-31-cloud-redroid-design.md`. The
  > *device lifecycle* items above (ADB automation, APK installation)
  > remain deferred; what landed is host provisioning plus a tunnelled ADB
  > endpoint.
```

- [ ] **Step 7: Update `CLAUDE.md:57`**

Replace the `**Redroid role mounts binderfs.**` bullet with:

```markdown
- **Redroid needs binder, not nested virt.** `ansible/roles/redroid/tasks/main.yml` probes `/proc/filesystems` for `binder`, installs `linux-modules-extra-{{ ansible_kernel }}` when it is missing (Ubuntu ships binder as a module and cloud images omit it), loads `binder_linux` with `devices=binder,hwbinder,vndbinder`, persists both via `/etc/modules-load.d/` + `/etc/modprobe.d/`, re-probes, then mounts `/dev/binderfs` and runs the container `--privileged` with 5555 exposed. Set `redroid_install_kernel_modules: false` to opt out on air-gapped hosts. Works on `local-libvirt` and `cloud-digitalocean`; `local-vbox` declares `redroid: false`. ADB has no auth — reach it with `playground adb --lab L --on VM`, never by opening 5555. The image tag lives in `ansible/roles/redroid/defaults/main.yml`.
```

- [ ] **Step 8: Confirm the baseline failure set is unchanged, then commit**

```bash
uv run pytest -q 2>&1 | tail -8
git add src/playground/preflight/doctor.py docs/architecture/CONTRACTS.md docs/product/mvp_scope.md CLAUDE.md
git commit -m "docs: Redroid requires binder, not nested virtualization

The nested-virt doctor check is still right for libvirt guests, but citing
redroid-host as its reason was wrong: Redroid is a container needing binder
in the kernel it runs on. Records the live DigitalOcean verification and the
kernel/package-skew failure mode."
```

---

### Task 6: Live validation on DigitalOcean

**Files:** none — this task verifies, it does not edit. Any bug it finds becomes a fix committed against the task that owns the file.

**Interfaces:**
- Consumes: Tasks 1-5, all committed.
- Produces: a pass/fail report, and `docs/superpowers/specs/2026-08-31-cloud-redroid-design.md` updated with live results.

**Prerequisites:** `DIGITALOCEAN_TOKEN` exported. `doctl` and `adb` on PATH. **This spends real money (~$0.07/hr).** Destroy the lab even if the run fails.

- [ ] **Step 1: Record the pre-existing Droplet inventory**

```bash
export DIGITALOCEAN_ACCESS_TOKEN="$DIGITALOCEAN_TOKEN"
doctl compute droplet list --format ID,Name,Status --no-header
```
Save this output. There is an unrelated `lab-scheduler-scheduler` Droplet that must still exist at the end. Never destroy a Droplet you did not create.

- [ ] **Step 2: Validate and plan without spending anything**

```bash
uv run playground validate --config-dir config
uv run playground plan redroid-cloud
```
Expected: zero errors, zero warnings from `redroid-cloud`.

- [ ] **Step 3: Apply**

```bash
uv run playground apply redroid-cloud
```
Expected: exit 0. Watch for the `redroid` role tasks — `Install kernel modules-extra for the running kernel` should report `changed`, and `Re-probe binder after remediation` must not lead to the abort task.

- [ ] **Step 4: Verify Android actually booted, through the tunnel**

In one shell: `uv run playground adb --lab redroid-cloud --on droid1`
In another:
```bash
adb connect 127.0.0.1:5555
adb -s 127.0.0.1:5555 shell getprop sys.boot_completed   # expect: 1
adb -s 127.0.0.1:5555 shell getprop ro.product.model     # expect: redroid11_x86_64
adb -s 127.0.0.1:5555 shell pm list packages | wc -l     # expect: > 100
```

- [ ] **Step 5: Prove 5555 is NOT publicly reachable**

```bash
IP=$(uv run playground status redroid-cloud --output json | python -c "import json,sys; print(json.load(sys.stdin)['vms'][0]['ssh_host'])")
timeout 10 bash -c "cat < /dev/null > /dev/tcp/$IP/5555" && echo "FAIL: 5555 IS PUBLIC" || echo "PASS: 5555 filtered"
```
Expected: `PASS`. A `FAIL` here is a security defect — stop and report it before doing anything else.

- [ ] **Step 6: Prove idempotency (principle 7)**

```bash
uv run playground apply redroid-cloud
```
Expected: the Ansible recap reports `changed=0`. Any non-zero `changed` count names a non-idempotent task — record which one; that is a bug in Task 1.

- [ ] **Step 7: Prove reboot persistence**

```bash
uv run playground exec --lab redroid-cloud --on droid1 -- sudo reboot || true
# wait for the box to come back, then:
uv run playground exec --lab redroid-cloud --on droid1 -- grep -w binder /proc/filesystems
```
Expected: `nodev binder`. This proves the `modules-load.d` persistence works. If it fails, the persistence tasks in Task 1 are wrong.

- [ ] **Step 8: Destroy and confirm no leak**

```bash
uv run playground destroy redroid-cloud
sleep 20
doctl compute droplet list --format ID,Name,Status --no-header
doctl compute firewall list --format ID,Name --no-header
```
Expected: the Droplet inventory matches Step 1 exactly, and no `redroid-cloud`-prefixed firewall remains.

- [ ] **Step 9: Record the results in the spec and commit**

Append a `## Live validation, 2026-08-31` section to
`docs/superpowers/specs/2026-08-31-cloud-redroid-design.md` with the actual
outcome of each step above — including anything that failed and how it was
fixed. State results plainly; do not describe a step as passing unless you
ran it and saw it pass.

```bash
git add docs/superpowers/specs/2026-08-31-cloud-redroid-design.md
git commit -m "docs: record live DigitalOcean validation of redroid-cloud"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| 1. Self-healing binder role | Task 1 |
| 2. Provider capabilities load-bearing | Task 2 |
| 3. `config/labs/redroid-cloud.yaml` | Task 3 |
| 3. `_check_backend_capability` extended to DO | Task 2, Step 8 |
| 4. `playground adb` | Task 4 |
| 5. Corrected framing (doctor, CONTRACTS, mvp_scope, CLAUDE) | Task 5 |
| Testing: unit / ansible / live / idempotency | Tasks 1-4 inline, Task 6 |
| Risk: kernel/package skew | Task 1 Step 4 (abort message), Task 5 Step 5 (documented), Task 6 Step 3 |
| Risk: cost / teardown | Task 6 Steps 1, 8 |
| Open question: is libvirt already broken? | See below |

**The spec's open question is now effectively answered** and does not need its own task: `tofu/variables.tf:69` boots libvirt guests from the same Noble cloud image, and `tofu/cloud_init.cfg` installs no `linux-modules-extra`, so the libvirt path hits the identical missing-binder failure. Task 1 fixes both paths with one change. A live libvirt run would confirm it, but the repo's libvirt integration tests are already gated behind `PLAYGROUND_LIVE_INFRA=1` and skipped by default, so this plan does not add one.

**Placeholder scan:** no TBD/TODO. Every code step carries real code. The one deliberate hedge is Task 2 Step 1's alternative form for the absent-capabilities test, which is a genuine either/or the implementer must resolve by observation, with the exact check to run (`getattr(spec, "capabilities", "MISSING")`) spelled out.

**Type consistency:** `_capabilities_for_vm(loaded, vm) -> dict[str, Any]` and `_check_role_provider_capabilities(lab, loaded, source) -> list[Diagnostic]` are named identically in Task 2's helper, its registration, and its test import. `ADB_REMOTE_PORT` and `_first_free_local_port(preferred, attempts)` are used consistently across Task 4's steps. `redroid_install_kernel_modules` is spelled identically in Task 1's defaults, tasks, test, and Task 5's prose. The diagnostic ID `config.backend.capability_unsupported` matches between the implementation, the test constant, and the module docstring.
