# PRD: DigitalOcean Cloud Backend

Date: 2026-05-31

Source input: `docs/product/cloud_backend_user_needs.md`

## Product Intent

Add a DigitalOcean cloud backend that lets the playground operator run existing
YAML-defined labs on short-lived cloud VMs while preserving the current
OpenTofu -> Ansible -> workload pipeline and the project's inspectable backend
model.

The feature is not a general cloud abstraction first. It is a narrow, working
`cloud-digitalocean` backend that proves cloud lifecycle, cost visibility,
credential hygiene, generated state layout, SSH inventory handoff, and cleanup
safety.

## Primary User

The primary user is the playground operator:

- Comfortable with Linux, SSH, Docker, OpenTofu, cloud consoles, and YAML.
- Wants to use cloud compute only when useful, not as always-on infrastructure.
- Accepts a small idle cost for metadata/logs/state, but does not want running
  compute or paid public resources left idle by accident.
- Wants warnings and visibility more than restrictive policy.

## Problem

The current platform is local-first. That is useful, but it limits experiments
when the local machine is unavailable, underpowered, or inconvenient to expose.

The operator has a DigitalOcean API token and wants to run generic VM/Docker
labs in the cloud with the same high-level workflow:

```text
validate -> plan -> apply -> status -> suspend/resume -> destroy
```

The platform must make cloud costs and cleanup behavior explicit. For
DigitalOcean specifically, powering off a Droplet does not make it free because
the allocated resource still bills. The backend must therefore treat Droplets
as expensive active resources and support deleting/recreating them when the lab
is suspended.

## Goals

- Add `cloud-digitalocean` as a supported backend.
- Keep provider-specific configuration under `config/providers/` and lab
  provider overrides.
- Read DigitalOcean credentials from environment, not committed config.
- Render all generated OpenTofu files, state, logs, and inventory under
  `.playground/`.
- Provision generic Ubuntu VMs on DigitalOcean.
- Reuse existing Ansible provisioning after SSH is available.
- Tag every cloud resource for lab ownership and cleanup.
- Provide clear cost estimates and warnings before mutating cloud resources.
- Support idempotent apply, status, suspend, resume, destroy, and reset/cleanup
  behavior.

## Non-Goals

- Do not implement every cloud provider in the first slice.
- Do not redesign generic lab intent around DigitalOcean-specific concepts.
- Do not commit or print API tokens.
- Do not promise Redroid/nested virtualization support on DigitalOcean.
- Do not hide generated OpenTofu behind opaque Python-only cloud API calls.
- Do not preserve arbitrary VM disk mutations during the first suspend/resume
  slice unless snapshot mode is explicitly implemented.
- Do not make a permanent always-on managed cloud environment.

## User Outcomes

### First Successful Cloud Smoke Test

The operator can run:

```text
playground doctor --backend cloud-digitalocean
playground validate
playground plan cloud-smoke
playground apply cloud-smoke
playground status cloud-smoke
playground suspend cloud-smoke
playground resume cloud-smoke
playground destroy cloud-smoke
```

Expected result:

- The plan shows one cheap DigitalOcean VM and estimated cost.
- Apply creates the Droplet, waits for cloud-init/SSH, and runs Ansible.
- Status shows actual DigitalOcean state and SSH reachability.
- Suspend removes expensive compute while preserving local generated state.
- Resume recreates compute from config.
- Destroy removes all lab-owned DigitalOcean resources.

### Cost-Aware Operation

The operator can distinguish:

- active paid compute
- low-cost or free supporting resources
- local generated state
- optional paid retained artifacts such as snapshots

The platform warns before creating or retaining resources that may continue to
bill after the command exits.

## Functional Requirements

### Provider Config

Add a provider config similar to:

```yaml
apiVersion: playground/v1
kind: ProviderConfig
metadata:
  name: cloud-digitalocean
spec:
  driver: cloud-digitalocean
  region: nyc3
  image: ubuntu-24-04-x64
  size: s-1vcpu-1gb
  token_env: DIGITALOCEAN_TOKEN
  ssh_public_key_path: ~/.ssh/id_rsa.pub
  firewall:
    ssh_cidrs: []
  tofu:
    provider_source: digitalocean/digitalocean
    provider_version: "~> 2.0"
    state_path: .playground/state/cloud-digitalocean
  capabilities:
    nested_virtualization: false
    privileged_containers: true
```

Requirements:

- `spec.driver` must equal `cloud-digitalocean`.
- `token_env` names an environment variable; it must never contain the token
  value itself.
- Labs may override region, image, size, SSH key, tags, and firewall settings
  under `spec.providers.cloud-digitalocean`.
- Defaults must be cheap and suitable for smoke tests.

### Credentials

- Load credentials from `DIGITALOCEAN_TOKEN` by default.
- Support `.env` loading only if the platform already has or adds a deliberate
  env-loading path; otherwise document shell export requirements.
- Never write the token to:
  - YAML config
  - generated OpenTofu files
  - run logs
  - diagnostics
  - terminal output
- Doctor must detect likely committed token values in tracked files and report
  an error.

### Planning

`playground plan <lab>` must show:

- provider: `cloud-digitalocean`
- region
- image
- size
- VM count
- generated resource names
- tags/labels
- SSH exposure
- estimated hourly and monthly compute cost where known
- resources that may continue billing after suspend/destroy decisions
- whether the plan fits configured resource and cost budgets

The plan must not require cloud mutation.

### Apply

`playground apply <lab>` must:

- validate config and backend support before mutation
- render generated OpenTofu files under `.playground/state/cloud-digitalocean/`
- run OpenTofu with the DigitalOcean provider
- create one Droplet per resolved VM in the first slice
- inject cloud-init/user-data for SSH bootstrap
- create or reference SSH keys safely
- create minimal firewall rules
- tag resources with lab/backend ownership metadata
- read outputs needed for inventory
- wait for SSH/cloud-init readiness
- run existing Ansible provisioning
- persist structured operation events and logs

### Status

`playground status <lab>` must report:

- whether each expected Droplet exists
- provider resource ID
- Droplet status
- public IPv4/IPv6 when present
- SSH reachability
- last known local run state
- orphaned lab-tagged resources
- likely cost-active resources

Status must distinguish "local state says this should exist" from "the provider
currently reports this exists".

### Suspend

`playground suspend <lab>` must:

- remove expensive running compute for the lab
- preserve local generated state and run history
- avoid pretending a powered-off DigitalOcean Droplet is free
- be safe after partial apply
- be idempotent

First-slice acceptable behavior:

- destroy Droplets and retain local state only
- resume later by recreating from current config

Deferred behavior:

- snapshot-based suspend with explicit cost warning and opt-in retention policy

### Resume

`playground resume <lab>` must:

- recreate missing compute from current config
- re-render or reuse generated OpenTofu inputs safely
- run readiness checks and Ansible as needed
- clearly state that disk-local VM changes are not preserved unless snapshot
  mode was used

### Destroy

`playground destroy <lab>` must:

- remove all DigitalOcean resources owned by the lab unless explicitly retained
- be idempotent
- report any failed deletions
- leave local run logs according to retention policy
- avoid deleting unrelated resources even if names are similar

### Reset / Cleanup

The backend should support a cleanup path for broken state:

- discover resources by ownership tags
- show the resources that would be removed
- remove only lab-owned resources
- record cleanup events

This may be implemented as `reset`, `destroy --force-orphans`, or a later
provider-specific cleanup command.

### Doctor

Doctor must check:

- `.env` or process environment contains the configured token env var
- token value is not tracked by Git
- `tofu` is installed
- required OpenTofu provider can initialize
- SSH public key exists and is readable
- generated state directory is writable
- provider region/size/image are configured
- firewall defaults are understood

Doctor must redact secrets in all output.

## Non-Functional Requirements

- **Inspectable:** generated files, logs, state, and inventories are easy to
  locate under `.playground/`.
- **Recoverable:** failed applies leave enough information to inspect and clean
  up resources.
- **Idempotent:** repeat apply/status/suspend/destroy should be safe.
- **Cost-aware:** cost-active resources are visible in plan/status and targeted
  by suspend.
- **Credential-safe:** no token values are committed, logged, or rendered.
- **Backend-portable:** generic lab concepts remain generic; DigitalOcean
  details stay in provider config and adapter code.
- **Local-safe:** local-libvirt and local-vbox behavior must not regress.

## MVP Scope

### Included

- One `cloud-digitalocean` provider config.
- One sample cloud smoke lab.
- One Droplet per VM.
- Public SSH access with warnings if broadly open.
- Static generated inventory from OpenTofu outputs.
- Existing Ansible provisioning path.
- Plan/apply/status/suspend/resume/destroy lifecycle.
- Cost estimates based on configured Droplet size metadata.
- Resource tagging for ownership and cleanup.

### Deferred

- Multi-provider abstraction.
- Private VPC-only access or bastion workflow.
- Load balancers.
- Managed databases or object storage.
- Snapshot-based suspend/resume.
- Redroid/nested virtualization.
- Dynamic Ansible inventory as the primary path.
- Cross-cloud cost optimizer.

## Acceptance Criteria

- A valid `cloud-smoke` lab validates without local-libvirt requirements.
- Missing `DIGITALOCEAN_TOKEN` produces an actionable doctor diagnostic.
- A token value in a tracked file produces an error diagnostic.
- `plan cloud-smoke` shows DigitalOcean region, size, image, resource names,
  SSH exposure, and cost estimate.
- `apply cloud-smoke` creates a Droplet and generated inventory under
  `.playground/`, then runs existing Ansible roles.
- `status cloud-smoke` reports actual provider state and SSH reachability.
- `suspend cloud-smoke` removes expensive Droplet compute and leaves local run
  history intact.
- `resume cloud-smoke` recreates the Droplet from config and reruns readiness
  and provisioning as needed.
- `destroy cloud-smoke` removes all lab-owned DigitalOcean resources.
- Re-running suspend or destroy after resources are already gone succeeds with
  a no-op or clear warning.
- No command prints the API token.

## Design Constraints

- Auth must use environment/profile idioms, not committed credentials.
- Generated state must stay under `.playground/`.
- OpenTofu and Ansible must remain visible and inspectable.
- Backend-specific settings must not pollute generic lab models unless a
  general concept is proven necessary.
- Every cloud resource must have deterministic names and ownership tags.
- Cleanup must prefer tag-based ownership checks over name-only matching.

## Risks

- DigitalOcean powered-off Droplets still cost money, so suspend semantics must
  be explicit.
- Public SSH defaults can create accidental exposure if operator CIDR is not
  configured.
- Failed OpenTofu state can diverge from provider reality and require careful
  reset behavior.
- Cost estimates can drift from provider pricing and must be treated as
  advisory unless refreshed from provider APIs.
- Snapshot-based suspend can preserve secrets and create ongoing storage costs.

## Open Questions

- What default region should the committed example use: `nyc3`, `fra1`, or an
  operator-local override?
- Should `.env` loading be built into the CLI, or should users source it before
  running commands?
- Should the first backend create SSH keys in DigitalOcean or require existing
  key fingerprints?
- Should monthly dollar budgets be added to the shared `Budget` model now or
  implemented as provider-specific warnings first?
- What should the confirmation model be for `destroy` and cleanup of retained
  resources?

