# Tech Stack

## 1. Current Stack

The repository currently uses:

- Ubuntu host
- KVM/libvirt
- OpenTofu
- dmacvicar/libvirt provider
- Cloud-init
- Ansible
- Docker CE inside guest VMs
- Redroid direction for future Android containers

This remains a good foundation for the first local backend.

## 2. Planned Platform Layers

### User Interface Layer

Primary:

- YAML config tree
- CLI
- TUI

Later:

- UI/dashboard

### Core Platform Layer

Responsibilities:

- config loading
- validation
- default resolution
- planning
- state/run/log management
- provider adapter orchestration
- command preset execution

Implementation language is not final.

### Backend Layer

Initial:

- OpenTofu for local VM/network provisioning
- Ansible for guest configuration
- Docker CLI/API for host and VM workloads
- SSH for command/session execution

Future:

- Cloud providers
- Android/Redroid lifecycle
- traffic capture integrations

## 3. Implementation Language Options

### Option A: Python

Pros:

- Strong YAML, schema, subprocess, SSH, Ansible ecosystem.
- Easy to prototype.
- Good for CLI tools.
- Mature libraries for JSON Schema, Pydantic, Rich/Textual.
- Works naturally with Ansible.

Cons:

- Packaging/distribution can get messy if not managed carefully.
- Long-running TUI and concurrent operations need disciplined structure.

Candidate libraries:

- Typer or Click for CLI.
- Textual for TUI.
- Pydantic for typed models.
- ruamel.yaml or PyYAML for YAML.
- jsonschema if using JSON Schema directly.
- Rich for terminal output.
- asyncio queues or an in-process pub/sub helper for operation events.

### Option B: Go

Pros:

- Single binary distribution.
- Strong concurrency and process handling.
- Good for CLI/TUI tools.
- Easier deployment on Linux hosts.

Cons:

- Less natural Ansible integration.
- YAML/schema ergonomics can be more verbose.
- TUI development can be more work.

Candidate libraries:

- Cobra for CLI.
- Bubble Tea/Lip Gloss for TUI.
- Viper or direct YAML parsing.
- channels for in-process event fanout.

### Option C: Rust

Pros:

- Strong correctness and distribution story.
- Good CLI performance.

Cons:

- Slower iteration.
- More complexity than currently needed.
- TUI and backend orchestration may take longer.

## 4. Recommended Direction

Use Python for the first platform implementation unless distribution becomes the main concern.

Rationale:

- Fastest path to robust config/schema/validation.
- Natural fit with Ansible and YAML-heavy workflows.
- Textual can deliver a capable TUI later.
- Typer/Rich can provide good CLI UX quickly.

Keep backend contracts language-agnostic so Go/Rust remains possible later.

## 5. Suggested Python Stack

```text
Python 3.12+
Typer        CLI
Rich         terminal rendering
Textual      TUI
Pydantic     typed config/runtime models
ruamel.yaml  YAML parsing with source/comment preservation
jsonschema   optional exported schema validation
pytest       tests
```

Operational dependencies:

```text
OpenTofu
Ansible
libvirt/virsh
Docker
SSH
```

Eventing:

```text
MVP: in-process pub/sub + append-only JSONL event log
Later: local API/websocket for UI
Optional future: Redis, NATS, or MQTT only if remote/multi-process subscribers require it
```

## 6. TUI Stack Recommendation

Use Textual if Python is chosen.

TUI design should be built after CLI/core operations stabilize. It should call the same internal operation APIs as CLI commands, not duplicate backend logic.

Initial TUI views:

- Lab selector.
- Active lab dashboard.
- Plan view.
- Resource tree.
- Run/log viewer.
- Command preset launcher.
- Doctor/diagnostics view.

## 7. Testing Stack

### Unit Tests

- Config loading.
- Schema validation.
- Default resolution.
- Placement decisions.
- Artifact source resolution.
- Run/log record creation.

### Integration Tests

- CLI commands on sample config.
- Rendered backend inputs.
- Ansible inventory generation.
- Doctor checks with mocked tools.

### System Tests

- Optional real local-libvirt apply in a controlled environment.
- Docker-host configuration smoke test.
- Offline cache resolution test.

## 8. Documentation Stack

Use Markdown docs in `ai/` for planning and `docs/` later for user-facing documentation.

Planning docs should remain split by purpose:

- product requirements
- MVP scope
- user stories
- system design
- backend contracts
- implementation plan
- task breakdown
- QA plan

## 9. Versioning

Config objects should include:

```yaml
apiVersion: playground/v1
kind: ...
```

This enables future schema migration without breaking old labs silently.
