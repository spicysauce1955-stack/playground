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

Status: in progress (slices 4a and 4b done; follow-ups queued).

Goal: reduce manual handoff without changing runtime behavior.

Slice 4a (done):

- `playground inventory render <lab>` writes
  `.playground/state/inventory/<lab>.ini` from a `ResolvedLab` plus
  `tofu output -json`
- new backend adapter layer under `src/playground/backend/local_libvirt/`
- `config.inventory.*` diagnostics for the failure modes
- `ansible/site.yml` and `ansible/roles/*` unchanged

Slice 4b (done):

- `tofu/outputs.tf` emits `vm_ips` as a **name-keyed map**
  (`{domain -> ip}`) instead of a positional tuple
- new `var.vm_names` in `tofu/variables.tf` lets the operator name
  libvirt domains after their lab VMs (`lab.spec.vms[*].name`); default
  falls back to `pg-node-N` for backward compatibility
- renderer matches by name; mismatches surface as
  `config.inventory.vm_ip_not_found` with a suggestion that lists the
  known tofu domain names
- legacy positional `vm_ips` payloads from pre-4b state are explicitly
  rejected so silent index drift can't return

Known limitations to close in follow-up slices:

- The operator still has to keep `var.vm_names` in `tofu/terraform.tfvars`
  aligned with `lab.spec.vms[*].name` by hand. Next slice: auto-generate
  `terraform.tfvars` from the resolved lab so the two stay in sync.
- Only a single `[playground]` group is emitted today. `[docker_host]` /
  `[router]` groups can be added when a playbook needs them.
- CLI imports the concrete `playground.backend.local_libvirt` adapter
  directly. Introduce a small adapter protocol / registry only when a
  second backend appears.
