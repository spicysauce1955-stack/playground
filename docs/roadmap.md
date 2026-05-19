# Roadmap

This is the current sequential task queue.

Source documents:

- `docs/product/requirements.md`
- `docs/product/user_stories.md`
- `docs/product/mvp_scope.md`
- `docs/system_design.md`
- `docs/config_design.md`
- `docs/engineering_principles.md`
- `docs/architecture_decisions.md`

## 1. Baseline Cleanup

Status: done.

Goal: make the repository ready for the next implementation slice.

Acceptance:

- extra local branches are gone
- root `main.tf` stub is retired
- workflow files are committed together
- old parallel planning tree is removed
- durable design constraints live under `docs/`
- product intent has been rehomed under `docs/product/`

## 2. Read-Only CLI

Status: done.

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

Status: done.

Goal: make `ResolvedLab` safe as a future backend input.

Acceptance:

- missing defaults are diagnostics, not late resolver exceptions
  (`config.required.defaults_missing`)
- workload placement targets are validated against the full
  `spec.extends` chain (`config.reference.unknown_workload_target`)
- routing intent survives resolution (`ResolvedVm.routing`)
- budget checks exist for VM totals (`config.budget.exceeded`,
  strict→error / permissive→warning, falls back to `Defaults.spec.budget`)
- source paths in diagnostics are accurate enough for CLI use
  (`LoadedConfig.sources[(kind, name)]` from the loader)
- offline labs flag missing VM-image artifacts before apply
  (`config.artifact.offline_missing`). Other artifact classes from
  `requirements.md` §5.13 are tracked separately; see
  `docs/config_design.md` "Validation Rules".

Note: `playground validate` now exits with code 1 when `Defaults` is
absent — previously the resolver crashed later with a less actionable
error.

## 4. OpenTofu / Ansible Bridge

Status: in progress (first slice done; follow-ups queued).

Goal: reduce manual handoff without changing runtime behavior.

First slice (done):

- `playground inventory render <lab>` writes
  `.playground/state/inventory/<lab>.ini` from a `ResolvedLab` plus
  `tofu output -json`
- new backend adapter layer under `src/playground/backend/local_libvirt/`
- five `config.inventory.*` diagnostics for the failure modes
- `tofu/`, `ansible/site.yml`, and `ansible/roles/*` are unchanged

Known limitations to close in follow-up slices:

- VMs are paired with `vm_ips` by **declaration order** (`lab.spec.vms[i]
  <-> tofu vm_ips[i]`). Reordering VMs in the lab YAML silently re-routes
  Ansible roles. Mitigation today: header warning in the generated file
  plus `config.inventory.count_mismatch` diagnostic. Permanent fix: enrich
  `tofu/outputs.tf` to expose a name-keyed map, then update the renderer
  to match on names.
- Only a single `[playground]` group is emitted today. `[docker_host]` /
  `[router]` groups can be added when a playbook needs them.
- CLI imports the concrete `playground.backend.local_libvirt` adapter
  directly. Introduce a small adapter protocol / registry only when a
  second backend appears.
