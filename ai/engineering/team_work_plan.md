# Team Work Plan

## 1. Goal

Split implementation across three teams that each design, plan, code, and test their assigned area on separate branches, then merge through a controlled integration path.

The split should minimize merge conflicts and force clear contracts between teams.

## 2. Branch Model

Recommended branches:

```text
main
  integration/mvp-platform
    team/core-config-state
    team/local-backend-runtime
    team/operator-experience
```

Rules:

- `main` remains stable.
- `integration/mvp-platform` is the shared merge branch for MVP work.
- Each team works on its own branch.
- Teams merge into `integration/mvp-platform`, not directly into `main`.
- `integration/mvp-platform` merges into `main` only after cross-team acceptance tests pass.

## 3. Shared Working Rules

- Each team owns its files/modules and avoids editing other teams' files without coordination.
- Shared contracts are reviewed before code begins.
- Every team must provide:
  - design notes
  - implementation plan
  - code
  - tests
  - usage examples
  - run logs or verification notes
- Teams must not change agreed interfaces silently.
- Any contract change requires updating the relevant document under `ai/architecture/` or `ai/engineering/`.

## 4. Team A: Core Config, State, And Events

Branch:

```text
team/core-config-state
```

Primary ownership:

- Config tree.
- YAML schemas/models.
- Validation.
- Resolution.
- `.playground/` state.
- Operation runs.
- Event bus.
- Structured logs.
- Retention.

Owned docs:

- `ai/architecture/config_design.md`
- relevant sections of `ai/architecture/system_design.md`
- event/run/state sections of `ai/engineering/task_breakdown.md`

Likely owned code areas:

```text
src/playground/config/
src/playground/models/
src/playground/validation/
src/playground/state/
src/playground/events/
src/playground/runs/
src/playground/logging/
tests/unit/config/
tests/unit/state/
tests/unit/events/
```

Deliverables:

- Initial `config/` tree.
- Typed config/resource models.
- Config loader.
- Validator with actionable diagnostics.
- Resolver that produces a normalized lab model.
- Active lab state file.
- Operation run model.
- In-process pub/sub event bus.
- JSONL event log subscriber.
- Human log/summary subscriber.
- Retention policy model.

Interfaces exported to other teams:

- `ResolvedLab` model.
- `Diagnostic` model.
- `OperationRun` model.
- `OperationEvent` model.
- `EventBus` publisher/subscriber API.
- State store read/write API.

Acceptance criteria:

- `playground validate` can validate sample configs.
- A valid lab resolves into a normalized model.
- Invalid references produce file/key/suggested-fix diagnostics.
- Operation runs can be created without backend mutation.
- Fake long-running operations publish events consumed by multiple subscribers.
- `.playground/` is initialized and ignored by Git.

Testing responsibility:

- Unit tests for config loading, validation, resolution, and events.
- Integration test for fake operation run producing JSONL logs and summary.

## 5. Team B: Local Backend And Runtime Execution

Branch:

```text
team/local-backend-runtime
```

Primary ownership:

- Local-libvirt adapter.
- OpenTofu integration.
- Ansible integration.
- Docker host configuration.
- Docker workload execution.
- Router/network runtime behavior.
- Doctor checks that touch host/backend readiness.

Owned docs:

- `ai/architecture/backend_contracts.md`
- backend sections of `ai/architecture/system_design.md`
- backend/runtime milestones in `ai/engineering/task_breakdown.md`

Likely owned code areas:

```text
src/playground/providers/
src/playground/providers/local_libvirt/
src/playground/backends/tofu/
src/playground/backends/ansible/
src/playground/backends/docker/
src/playground/doctor/
src/playground/runtime/
tests/unit/providers/
tests/unit/backends/
tests/integration/backend_rendering/
```

Deliverables:

- Provider adapter interface implementation for `local-libvirt`.
- OpenTofu input renderer/wrapper.
- OpenTofu output parser.
- Project-local OpenTofu state path strategy.
- Ansible inventory renderer.
- Ansible runner wrapper.
- Docker readiness checks.
- Docker workload execution skeleton.
- Doctor checks for host/backend readiness.
- Backend events published to Team A event API.

Interfaces consumed from Team A:

- `ResolvedLab`.
- `OperationRun`.
- `EventBus`.
- State store API.
- Diagnostics model.

Interfaces exported to Team C:

- Backend status model.
- Resource status model.
- Plan/apply/destroy operation APIs.
- Doctor result model.

Acceptance criteria:

- `playground plan` can render local-libvirt backend inputs.
- Ansible inventory can be generated from resolved/observed VM state.
- Doctor reports missing binaries and backend readiness problems.
- Backend wrapper emits structured events for command start/output/finish/failure.
- With real backend enabled later, one `generic-node` can be provisioned and destroyed.

