# Platform Design Notes

This document summarizes active design constraints. Product intent lives in
`docs/product/requirements.md`, `docs/product/user_stories.md`, and
`docs/product/mvp_scope.md`.

## Source Of Truth

The working infrastructure path is still:

```text
tofu/ -> ansible/ -> Redroid -> ADB
```

The Python platform code under `src/playground/` is the emerging control layer.
It must prove each read-only step before it replaces or automates the existing
OpenTofu and Ansible flow.

## Current Boundaries

- `config/`: user-authored lab intent.
- `src/playground/models/`: typed YAML and resolved-lab models.
- `src/playground/config/`: discovery, loading, and resolution.
- `src/playground/validation/`: diagnostics for schema and references.
- `.playground/`: generated runtime state and logs.
- `tofu/`: current OpenTofu implementation.
- `ansible/`: current configuration implementation.

## Design Constraints

- Keep generated runtime state under `.playground/`.
- Keep OpenTofu and Ansible idempotent.
- Do not hardcode secrets, SSH keys, passwords, or local-only credentials.
- Preserve `cpu { mode = "host-passthrough" }` for Redroid-capable VMs.
- Preserve Redroid binderfs, privileged container mode, and ADB port exposure
  unless a replacement is explicitly designed and tested.

## Current Design Gap

The config plane and IaC plane are not yet unified. `config/labs/generic-infra.yaml`
describes richer intent than `tofu/` and `ansible/` currently implement.

Do not drive backend automation directly from `ResolvedLab` until validation and
resolution cover:

- required defaults
- workload placement targets
- budget totals
- offline artifact behavior
- routing intent
- source tracking for accurate diagnostics

The next safe product slice is read-only CLI support: validate, list, and show.

Detailed design:

- `docs/system_design.md`
- `docs/config_design.md`
