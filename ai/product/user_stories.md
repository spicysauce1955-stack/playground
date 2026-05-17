# User Stories: Configurable Infrastructure, Android, and Security Playground

## Product Direction

The playground is a local-first, backend-portable lab platform for creating and operating VMs, Docker workloads, virtual networks, and later Android/security workflows. The primary user is a technical operator who wants a highly configurable, extensible environment controlled mainly through YAML config trees and a TUI, with CLI and UI support over time.

The product should support both declarative desired-state labs and temporary runtime experimentation. Config defines defaults and reusable presets. The TUI/CLI can override live resources temporarily, and users may explicitly promote useful runtime changes back into config.

## Epic 1: Named Labs

### Story 1.1: Define Named Labs

As the playground operator, I want to define named labs so that each experiment has its own reproducible resources, defaults, networks, and state.

Acceptance criteria:
- Labs are defined in user-authored YAML under the config tree.
- A lab has a unique name, description, backend, resources, networks, defaults, tags, and optional offline mode.
- The system supports many lab definitions but only one active lab at a time in the first version.
- The active lab is tracked in project-local generated state under `.playground/`.
- Lab names are used to scope DNS names and generated resource identifiers.

### Story 1.2: Select One Active Lab

As the playground operator, I want to select the active lab from the TUI or CLI so that all operations target the intended environment.

Acceptance criteria:
- The TUI lists available lab definitions.
- The current active lab is clearly shown.
- Starting, stopping, planning, validating, and inspecting resources operate on the active lab by default.
- Switching labs is blocked or warned if another lab is currently running.

## Epic 2: YAML Config Tree

### Story 2.1: Use A Config Tree As The Main Interface

As the playground operator, I want a YAML config tree so that labs, roles, network profiles, defaults, commands, and templates are easy to inspect and modify.

Acceptance criteria:
- User-authored config is separate from generated runtime state.
- Suggested structure includes configurable equivalents of `config/labs`, `config/roles`, `config/networks`, `config/templates`, `config/defaults`, and `config/commands`.
- Built-in presets are YAML from day one, not hidden only in code.
- Config values can override default artifact sources, images, resource sizes, networks, and provider settings.

### Story 2.2: Validate Config Before Applying

As the playground operator, I want config validation with useful errors so that mistakes are caught before resources are changed.

Acceptance criteria:
- Validation runs before plan/apply/start operations.
- Validation reports file path, YAML key path, reason, severity, and suggested fix when possible.
- Invalid references are detected, including missing roles, networks, templates, command presets, artifact sources, and provider settings.
- Validation distinguishes warnings from blocking errors.

### Story 2.3: Support Runtime Overrides

As the playground operator, I want the TUI/CLI to temporarily override config so that I can experiment without editing files.

Acceptance criteria:
- Runtime overrides are temporary by default.
- The system clearly marks values that differ from config.
- Users can explicitly persist/promote selected runtime changes back into YAML.
- Runtime overrides are stored under `.playground/` and ignored by Git.

## Epic 3: Provider-Portability

### Story 3.1: Use Backend-Neutral Lab Concepts

As the playground operator, I want lab config to describe generic concepts so that local-libvirt works now and cloud providers can be added later.

Acceptance criteria:
- YAML uses generic resource concepts such as VM, network, route, container workload, role, artifact, and command.
- Labs declare a backend such as `local-libvirt`.
- Provider-specific settings live in provider override sections.
- Generic settings remain portable where possible.
- Backend adapters translate generic intent into OpenTofu, Ansible, Docker, or future cloud-specific behavior.

### Story 3.2: Support A Local Libvirt Backend First

As the playground operator, I want `local-libvirt` as the first backend so that I can run labs on my local tower.

Acceptance criteria:
- The backend can provision local VMs and virtual networks through the existing OpenTofu/libvirt direction.
- Backend-specific settings include libvirt URI, storage pool, image sources, network settings, and VM defaults.
- Backend behavior is documented as a contract, including required variables, outputs, inventory conventions, and Ansible role interfaces.

## Epic 4: VM Management

### Story 4.1: Create VMs From Role Presets

As the playground operator, I want to create VMs from named role presets so that common node types are easy to define but still configurable.

Acceptance criteria:
- Day-one VM roles include `generic-node`, `docker-host`, and `router`.
- Each role is defined in YAML with defaults.
- Per-VM config can override CPU, memory, disk, image, networks, tags, and provisioning behavior.
- Role presets remain editable by the user.

### Story 4.2: Provision Generic Nodes

