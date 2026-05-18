# Overall System Design

This document describes the intended system as a whole: the current working
OpenTofu/Ansible/Redroid baseline, the emerging Python control layer, and the
future bridge between them.

## Source Documents

Product intent:

1. `docs/product/requirements.md`
2. `docs/product/user_stories.md`
3. `docs/product/mvp_scope.md`

Supporting design:

1. `docs/config_design.md`
2. `docs/engineering_principles.md`
3. `docs/architecture_decisions.md`
4. `docs/platform.md`
5. `docs/roadmap.md`
6. `PRD.md`

If there is a conflict, prefer the product requirements, then current code and
tests, then supporting docs.

## Design Goals

- Let the operator define reproducible labs in YAML.
- Keep generated runtime state under `.playground/`.
- Keep OpenTofu and Ansible visible, editable, and inspectable.
- Model lab intent in backend-neutral terms before translating it to concrete
  provider actions.
- Support local libvirt first while leaving room for future cloud or remote
  backends.
- Warn and explain risks by default; block only when strict mode or hard
  validation errors require it.
- Build CLI-first so the TUI can later reuse stable config, state, and operation
  APIs.

## Current System Layers

```text
Product intent/docs
  -> YAML config tree
  -> Python config/model/validation layer
  -> read-only CLI
  -> planner
  -> operation runner and event stream
  -> backend adapters
  -> OpenTofu / Ansible / Docker / future providers
  -> .playground state, runs, logs, cache, artifacts
```

Only the YAML config tree and Python config/model/validation layer exist today.
The OpenTofu/Ansible/Redroid path also exists, but it is still manually driven
and not yet controlled by the Python layer.

## Runtime Baseline

The current runnable path is:

```text
tofu/ -> ansible/ -> Docker/Redroid -> ADB
```

Responsibilities:

- `tofu/`: provision local libvirt network, cloud-init disks, and VM domains.
- `ansible/`: configure guest VMs with Docker and Redroid.
- `README.md`: document the manual operator flow.

This baseline must remain valid while the Python platform grows. The platform
should wrap or generate inputs for the baseline incrementally; it should not
hide or rewrite it prematurely.

## Control Layer

The emerging Python package is the future control layer:

```text
src/playground/config/
src/playground/models/
src/playground/validation/
```

Responsibilities:

- discover and parse YAML config
- create typed resource models
- validate schema and cross-file references
- resolve loaded config into a backend-neutral `ResolvedLab`
- expose read-only CLI commands first

The control layer must not drive backend mutation until validation and the
resolved model are strong enough to represent the required intent safely.

## Config Model

User-authored config lives under `config/` and is intended to be committed.

Current kinds:

- `Defaults`
- `ProviderConfig`
- `ArtifactSources`
- `NetworkProfile`
- `VmRole`
- `CommandPreset`
- `Lab`

Design rules:

- Generic lab intent should remain backend-neutral.
- Provider-specific settings belong in provider config or provider override
  sections.
- Presets stay YAML-editable.
- Runtime overrides are temporary by default and later live under `.playground/`.
- Persisting runtime changes back to config must be explicit.

See `docs/config_design.md` for detailed config design.

## Validation And Diagnostics

Validation returns diagnostics rather than crashing on user mistakes.

Diagnostics should include:

- severity
- message
- file path
- YAML/key path when available
- suggested fix when useful

Validation currently covers schema shape and several reference checks. Before
backend automation, it must also cover:

- missing defaults
- workload placement targets
- routing intent preservation
- budget totals
- offline artifact availability
- accurate source tracking when filenames differ from metadata names

## Resolution

The resolver converts typed config into a backend-neutral `ResolvedLab`.

Resolution responsibilities:

- apply defaults
- select the requested lab
- flatten VM role inheritance
- apply VM-level overrides
- expand network profiles
- expand command presets
- resolve artifact references
- include enough source mapping for CLI output and later backend planning

`ResolvedLab` is the future input to planning and backend adapters. It must
remain conservative: if important intent is missing, do not use it for apply.

## CLI

The first CLI slice is read-only:

```text
playground validate
playground lab list
playground lab show <name>
```

Goals:

- prove config loading and validation from a user-facing command
- show useful diagnostics
- expose resolved lab output without mutating infrastructure
- establish command structure before TUI work

Later CLI commands can add active lab selection, plan, apply, status, destroy,
doctor, cache, command presets, and run inspection.

