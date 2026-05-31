# MVP Scope

## Current Implementation Slice

The full MVP remains the target below. The immediate next slice is intentionally
smaller and read-only:

```text
playground validate
playground lab list
playground lab show <name>
```

This proves the config/model/validation layer before backend automation.

## MVP Goal

Build the smallest useful version of the playground platform that proves the core operating model:

- YAML config tree as the source of defaults and lab intent.
- One active named lab.
- Local-libvirt backend.
- VM, Docker, and network resources.
- Structured state and operation runs under `.playground/`.
- CLI-first operations that can later power the TUI.

The MVP should not try to complete Android, packet capture, cloud, or a full UI. It should make those future capabilities natural extensions.

## MVP User Outcome

The operator can define a generic infra lab in YAML, validate it, see a plan, apply it on a local libvirt host, inspect created VMs/networks/Docker readiness, run basic commands, and stop/destroy the lab with structured logs and state retained locally.

## MVP Resource Scope

### Included

- Named lab definitions.
- One active lab at a time.
- `local-libvirt` backend.
- YAML role presets:
  - `generic-node`
  - `docker-host`
  - `router`
- YAML network profiles:
  - `nat`
  - `isolated`
  - `routed`
- VM provisioning through existing/future OpenTofu backend contract.
- VM configuration through existing/future Ansible backend contract.
- Docker installation on `docker-host`.
- Basic standalone container placement model.
- Compose and Swarm requirements documented in schema, with implementation sliced after basic Docker readiness if needed.
- Project-local `.playground/` state.
- Operation run records and structured logs.
- Config validation.
- Doctor/check readiness checks.
- Offline artifact source model and cache metadata.

### Deferred But Designed For

- Redroid/Android device lifecycle.
- ADB automation.
- APK installation.
- Packet capture and `.pcap` artifacts.
- Security tool presets.
- Attacker/victim templates.
- Cloud providers.

  > Update (2026-05-31): a `cloud-digitalocean` backend has since been added
  > as a post-MVP backend; see `docs/architecture/cloud_digitalocean_design.md`.
- Full graphical UI.

## MVP Commands

The exact binary name is not final. The planning name is `playground`.

Required CLI commands:

```text
playground doctor
playground validate [--lab LAB]
playground lab list
playground lab select LAB
playground plan [--lab LAB]
playground apply [--lab LAB]
playground status [--lab LAB]
playground stop [--lab LAB]
playground destroy [--lab LAB]
playground run COMMAND_OR_PRESET [--target SELECTOR]
playground cache prepare [--lab LAB]
playground runs list
playground runs show RUN_ID
```

TUI MVP can be specified after CLI behavior is stable, but the CLI must expose the same operations needed by the TUI.

## MVP Config Tree

Initial suggested structure:

```text
config/
  defaults.yaml
  providers/
    local-libvirt.yaml
  artifacts/
    sources.yaml
  networks/
    nat.yaml
    isolated.yaml
    routed.yaml
  roles/
    generic-node.yaml
    docker-host.yaml
    router.yaml
  commands/
    check-docker.yaml
    ping-network.yaml
  labs/
    generic-infra.yaml
```

Generated state:

```text
.playground/
  state/
    active-lab.json
    inventory/
    rendered/
  runs/
    <run-id>/
      run.json
      summary.md
      logs/
  cache/
    artifacts/
    metadata/
  artifacts/
```

## MVP Acceptance Tests

### Config And Validation

- A valid `generic-infra` lab passes validation.
- A lab referencing a missing role fails validation with file/key/suggested fix.
- A lab exceeding resource budget warns by default.
- `offline: true` with missing local artifact source fails before apply.

### Lab Lifecycle

- User can select `generic-infra` as active lab.
- `plan` shows intended VMs, networks, generated inventory, and backend actions.
- `apply` creates or configures resources.
- `status` shows VM/network/Docker readiness.
- `stop` stops running workloads or VMs according to MVP behavior.
- `destroy` removes lab resources after confirmation.

### State And Logging

- Each operation creates a run under `.playground/runs`.
- Run record includes ID, lab, operation, timestamps, status, resources, backend tools, and summary.
- Logs can be filtered or grouped by operation/resource/backend/severity.
- `.playground/` is ignored by Git.

### Backend Contracts

- The local-libvirt backend can consume generated variables/config and produce VM IPs.
- The Ansible backend can consume generated inventory and configure Docker hosts.
- Backend contract validation catches missing required outputs or role interfaces.

## MVP Risks

- Swarm day-one support may expand MVP complexity.
- Router implementation can become deep if firewalling/routing is over-specified.
- Offline mode can become large if full package mirroring is required immediately.
- TUI can distract from core state/config correctness if started too early.

## MVP Recommendation

Implement MVP in slices:

1. Config tree, schema, validation, state layout.
2. Local-libvirt backend contract and plan rendering.
3. VM lifecycle for `generic-node`.
4. Docker-host configuration.
5. Network profiles and router role.
6. Operation runs/logging.
7. Container/Compose/Swarm workflows.
8. TUI wrapper over stable CLI operations.
