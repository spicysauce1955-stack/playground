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

Status: done.

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
  `config.inventory.vm_ip_not_found`
- legacy positional `vm_ips` payloads from pre-4b state are explicitly
  rejected so silent index drift can't return

Slice 4d (done):

- `playground tofu render <lab>` writes
  `.playground/state/tofu/<lab>.tfvars.json` from a `ResolvedLab` so
  `var.vm_names` stays in sync with the lab. Closes the last manual
  handoff: operator runs `playground tofu render … && tofu -chdir=tofu
  apply -var-file=…`.
- new validator check `config.backend.per_vm_resources_unsupported`
  (warning) fires whenever a lab declares heterogeneous per-VM resources
  that the local-libvirt backend cannot honor today. Surfaces under
  `playground validate` and every command that depends on it.
- `_resolve_lab_or_exit` helper extracted from the three CLI commands
  that resolve a lab (`lab show`, `inventory render`, `tofu render`).

Slice 4c (done):

- Inventory now emits one `[role_group]` per distinct VM role in
  addition to `[playground]`. Group names normalize kebab→snake
  (`docker-host` → `docker_host`) so they're valid Ansible
  identifiers. Future playbooks can target `hosts: docker_host` etc.
  without scanning host vars.

Carried forward to future work:

- Per-VM `resources` from the lab still don't reach tofu. Today's
  `tofu/main.tf` applies global `var.vm_memory` / `var.vm_vcpu`
  uniformly; the `config.backend.per_vm_resources_unsupported`
  warning documents the gap. Future slice can enrich tofu to accept
  per-VM resources as a list of objects.
- CLI imports the concrete `playground.backend.local_libvirt` adapter
  directly. Introduce a small adapter protocol / registry only when a
  second backend appears.

## 5. Plan Rendering

Status: in progress (first slice done; state-observation slice queued).

Slice 5a (done):

- `playground plan <lab>` renders a backend-neutral `Plan` from a
  `ResolvedLab`. Today every action verb is `create`; future verbs
  (`update` / `delete` / `no_op`) are reserved in `ActionVerb` and
  unlock when state observation lands.
- `Plan` carries: per-resource actions (network/vm/workload),
  aggregate budget (totals vs limits + `fits` flag), and validator
  warnings carried forward as a snapshot.
- New module `src/playground/planner/` — peer of `validation/`,
  `config/`, `backend/`. Pure function `render_plan(resolved,
  warnings=None) -> Plan`.
- Human and JSON output modes.

Slice 5b (queued):

- State observation: read `.playground/state/observed/` and backend
  reports (e.g. `tofu state list -json`, libvirt domain query).
- Emit `update` / `delete` / `no_op` actions and `before`/`after`
  details where applicable.
- Promote `plan` to a subapp (`plan render`, `plan show <run-id>`,
  `plan diff`) once operation runs land.

## 6. Apply / Status / Destroy

Status: done.

Slice 6a (done):

- `playground apply <lab>` chains render tfvars → tofu apply →
  fetch_vm_ips → render inventory → ansible-playbook, wrapped in an
  operation run record.
- New module `src/playground/runs/operation.py`: `OperationRun`,
  `StepResult`, `allocate_run_id`, `start_run`, `finish_run`. Writes
  `.playground/runs/<id>/run.json` plus captured subprocess logs.
- New module `src/playground/backend/local_libvirt/apply.py`: thin
  subprocess wrappers for `tofu apply` and `ansible-playbook` with
  combined-stream log capture.
- Failure protocol: any step's nonzero exit (or missing-binary
  diagnostic) finalizes the run as `failed` with a summary tailored
  to what state the lab is now in (e.g. "VMs were provisioned but
  Ansible configuration failed — re-run apply or destroy via tofu").
- Two new diagnostic IDs: `runtime.apply.tofu_binary_missing`,
  `runtime.apply.ansible_binary_missing`. New `runtime.*` namespace
  separates execution-time concerns from config-side `config.*`
  diagnostics.

Slice 6b (done):

- `playground destroy <lab>` re-renders the same tfvars apply uses,
  then runs `tofu destroy -auto-approve -var-file=...`. Wrapped in
  an OperationRun with `operation: destroy`. Same failure protocol
  as apply: nonzero tofu exit finalizes the run as `failed` with a
  summary telling the operator what to inspect.
- Symmetric with apply: re-rendering the tfvars guarantees tofu
  sees the same `var.vm_names` as the apply did, so destroy
  targets the right resources.

Slice 6c (done):

- `playground status <lab>` — read-only snapshot. Pairs
  `ResolvedLab.vms` with `tofu output -json` to report
  `provisioned` / `missing` per VM. No run record (read-only per
  §5.10). Ansible reachability + docker readiness are reserved as
  states (`running` / `failed` / `degraded` in `VmState`) and land
  alongside §8 (Docker workloads).
- New backend-neutral model `playground.models.status` (`LabStatus`,
  `VmStatus`). Adapter `playground.backend.local_libvirt.status`
  composes `fetch_vm_ips` with the model and treats `tofu_no_state`
  as the steady "nothing applied yet" status rather than an error.
- `TOFU_NO_STATE_DIAGNOSTIC_ID` exported from `inventory.py` so the
  status adapter doesn't depend on a magic string.

## 7. Operation Runs + Events

Status: in progress (first slice done).

Slice 7a (done):

- New module `src/playground/events/` with `OperationEvent`,
  in-process `EventBus`, and a `JsonlWriter` subscriber that appends
  one event per line to `.playground/runs/<id>/events.jsonl`.
- `playground apply` and `playground destroy` now publish
  `operation_started`, `step_started`, `step_finished`, and
  `operation_finished` events around their tofu/ansible steps. The
  `operation_finished` event fires even on failure so an event log
  is always reconstructable.
- New CLI: `playground runs list` (newest-first, with status + start/
  end timestamps) and `playground runs show <run-id>` (renders the
  recorded `run.json`, step results, events path, log dir).

Carried forward to future work:

- Live subprocess streaming as events (`log_line`-style). Today we
  still capture combined stdout/stderr to per-step log files.
- Retention enforcement (the `RetentionPolicy` model exists; the
  cleanup pass doesn't).
- Additional event consumers (TUI views, status caches) when those
  arrive in §9.

## Backlog (acknowledged, not sequenced)

Items confirmed as real product needs but explicitly not urgent —
captured here so they aren't lost.

- `TargetSelector.network` field — requirements §5.9 calls for
  selectors keyed on **network** in addition to name / role / tag.
  Today's `TargetSelector` has `role / vm / tag / any` only.
- Lab-scoped DNS — Story 5.2 / §5.6 require DNS names scoped per
  lab. Today `tofu/main.tf` hardcodes `domain = "playground.local"`
  and the schema has no per-lab `dns_domain`. Worth landing
  alongside the §6 apply slice so DNS shows up correctly the first
  time real VMs come up.
- Runtime overrides + promote — Story 2.3 / §5.2 require temporary
  CLI/TUI overrides on top of YAML, with an explicit "promote back
  to YAML" path. Schema slot `ResolvedLab.runtime_overrides:
  list[Any]` is reserved and unused; needs a real type, a state
  store under `.playground/state/overrides/`, and CLI commands to
  set / clear / promote.
