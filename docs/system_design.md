# System Design

This design is derived from `docs/product/requirements.md` and the current repo
state.

## Source Of Truth

Product intent:

1. `docs/product/requirements.md`
2. `docs/product/user_stories.md`
3. `docs/product/mvp_scope.md`

Current implementation:

1. `config/`
2. `src/playground/`
3. `tofu/`
4. `ansible/`

## Conceptual Flow

```text
YAML config tree
  -> config discovery/loading
  -> schema and reference validation
  -> resolved lab model
  -> read-only CLI inspection
  -> planner
  -> operation runner
  -> backend adapters
  -> .playground state/runs/logs/cache
```

Only the first four steps are in scope for the next implementation slice.

## Components

### Config Tree

User-authored YAML under `config/` defines defaults, providers, artifact
sources, network profiles, VM roles, command presets, and labs. These files are
intended to be committed.

### Config Loader

`src/playground/config/` discovers YAML files, parses them into typed models,
and returns diagnostics instead of aborting on the first user error.

### Validation

`src/playground/validation/` checks references and emits `Diagnostic` objects.
Validation must grow before backend automation: missing defaults, workload
placement, routing intent, budget totals, offline artifacts, and source tracking
are the next gaps.

### Resolver

The resolver lowers loaded config into a backend-neutral `ResolvedLab`. It must
remain conservative: if intent cannot be represented safely, do not use it to
drive backend operations.

### CLI

The CLI should arrive before TUI or backend automation. Initial commands:

```text
playground validate
playground lab list
playground lab show <name>
```

The CLI must support human-readable output first and machine-readable output as
the model stabilizes.

### Backend Modules

`tofu/` and `ansible/` are the current working backend modules. They should stay
visible and editable. The Python platform should wrap or generate inputs for
them only after read-only config behavior is proven.

### State

Generated state belongs under `.playground/` and must remain Git-ignored. Future
state includes active lab, rendered backend files, inventories, operation runs,
logs, cache metadata, and artifacts.

## Current Design Gap

The config plane describes richer intent than the current OpenTofu and Ansible
implementation. For example, `generic-infra` includes multiple networks, router
intent, and Docker workload placement, while the current backend path is still a
fixed local libvirt VM flow.

Do not automate apply/destroy from `ResolvedLab` until this gap is closed.

## Sequencing

1. Read-only CLI.
2. Validation hardening.
3. Complete resolved model for backend input.
4. Generate inventory from `tofu output -json`.
5. Plan rendering.
6. Apply/status/destroy wrappers.
7. Operation runs/logging.
8. TUI over stable CLI/core APIs.
