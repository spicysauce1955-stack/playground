# Test Strategy

## 1. Goals

The playground controls real local infrastructure, so tests must catch config and planning errors early before backend tools mutate state. The test strategy should emphasize fast deterministic tests first, then integration tests, then optional real-system tests.

## 2. Test Layers

### 2.1 Static And Schema Tests

Purpose:

- Validate YAML shape, references, defaults, and diagnostics.

Coverage:

- Valid sample lab.
- Invalid YAML syntax.
- Missing role references.
- Missing network references.
- Duplicate object names.
- Invalid provider name.
- Invalid network mode.
- Invalid resource budget.
- Offline mode with missing artifact source.

Expected result:

- Diagnostics include severity, file path, YAML path, message, and suggested fix where possible.

### 2.2 Unit Tests

Purpose:

- Test core platform logic without external tools.

Coverage:

- Config loader.
- Default merge.
- Runtime override merge.
- Role expansion.
- Network profile expansion.
- Artifact source resolution.
- Budget estimation.
- Placement resolution.
- Operation run creation.
- Operation event publishing.
- Event subscriber fanout.
- Event persistence to JSONL.
- Structured log writing.
- Retention policy selection.

### 2.3 CLI Tests

Purpose:

- Ensure command wiring and output behavior work.

Coverage:

- `playground validate`
- `playground lab list`
- `playground lab select`
- `playground plan`
- `playground runs list`
- `playground runs show`
- JSON output mode.
- Non-interactive failure behavior.

### 2.4 Adapter Tests With Mocked Tools

Purpose:

- Verify backend adapters parse and handle command output correctly without requiring real infrastructure.

Coverage:

- OpenTofu output parsing.
- OpenTofu failure handling.
- Ansible inventory rendering.
- Ansible result parsing.
- Docker status parsing.
- Doctor binary/version checks.

### 2.5 Integration Tests

Purpose:

- Exercise multiple components together with temporary project directories.

Coverage:

- Load sample config, validate, plan, write run record.
- Select active lab and generate state.
- Run a fake long-running operation and verify live events, logs, status snapshots, and summary all derive from the same event stream.
- Render local-libvirt backend inputs.
- Render Ansible inventory.
- Create logs and summaries.
- Cleanup/retention dry-run.

### 2.6 System Tests

Purpose:

- Verify real local backend behavior on a prepared host.

Coverage:

- Provision one `generic-node`.
- Destroy one `generic-node`.
- Provision one `docker-host` and configure Docker.
- Validate network creation.
- Run a simple container or Compose stack.

These tests should be explicitly marked and not run by default on every machine.

## 3. Test Data

Recommended fixtures:

```text
tests/fixtures/configs/
  valid-generic-infra/
  invalid-missing-role/
  invalid-missing-network/
  invalid-offline-artifact/
  budget-warning/
```

## 4. Acceptance Test Scenarios

### Scenario 1: Valid Generic Lab

Given a valid `generic-infra` config,
when the user runs validation,
then validation succeeds with no errors.

### Scenario 2: Missing Role

Given a lab references `role: missing-role`,
when the user runs validation,
then validation fails and suggests adding or correcting the role reference.

### Scenario 3: Budget Warning

Given a lab exceeds configured RAM budget in permissive mode,
when the user runs plan,
then the plan includes a warning and does not block.

### Scenario 4: Offline Artifact Missing

Given `offline: true` and a missing local VM image/cache entry,
when the user runs plan or apply,
then the operation fails before backend mutation.

### Scenario 5: Operation Run Created

Given any validate/plan/apply operation,
when the operation runs,
then a run record exists under `.playground/runs`.

### Scenario 5.1: Long Operation Emits Events

Given a long-running operation,
when it starts, reports progress, updates a resource, and completes,
then the CLI/TUI subscriber, JSONL log, human log, status snapshot, and run summary receive consistent events.

### Scenario 6: Docker Host Ready

Given a lab with one `docker-host`,
when apply/configure completes,
then status shows Docker installed and reachable on that VM.

## 5. Manual QA

Manual checks:

- Confirm `.playground/` is ignored by Git.
- Confirm run summaries are readable.
- Confirm raw backend logs are available.
- Confirm failed backend commands do not erase useful state.
- Confirm cleanup does not remove user-authored config.
- Confirm TUI/CLI show the same active lab.

## 6. CI Guidance

Default CI should run:

- formatting/linting
- unit tests
- schema/config tests
- CLI tests with temp directories
- mocked adapter tests

CI should not run real libvirt/KVM tests unless a dedicated runner is configured.

## 7. Risk-Based Focus

Highest-risk areas:

- Bad config causing unwanted resource mutation.
- Incorrect destroy scope.
- Offline mode accidentally downloading from the internet.
- Generated files mixing with user-authored config.
- Backend output parsing failures.
- Partial apply leaving unclear state.

Tests should prioritize these areas before polishing UI behavior.
