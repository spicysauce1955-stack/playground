# Task Breakdown

## Milestone 1: Planning Package

Status: drafted

Tasks:

- [x] Write user stories.
- [x] Write requirements.
- [x] Write MVP scope.
- [x] Write system design.
- [x] Write config design.
- [x] Write backend contracts.
- [x] Write tech stack recommendation.
- [x] Write implementation plan.
- [x] Write task breakdown.
- [x] Write team work plan.
- [ ] Write QA/test strategy.

## Milestone 2: Config Skeleton

Status: drafted (Team A, branch `team/core-config-state`)

Tasks:

- [x] Create `config/` directory.
- [x] Add `config/defaults.yaml`.
- [x] Add provider config for `local-libvirt`.
- [x] Add artifact sources config.
- [x] Add role presets:
  - [x] `generic-node`
  - [x] `docker-host`
  - [x] `router`
- [x] Add network profiles:
  - [x] `nat`
  - [x] `isolated`
  - [x] `routed`
- [x] Add command presets:
  - [x] `check-docker`
  - [x] `ping-network`
- [x] Add sample lab `generic-infra`.

Definition of done:

- Config tree is readable.
- Sample lab captures day-one product intent.
- No backend implementation required yet.

## Milestone 3: Core CLI Skeleton

Tasks:

- [ ] Choose implementation language and package manager.
- [ ] Create source directory and executable entrypoint.
- [ ] Add CLI framework.
- [ ] Add commands:
  - [ ] `doctor`
  - [ ] `validate`
  - [ ] `lab list`
  - [ ] `lab select`
  - [ ] `plan`
  - [ ] `apply`
  - [ ] `status`
  - [ ] `destroy`
  - [ ] `runs list`
  - [ ] `runs show`
- [ ] Add common output modes:
  - [ ] human
  - [ ] JSON

Definition of done:

- CLI help works.
- Commands can run as stubs without backend mutation.
- Tests cover command wiring.

## Milestone 4: Config Loader And Schema

Status: done (Team A, branch `team/core-config-state`, commits
`38ae25f`, `ce0a3cd`, `6b1f0d9`, `db31d42`). See
`ai/handoffs/team-a-phase1.md`.

Tasks:

- [x] Implement YAML discovery. (`playground.config.discovery`)
- [x] Implement typed models. (`playground.models.kinds`,
      `playground.models.resolved`)
- [x] Implement source location tracking. (Diagnostic.source carries
      repo-relative path + key_path; per-key precision in source_map
      is deferred to Milestone 5.5)
- [x] Implement default merge behavior. (resolver §3.2 pipeline)
- [x] Implement role/network/artifact/command reference resolution.
      (`playground.validation.validator`)
- [x] Implement validation diagnostics. (15 IDs registered; see
      `ai/architecture/diagnostic_ids.md`)
- [x] Add tests for valid sample config.
- [x] Add tests for invalid references. (10 tests in
      `tests/unit/validation/`)
- [x] Add tests for malformed YAML. (10 tests in
      `tests/unit/config/test_loader.py`)
- [x] Add tests for duplicate object names. (loader-level + Lab-level
      duplicate guards)

Definition of done:

- `playground validate` gives actionable diagnostics. ✓ (function-level;
  CLI wiring is Team C)
- Sample config resolves into a normalized lab model. ✓

## Milestone 5: `.playground/` State And Runs

Status: not started; Team A next slice. Contracts are frozen in
`ai/architecture/shared_contracts.md §4–§5, §8, §9`. See §9 of
`ai/handoffs/team-a-phase1.md` for the planned commit sequence.

Tasks:

- [ ] Create state directory manager.
- [x] Add `.gitignore` entry for `.playground/`. (commit `280a111`)
- [ ] Implement active lab state file.
- [ ] Implement operation run ID generation.
- [ ] Implement run metadata file.
- [x] Define operation event schema. (doc-only;
      `shared_contracts.md §5`)
- [ ] Implement in-process event bus.
- [ ] Implement event publisher API for long-running operations.
- [ ] Implement JSONL event subscriber.
- [ ] Implement human log subscriber.
- [ ] Implement run summary subscriber.
- [ ] Implement status snapshot subscriber.
- [ ] Implement structured JSONL log writer.
- [ ] Implement human summary writer.
- [ ] Implement run listing.
- [ ] Implement run detail view.
- [x] Implement retention config parsing. (`RetentionPolicy` /
      `RetentionLogs` / `RetentionRuns` Pydantic models in commit
      `38ae25f`; enforcement is still TODO)
- [ ] Implement cleanup dry-run.

### Milestone 5.5: Resolver precision (deferred from Milestone 4)

