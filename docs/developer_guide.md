# Developer Guide

This is the deep-dive entry point for newcomers to the codebase. It complements
the higher-level docs:

- **Visual overview with diagrams**: [`docs/system_overview.md`](system_overview.md) — start there if you want a map before the prose
- Product intent: `docs/product/requirements.md`, `user_stories.md`, `mvp_scope.md`
- System architecture: `docs/system_design.md`
- Config tree shape: `docs/config_design.md`
- Engineering principles: `docs/engineering_principles.md`
- Architecture decisions: `docs/architecture_decisions.md`
- Current sequential task queue: `docs/roadmap.md`
- Process / agents: `docs/workflow.md`, `AGENTS.md`, `CODEX.md`, `CLAUDE.md`

This document focuses on *the code as it stands today* — what's in each module,
why it's shaped the way it is, and where to make common changes.

---

## Mental model

The repo holds **two layers** that coexist on purpose:

```text
┌──────────────────────────────┐       ┌──────────────────────────────┐
│  Python control layer        │       │  Runtime baseline            │
│  (src/playground/)           │       │  (tofu/, ansible/)           │
│                              │       │                              │
│  YAML → typed models →       │       │  OpenTofu provisions VMs     │
│  diagnostics → resolved      │       │  Ansible installs Docker     │
│  lab → (future) backend      │       │  Docker runs Redroid         │
│  adapters                    │       │  ADB connects to Android     │
│                              │       │                              │
│  Read-only today.            │       │  Manually driven today.      │
└──────────────────────────────┘       └──────────────────────────────┘
                  ↓                                    ↑
                  └────── future bridge (roadmap §4) ──┘
```

`ADR-0002` (`docs/architecture_decisions.md:33`) is load-bearing: the Python
layer must not hide or rewrite OpenTofu/Ansible prematurely. `ADR-0004`
(`docs/architecture_decisions.md:77`) was about staging — the read-only CLI
shipped first (validate, lab list/show, plan, inventory render, tofu render,
status, runs list/show) before the mutating slice (apply, destroy, tui). All
nine roadmap phases have landed; runtime adapters now drive `tofu` and
`ansible-playbook` from the resolved lab end-to-end.

---

## Repository layout

```text
playground/
├── src/playground/         # Python control layer
│   ├── cli/                # Typer commands (every subcommand below)
│   ├── config/             # Discovery, loading, resolution
│   ├── models/             # Pydantic models: kinds, resolved, status, diagnostic
│   ├── validation/         # Cross-reference checks → Diagnostics
│   ├── planner/            # Plan rendering + workload scheduling + file staging
│   ├── backend/            # Adapter layer (today: local_libvirt)
│   │   └── local_libvirt/  # inventory, tfvars, apply (subprocess), status, runner
│   ├── events/             # In-process EventBus + JsonlWriter
│   ├── runs/               # OperationRun + start/finish helpers
│   ├── tui/                # Textual app (lab browser, live ops, runs viewer)
│   ├── logging/            # (Reserved — richer structured logs later)
│   └── state/              # (Reserved — typed .playground/ store later)
│
├── config/                 # User-authored lab intent (committed)
│   ├── defaults.yaml       #   project-wide defaults
│   ├── providers/          #   backend-specific configs (libvirt URI etc.)
│   ├── artifacts/          #   ArtifactSources singleton
│   ├── networks/           #   NetworkProfile presets (nat/isolated/routed)
│   ├── roles/              #   VmRole presets with `extends:` inheritance
│   ├── commands/           #   CommandPreset for `playground run` (planned)
│   └── labs/               #   Named labs (generic-infra today)
│
├── compose/                # Compose source files referenced by lab YAMLs
├── tofu/                   # OpenTofu module (libvirt provider)
├── ansible/                # site.yml + roles/{docker,redroid,
│                           #   workload_container, workload_compose, workload_swarm}
│
├── tests/
│   ├── unit/               # Pytest unit tests
│   ├── cli/                # Typer CLIRunner tests
│   └── ... (mirrors the src/ layout)
│
├── docs/                   # Product, system, config, ADRs, roadmap, this file
├── .playground/            # GENERATED runtime state (git-ignored)
├── .claude/agents/         # Project-local Claude subagent definitions
├── .codex/agents/          # Project-local Codex subagent definitions
│
├── PRD.md, README.md       # Top-level entry points
├── AGENTS.md, CODEX.md, CLAUDE.md   # Agent operating guides
└── pyproject.toml          # Build, test, lint, mypy config
```