As the playground operator, I want generic Ubuntu VMs so that I can run arbitrary infrastructure experiments.

Acceptance criteria:
- A `generic-node` VM can be created with default image, CPU, RAM, disk, SSH user, and network attachment.
- SSH access is configured automatically from configured keys.
- VM DNS names are scoped by lab.
- The VM appears in TUI status and operation logs.

### Story 4.3: Provision Docker Host VMs

As the playground operator, I want Docker-capable VMs so that container workloads can run inside managed guest nodes.

Acceptance criteria:
- A `docker-host` VM installs and configures Docker automatically.
- Docker setup is idempotent.
- The configured user can run Docker as intended.
- The system can target these hosts for standalone containers, Compose stacks, and Swarm.

### Story 4.4: Provision Router VMs

As the playground operator, I want router VMs so that multiple lab networks can be connected with controlled routing.

Acceptance criteria:
- A `router` VM can attach to multiple networks.
- Basic routing is generated automatically from attached networks.
- Routing behavior is configurable for advanced use.
- The router role can support NAT/routing/firewall behavior as provider capabilities mature.

## Epic 5: Virtual Networking

### Story 5.1: Define Named Virtual Networks

As the playground operator, I want named virtual networks so that resources can be grouped and isolated by network intent.

Acceptance criteria:
- Networks are first-class YAML objects.
- Day-one network intents include `nat`, `isolated`/no-internet, and `routed`.
- Resources can attach to one or more named networks.
- Network intent is generic and translated by the selected backend.

### Story 5.2: Use Lab-Scoped DNS Names

As the playground operator, I want lab-scoped DNS names so that resources can communicate predictably without manually tracking IPs.

Acceptance criteria:
- Resource names resolve within the active lab namespace.
- DNS names include lab scope to avoid conflicts across lab definitions.
- Generated names are deterministic unless explicitly overridden.
- DNS behavior is documented for each backend.

### Story 5.3: Support No-Internet And Routed Networks First

As the playground operator, I want no-internet and routed networks early so that I can model realistic isolated environments.

Acceptance criteria:
- A no-internet network prevents direct external connectivity by default.
- A routed network can be connected through a router VM or backend route configuration.
- Validation warns when a resource placement requires internet but is attached only to no-internet networks.

## Epic 6: Docker Workloads

### Story 6.1: Run Containers On Host Or VM

As the playground operator, I want containers to run either on the host or inside VMs so that I can choose the right execution location for each workload.

Acceptance criteria:
- Workload placement supports `host`, `vm`, and policy/default-driven placement.
- The scheduler chooses a target based on config/defaults unless explicitly pinned.
- Placement considers required location, network access, host capability, resource needs, and optional affinity/pinning.
- The system trusts the user and does not block risky placement solely based on security assumptions.

### Story 6.2: Support Docker Compose Day One

As the playground operator, I want Compose stacks to be managed by the playground so that multi-container services can be part of a lab.

Acceptance criteria:
- Compose stacks are definable from YAML or referenced Compose files.
- The playground decides placement from config/defaults unless pinned.
- Compose logs and status are visible in structured operation records.
- Compose services can be associated with lab networks and tags.

### Story 6.3: Support Docker Swarm Day One

As the playground operator, I want Swarm support so that clustered container workloads can be tested early.

Acceptance criteria:
- Swarm uses a hybrid model: automatic manager/worker assignment by default, explicit manager/worker roles when configured.
- Swarm can be initialized across eligible `docker-host` VMs.
- Swarm state and node roles are visible in the TUI.
- Validation catches invalid Swarm configs, such as no eligible Docker hosts.

## Epic 7: TUI And CLI Operations

### Story 7.1: Operate The Active Lab From A TUI

As the playground operator, I want a TUI operator console so that I can manage the active lab without manually chaining backend commands.

Acceptance criteria:
- The TUI can select active lab, validate config, show plan/diff, apply/start, stop, and inspect state.
- The TUI shows VMs, containers, networks, and backend operation status.
- The TUI can open SSH/session access for selected resources.
- The TUI can run ad-hoc commands on selected resources.

### Story 7.2: Run Saved Command Presets

As the playground operator, I want saved command presets so that common operational tasks are reusable.

Acceptance criteria:
- Command presets are YAML-defined.
- Presets can target one selected resource or groups selected by name, role, network, or tag.
- Presets can be run from TUI and CLI.
- Results are captured in structured operation logs.

### Story 7.3: Support CLI Automation

