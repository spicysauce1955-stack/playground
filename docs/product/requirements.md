# Requirements: Configurable Lab Playground

## Current Interpretation

This document is the highest-signal source of product intent. It was restored
from the planning work that captured the operator's answers.

Current implementation choices:

- Python is now the control-layer implementation language.
- The next implementation slice is read-only CLI: validate, lab list, lab show.
- TUI, backend apply wrappers, event streaming, Compose execution, and Swarm
  execution are product requirements, but not the immediate next slice.

## 1. Product Summary

The playground is a local-first lab platform for creating, operating, and inspecting infrastructure experiments. It should let a technical user define labs as YAML config trees, run and inspect them through CLI/TUI workflows, and eventually expand into Android and security-specific scenarios.

The first platform target is a local tower running Ubuntu, KVM/libvirt, OpenTofu, Ansible, and Docker. The design must remain portable enough to add cloud backends later.

## 2. Primary User

The primary user is the project owner/operator:

- Comfortable with infrastructure, Linux, Docker, networking, Android, and security experimentation.
- Wants high configurability and extensibility.
- Trusts themselves to make risky lab choices.
- Wants guardrails, validation, and visibility, but not heavy-handed policy blocking.

## 3. Core Goals

- Define reproducible labs from YAML config trees.
- Operate one active lab at a time.
- Create and manage VMs, Docker workloads, and virtual networks first.
- Support host containers and VM-hosted containers.
- Support standalone Docker, Docker Compose, and Docker Swarm from day one.
- Make network topology first-class, including no-internet and routed networks.
- Keep state, logs, runs, cache, and artifacts project-local under `.playground/`.
- Support offline operation through configurable artifact sources and reusable local cache.
- Keep backend modules visible and editable while documenting the contracts the platform relies on.
- Leave room for later Android/Redroid, traffic capture, and security lab presets.

## 4. Non-Goals For First Planning Phase

- Do not treat the Python control layer as a reason to hide or rewrite the
  visible OpenTofu and Ansible backend modules prematurely.
- Do not build the TUI before the CLI/core operations are specified.
- Do not hardcode cloud support in the first implementation.
- Do not make Android/Redroid the first MVP slice, even though the architecture must support it later.
- Do not enforce security trust policies by default. The system should expose risks and topology, not block expert use.

## 5. Functional Requirements

### 5.1 Labs

- The system must support many named lab definitions.
- Only one lab must be active at a time in the first version.
- A lab must declare or inherit:
  - backend/provider
  - resources
  - networks
  - defaults
  - artifact sources
  - optional tags
  - optional offline mode
  - optional resource budget
- The active lab must be tracked in generated project-local state.

### 5.2 Config Tree

- The main user interface for reproducible behavior must be YAML files.
- Presets/templates must be YAML-editable from day one.
- Config must support defaults and overrides.
- Runtime changes from TUI/CLI must be temporary by default.
- Users must be able to explicitly persist selected runtime changes back into config.

### 5.3 Validation

- Config validation must run before plan/apply/start operations.
- Errors must identify file path, YAML path, reason, severity, and suggested fix when possible.
- Validation must detect missing references, invalid provider settings, unsupported network modes, invalid placements, and offline artifact gaps.
- Validation should warn, not block, for reproducibility concerns such as mutable Docker tags unless strict policy is enabled later.

### 5.4 Providers And Backends

- YAML must describe generic concepts first.
- `local-libvirt` must be the first provider/backend.
- Provider-specific settings must live in provider-specific sections.
- The local backend may use OpenTofu, Ansible, Docker, SSH, and libvirt internally.
- The platform must document required backend contracts.

### 5.5 VMs

- Day-one VM roles:
  - `generic-node`
  - `docker-host`
  - `router`
- VM roles must be YAML-defined presets.
- VM instances must allow overrides for image, CPU, memory, disk, networks, tags, SSH user, and provisioning.
- Docker hosts must install/configure Docker automatically.
- Router VMs must support automatic routing behavior with configurable overrides.

### 5.6 Networks

- Networks must be first-class YAML resources.
- Day-one network intents:
  - `nat`
  - `isolated` / no-internet
  - `routed`
- Resources must attach to one or more named networks.
- DNS names must be scoped by lab.
- Future packet capture and inspection must be possible without redesigning the model.

### 5.7 Docker Workloads

- Workloads must support host placement and VM placement.
- Placement must be policy/default-driven unless pinned.
- Placement inputs include:
  - execution location
  - network requirements
  - host capabilities
  - resources
  - optional affinity or explicit target