---

## Tooling cheat sheet

| Tool | Why | Pin |
|---|---|---|
| Python 3.12+ | Modern typing, `Literal`, `StrEnum`, `from __future__ import annotations` | `pyproject.toml:10` |
| pydantic 2.7+ | Strict typed models, validation errors carry `loc` → `key_path` | `pyproject.toml:24` |
| ruamel.yaml 0.18+ | Round-trip parser; accurate error positions; will support comment-preserving writes later | `pyproject.toml:25` |
| typer 0.12+ | CLI surface |  |
| pytest 8 | Unit + CLI tests | `pyproject.toml:35` |
| mypy strict | `strict = true` everywhere | `pyproject.toml:59` |
| ruff (E,F,I,B,UP,W) | Lint + import-sort; line length 100 | `pyproject.toml:52` |
| uv | Disposable per-command venvs via `uv run --no-project --with …` |  |
| hatchling | PEP 517 build backend | `pyproject.toml:1` |

Runtime baseline:

| Tool | Role |
|---|---|
| OpenTofu | VM/network provisioning under `tofu/` |
| `dmacvicar/libvirt` `~> 0.7.1` | Local libvirt provider |
| Ansible + `community.docker` | `docker` and `redroid` roles |
| cloud-init | SSH key injection (`tofu/cloud_init.cfg`) |
| Docker CE | Installed inside guest VMs |
| Redroid | Containerized Android, port 5555 |
| ADB | Operator connection from host |

---

## Data flow

The control-layer pipeline is linear and one-directional:

```text
config/ (YAML files)
        │
        ▼  discover_config_files()
DiscoveredFile  (path + repo-relative path + expected_kind)
        │
        ▼  load_config()
LoadedConfig    (typed kinds, parse diagnostics, source map)
        │
        ├──────► validate()    →   list[Diagnostic]   (cross-references)
        │
        ▼  resolve_lab(loaded, "lab-name")
ResolvedLab     (backend-neutral, frozen, ready for adapters)
        │
        ▼  (future) backend adapter
tofu / ansible / docker / future providers
```

Every stage produces `Diagnostic`s rather than throwing on user mistakes
(*Diagnostics Over Crashes*, `docs/engineering_principles.md` §9). Exceptions
are reserved for programmer errors and "you tried to resolve without
validating first" contract violations.

---

## Module-by-module tour

### `src/playground/models/base.py` (32 lines)

Defines `StrictModel`, the base for every typed model:

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",          # YAML typos raise instead of silently dropping
        frozen=True,             # Models are immutable once parsed
        str_strip_whitespace=True,
    )
```

Also defines `Metadata` (the `metadata:` block of every kind) and
`ResourceEnvelope` (`apiVersion: playground/v1` + `kind:` + `metadata:` —
shared by every on-disk file).

Two places intentionally relax `extra="forbid"`:

- `ProviderConfig.spec` — open keys; backend adapters version their own schema
- `LabProviders` — per-lab provider overlays; same reason

### `src/playground/models/diagnostic.py` (30 lines)

The universal feedback channel:

```python
Severity = Literal["error", "warning", "info"]

class Diagnostic(StrictModel):
    id: str              # namespaced, e.g. "config.reference.unknown_role"
    severity: Severity
    message: str
    source: SourceLocation | None   # file path + optional line/column
    key_path: str | None            # YAML key path, e.g. "spec.vms[0].role"
    suggestion: str | None
    tags: list[str]
