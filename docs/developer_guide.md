# Developer Guide

This is the deep-dive entry point for newcomers to the codebase. It complements
the higher-level docs:

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
(`docs/architecture_decisions.md:77`) sets the sequence: read-only CLI before
backend automation.

---

## Repository layout

```text
playground/
├── src/playground/         # Python control layer (~2200 LOC)
│   ├── cli/                # Typer commands (validate, lab list, lab show)
│   ├── config/             # Discovery, loading, resolution
│   ├── models/             # Pydantic models for every kind + ResolvedLab
│   ├── validation/         # Cross-reference checks → Diagnostics
│   ├── events/             # (Placeholder — operation events)
│   ├── logging/            # (Placeholder — structured logs)
│   ├── runs/               # (Placeholder — operation run records)
│   └── state/              # (Placeholder — .playground/ store)
│
├── config/                 # User-authored lab intent (committed)
│   ├── defaults.yaml
│   ├── providers/
│   ├── artifacts/
│   ├── networks/
│   ├── roles/
│   ├── commands/
│   └── labs/
│
├── tofu/                   # OpenTofu module (libvirt provider)
├── ansible/                # Ansible site.yml + roles/docker, roles/redroid
│
├── tests/
│   ├── unit/               # Pytest unit tests — 90 today
│   └── cli/                # Typer CLIRunner tests
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

### `src/playground/cli/main.py` (273 lines)

Typer entry point. Three commands:

```text
playground validate [--config-dir DIR] [--output human|json] [--check-ansible-roles]
playground lab list [--config-dir DIR] [--output human|json]
playground lab show LAB [--config-dir DIR] [--output human|json]
```

Flow:

```python
load_config(config_dir)                 # → (LoadedConfig, parse diagnostics)
if not _has_errors(diagnostics):
    diagnostics.extend(validate(loaded))  # → cross-reference diagnostics
# render human or JSON, exit 1 if any error severity
```

`--output json` emits `{"ok": bool, "diagnostics": […]}` so downstream tooling
can parse without scraping human prose. The console entry point
`playground = "playground.cli.main:app"` is wired in `pyproject.toml:30`.

### Placeholder modules

These directories exist with one-line docstrings and reserve names for
upcoming roadmap items. Don't start a feature inside them without a plan —
their shape will be decided when the relevant roadmap item begins.

- `events/` — operation event bus (system_design.md "Operation Runner And Events")
- `logging/` — structured logs
- `runs/` — operation run records under `.playground/runs/`
- `state/` — `StateStore` over `.playground/`

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

---

## Ansible roles (`ansible/`)

Two roles wired through `ansible/site.yml`:

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

## Where to read next

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
