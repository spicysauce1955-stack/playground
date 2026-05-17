# System Design

## 1. Design Intent

The playground should behave like a lab operating system for local infrastructure experiments. The user describes lab intent in YAML. The platform validates that intent, resolves defaults and presets, plans backend actions, applies changes through provider adapters, and records structured state/logs locally.

The system must not be tightly coupled to libvirt even though local-libvirt is the first backend. The core model should describe labs, resources, networks, artifacts, commands, and operations in backend-neutral terms.

## 2. Conceptual Architecture

```text
YAML config tree
  ↓
Config loader
  ↓
Schema validation + reference validation
  ↓
Resolved lab model
  ↓
Planner
  ↓
Operation runner
  ↓
Provider adapters
  ├── local-libvirt/OpenTofu adapter
  ├── Ansible configuration adapter
  ├── Docker host adapter
  └── future cloud/android/security adapters
  ↓
.playground state, runs, logs, cache, artifacts
```

## 3. Core Components

### 3.1 Config Loader

Responsibilities:

- Load YAML files from the config tree.
- Merge defaults, presets, lab-specific values, and runtime overrides.
- Track source locations for good validation errors.
- Produce an unresolved intermediate config model.

Required behavior:

- Keep user-authored config separate from generated state.
- Preserve enough source metadata to point errors to file/key.
- Support many labs but only one active lab at a time in the first version.

### 3.2 Validator

Responsibilities:

- Validate schema shape and primitive values.
- Validate references across files.
- Validate provider/backend compatibility.
- Validate placement requirements.
- Validate offline artifact availability when `offline: true`.
- Validate backend contracts before apply.

Output:

- Structured diagnostics with severity, file, YAML path, message, and suggested fix.

### 3.3 Resolver

Responsibilities:

- Convert config plus defaults into a resolved lab model.
- Expand role presets into concrete VM definitions.
- Expand network profiles into concrete network definitions.
- Resolve artifact sources and cache paths.
- Resolve placement defaults for workloads.
- Apply temporary runtime overrides when present.

Output:

- A backend-neutral resolved model suitable for planning.

### 3.4 Planner

Responsibilities:

- Compare resolved desired state with current `.playground/` state and backend-observed state.
- Produce an operation plan.
- Mark changes as create/update/delete/no-op/unknown.
- Estimate resource budget impact.
- Surface warnings before backend operations begin.

Output:

- Human-readable plan.
- Machine-readable plan for CLI/TUI.
- Optional rendered backend inputs.

### 3.5 Operation Runner

Responsibilities:

- Create an operation run ID.
- Execute plan steps.
- Publish structured lifecycle events while work is running.
- Stream structured logs derived from the event stream.
- Track per-resource status.
- Write summaries and final status.
- Handle failure and partial completion.

Required behavior:

- Every meaningful operation creates a run record.
- Run output is grouped by operation, backend, resource, severity, and timestamp.
- Failures leave enough information to retry, inspect, or clean up.

### 3.6 Event Bus

Responsibilities:

- Provide a publish/subscribe channel for operation events.
- Decouple long-running backend work from CLI/TUI rendering.
- Let multiple consumers observe the same operation without duplicating backend logic.
- Persist events through a log writer so run history survives process exit.

Initial event producers:

- Config validator.
- Planner.
- Operation runner.
- OpenTofu adapter.
- Ansible adapter.
- Docker adapter.
- Doctor checks.
- Command preset runner.
- Cache manager.

Initial event consumers:

- JSONL event log writer.
- Human-readable log writer.
- Run summary builder.
- CLI live output.
- TUI state updater.
- Status snapshot updater.

MVP implementation:

- Use an in-process event bus.
- Persist events to `.playground/runs/<run-id>/logs/events.jsonl`.
- Keep the event schema stable enough for CLI, TUI, and future UI use.
- Do not require Redis, NATS, MQTT, or another external broker in MVP.

Future implementation:

- Add a websocket or local API subscriber for graphical UI.
- Add an external broker only if operations need cross-process, remote, or distributed subscribers.

### 3.7 State Store

Default path:

```text
.playground/
```

Responsibilities:

- Track active lab.
- Track generated inventories/rendered backend files.
- Track run records.
- Track cache metadata.
- Track artifacts.
- Support retention cleanup.

Important distinction:

- `config/` is authored and committed.
- `.playground/` is generated and Git-ignored.
- `tofu/` and `ansible/` remain normal visible backend modules.

### 3.8 Provider Adapter Interface

Provider adapters translate generic lab intent into backend actions.

Initial adapter:

- `local-libvirt`

Future adapters:

- `aws`
- `gcp`
- `azure`
- possibly remote bare-metal or Proxmox-style providers

Adapter responsibilities:

- Declare capabilities.
- Validate provider-specific settings.
- Render or invoke backend tooling.
- Return observed state.
- Return resource outputs such as IPs and DNS names.
- Emit structured events.

### 3.9 Local-Libvirt Adapter

Responsibilities:

- Use OpenTofu/libvirt for VM and virtual network provisioning.
- Use generated variables or backend-specific rendered inputs.
- Consume OpenTofu outputs for VM IPs and other resource data.
- Keep OpenTofu state location predictable.
- Respect local artifact/image source settings.

Backend contract examples:

- Must support VM count or concrete VM definitions.
- Must output VM names and IPs in machine-readable form.
- Must support network creation based on desired network profiles.
- Must expose enough data to generate Ansible inventory.

### 3.10 Ansible Adapter

Responsibilities:

- Generate inventory from resolved/observed VM state.
- Run playbooks/roles for VM configuration.
- Configure Docker on `docker-host`.
- Configure routing behavior on `router`.
- Emit structured task events into operation runs.

Backend contract examples:

- Inventory must preserve lab, role, network, and tag metadata.
- Roles must be idempotent.
- Role variables must be documented.

### 3.11 Docker Adapter

Responsibilities:

- Manage host Docker workloads.
- Manage VM Docker workloads via SSH/remote context/Ansible as chosen later.
- Support standalone containers, Compose, and Swarm.
- Report workload status.
- Capture logs.

Placement model:

- Config expresses desired location and constraints.
- Resolver/planner chooses a target unless pinned.
- User can override placement explicitly.

## 4. Data Model

### Lab

Fields:

- name
- description
- backend
- defaults
- providers
- networks
- vms
- workloads
- commands
- artifacts
- budget
- offline
- tags

### VM

Fields (resolved form; see
`ai/architecture/shared_contracts.md §3` for the authoritative shape):

- name
- role
- image
- vcpu
- memory_mb
- disk_gb
- networks
- ssh
- provisioners
- tags
- provider_overrides

User-authored YAML nests cpu/memory/disk under `resources:` for
ergonomics; the resolver flattens them onto `ResolvedVm`.

### Network

Fields:

- name
- mode/intent
- cidr
- dns
- routes
- internet_access
- provider_overrides
- tags

### Workload

Fields:

- name
- type: container, compose, swarm
- image or compose file
- placement
- networks
- ports
- volumes
- environment
- resources
- tags

### Command Preset

Fields:

- name
- description
- target selector
- command/script
- working directory
- environment
- timeout
- privilege/escalation behavior

### Operation Run

Fields:

- run_id
- lab
- operation
- status
- start_time
- end_time
- backend tools
- affected resources
- diagnostics
- summary path
- logs path

### Operation Event

Fields:

- event_id
- run_id
- lab
- timestamp
- level
- event_type
- producer
- backend
- resource_ref
- phase
- message
- progress
- payload
- raw_output_ref

Event type examples:

- `operation.started`
- `operation.completed`
- `operation.failed`
- `plan.step.created`
- `backend.command.started`
- `backend.command.output`
- `backend.command.completed`
- `resource.status.changed`
- `diagnostic.emitted`
- `progress.updated`

## 5. State And Directory Layout

Recommended generated layout:

```text
.playground/
  state/
    active-lab.json
    inventory/
    rendered/
    observed/
  runs/
    <run-id>/
      run.json
      summary.md
      logs/
        events.jsonl
        human.log
  cache/
    artifacts/
    metadata/
  artifacts/
```

Config and backend modules:

```text
config/
  labs/
  roles/
  networks/
  providers/
  artifacts/
  commands/

tofu/
ansible/
```

## 6. Operation Lifecycle

### Validate

1. Load config.
2. Validate schema.
3. Resolve references.
4. Check backend compatibility.
5. Check artifact availability if offline.
6. Report diagnostics.

### Plan

1. Validate.
2. Resolve lab model.
3. Load generated/current state.
4. Query backend state when needed.
5. Compute planned actions.
6. Estimate resource budget.
7. Render backend inputs where useful.

### Apply

1. Validate.
2. Plan.
3. Create operation run.
4. Start event bus for the run.
5. Start persistent event subscribers.
6. Execute provider steps.
7. Execute configuration steps.
8. Update status snapshots from events.
9. Update state.
10. Write summary.

### Stop/Destroy

1. Load active lab and state.
2. Confirm scope.
3. Create operation run.
4. Stop or remove managed resources.
5. Update state and summary.

## 7. Error Handling

Error categories:

- Config/schema errors.
- Reference errors.
- Provider capability errors.
- Host readiness errors.
- Backend command failures.
- Resource timeout errors.
- Offline artifact errors.
- State drift errors.

Required behavior:

- Prefer early validation before backend mutation.
- Keep backend raw logs accessible.
- Surface concise summaries in CLI/TUI.
- Preserve failed run state.

## 8. Extensibility Points

- New provider adapters.
- New VM roles.
- New network profiles.
- New workload types.
- New artifact source types.
- New command preset types.
- New future resource category for Android devices.
- New capture/inspection artifacts.

## 9. Key Design Decisions

- YAML config tree is the primary product interface.
- TUI/CLI runtime overrides are temporary unless explicitly persisted.
- One active lab at a time for first version.
- `.playground/` stores generated state/history/cache/artifacts.
- Backend modules remain visible directories.
- Local-libvirt is first backend.
- Cloud portability is designed into schema, not implemented first.
- Offline mode is explicit and strict when enabled.