```

The `id` is the **stable public contract**. New diagnostic IDs go in the
docstring of whatever module emits them; the validator's docstring is the
de-facto registry today.

### `src/playground/models/kinds.py` (467 lines)

Pydantic models for every on-disk YAML kind:

| Kind | Purpose |
|---|---|
| `Defaults` | Project-wide defaults applied before lab-specific values (one per tree) |
| `ProviderConfig` | Backend-specific settings (e.g. libvirt URI, pool) |
| `ArtifactSources` | VM images, Tofu providers, Ansible collections, Docker images (one per tree) |
| `NetworkProfile` | Reusable network intent: `nat`, `isolated`, `routed` |
| `VmRole` | Reusable VM presets with single-chain `extends` inheritance |
| `CommandPreset` | Operator commands with target selectors and timeouts |
| `Lab` | Named lab intent composed from the above |

Key choices to be aware of when extending:

- **`TargetSelector` and `WorkloadPlacement` use `@model_validator(mode="after")` for "exactly one of"** (`kinds.py:50`, `kinds.py:302`). YAML can't express this; a model validator is the cleanest way.
- **`parse_resource(raw_dict)` (`kinds.py:405`) is the entry point** for "I have a parsed YAML dict, give me the right typed model". It dispatches on `kind:` and emits specific `ValueError`s the loader translates into diagnostics.
- **`LabSpec.names_unique` (`kinds.py:363`)** catches duplicate VM/network/workload names at parse time so the validator doesn't have to.
- **`Resources` has floor constraints** (vcpu ≥ 1, memory ≥ 128, disk ≥ 1). These are per-VM invariants — budget checks are a separate concept in `Budget` because budgets are *lab-level policy*.

### `src/playground/models/resolved.py` (132 lines)

`ResolvedLab` is what backend adapters will consume — backend-neutral and
immutable.

Design choices to preserve when adding fields:

- **No backend-specific shapes.** `ResolvedLab.providers` is `dict[str, dict[str, Any]]` so libvirt-specific keys live inside without bleeding into the type system. Cloud adapters will reuse the same hole.
- **`ResolvedVm.routing: VmRouting | None`** survives resolution — added in commit `b84c4b8`. The pattern: if some intent is meaningful to backend adapters, thread it through `_resolve_vm` and the model.
- **`source_map: dict[str, str]`** is intentionally coarse today (just `{"spec": "config/labs/foo.yaml"}`); the shape allows per-key origins later without a model change.
- **`runtime_overrides: list[Any]`** is a reserved slot for User Story 2.3 (temporary overrides). The field exists so the model doesn't churn when that feature lands.
- **`ResolvedArtifactImage.available_locally / available_remote`** are reserved for future doctor checks.

### `src/playground/config/discovery.py` (68 lines)

Walks `config_dir.rglob("*.yaml")` and yields `DiscoveredFile(path,
repo_relative_path, expected_kind)`. Three points worth knowing:

- **`expected_kind` is advisory.** A `Lab` accidentally placed under
  `config/networks/` parses fine and surfaces a `config.schema.kind_mismatch`
  *warning*. Hard enforcement would block legitimate experiments.
- **Hidden directories are skipped** — keeps editor scratch files (`.vscode`,
  `.idea`) out of the load.
- **`base_for_relative = config_dir.parent`** — diagnostics carry
  `config/labs/foo.yaml`, not absolute paths, so they're portable.

### `src/playground/config/loader.py` (287 lines)

The first half of the parse pipeline. Public surface:

```python
def load_config(config_dir: Path) -> tuple[LoadedConfig, list[Diagnostic]]:
```

`LoadedConfig` is a plain `@dataclass` (not a pydantic model) — it's an
internal mutable container; the validator and resolver fill in additional
state during their passes.

Diagnostic IDs emitted here are listed in the module docstring:

- `config.yaml.parse_failed`
- `config.schema.kind_missing` / `kind_mismatch` / `unknown_kind`
- `config.schema.validation_failed`
- `config.identity.duplicate_name`

The loader **doesn't abort on a single failure**. Each file gets its own
diagnostic so the user sees every problem at once.

### `src/playground/validation/validator.py` (530 lines)

Cross-reference checks. Public surface:

```python
def validate(
    loaded: LoadedConfig,
    ansible_roles_dir: Path | None = None,
) -> list[Diagnostic]:
```

The module docstring lists every diagnostic ID the validator emits — keep
that list authoritative when adding new checks.

The validator **never** produces a `ResolvedLab`. Its only job is "does every
name in every reference point at something that exists, and do lab-level
policies hold". The resolver assumes the validator has run with no errors.

Three helpers shared across checks:

- `_role_ancestors(loaded, role_name)` — walks `spec.extends` leaf-to-root,
  clipping cycles. Three call sites: image resolution, resources resolution,
  workload-target-role matching.
- `_image_for_vm(loaded, vm)` — first-non-`None` image up the chain, then
  `defaults.spec.vm.image`. Must agree with how `_flatten_role` in the
  resolver decides the image; a comment in the function calls this out.
- `_resources_for_role` / `_resources_for_vm` — same pattern for resources.

### `src/playground/config/resolver.py` (288 lines)

The second half of the parse pipeline. Public surface:

```python
def resolve_lab(loaded: LoadedConfig, lab_name: str) -> ResolvedLab:
```

Pipeline (`resolver.py:36`):

1. Apply `Defaults.spec` as the base
2. Layer the lab's `spec`
3. For each VM: walk `spec.extends`, flatten the role, apply VM-level overrides
4. Expand command names → command bodies
5. Resolve artifact sources for declared images
6. Apply runtime overrides (no-op today; placeholder)
7. Populate `source_map`

The resolver **raises** on cross-reference mistakes (`KeyError` / `ValueError`)
rather than producing diagnostics. The docstring says "the caller is expected
to have run `validate` first and gated on errors" — this keeps the resolver
small.

### `src/playground/cli/main.py`

Typer entry point. Subcommands:

| Command | Surface | Mutates |
|---|---|---|
| `playground doctor` | probe host prerequisites (binaries, libvirt pool, SSH key, ansible collections) | no |
| `playground validate` | full cross-reference check | no |
| `playground lab list` / `lab show <lab>` | inspect lab definitions | no |
| `playground plan <lab>` | render backend-neutral preview | no |
| `playground tofu render <lab>` | write `terraform.tfvars.json` | writes `.playground/state/tofu/` |
| `playground inventory render <lab>` | write Ansible inventory | writes `.playground/state/inventory/` |
| `playground status <lab>` | observed state snapshot | no |
| `playground apply <lab>` | full deploy (tfvars → tofu → inventory → ansible) | **yes** |
| `playground destroy <lab>` | `tofu destroy` | **yes** |
| `playground reset <lab>` | scrub-by-name (virsh) + best-effort tofu destroy + per-lab state wipe | **yes** |
| `playground runs list` / `runs show <id>` | browse past operations | no |
| `playground tui` | launch Textual TUI | inside |

Flow (read-only commands):

```python
load_config(config_dir)                 # → (LoadedConfig, parse diagnostics)
if not _has_errors(diagnostics):
    diagnostics.extend(validate(loaded))  # → cross-reference diagnostics