Testing responsibility:

- Unit tests with mocked OpenTofu/Ansible/Docker outputs.
- Integration tests for rendering backend inputs and inventories.
- Optional real local-libvirt system tests gated behind explicit flag.

## 6. Team C: Operator Experience, CLI, TUI, And Workflows

Branch:

```text
team/operator-experience
```

Primary ownership:

- CLI command surface.
- TUI.
- Human-readable output.
- Plan/status/run views.
- Command presets UX.
- Documentation and examples for operator workflows.

Owned docs:

- CLI/TUI sections of `ai/product/mvp_scope.md`
- operator workflow sections of `ai/engineering/implementation_plan.md`
- user-facing usage docs added later

Likely owned code areas:

```text
src/playground/cli/
src/playground/tui/
src/playground/output/
src/playground/commands/
docs/
tests/cli/
tests/tui/
```

Deliverables:

- CLI skeleton and command routing.
- CLI output modes: human and JSON.
- Lab list/select commands.
- Validate command presentation.
- Plan/status/run views.
- Command preset runner UI/CLI.
- TUI lab selector.
- TUI dashboard/resource tree/log viewer after core APIs stabilize.

Interfaces consumed from Team A:

- Config validation API.
- Active lab state API.
- Operation run API.
- Event subscription API.
- Diagnostics model.

Interfaces consumed from Team B:

- Plan/apply/status/destroy APIs.
- Doctor result API.
- Backend/resource status models.

Acceptance criteria:

- CLI commands are wired and tested, even if some backend operations are initially stubbed.
- CLI renders diagnostics clearly.
- CLI can stream events from a fake long-running operation.
- TUI uses the same core APIs as CLI.
- TUI shows active lab, resources, run logs, and operation progress.

Testing responsibility:

- CLI tests with temporary project directories.
- Snapshot-style tests for human output where useful.
- JSON output contract tests.
- TUI smoke tests after framework choice.

## 7. Shared Contracts To Freeze First

Before coding starts, all teams should agree on these minimal contracts:

1. `ResolvedLab`
2. `Diagnostic`
3. `OperationRun`
4. `OperationEvent`
5. `ResourceStatus`
6. `ProviderAdapter`
7. `StateStore`
8. CLI command names and exit-code rules

These can be simple at first. The important part is that teams do not invent incompatible versions.

## 8. Merge Order

Recommended first integration order:

1. Team A merges config/state/event foundations into `integration/mvp-platform`.
2. Team C merges CLI skeleton that uses Team A APIs.
3. Team B merges backend render/doctor logic that consumes Team A models and emits Team A events.
4. Team C updates CLI/TUI views to consume Team B status/plan APIs.
5. All teams run integration tests.
6. `integration/mvp-platform` merges to `main`.

Reasoning:

- Team A defines the data/event backbone.
- Team C can build against stubs early.
- Team B needs stable models/events to avoid rework.

## 9. Cross-Team Integration Tests

Required before merging `integration/mvp-platform` to `main`:

- Valid sample lab loads, validates, and resolves.
- CLI lists and selects labs.
- Plan command renders intended resources.
- Fake backend operation streams events.
- Run record contains events, human log, and summary.
- Doctor command reports mocked host readiness.
- JSON output contracts are stable.
- `.playground/` state is created and ignored.

Optional gated tests:

- Real OpenTofu/libvirt provision one VM.
- Real Ansible configure Docker host.
- Real Docker/Compose workload smoke test.

## 10. Coordination Cadence

Suggested cadence:

- Start of slice: each team writes a short design note and task checklist.
- Mid-slice: contract review if interfaces changed.
- Before merge: team posts verification notes and commands run.
- Integration day: merge into `integration/mvp-platform` and run cross-team tests.

## 11. Conflict Hotspots

Likely conflict areas:

- CLI command definitions.
- Shared model definitions.
- Config schema.
- State/run/event models.
- Sample `config/` tree.

Mitigation:

- Team A owns shared models.
- Team C owns command presentation, not core models.
- Team B owns backend adapters, not config schema.
- Contract changes go through docs first.

## 12. First Sprint Recommendation

Team A:

- Build config skeleton, models, validation, event/run foundation.

Team B:

- Design backend adapter contract in code against Team A draft models.
- Build mocked OpenTofu/Ansible/Docker runners.
- Build doctor checks that do not mutate the system.

Team C:

- Build CLI skeleton and fake-operation event streaming.
- Design initial TUI screens using stubbed APIs.

The first sprint should end with no real VM mutation required. It should prove that teams can share models, events, CLI output, and run records.
