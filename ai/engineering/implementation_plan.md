# Implementation Plan

## 1. Strategy

Implement the playground as a planning-first platform. The first implementation should prove the config/state/operation model before building a rich TUI or advanced Android/security features.

Work should be split across three teams using the branch and ownership model in `ai/engineering/team_work_plan.md`.

The core order is:

1. Define config tree and schemas.
2. Build validation and resolution.
3. Build state/run/log infrastructure.
4. Wrap local-libvirt/OpenTofu and Ansible as backend contracts.
5. Add VM/network lifecycle.
6. Add Docker workloads.
7. Add TUI on top of stable CLI/core APIs.

## 2. Phase 0: Planning Baseline

Goal:

- Establish the product and architecture documents needed before coding.

Deliverables:

- `ai/product/user_stories.md`
- `ai/product/requirements.md`
- `ai/product/mvp_scope.md`
- `ai/architecture/system_design.md`
- `ai/architecture/config_design.md`
- `ai/architecture/backend_contracts.md`
- `ai/architecture/tech_stack.md`
- `ai/engineering/implementation_plan.md`
- `ai/engineering/task_breakdown.md`
- `ai/qa/test_strategy.md`

Exit criteria:

- MVP scope is readable and bounded.
- Open questions are explicit.
- Next engineering slice is clear.

## 3. Phase 1: Config And Validation Core

Goal:

- Make YAML configs loadable, validateable, and resolvable into a typed lab model.

Deliverables:

- Initial `config/` tree with defaults, providers, roles, networks, artifacts, commands, and one generic lab.
- Config loader.
- Schema/typed model.
- Validation diagnostics.
- Reference resolver.
- Budget checker.
- Offline artifact source validation.

CLI commands:

```text
playground validate
playground lab list
playground lab select
```

Exit criteria:

- Valid sample lab passes.
- Invalid references produce useful file/key diagnostics.
- Active lab state is written under `.playground/state/active-lab.json`.
- Unit tests cover config loading and validation.

## 4. Phase 2: State, Runs, Logs

Goal:

- Establish durable operation tracking and reactive event streaming before mutating real infrastructure.

Deliverables:

- `.playground/` directory initializer.
- Operation run model.
- Operation event schema.
- In-process pub/sub event bus.
- Event subscribers for JSONL logs, human logs, summaries, CLI output, and status snapshots.
- Structured log event format.
- Human summary writer.
- Retention policy model.
- Run inspection commands.

CLI commands:

```text
playground runs list
playground runs show RUN_ID
playground cleanup
```

Exit criteria:

- Every CLI operation can create a run record.
- Long-running operations can publish progress/status events.
- Multiple subscribers can consume the same event stream.
- Logs are written as structured JSONL plus optional human log.
- Retention settings can be parsed and dry-run cleanup can report what would be removed.

## 5. Phase 3: Doctor And Backend Contracts

Goal:

- Verify local host readiness and backend contract integrity before applying labs.

Deliverables:

- Doctor checks for required binaries.
- KVM/libvirt readiness checks.
- Project structure checks.
- Backend contract checks.
- SSH key checks.
- Tool version warnings.
- Offline artifact checks.

CLI commands:

```text
playground doctor
```

Exit criteria:

- Doctor reports structured diagnostics.
- Doctor can create safe project-local directories when requested.
- Doctor does not modify system-level settings automatically.

## 6. Phase 4: Planning And Rendered Backend Inputs

Goal:

- Produce useful plans and rendered backend inputs without applying them.

Deliverables:

- Planner for VM/network desired state.
- Renderer for local-libvirt/OpenTofu inputs.
- Renderer for Ansible inventory.
- Resource budget plan warnings.
- Plan output in human and machine-readable forms.

CLI commands:

```text
playground plan
```

Exit criteria:

- Plan shows networks, VMs, roles, resources, and generated backend actions.
- Generated inventory includes role/network/tag metadata.
- Plan detects missing provider support or incomplete backend contract.

