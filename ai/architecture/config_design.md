# Config Design

## 1. Goals

The config system should make the playground easy to start but deeply configurable. Users should be able to define labs declaratively, reuse role/network/workload presets, override defaults, and keep provider-specific settings separate from generic lab intent.

## 2. Principles

- YAML is the main user-authored format.
- Presets are YAML-editable from day one.
- Generic lab intent stays separate from provider-specific details.
- Defaults should be useful and conservative.
- Validation must produce actionable errors.
- Runtime overrides are temporary unless explicitly persisted.

## 3. Proposed Config Tree

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

This is a starting layout, not a permanent constraint.

## 4. Example Lab

```yaml
apiVersion: playground/v1
kind: Lab
metadata:
  name: generic-infra
  description: Generic VM, Docker, and network playground
  tags: [infra, local]

spec:
  backend: local-libvirt
  offline: false

  budget:
    mode: permissive
    max_vcpu: 12
    max_memory_mb: 24576
    max_disk_gb: 250
    max_vms: 8
    max_containers: 30

  networks:
    - name: edge
      profile: nat
      cidr: 10.20.10.0/24
    - name: lab-private
      profile: isolated
      cidr: 10.20.20.0/24
    - name: routed-a
      profile: routed
      cidr: 10.20.30.0/24

  vms:
    - name: node1
      role: generic-node
      networks: [lab-private]
    - name: docker1
      role: docker-host
      networks: [edge, lab-private]
      resources:
        vcpu: 2
        memory_mb: 4096
        disk_gb: 40
    - name: router1
      role: router
      networks: [edge, lab-private, routed-a]

  workloads:
    - name: demo-compose
      type: compose
      source: ./compose/demo.yaml
      placement:
        target_role: docker-host
      networks: [lab-private]

  commands:
    enabled:
      - check-docker
      - ping-network

  providers:
    local-libvirt:
      uri: qemu:///system
      pool: default
```

## 5. Example Role Presets

### `generic-node`

```yaml
apiVersion: playground/v1
kind: VmRole
metadata:
  name: generic-node

spec:
  image: ubuntu-noble
  resources:
    vcpu: 1
    memory_mb: 2048
    disk_gb: 20
  ssh:
    user: ubuntu
  provisioners: []
```

### `docker-host`

```yaml
apiVersion: playground/v1
kind: VmRole
metadata:
  name: docker-host

spec:
  extends: generic-node
  resources:
    vcpu: 2
    memory_mb: 4096
    disk_gb: 40
  capabilities:
    docker: true
    compose: true
    swarm: true
  provisioners:
    - ansible_role: docker
```

### `router`

```yaml
apiVersion: playground/v1
kind: VmRole
metadata:
  name: router

spec:
  extends: generic-node
  capabilities:
    routing: true
  provisioners:
    - ansible_role: router
  routing:
    mode: automatic
    allow_overrides: true
```

## 6. Example Network Profiles

### `nat`

```yaml
apiVersion: playground/v1
kind: NetworkProfile
metadata:
  name: nat

spec:
  intent: nat
  internet_access: true
  dns:
    enabled: true
```

### `isolated`

```yaml
apiVersion: playground/v1
kind: NetworkProfile
metadata:
  name: isolated

spec:
  intent: isolated
  internet_access: false
  dns:
    enabled: true
```

### `routed`

```yaml
apiVersion: playground/v1
kind: NetworkProfile
metadata:
  name: routed

spec:
  intent: routed
  internet_access: configurable
  dns:
    enabled: true
```

## 7. Example Artifact Sources

```yaml
apiVersion: playground/v1
kind: ArtifactSources

spec:
  defaults:
    offline: false

  vm_images:
    ubuntu-noble:
      type: qcow2
      version: "24.04"
      default_source: https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
      local_path: .playground/cache/artifacts/vm-images/ubuntu-noble/24.04/noble.qcow2

  tofu_providers:
    dmacvicar-libvirt:
      version: "~> 0.7.1"
      default_source: registry.opentofu.org/dmacvicar/libvirt

  ansible_collections:
    community-docker:
      version: "*"
      default_source: galaxy.ansible.com/community/docker

  docker_images:
    nginx:
      image: nginx:latest
      registry: docker.io
      local_archive: .playground/cache/artifacts/docker/nginx/latest/image.tar
```

## 8. Runtime Overrides

Runtime override examples:

- Increase VM memory from TUI for this run only.
- Attach a temporary network.
- Run a temporary container.
- Override workload placement.

Rules:

- Runtime overrides are stored under `.playground/state`.
- They are shown clearly in plan/status.
- They do not modify YAML unless explicitly promoted.
- Promotion should write the smallest useful YAML change.

## 9. Schema Strategy

Schema should support:

- `apiVersion`
- `kind`
- `metadata.name`
- `metadata.description`
- `metadata.tags`
- `spec`

Recommended object kinds:

- `Lab`
- `VmRole`
- `NetworkProfile`
- `ProviderConfig`
- `ArtifactSources`
- `CommandPreset`
- `Defaults`

Validation should happen in layers:

1. YAML syntax.
2. Schema shape.
3. Object identity and uniqueness.
4. Cross-reference resolution.
5. Provider compatibility.
6. Offline/source availability.
7. Budget and placement checks.

## 10. Open Design Questions

- Whether to use JSON Schema directly or a typed model that can export schemas.
- Whether labs can inline role/network definitions or must reference presets.
- Whether provider-specific overrides should allow arbitrary keys or only schema-known keys.
- How to represent generated DNS names exactly.
- How to represent Swarm topology without overcomplicating basic Docker use.