## Planner

The planner is future work. It compares desired state from `ResolvedLab` with
current `.playground/` state and backend-observed state.

Planner output should include:

- create/update/delete/no-op actions
- resource budget impact
- warnings and validation blockers
- rendered backend input previews where useful
- human-readable and machine-readable plan formats

The planner should not execute backend commands.

## Operation Runner And Events

The operation runner is future work for mutating operations.

Responsibilities:

- allocate operation run IDs
- execute plan steps
- publish structured lifecycle events
- stream output to CLI/TUI
- persist JSONL logs and summaries
- track per-resource operation status
- leave recoverable state after failures

The initial implementation can use an in-process event bus plus append-only
JSONL event logs under `.playground/runs/`.

## Backend Adapters

Backend adapters translate generic lab intent into concrete tools.

Initial adapters:

- local-libvirt/OpenTofu provisioning
- Ansible guest configuration
- Docker workload management

Future adapters:

- cloud providers
- Android-specific workflows
- security/capture tooling

Adapter requirements:

- declare capabilities
- validate provider-specific settings
- render or invoke backend tools
- return observed state and outputs
- emit structured events
- keep backend files inspectable

## Local-Libvirt / OpenTofu Bridge

The local-libvirt bridge should eventually consume resolved lab input and render
or pass concrete backend inputs into `tofu/`.

Required outputs:

- VM names
- VM IDs where available
- IP addresses
- network names and CIDRs
- SSH targets

The first bridge slice should not rewrite provisioning. A safe first step is to
generate Ansible inventory from `tofu output -json`.

## Ansible Bridge

The Ansible bridge should consume generated inventory from observed VM state and
run roles against selected hosts.

Inventory should preserve:

- lab name
- host name
- IP/SSH user
- VM role
- attached networks
- tags where useful

Roles must remain idempotent and visible under `ansible/roles/`.

## Docker Workloads

Docker support is a product requirement, but execution should be sliced after
basic VM and Docker-host readiness.

Future workload support:

- standalone containers
- Compose stacks
- Swarm initialization and join
- host and VM placement
- status and logs

Placement must consider explicit targets, role capabilities, network needs,
resource needs, and operator overrides.

## State Layout

Generated state must live under `.playground/`.

Reserved layout:

```text
.playground/
  state/
    active-lab.json
    observed/
    rendered/
    inventory/
  runs/
    <run-id>/
      run.json
      summary.md
      logs/
  cache/
    artifacts/
    metadata/
  artifacts/
```

Rules:

- `.playground/` is Git-ignored.
- User-authored config stays outside `.playground/`.
- Backend modules stay visible in `tofu/` and `ansible/`.
- Cleanup must never remove user-authored config.

## Offline And Artifact Handling

Artifact sources are configured under `config/artifacts/`.

Supported source types should include:

- remote URLs
- local files
- local directories
- registries
- mirrors
- archives

When `offline: true`, validation and planning must reject uncontrolled internet
downloads before apply. Cache preparation can be added after read-only CLI and
basic backend bridging are stable.

## TUI

The TUI is a future layer over stable CLI/core APIs.

It should not define separate business logic. It should consume the same config,
state, operation, event, and diagnostic models as the CLI.

## Security And Trust Model

The operator is trusted. The system should:

- expose risks clearly
- validate dangerous or inconsistent inputs
- avoid hardcoded secrets
- keep SSH keys and credentials configurable
- avoid silent host mutation
- reserve strict blocking behavior for explicit strict modes or hard errors

## Current Design Gap

The config plane currently describes richer intent than the working backend:

- multiple network profiles
- router role and routing intent
- Docker workload placement
- offline artifact model
- operation runs and logs

The OpenTofu/Ansible baseline currently provisions and configures a narrower
fixed local VM flow.

Therefore:

- read-only CLI comes first
- validation hardening comes next
- backend automation waits until `ResolvedLab` can safely represent the needed
  intent

## Implementation Sequence

1. Read-only CLI: validate, lab list, lab show.
2. Validation hardening.
3. Resolved model completion.
4. Generate Ansible inventory from `tofu output -json`.
5. Plan rendering from `ResolvedLab`.
6. Apply/status/destroy wrappers.
7. Operation runs and structured logs.
8. Docker workload management.
9. TUI over stable CLI/core APIs.