- [ ] Thread `DiscoveredFile` through the loader/resolver so
  `ResolvedLab.source_map` carries per-key origins rather than the
  current coarse `spec → config/labs/<name>.yaml`.

Definition of done:

- Operations create inspectable run records.
- Operations emit structured events while running.
- CLI/TUI-facing status can be driven from events instead of blocking command output.
- Generated state stays under `.playground/`.

## Milestone 6: Doctor

Tasks:

- [ ] Check required binaries.
- [ ] Check KVM CPU capability.
- [ ] Check KVM modules.
- [ ] Check libvirt service/socket.
- [ ] Check user permissions where possible.
- [ ] Check project directories.
- [ ] Check backend module presence.
- [ ] Check Ansible role presence.
- [ ] Check SSH key paths.
- [ ] Check disk/RAM availability.
- [ ] Check offline artifacts when offline mode is enabled.
- [ ] Produce suggested fixes.

Definition of done:

- Doctor produces structured pass/warn/fail diagnostics.
- Doctor does not silently modify system state.

## Milestone 7: Planner

Tasks:

- [ ] Implement resolved lab model to resource graph.
- [ ] Implement budget estimator.
- [ ] Implement placement resolver.
- [ ] Implement local-libvirt plan renderer.
- [ ] Implement Ansible inventory renderer.
- [ ] Implement human-readable plan output.
- [ ] Implement JSON plan output.

Definition of done:

- `playground plan` shows intended changes for `generic-infra`.
- Plan output is useful without applying resources.

## Milestone 8: Local-Libvirt Apply

Tasks:

- [ ] Decide rendered OpenTofu input format.
- [ ] Configure project-local OpenTofu state path.
- [ ] Implement `tofu init` wrapper.
- [ ] Implement `tofu plan` wrapper.
- [ ] Implement `tofu apply` wrapper.
- [ ] Parse OpenTofu outputs.
- [ ] Store observed VM/network state.
- [ ] Implement destroy wrapper.
- [ ] Add failure handling and logs.

Definition of done:

- One `generic-node` can be provisioned and destroyed.
- VM IP appears in state/status.

## Milestone 9: Ansible Configuration

Tasks:

- [ ] Generate inventory from observed VM state.
- [ ] Run Ansible playbook against selected hosts.
- [ ] Configure `docker-host`.
- [ ] Collect Docker readiness facts.
- [ ] Add idempotency check.
- [ ] Add structured Ansible log handling.

Definition of done:

- `docker-host` reaches Docker-ready state.
- Re-run does not degrade state.

## Milestone 10: Networks And Router

Tasks:

- [ ] Implement named network rendering.
- [ ] Implement isolated/no-internet network mode.
- [ ] Implement routed network mode.
- [ ] Implement router role variables.
- [ ] Implement automatic route generation.
- [ ] Add topology validation.
- [ ] Add route/status display.

Definition of done:

- Sample lab can include NAT, isolated, and routed networks.
- Router role has basic automatic behavior.

## Milestone 11: Docker Workloads

Tasks:

- [ ] Implement standalone container model.
- [ ] Implement Compose workload model.
- [ ] Implement Swarm workload model.
- [ ] Implement target selection.
- [ ] Implement host Docker execution.
- [ ] Implement VM Docker execution.
- [ ] Implement workload status.
- [ ] Implement workload logs.

Definition of done:

- Compose stack runs on selected/auto-selected Docker host.
- Swarm cluster initializes in hybrid automatic/explicit mode.

## Milestone 12: TUI

Tasks:

- [ ] Choose TUI framework after language decision.
- [ ] Build lab selector view.
- [ ] Build active lab dashboard.
- [ ] Build resource tree.
- [ ] Build plan viewer.
- [ ] Build run/log viewer.
- [ ] Build command launcher.
- [ ] Build doctor diagnostics view.

Definition of done:

- TUI covers the common CLI workflow.
- TUI uses the same operation APIs as CLI.

## Milestone 13: Offline Cache

Tasks:

- [ ] Implement artifact source resolver.
- [ ] Implement cache metadata schema.
- [ ] Implement cache prepare for file/URL artifacts.
- [ ] Implement Docker image cache strategy.
- [ ] Implement provider/collection cache strategy.
- [ ] Enforce offline mode.
- [ ] Add cache listing.

Definition of done:

- Reusable cache supports multiple labs and versions.
- Offline validation is strict when enabled.

## Milestone 14: Future Expansion Planning

Tasks:

- [ ] Write Android/Redroid design.
- [ ] Write traffic capture design.
- [ ] Write security lab preset design.
- [ ] Write cloud provider adapter design.

Definition of done:

- Future features have designs that fit the existing model.
