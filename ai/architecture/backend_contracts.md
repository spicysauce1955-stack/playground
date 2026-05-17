# Backend Contracts

## 1. Purpose

Backend modules such as `tofu/` and `ansible/` remain visible and editable. The platform should rely on documented contracts instead of treating those directories as opaque magic.

Advanced users may customize backend modules, but customizations must preserve the contracts or update the adapter layer.

## 2. Local-Libvirt Provider Contract

### Inputs

The local-libvirt backend must accept enough input to create:

- named lab resources
- VM definitions
- network definitions
- image/source settings
- SSH key/user data
- provider-specific settings

Input format is not final. It may be generated variable files, JSON, HCL snippets, or adapter-specific rendered files.

### Required VM Capabilities

The backend must support:

- VM name
- role metadata
- vCPU
- memory
- disk size
- base image
- SSH key injection
- network attachments
- deterministic generated names

### Required Network Capabilities

The backend must support:

- named networks
- NAT network intent
- isolated/no-internet network intent
- routed network intent or a documented partial implementation
- DHCP or equivalent address assignment
- DNS behavior where provider supports it

### Outputs

The backend must output machine-readable state including:

- VM names
- VM IDs if available
- IP addresses
- network names
- network CIDRs
- provider resource identifiers

The output must be enough to generate Ansible inventory and TUI/CLI status.

### State Location

OpenTofu state location should be predictable and project-local. A likely target is under:

```text
.playground/state/rendered/tofu/
```

or another `.playground/` path, while hand-authored modules remain in `tofu/`.

## 3. Ansible Contract

### Inventory

Generated inventory must include:

- host name
- ansible host/IP
- SSH user
- VM role
- attached networks where useful
- tags where useful
- lab name

### Role Contracts

Required day-one roles:

- `docker`
- `router`

Existing/future roles must be idempotent and expose documented variables.

### Docker Role

Responsibilities:

- install Docker
- configure Docker service
- enable non-root Docker usage for configured user where intended
- install Compose support where required
- prepare node for Swarm where required

### Router Role

Responsibilities:

- enable IP forwarding
- configure routes/NAT/firewall behavior from generated variables
- support automatic defaults
- allow explicit overrides later

### Outputs

Ansible runs must return or write structured status where possible:

- changed/failed counts
- per-host status
- relevant facts
- Docker version/readiness
- routing readiness

## 4. Docker Contract

Docker management must support:

- host Docker engine
- VM Docker engine
- standalone containers
- Compose stacks
- Swarm initialization and join
- status inspection
- logs

For VM-hosted Docker, the platform may use SSH, Docker contexts, Ansible modules, or generated scripts. The exact mechanism is implementation-specific.

## 5. Artifact Contract

Artifacts must be resolvable through configured sources:

- remote URL
- local file
- local directory
- private registry
- mirror
- archive

When `offline: true`, backend adapters must not perform uncontrolled internet downloads.

## 6. Logging Contract

Backends must emit or be wrapped into structured events with:

- timestamp
- level
- lab
- run_id
- backend
- resource
- message
- raw output reference when needed

Raw backend logs may be retained, but the primary TUI/CLI should consume structured events and summaries.

## 7. Contract Validation

Doctor/check must verify:

- expected files/directories exist
- required backend commands are available
- required outputs are parsable
- required roles exist
- required variables can be generated
- state paths are writable

## 8. Known Current Gap

The existing repo has useful initial `tofu/` and `ansible/` modules, but they are closer to a direct IaC scaffold than a backend adapter contract. Future implementation should decide whether to:

- wrap the current modules with generated variables/inventory, or
- refactor them into more generic modules that accept resolved lab input.