# render human or JSON, exit 1 if any error severity
```

Flow (mutating commands):

```python
load_config → validate → resolve_lab → execute_apply/destroy
                                       ↑ runner handles everything below
                                       ↑ never raises; returns OperationRun
# CLI is a thin presentation wrapper; the TUI runs the same runner.
```

`--output json` emits `{"ok": bool, "diagnostics": […]}` so downstream tooling
can parse without scraping human prose. The console entry point
`playground = "playground.cli.main:app"` is wired in `pyproject.toml:30`.

### `src/playground/planner/`

Two sibling modules:

- `plan.py` — `Plan`, `PlanAction`, `PlanBudget`, `render_plan(resolved,
  warnings=None) -> Plan`. Backend-neutral preview of what `apply` would
  do. Today every action verb is `create`; `update / delete / no_op` are
  reserved in `ActionVerb` for the state-observation slice.
- `scheduling.py`:
  - `schedule_workloads(resolved) -> ({vm_name: [workloads]},
    diagnostics)` — resolves placement (`target_vm` / `target_role`
    walking the **full** `spec.extends` ancestry / `target_tag` /
    `auto` matching `capabilities['docker']`).
  - `assign_swarm_membership(scheduled, vms) -> ({vm: "manager" |
    "worker" | "none"}, diagnostics)` — first docker-capable VM is
    the manager; others are workers. Emits
    `config.workload.swarm_needs_docker_host` when none qualify.
  - `stage_workload_files(scheduled, source_base, stage_dir) ->
    ({vm: {workload: path}}, diagnostics)` — copies compose/swarm
    source files from `source_base/<workload.source>` into
    `stage_dir/<vm>/<workload>.<ext>`. Emits
    `config.workload.source_missing` for absent files.
  - `workload_to_ansible_payload(workload, staged_source=None) ->
    dict` — the JSON-serialized payload the
    `workload_container` / `workload_compose` / `workload_swarm`
    ansible roles consume via `pg_workloads`.

### `src/playground/events/`

In-process pub/sub plus the canonical JSONL persistence subscriber.
External brokers explicitly out of scope per `requirements.md` §5.11.

- `OperationEvent` — `run_id`, `timestamp`, `type`, `payload`.
- `EventType` literal — `operation_started`, `step_started`,
  `step_finished`, `operation_finished`, `log_line`.
- `EventBus` — synchronous; subscribers run on the publishing thread.
  Subscriber exceptions are captured on `bus.errors`, not swallowed.
- `JsonlWriter(run_dir)` — appends each event as one JSON line to
  `run_dir/events.jsonl`.
- `operation_events(bus, run_id, op, lab)` — context manager that
  brackets a block with `operation_started` / `operation_finished`.

### `src/playground/runs/`

Operation lifecycle records that satisfy `requirements.md` §5.10
("mutating ops MUST create a run record; read-only ops MUST NOT").

- `OperationRun` (frozen StrictModel) — `run_id`, `operation`, `lab`,
  `status`, `started_at`, `finished_at`, `steps`, `summary`.
- `StepResult` — `name`, `command`, `exit_code`, `log_path`,
  `started_at`, `finished_at`.
- `allocate_run_id(op, lab, now=None)` — sortable id
  `YYYYMMDDTHHmmssZ-<op>-<lab>`.
- `start_run(runs_dir, op, lab)` — creates `runs_dir/<id>/{run.json,
  logs/}` with `status="running"`; collisions surface loudly via
  `mkdir(exist_ok=False)` on the run-id directory.
- `finish_run(run, run_dir, *, status, steps, summary=None)` —
  finalizes the on-disk record.

### `src/playground/backend/local_libvirt/`

Adapter layer. Six modules:

- `inventory.py` — `render_inventory` (with optional staged-workload
  map and emitted `[swarm_manager]` / `[swarm_worker]` groups when
  applicable) and `fetch_vm_ips`.
- `tfvars.py` — `render_tfvars(resolved) -> dict` (just `vm_names`
  today; per-VM resources deferred).
- `apply.py` — subprocess wrappers `run_tofu_apply`,
  `run_tofu_destroy`, `run_ansible_playbook`. Spawns with `Popen`,
  reads stdout line-by-line, writes to a log file AND (when given
  `bus=` + `run_id=`) publishes one `log_line` event per line.
  `tail_log()` reads the last N lines for failure reporting.
- `status.py` — `query_status(resolved, tofu_dir) -> (LabStatus,
  diagnostics)`. Treats `tofu_no_state` as the steady "nothing
  applied yet" status, not an error.
- `runner.py` — the **service layer**: `execute_apply` and
  `execute_destroy` orchestrate the multi-step operations. Never
  raise; always return a finalized `OperationRun` so both the CLI
  and the TUI can present the result consistently.

### `src/playground/tui/`

Textual app over the existing primitives — no parallel business logic
per `requirements.md` §5.8. Two-pane layout (lab list / detail) with a
live log pane at the bottom.

- `PlaygroundTui` (`app.py`) — main app with bindings: `r` refresh,
  `a` apply, `d` destroy, `v` runs, `q` quit.
- `_ConfirmScreen` — modal guard before mutating actions.
- `_LogPane` — append-only widget bounded at ~1000 lines. The bus
  bridge calls `App.call_from_thread()` so subprocess output from
  the worker thread renders on the foreground event loop.
- `RunsScreen` / `RunDetailScreen` — runs viewer; the detail screen
  renders the `events.jsonl` timeline.

### Reserved modules

- `logging/` — richer structured logs (per-resource / per-stage
  filters). Today's JSONL is the single log surface.
- `state/` — typed `StateStore` over `.playground/`. Today the
  filesystem layout is implicit (runs/, state/inventory/,
  state/tofu/, state/workloads/, cache/).

---

## Config tree (`config/`)

The user-facing surface — the validator and resolver are written against
this. Today's committed tree:

```text
config/
├── defaults.yaml                       # one Defaults singleton
├── providers/local-libvirt.yaml        # only first-class backend today
├── artifacts/sources.yaml              # one ArtifactSources singleton
├── networks/
│   ├── nat.yaml                        # internet_access: true
│   ├── isolated.yaml                   # internet_access: false (air-gap)
│   └── routed.yaml                     # internet_access: "configurable"
├── roles/
│   ├── generic-node.yaml               # root role; image: ubuntu-noble
│   ├── docker-host.yaml                # extends: generic-node
│   └── router.yaml                     # extends: generic-node; routing.mode: automatic
├── commands/
│   ├── check-docker.yaml
│   └── ping-network.yaml
└── labs/generic-infra.yaml             # the example lab — uses all three networks
```

Constraints that show up in code:

- `Defaults` and `ArtifactSources` are **singletons**. A second of either is a
  `config.identity.duplicate_name` error.
- Role inheritance is **single-chain** (`extends`), not multi-parent. Merge
  rule: first non-`None` wins as you walk leaf-to-root.
- `generic-infra` deliberately uses all three network intents so the test
  suite exercises every NetworkProfile path.

---

## Tofu module (`tofu/`)

~95 LOC, four files. Five resources in `tofu/main.tf`:

| Resource | Purpose | Constraint |
|---|---|---|
| `libvirt_network.playground_net` | NAT network, `10.0.10.0/24`, DHCP | Must stay NAT for Phase 1 |
| `libvirt_volume.ubuntu_image` | Base qcow2 from `var.ubuntu_image_url` | |
| `libvirt_volume.vm_disk` (count = `var.vm_count`) | Per-VM disks | 20 GB hardcoded |
| `libvirt_cloudinit_disk.commoninit` (count) | cloud-init ISOs injecting SSH key | Password auth disabled |
| `libvirt_domain.playground_node` (count) | The VMs | `cpu { mode = "host-passthrough" }` is non-negotiable — Redroid needs it |

`tofu/variables.tf` exposes `vm_count` (1), `vm_memory` MB (4096), `vm_vcpu`
(2), `ssh_public_key_path` (`~/.ssh/id_rsa.pub`), `ubuntu_image_url`. Override
through `terraform.tfvars` or `-var`; never hardcode secrets in `.tf` files.

**Lab-scoped DNS.** Each `libvirt_network` sets
`domain = var.dns_domain` and renders authoritative
`dns { hosts { hostname, ip } }` records populated from
`var.vm_dns_hosts` (keyed by network name). `tofu/cloud_init.cfg`
sets `hostname: ${vm_name}` and
`fqdn: ${vm_name}.${dns_domain}` with `preserve_hostname: false`
so each VM advertises the right name via DHCP. `render_tfvars`
emits both vars from `ResolvedLab.dns_domain` (defaults to
`<lab>.lab`) and per-VM `network_ips` pins — labs no longer need
`extra_hosts` for intra-lab hostname resolution.

---

## Ansible roles (`ansible/`)

`ansible/site.yml` runs several plays in order; the highlights:

1. `Apply lab-declared extra_hosts entries` (legacy workaround,
   still useful for non-lab hostnames).
2. `Baseline configuration` → `common` role (UTC timezone via
   `community.general.timezone`; minimal `jq curl ca-certificates`
   install). Idempotent on re-apply.
3. `Configure Playground Guests` → `docker` then `redroid`.
4. Per-host-class plays for cross-VM labs (`docker_tunneler`,
   `ssh_keypair_*`, `barak_deploy_staging`, `barak_deploy_agent`).

### `docker`

Order is load-bearing:

1. Remove distro `docker.io` and `containerd` (they'd conflict with `docker-ce`)
2. Install prerequisites
3. Add Docker's GPG key + apt repo
4. Install `docker-ce`
5. Add the SSH user to the `docker` group

Re-running must be idempotent (`changed=0` on a configured host).

### `redroid`

1. Best-effort `modprobe binder_linux ashmem_linux` (modules may be built-in;
   uses `ignore_errors: true`)
2. Assert binderfs support
3. Mount `/dev/binderfs`
4. Run the Redroid container `--privileged` with port `5555:5555` and a
   `/dev/binderfs` bind mount

Image tag lives in `ansible/roles/redroid/defaults/main.yml`.

The `router` role referenced from `config/roles/router.yaml` doesn't exist
yet under `ansible/roles/` — the validator warns about this when run with
`--check-ansible-roles`. That's intentional: the role is on the roadmap.

---

## Development workflow

### Setup

The project uses `uv` for disposable per-command environments. No need to
manage a virtualenv:

```bash
# Verify Python 3.12+ is available
python3 --version

