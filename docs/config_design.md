# Config Design

This design is derived from `docs/product/requirements.md` and the current
`config/` tree.

## Goals

- YAML is the main user-authored interface.
- Presets are editable from day one.
- Generic lab intent stays separate from provider-specific settings.
- Defaults are useful and conservative.
- Validation produces actionable diagnostics.
- Runtime overrides are temporary unless explicitly persisted.

## Current Tree

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

## YAML Kinds

- `Defaults`: project defaults for backend, budget, VM defaults, network
  defaults, and retention.
- `ProviderConfig`: provider-specific settings such as libvirt URI, pool, VM
  machine settings, and backend capability flags.
- `ArtifactSources`: VM images, OpenTofu providers, Ansible collections, Docker
  images, remote sources, and local cache paths.
- `NetworkProfile`: reusable network intent such as `nat`, `isolated`, or
  `routed`.
- `VmRole`: reusable VM presets such as `generic-node`, `docker-host`, and
  `router`.
- `CommandPreset`: reusable operator commands with target selectors.
- `Lab`: named lab intent composed from the above resources.

## Resolution Rules

The resolver should:

1. Load `Defaults`.
2. Load the selected `Lab`.
3. Resolve role inheritance from root to leaf.
4. Apply VM-level overrides.
5. Resolve network profiles into concrete lab networks.
6. Resolve command names into command bodies.
7. Resolve artifact sources and local cache paths.
8. Produce `ResolvedLab`.

If a required source is missing, validation should report a diagnostic before
resolution is used for backend automation.

## Validation Rules

Validation must report file path, key path where available, severity, message,
and suggested fix where useful.

Near-term validation gaps:

- missing `Defaults`
- workload placement target references
- budget totals
- offline artifact availability
- routing intent preservation
- accurate source tracking when filenames differ from metadata names

## Provider Separation

Generic lab intent should stay backend-neutral. Provider-specific settings must
live under provider config or provider override sections.

The first backend is `local-libvirt`, but the config model should not make cloud
or other future providers impossible.

## Runtime Overrides

Future CLI/TUI runtime changes should be temporary by default and stored under
`.playground/`. Persisting a runtime change back to YAML should be explicit.