## 7. Phase 5: VM And Network Apply

Goal:

- Create and inspect real local-libvirt VMs and networks from the resolved lab model.

Deliverables:

- Local-libvirt apply adapter.
- OpenTofu init/plan/apply wrapper with structured logs.
- Output parser for VM IPs/network state.
- Status command for VM/network resources.
- Destroy command.

CLI commands:

```text
playground apply
playground status
playground destroy
```

Exit criteria:

- `generic-node` can be provisioned.
- VM IPs are captured into state.
- Generated inventory is updated.
- Destroy removes managed resources.
- Failed apply leaves a usable run record.

## 8. Phase 6: Docker Host Configuration

Goal:

- Configure `docker-host` VMs automatically.

Deliverables:

- Ansible execution adapter.
- Docker role contract check.
- Docker host readiness facts.
- Status display for Docker engine.

Exit criteria:

- `docker-host` VM is provisioned and configured.
- Docker version/readiness appears in status.
- Re-running configure is idempotent.

## 9. Phase 7: Router And Network Behavior

Goal:

- Make `router` role useful for routed/no-internet lab topologies.

Deliverables:

- Router role variables.
- Automatic route plan from attached networks.
- Basic IP forwarding.
- Optional NAT/firewall behavior where needed.
- Validation for invalid routed topology.

Exit criteria:

- Router VM can attach to multiple networks.
- Basic routing behavior is configured from lab model.
- TUI/CLI status shows router networks/routes.

## 10. Phase 8: Docker Workloads

Goal:

- Manage standalone containers, Compose stacks, and Swarm according to placement rules.

Deliverables:

- Workload model implementation.
- Placement resolver.
- Host/VM Docker targeting.
- Compose stack support.
- Swarm manager/worker planning.
- Workload logs/status.

Exit criteria:

- A Compose stack can run on an automatically selected docker host.
- A Swarm can be initialized with automatic manager/worker defaults.
- Explicit Swarm roles override automatic assignment.
- Workload logs are tied to operation runs.

## 11. Phase 9: TUI

Goal:

- Provide an operator console over the stable CLI/core operation model.

Deliverables:

- Lab selector.
- Active lab dashboard.
- Resource tree.
- Plan viewer.
- Run/log viewer.
- Command preset launcher.
- Doctor diagnostics view.

Exit criteria:

- TUI can perform the same key operations as CLI.
- TUI clearly shows active lab and temporary runtime overrides.
- TUI does not duplicate backend logic.

## 12. Phase 10: Offline Cache

Goal:

- Support reusable artifact preparation and offline operation.

Deliverables:

- Artifact resolver.
- Cache metadata.
- Cache prepare command.
- Local path/registry/archive support.
- Offline validation enforcement.

CLI commands:

```text
playground cache prepare
playground cache list
```

Exit criteria:

- Configured artifacts can be cached for reuse across labs.
- Multiple versions/tags can coexist.
- `offline: true` blocks uncontrolled downloads.

## 13. Deferred Phases

### Android/Redroid

- Android host role.
- Redroid lifecycle.
- ADB connection management.
- APK installation presets.
- Device status and logs.

### Traffic Capture

- Capture points on host/network/VM/container/device.
- `.pcap` artifacts.
- Wireshark/tshark integration.

### Security Lab Presets

- Attacker/victim topologies.
- Proxy/interception workflows.
- Vulnerable targets.
- Scan/report commands.

### Cloud Providers

- Provider adapters for AWS/GCP/Azure.
- Provider-specific network translation.
- Remote state/secrets handling.

## 14. Engineering Rules

- Keep changes scoped to the current phase.
- Prefer config/schema tests before backend mutation tests.
- Preserve user-authored backend modules unless intentionally refactoring.
- Every operation that mutates or inspects real state should create a run record.
- Do not hide backend logs; structure them and keep raw references available.
- Keep generated files under `.playground/`.