# Run unit tests
PYTHONPATH=src uv run --no-project \
  --with pytest --with pydantic --with ruamel.yaml --with jsonschema --with typer \
  pytest tests -q

# Run mypy strict
uv run --no-project \
  --with mypy --with pydantic --with ruamel.yaml --with jsonschema --with typer \
  mypy src

# Run ruff
uv run --no-project --with ruff ruff check src tests

# Run the CLI against the committed config
PYTHONPATH=src uv run --no-project \
  --with pydantic --with ruamel.yaml --with jsonschema --with typer \
  python -m playground.cli.main validate
```

If you'd rather install once:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,tui]"
pytest tests -q
mypy src
ruff check src tests
playground validate
```

### Infra checks

```bash
cd tofu && tofu fmt -check -recursive && tofu init -backend=false && tofu validate
cd ../ && ansible-playbook -i ansible/inventory.ini ansible/site.yml --syntax-check
```

### Running the live pipeline (manual today)

This isn't required for control-layer work — only do it when you need to
verify the runtime baseline on your machine.

```bash
cd tofu && tofu apply -auto-approve
# Copy IPs from `tofu output vm_ips` into ansible/inventory.ini
cd ../ansible
ansible-galaxy collection install -r requirements.yml
ansible-playbook -i inventory.ini site.yml
# From the host:
adb connect <VM_IP>:5555
# Teardown:
cd ../tofu && tofu destroy -auto-approve
```

