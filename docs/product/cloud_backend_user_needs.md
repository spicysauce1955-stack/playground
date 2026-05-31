# Cloud Backend User Needs

Date: 2026-05-31

Perspective: the playground operator using a DigitalOcean account as the first
cloud target.

## What I Am Trying To Do

I want to run the same kinds of labs I already define locally, but on cloud VMs
when my local machine is unavailable, underpowered, or inconvenient. I do not
want a permanent cloud environment. I want short-lived lab compute that is easy
to create, inspect, suspend, resume when practical, and clean up.

I am comfortable with infrastructure tools, but I want the platform to remember
the boring details: provider setup, SSH access, cloud-init, tags, generated
inventory, and cost-risk warnings.

## Core Needs

- I need to use my DigitalOcean API token without committing it to Git.
- I need cloud resources to be visibly tied to a lab name so I can identify and
  clean them up in the DigitalOcean console.
- I need cheap defaults for experimentation, with the ability to choose a larger
  size for Docker-heavy labs.
- I need the platform to tell me estimated cost before it creates resources.
- I need running compute to be treated as expensive and optional, not as state
  that must sit idle.
- I need a clear difference between stopping expensive resources and deleting
  every trace of the lab.
- I need the existing Ansible provisioning flow to keep working after the VM is
  created.
- I need failures to leave enough generated files and logs to understand what
  happened and clean up manually if required.

## Desired Workflow

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

The default flow should be:

1. Validate local config and DigitalOcean readiness.
2. Show a plan with resource names, region, VM size, estimated hourly/monthly
   cost, public ingress, and cleanup behavior.
3. Create the VM and cheap supporting resources.
4. Wait for SSH/cloud-init.
5. Run the existing Ansible roles.
6. Record all generated state under `.playground/`.
7. Provide `status`, `suspend`, and `destroy` commands that are safe to repeat.

## Functional Requirements

### Provider Configuration

- Add a `cloud-digitalocean` provider config.
- Read the API token from an environment variable, defaulting to
  `DIGITALOCEAN_TOKEN`.
- Never store token values in YAML, generated Tofu files, logs, run summaries,
  or diagnostics.
- Support region, image, VM size, SSH key, tags, and firewall defaults.
- Allow lab-level overrides under `spec.providers.cloud-digitalocean`.

### Planning

- `playground plan` must show DigitalOcean resources that would be created.
- The plan must include estimated compute cost and any known persistent costs.
- The plan must identify which resources are considered expensive.
- The plan must warn when the chosen size is larger than the configured budget.
- The plan must show public network exposure, especially SSH ingress.

### Apply

- `playground apply` must create the cloud VM and required supporting resources.
- Created resources must be tagged/labeled with at least:
  - lab name
  - VM name
  - backend
  - project/tool owner marker
- The VM must receive cloud-init user data for SSH bootstrap.
- The backend must output SSH host, port, username, and VM identity in a shape
  the existing inventory renderer can consume.
- Ansible provisioning should run through the same event/log pipeline as local
  backends.

### Status

- `playground status` must report whether each cloud VM exists, is running, and
  has a reachable SSH endpoint.
- Status must distinguish local generated state from actual DigitalOcean state.
- Status must warn about orphaned tagged resources that are no longer in the
  resolved lab.

### Suspend

- `playground suspend` must remove or stop expensive resources while preserving
  the cheap state needed to understand or recreate the lab.
- For DigitalOcean, powered-off Droplets still cost money, so suspend should not
  merely power off the Droplet and call it cheap.
- The first DigitalOcean suspend implementation may destroy Droplets and keep
  only local generated state. Snapshot-based suspend should be explicit because
  snapshots have storage cost and may preserve sensitive data.
- Suspend must be idempotent and safe to run after a partial apply.

### Resume

- `playground resume` should recreate suspended compute from current config.
- Resume does not need to preserve VM disk changes unless snapshot-based suspend
  is explicitly enabled.
- The platform must clearly state whether resume is rebuilding from config or
  restoring from a snapshot.

### Destroy

- `playground destroy` must remove all DigitalOcean resources owned by the lab
  unless a retention policy explicitly keeps something.
- Destroy must be idempotent.
- Destroy must show any resources it could not remove and how to find them in
  the provider console.

### Doctor

- Doctor must check:
  - `.env` or shell has `DIGITALOCEAN_TOKEN`
  - token is not committed in Git-tracked files
  - `tofu` is installed
  - SSH public key exists
  - provider region and size are configured
  - generated state path is writable
- Doctor should avoid printing secrets.

## Non-Functional Requirements

- Cloud support must not weaken the local-libvirt path.
- Generated OpenTofu files and state must stay under `.playground/`.
- Auth should follow provider environment/profile idioms instead of committed
  credentials.
- The first implementation should favor a narrow working backend over a generic
  abstraction that tries to cover every cloud.
- Cleanup safety matters more than saving a few seconds.
- Cost visibility is part of correctness.

## DigitalOcean Defaults I Want

- Region: configurable, with a reasonable default such as `nyc3` or `fra1`.
- Smoke-test size: `s-1vcpu-1gb` or the current cheapest acceptable equivalent.
- Docker-heavy size: `s-2vcpu-4gb`.
- Image: Ubuntu 24.04 LTS.
- SSH: use my configured public key.
- Firewall: SSH only from a configured operator CIDR when available; otherwise
  warn if SSH is open broadly.
- Retention: keep local run logs; do not keep paid cloud resources unless I
  explicitly ask.

## Open Questions

- Should the default region optimize for lowest latency to the operator, lowest
  cost, or a fixed documented region?
- Should snapshot-based suspend be supported in the first slice, or should the
  first slice rebuild from config every time?
- How should monthly dollar budgets be modeled alongside existing vCPU/RAM/disk
  budgets?
- Should `destroy` require an extra confirmation when resources are tagged as
  manually retained?