- Docker Compose must be supported.
- Docker Swarm must be supported with hybrid automatic/explicit manager-worker assignment.

### 5.8 TUI And CLI

- CLI and TUI must operate on the same config/state/operation model.
- First TUI capabilities:
  - select active lab
  - validate config
  - show plan/diff
  - apply/start lab
  - stop lab
  - view VMs, containers, networks
  - inspect status/logs
  - open SSH/session
  - run ad-hoc commands
  - run saved command presets
- CLI must support automation and machine-readable output.

### 5.9 Command Presets

- Command presets must be YAML-defined.
- Presets must support single-resource targets and group selectors.
- Selectors must support name, role, network, and optional tags.
- Preset outputs must be captured in operation logs.

### 5.10 State, Runs, Logs, Artifacts

- Generated platform data must live under `.playground/`.
- `.playground/` must be Git-ignored by default.
- Every meaningful operation must create an operation run record.
- Long-running operations must emit structured lifecycle events while they run.
- CLI, TUI, logs, status views, and future UI updates must consume the same operation event stream.
- Logs must be structured by:
  - lab
  - operation/run
  - resource
  - backend tool
  - severity
  - timestamp
- Retention must support count, age, and max disk size.

### 5.11 Reactive Operation Events

- The platform must use a publish/subscribe-style event model internally for long-running operations.
- Event producers include planners, provider adapters, Ansible runs, Docker operations, command presets, doctor checks, and cache operations.
- Event consumers include persistent log writers, run summary builders, CLI stream output, TUI views, status caches, and future UI/websocket integrations.
- Events must be persisted enough to reconstruct operation history after a process exits.
- The MVP may use an in-process event bus and append-only JSONL event logs.
- External brokers such as Redis, NATS, or MQTT are not required for MVP and should be introduced only when multi-process or remote subscribers need them.

### 5.12 Resource Budgets

- Labs must support configurable budgets.
- Budget dimensions should include vCPU, RAM, disk, VM count, and container count.
- Default enforcement is permissive: warn but continue.
- Strict mode may block operations when enabled.

### 5.13 Offline Mode And Artifacts

- Artifact sources must be configurable with remote defaults.
- Config must support local files, local directories, private registries, mirrors, and archives.
- `offline: true` must forbid uncontrolled internet downloads.
- The platform must support a reusable project-local cache across labs.
- The cache must support multiple versions/tags of the same artifact.

### 5.14 Doctor Checks

- A doctor/check command must verify readiness across:
  - required binaries
  - KVM/libvirt
  - project structure
  - backend contracts
  - SSH keys
  - networking readiness
  - offline artifacts
  - resource budget
  - tool versions
  - stale state
- Doctor may offer safe project-local fixes.
- Doctor must not silently alter host networking, firewall, system packages, libvirt settings, secrets, or credentials.

## 6. Non-Functional Requirements

- **Extensible:** New resource types, providers, roles, and presets should not require redesigning the core model.
- **Readable:** YAML config and generated operation summaries must be understandable by a human.
- **Inspectable:** Plans, logs, generated backend files, and state should be easy to locate.
- **Recoverable:** Failed runs should leave enough state and logs to continue or clean up.
- **Idempotent:** Re-running apply/configure should avoid unnecessary churn.
- **Portable:** Lab intent should be backend-neutral where possible.
- **Offline-capable:** No hardcoded internet dependency when offline mode is enabled.
- **Conservative defaults:** Default resource usage should suit a local tower with limited resources.
- **User-trusting:** Warn about risk, but do not block advanced usage without explicit strict mode.

## 7. Open Questions

- Exact TUI framework. `textual` is reserved as an optional dependency in
  `pyproject.toml` but no TUI work has started.
- How router behavior should be implemented in the first technical slice.
  The `router` role exists in config, but `ansible/roles/router/` does
  not, and routing is not exercised end-to-end.
- Whether the first Swarm implementation must be full production-grade
  cluster management or a minimal lab-oriented workflow.

Resolved questions (kept here as a record):

- Implementation language and packaging — Python 3.12+ via `hatchling`
  (`pyproject.toml`).
- Schema validation tooling — Pydantic 2 with `StrictModel`
  (`extra="forbid"`, `frozen=True`).
- Backend file generation — rendered from Python
  (`backend/local_libvirt/inventory.py`, `tfvars.py`); `tofu` and
  `ansible-playbook` are invoked as subprocess commands rather than
  re-implemented. See ADR-0002.
- Refactor vs wrap the existing `tofu/` / `ansible/` modules — wrap.
  See ADR-0002.
