# Roadmap

This is the current sequential task queue.

Source documents:

- `docs/product/requirements.md`
- `docs/product/user_stories.md`
- `docs/product/mvp_scope.md`
- `docs/system_design.md`
- `docs/config_design.md`

## 1. Baseline Cleanup

Goal: make the repository ready for the next implementation slice.

Acceptance:

- extra local branches are gone
- root `main.tf` stub is retired
- workflow files are committed together
- old parallel planning tree is removed
- durable design constraints live under `docs/`
- product intent has been rehomed under `docs/product/`

## 2. Read-Only CLI

Goal: prove the Python config layer without touching real infrastructure.

Commands:

```text
playground validate
playground lab list
playground lab show <name>
```

Acceptance:

- `validate` reports diagnostics and exits nonzero on errors
- `lab list` shows configured labs
- `lab show <name>` emits a resolved lab as JSON
- unit tests cover command wiring and invalid config behavior

## 3. Validation Hardening

Goal: make `ResolvedLab` safe as a future backend input.

Acceptance:

- missing defaults are diagnostics, not late resolver exceptions
- workload placement targets are validated
- routing intent survives resolution
- budget checks exist for VM totals
- source paths in diagnostics are accurate enough for CLI use

## 4. OpenTofu / Ansible Bridge

Goal: reduce manual handoff without changing runtime behavior.

Candidate first slice:

- generate `ansible/inventory.ini` from `tofu output -json`
- keep the current manual flow documented as fallback
- do not change Redroid/Docker role behavior in the same slice