Requirements: KVM/libvirt on the host, user in the `libvirt` group (or sudo),
~5 GB free disk for the base image, ~4 GB RAM per VM.

---

## How to make common changes

### Add a new diagnostic

1. **Pick an ID** — follow the namespace pattern
   `config.<category>.<specific>`, e.g. `config.reference.unknown_image`.
   Categories in use today: `yaml`, `schema`, `identity`, `reference`,
   `role`, `required`, `budget`, `artifact`, `lab`, `discovery`.
2. **Add the ID to the docstring** of the module that emits it
   (`validator.py` or `loader.py`). Diagnostic IDs are public contract.
3. **Emit it** with full context — `severity`, `message`, `source` (use
   `_source_for(loaded, kind, name)` in the validator to get a real
   repo-relative path), `key_path` (e.g. `spec.vms[0].role`), `suggestion`.
4. **Pin it with a test** in `tests/unit/validation/test_validator.py` —
   construct a `LoadedConfig` that triggers it, assert exactly one matching
   diagnostic, assert `key_path` and message content.
5. **Smoke-test through the CLI** — copy `config/` to `/tmp/`, mutate it,
   run `python -m playground.cli.main validate --config-dir /tmp/...`,
   confirm exit code 1 and the diagnostic renders cleanly.

### Add a new YAML kind

