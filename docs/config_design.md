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
    local-vbox.yaml
    cloud-digitalocean.yaml
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

- missing `Defaults` — closed; emits `config.required.defaults_missing`
- workload placement target references — closed; emits
  `config.reference.unknown_workload_target` and matches `target_role` against
  the full `spec.extends` chain
- budget totals — closed; emits `config.budget.exceeded` (strict→error,
  permissive→warning)
- offline artifact availability — partially closed; emits
  `config.artifact.offline_missing` for `ArtifactSources.spec.vm_images`. Other
  artifact kinds listed in `requirements.md` §5.13 (Tofu providers, Ansible
  collections, Docker images, package repositories, mirrors, archives) are
  tracked for a later slice — workload-level Docker image references and Tofu
  provider references don't have lab-side intent to validate against yet.
- routing intent preservation — closed; `ResolvedVm.routing` carries the
  resolved value
- accurate source tracking when filenames differ from metadata names — closed;
  `LoadedConfig.sources[(kind, name)]` is populated from
  `DiscoveredFile.repo_relative_path`

## Provider Separation

Generic lab intent should stay backend-neutral. Provider-specific settings must
live under provider config or provider override sections.

The first backend was `local-libvirt`; `local-vbox` and `cloud-digitalocean`
have since been added, validating the design's backend-neutrality claim.
DigitalOcean settings (token env var, region, slug) live in the provider config
and lab override sections; the generic lab model was unchanged.

## Runtime Overrides

Future CLI/TUI runtime changes should be temporary by default and stored under
`.playground/`. Persisting a runtime change back to YAML should be explicit.