As the playground operator, I want CLI commands so that validation, planning, applying, stopping, and inspection can be scripted.

Acceptance criteria:
- CLI behavior maps to the same config, state, and operation model as the TUI.
- CLI commands support non-interactive mode where practical.
- CLI output can be human-readable and machine-readable.

## Epic 8: Structured State, Logs, And Runs

### Story 8.1: Store Generated State Under `.playground/`

As the playground operator, I want generated state to live inside the project so that the project is self-contained and easy to clean up.

Acceptance criteria:
- Generated runtime state, inventories, run history, logs, cache metadata, and artifacts live under project-local `.playground/`.
- `.playground/` is ignored by Git by default.
- User-authored YAML config remains outside `.playground/` and is intended to be committed.
- Hand-authored backend modules such as `tofu/` and `ansible/` remain normal project directories.

### Story 8.2: Create Operation Runs

As the playground operator, I want every meaningful operation to create a saved run record so that I can inspect what happened later.

Acceptance criteria:
- Apply/start/stop/configure/command/cache operations create a run ID.
- A run records lab, operation type, status, timestamps, backend tools, affected resources, summary, and logs.
- Logs are structured by lab, operation, resource, backend tool, severity, and timestamp.
- Runs are inspectable from TUI and CLI.

### Story 8.3: Control Retention

As the playground operator, I want retention controls so that logs and run history do not bloat the local OS.

Acceptance criteria:
- Retention supports count, age, and maximum disk size.
- Retention policies are configurable.
- Cleanup targets generated run/log/cache data and does not remove user-authored config.
- The system can report current `.playground/` disk usage.

## Epic 9: Resource Budgets

### Story 9.1: Warn On Resource Budget Pressure

As the playground operator, I want per-lab resource budgets so that a lab does not accidentally consume too much of my local tower.

Acceptance criteria:
- Budgets can include vCPU, RAM, disk, VM count, and container count.
- Budget checks run during validation/plan.
- Default behavior is permissive: warn but allow continuation.
- Strict mode can refuse to start/apply when the plan exceeds budget.

## Epic 10: Offline And Artifact Sources

### Story 10.1: Configure Artifact Sources

As the playground operator, I want all remote artifact sources to be configurable so that I can use defaults online or internal/local sources offline.

Acceptance criteria:
- Artifact sources include VM images, OpenTofu providers, Ansible collections, Docker images, package repositories, and future Android/Redroid images.
- Each artifact has a default remote source.
- Config can override sources to local files, local directories, private registries, internal mirrors, or archives.
- `offline: true` prevents uncontrolled internet downloads.

### Story 10.2: Prepare A Reusable Offline Cache

As the playground operator, I want an online prepare/cache workflow so that labs can later run offline.

Acceptance criteria:
- The cache can collect configured artifacts while online.
- The cache is reusable across labs in the same project.
- Multiple artifact versions/tags can coexist.
- Cache metadata records source, version/tag, checksum when available, and consuming labs.
- Labs can resolve artifacts from the cache when `offline: true`.

## Epic 11: Doctor And Readiness Checks

### Story 11.1: Check Host And Project Readiness

As the playground operator, I want a doctor/check command so that host, backend, config, resource, offline, and state issues are found before operations fail halfway.

Acceptance criteria:
- Doctor checks required binaries, virtualization/KVM/libvirt readiness, project structure, backend contracts, SSH keys, network readiness, offline artifacts, resource budgets, tool versions, and stale state.
- Doctor reports severity, affected area, and suggested fix.
- Doctor can offer safe project-local fixes.
- Doctor does not silently install system packages, alter host networking, modify firewall rules, or change libvirt settings.

## Epic 12: Future Android And Security Expansion

### Story 12.1: Leave Room For Android Device Labs

As the playground operator, I want the foundation to support Android/Redroid later so that Android traffic and device experiments can become lab presets.

Acceptance criteria:
- Config concepts do not assume all workloads are generic VMs or containers.
- Artifact sources can represent Android/Redroid images.
- VM roles can later include `android-host`.
- Network and logging models can support ADB access, app installation, and device traffic inspection.

### Story 12.2: Leave Room For Traffic Capture And Security Workflows

As the playground operator, I want networking and resource models to support security workflows later so that I can inspect communications and build attacker/victim labs.

Acceptance criteria:
- Networks, resources, and commands support optional tags.
- The model can later attach capture points to host, network, VM, container, or device scope.
- Artifacts can later store packet captures, reports, screenshots, and scan results.
- The platform does not hardcode trust restrictions but makes topology and placement visible.