1. Add the model class to `models/kinds.py` (extend `ResourceEnvelope`, use
   `StrictModel` for sub-models).
2. Register it in `_KIND_MODELS` and the `AnyResource` union in
   `kinds.py:382`.
3. Add a directory under `config/` and wire the directory→kind mapping in
   `discovery.py:_DIRECTORY_KIND_MAP`.
4. Handle the new kind in `loader.py:_file_into_collection` (singleton or
   dict, source tracking).
5. Add a field to `LoadedConfig` in `loader.py:46`.
6. Add cross-reference checks in `validator.py`.
7. Wire it into the resolver only when it actually flows into `ResolvedLab`.
8. Tests: `tests/unit/models/test_kinds.py` for parse, plus
   `tests/unit/config/test_loader.py` for the loader side.

### Add a new CLI command

1. Add the function to `cli/main.py` decorated with `@app.command(…)` (or
   `@lab_app.command(…)` for `playground lab <foo>`).
2. Follow the pattern: `_load_config_or_exit` → `validate_loaded_config` →
   render → `_exit_on_errors`.
3. Support both `--output human` and `--output json` via the `OutputFormat`
   enum.
4. Test in `tests/cli/test_cli.py` using Typer's `CliRunner`.

### Extend `ResolvedLab`

1. Add the field to the relevant `Resolved*` model in `models/resolved.py`
   (default to `None` or empty if the slice doesn't always populate it).
2. Populate it in `resolver.py` in the matching `_resolve_*` function.
3. Pin both the populated and unpopulated cases in
   `tests/unit/config/test_resolver.py`.
4. Update the doc note in `docs/system_design.md` "Resolution" if the
   resolver gains a new responsibility.

---

## Testing approach

Tests live under `tests/`:

```text
tests/
├── unit/
│   ├── config/test_loader.py        # Loader + source tracking
│   ├── config/test_resolver.py      # LoadedConfig → ResolvedLab
│   ├── models/test_base.py          # StrictModel invariants
│   ├── models/test_diagnostic.py    # Diagnostic model
│   ├── models/test_kinds.py         # Each on-disk kind parses
│   ├── validation/test_validator.py # Every diagnostic ID
│   └── test_package_import.py       # Smoke
└── cli/test_cli.py                  # Typer CLIRunner tests
```

Conventions:

- **The committed `config/` tree is a test fixture.** Most validator tests
  start from a `committed_load` fixture, then mutate one thing to trigger a
  specific diagnostic. This keeps the test surface concrete.
- **Test names match diagnostic IDs** where applicable, e.g.
  `test_offline_missing_fires_alongside_unknown_image`.
- **`tests scale with risk`** (engineering principle §12). A one-line
  validator change needs one targeted test; a new kind needs parse + load +
  validate + resolve coverage.

### Running narrow

```bash
# One file
PYTHONPATH=src uv run --no-project --with pytest --with pydantic --with ruamel.yaml --with jsonschema --with typer pytest tests/unit/validation/test_validator.py -q

# One test
PYTHONPATH=src uv run --no-project --with pytest --with pydantic --with ruamel.yaml --with jsonschema --with typer pytest tests/unit/validation/test_validator.py::test_budget_exceeded_is_error_in_strict_mode -q
```

---

## Conventions and gotchas

- **`from __future__ import annotations`** is at the top of every Python
  source file. Keep adding it to new files.
- **No `Optional[T]`** — use `T | None`. Same for `Union` → `|`.
- **Diagnostics never use exceptions for user mistakes.** If you find
  yourself writing `raise ValueError("bad config")`, stop and emit a
  `Diagnostic` instead.
- **The validator and resolver must stay decoupled.** The validator must
  not produce a `ResolvedLab`; the resolver must not invent diagnostics.
- **Source paths are real.** If you emit a diagnostic with `source=…`, use
  `_source_for(loaded, kind, name)` so the user sees the file they edited.
- **`extra="forbid"` is the default.** When you need to relax it, do it on
  one model (not globally), and add a comment explaining why backend
  adapters need the open key set.
- **Diagnostic IDs are public.** Don't rename one without a deprecation
  plan — they show up in JSON output that downstream tools may grep.
- **6 pre-existing ruff errors** in `src/playground/__init__.py` and
  `tests/unit/models/test_kinds.py` are unrelated tech debt. Don't include
  them in unrelated commits — they're a separate cleanup.

---

## Multi-VM integration tests

`tests/integration/multi_vm/` houses tests that bring up real labs
end-to-end. They're **skipped by default** — running them needs real
libvirt access, ~8 GiB free RAM, and the sibling `barak-deploy` repo
checked out at `~/Workspace/barak-deploy/`.

To enable:

```bash
PLAYGROUND_LIVE_INFRA=1 pytest tests/integration/multi_vm -v
```

The harness uses `subprocess` to invoke the real `playground` CLI plus
plain `ssh` to talk to VMs once they're up. No mocking — the test
exercises everything the operator would do by hand. A `try/finally`
ensures `playground destroy` runs even when an assertion fails.

`tests/integration/multi_vm/test_cross_vm_deploy.py` validates every
pass/fail criterion from `playground-requirements.md`:

1. Hello container running on the target VM.
2. Templated `hello.conf` placed at `/etc/hello/`.
3. `barak-deploy history` shows a pipeline run with status=ok and four
   step records (`unwrap`, `load`, `place-config`, `run`).
4. Tar archived under `/var/spool/deploys/archive/ok/`.
5. Manifest written with the expected files + tar_sha256.
6. Idempotency: a second ship-deploy run produces a history entry
   where every step has `skipped: true`.

Manual fallback (when you can't run pytest with `PLAYGROUND_LIVE_INFRA`
set): the `playground-requirements.md` document at the repo root has a
`## Bringing up the test` section with the equivalent bash commands.

## Where to read next

- [`docs/system_overview.md`](system_overview.md) — diagrams of the same
  picture you just walked through; useful as a refresher
- `docs/system_design.md` — full intended system, including the parts not
  built yet (planner, event runner, backend adapters)
- `docs/config_design.md` — config tree shape and validation gap list
- `docs/roadmap.md` — what's done, what's next
- `docs/architecture_decisions.md` — the five ADRs that constrain future work
- `docs/product/requirements.md` — non-negotiable product intent (re-read
  this before any non-trivial change)
- `CLAUDE.md` / `CODEX.md` / `AGENTS.md` — agent operating guides

If you're picking up the next slice, the queue head is in
`docs/roadmap.md` §4 — the OpenTofu/Ansible bridge that consumes
`ResolvedLab` and renders an Ansible inventory under `.playground/`.
