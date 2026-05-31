# Cloud VM Backend Research

Date: 2026-05-31

Purpose: durable research notes for adding an option to provision lab VMs and
cloud resources from Playground while preserving the current visible
OpenTofu -> Ansible -> workload pipeline.

## Current Repo Fit

The repo is already shaped for this feature:

- `ResolvedLab.backend` selects a backend adapter.
- `ProviderConfig.spec` and lab `spec.providers.<backend>` are open maps, so
  provider-specific cloud settings can live in YAML without changing generic lab
  intent first.
- `local-libvirt` and `local-vbox` prove the adapter contract: provision VMs,
  discover SSH endpoints, render generated inventory under `.playground/`, then
  run the existing Ansible roles.
- Cloud support should keep authored YAML under `config/`, generated OpenTofu
  inputs/state/inventory under `.playground/`, and secrets outside the repo.

Likely first implementation shape:

1. Add `config/providers/cloud-<name>.yaml`.
2. Add `src/playground/backend/cloud_<name>/`.
3. Render backend-specific OpenTofu files or generated `.tfvars.json` under
   `.playground/state/<backend>/<lab>/`.
4. Run `tofu init/plan/apply/destroy` from generated state, not committed
   provider secrets.
5. Read `tofu output -json` and reuse the existing SSH wait + Ansible inventory
   path.

## Tooling And Idioms

### OpenTofu

OpenTofu is still the best fit for provisioning because the project already
uses it and ADR-0002 requires visible backend modules. Cloud backends should use
provider modules/resources rather than introducing a second IaC system.

Current idioms to preserve:

- Declare provider requirements explicitly. OpenTofu provider requirements use
  a local name, source address, and version constraint.
- Keep provider authentication out of HCL files. Prefer CLI profiles, standard
  environment variables, or workload identity/OIDC in CI.
- Keep state local under `.playground/` for single-operator labs unless a
  provider config explicitly opts into remote state.
- If remote state is added, use backend partial configuration and avoid storing
  credentials in generated files. OpenTofu's S3 backend now supports native S3
  locking with `use_lockfile = true`; DynamoDB locking is still supported.

Useful sources:

- Provider requirements:
  https://opentofu.org/docs/language/providers/requirements/
- Provider configuration:
  https://opentofu.org/docs/language/providers/configuration/
- State storage and locking:
  https://opentofu.org/docs/language/state/backends/
- S3 backend, native lock files, versioning, auth guidance:
  https://opentofu.org/docs/language/settings/backends/s3/

### Ansible Inventory

The first cloud adapter should probably keep rendering static inventory from
OpenTofu outputs, matching `local-libvirt`, because this produces deterministic
per-run artifacts and avoids requiring cloud API credentials for Ansible.

Dynamic inventory remains useful for later `status`, imported resources, or
adopting existing fleets:

- AWS EC2: `amazon.aws.aws_ec2`; current docs list `amazon.aws` collection
  version 10.3.1, requires boto3/botocore, supports profiles, regions, filters,
  keyed groups, and tag-based grouping.
- Azure: `azure.azcollection.azure_rm`; current docs list version 3.18.0 and
  support CLI auth, env/service-principal auth, caching, host expressions, and
  resource group filters.
- Hetzner: `hetzner.hcloud.hcloud`; current docs list version 6.9.0 and require
  a YAML file ending in `hcloud.yml` or `hcloud.yaml`.
- DigitalOcean: `digitalocean.cloud`; DigitalOcean documents this as the newer
  collection based on `pydo` and includes one inventory plugin.

Sources:

- AWS EC2 inventory:
  https://docs.ansible.com/projects/ansible/latest/collections/amazon/aws/aws_ec2_inventory.html
- Azure inventory:
  https://docs.ansible.com/projects/ansible/latest/collections/azure/azcollection/azure_rm_inventory.html
- Hetzner inventory:
  https://docs.ansible.com/projects/ansible/latest/collections/hetzner/hcloud/hcloud_inventory.html
- DigitalOcean Ansible collection:
  https://docs.digitalocean.com/reference/ansible/reference/

### cloud-init

Keep cloud-init as the guest bootstrap layer. It is portable across EC2, Azure,
DigitalOcean, GCE, OpenStack-style clouds, NoCloud, ConfigDrive, and many VPS
providers. The platform should render one cloud-config intent and let each
provider adapter map it into the provider's `user_data` field or equivalent.

Source:

- cloud-init datasources:
  https://docs.cloud-init.io/en/latest/reference/datasources.html

## Provider Comparison

Prices below are public on-demand/list prices found on 2026-05-31. Treat them
as planning estimates only; always re-check before implementing a default.
They usually exclude tax, block storage beyond included disk, snapshots,
reserved/static IPs, load balancers, NAT gateways, monitoring, and internet
egress unless stated otherwise.

| Provider | Good first use | Example VM | Approx compute price | Notes |
| --- | --- | --- | --- | --- |
| Hetzner Cloud | Cheapest generic Linux/Docker labs | CPX22, 2 vCPU, 4 GB | Germany/Finland: EUR 7.99/mo or USD 9.49/mo after 2026-04-01 price change | Strong price/perf and simple API. U.S. CPX21 is USD 13.99/mo after the 2026 adjustment. Not the safest Redroid/nested-virt target unless explicitly verified. |
| DigitalOcean | Simple developer UX, predictable billing | Basic 2 vCPU, 4 GB, 80 GB SSD | USD 0.03571/hr, USD 24/mo cap | Good default if simplicity and official docs matter more than cheapest cost. Per-second billing from 2026-01-01 with 60-second/$0.01 minimum. |
| AWS EC2 | Enterprise/cloud-native, IAM, VPC, broad regions | t3.medium, 2 vCPU, 4 GiB | USD 0.0418/hr in us-east-1; about USD 30.51/mo compute | Add EBS, public IPv4, egress, and NAT costs. Cheapest T-family is burstable; Redroid/nested virt should use supported C8i/M8i/R8i or metal, not t3. |
| Google Compute Engine | Good nested-KVM story, labels/IAM, global VPC | e2-standard-2, 2 vCPU, 8 GB | Public pricing examples around USD 0.067/hr in us-central1; about USD 49/mo compute | E2 is cheap but GCP nested virtualization docs explicitly exclude E2. Use eligible Intel-backed machine series for nested KVM. |
| Azure VM | Enterprise identity/networking, Windows/Linux, nested virt on some series | Standard_B2s, 2 vCPU, 4 GiB | Third-party indexed prices show roughly USD 0.0416/hr in some East US regions; verify in Azure calculator/API | Stopped-but-allocated VMs still bill for cores; only deallocated stops compute billing. Dv3 docs mark nested virtualization supported. |

Pricing sources:

- Hetzner 2026 price adjustment:
  https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/
- DigitalOcean Droplet pricing:
  https://www.digitalocean.com/pricing/droplets
- AWS T3 pricing:
  https://aws.amazon.com/ec2/instance-types/t3/
- AWS EC2 on-demand pricing and extra cost notes:
  https://aws.amazon.com/ec2/pricing/on-demand/
- GCP E2 specs and disk caveat:
  https://docs.cloud.google.com/compute/docs/general-purpose-machines
- GCP pricing:
  https://cloud.google.com/products/compute/pricing
- Azure VM pricing FAQ:
  https://azure.microsoft.com/en-us/pricing/details/virtual-machines/windows/
- Azure B2s price index used only as a planning estimate:
  https://cloudprice.net/vm/Standard_B2s

## Redroid And Nested Virtualization

For generic Docker workloads, cheap VPS instances are enough. For Redroid and
other Android/nested-virt work, make this an explicit capability rather than a
generic cloud default.

Current findings:

- AWS announced nested virtualization on virtual EC2 instances on 2026-02-16.
  It is available in commercial regions on C8i, M8i, and R8i instances. This is
  new enough that implementation should verify `/dev/kvm`, kernel modules, and
  Redroid binderfs behavior before advertising support.
- GCP supports nested virtualization with Linux KVM as the L1 hypervisor, but
  its docs exclude E2, memory-optimized VMs, AMD/Arm-powered VMs, and H4D VMs.
  The docs also warn about 10% or greater performance loss.
- Azure Dv3 docs mark nested virtualization as supported.
- Hetzner dedicated servers are a better candidate than Hetzner Cloud for
  guaranteed virtualization-extension availability. Treat Hetzner Cloud
  nested-virt as unverified until a doctor check proves `/dev/kvm`.

Sources:

- AWS nested virtualization announcement:
  https://aws.amazon.com/about-aws/whats-new/2026/02/amazon-ec2-nested-virtualization-on-virtual/
- GCP nested virtualization:
  https://docs.cloud.google.com/compute/docs/instances/nested-virtualization/overview
- Azure Dv3 size series:
  https://learn.microsoft.com/en-in/azure/virtual-machines/sizes/general-purpose/dv3-series
- Existing repo note:
  `docs/architecture/nested_virtualization.md`

## Recommended First Backend

For a practical first slice, implement one generic cloud VM backend before
trying to cover every provider.

Recommended order:

1. `cloud-digitalocean` or `cloud-hetzner` for generic Docker/Linux labs.
   - DigitalOcean has clearer official pricing/docs and predictable developer
     UX.
   - Hetzner is much cheaper, especially in Europe, but pricing changed on
     2026-04-01 and U.S. options differ from Europe.
2. Add `cloud-aws` or `cloud-gcp` only when IAM/VPC/nested-virt behavior is a
   real requirement.
3. Treat Redroid-on-cloud as a separate capability-gated feature. It should not
   silently run on the cheapest generic cloud VM.

My bias for this repo: start with `cloud-digitalocean` if the goal is a stable
MVP with simple docs; start with `cloud-hetzner` if the goal is lowest-cost
ephemeral labs and European regions are acceptable.

## Config Shape Sketch

Provider config:

```yaml
apiVersion: playground/v1
kind: ProviderConfig
metadata:
  name: cloud-digitalocean
spec:
  driver: cloud-digitalocean
  region: nyc3
  size: s-2vcpu-4gb
  image: ubuntu-24-04-x64
  ssh_key_fingerprints: []
  token_env: DIGITALOCEAN_TOKEN
  network:
    create_vpc: true
  tofu:
    provider_source: digitalocean/digitalocean
    provider_version: "~> 2.0"
    state_path: .playground/state/cloud-digitalocean
  capabilities:
    nested_virtualization: false
    privileged_containers: true
```

Lab override:

```yaml
spec:
  backend: cloud-digitalocean
  providers:
    cloud-digitalocean:
      region: nyc3
      size: s-2vcpu-4gb
```

Do not commit API tokens, SSH private keys, or generated state. Provider configs
should name environment variables or credential profiles, not secret values.

## Validation And Doctor Gaps To Close

Before a mutating cloud adapter:

- Validate `spec.backend` has both `ProviderConfig` and implemented adapter.
- Validate cloud region/size/image fields are present for the chosen backend.
- Validate lab budget estimates can represent money, not only vCPU/RAM/disk.
- Add doctor checks for required tools: `tofu`, provider plugin availability
  after `tofu init`, cloud CLI/profile/token presence if the backend uses it.
- Add a no-secrets diagnostic for obvious token/private-key values in YAML.
- Add provider-specific capability checks:
  - `privileged_containers`
  - `nested_virtualization`
  - public IPv4 required or IPv6-only supported
  - cloud-init/user-data support
- Add destroy/reset safeguards for cloud resources, because failed cleanup costs
  money.

## Implementation Notes

- Reuse the existing Ansible roles first. Avoid dynamic inventory until it
  solves an actual status/import problem.
- Tag/label every cloud resource with lab name, VM name, backend, and owner
  fields. This is critical for reset/status and cost cleanup.
- Prefer provider-native firewalls/security groups with minimum ingress:
  SSH from operator CIDR if known, workload ports only when requested.
- Render cloud-specific Tofu under `.playground/state/<backend>/<lab>/` so the
  committed `tofu/` baseline stays local-libvirt focused.
- Consider a generic `backend/tofu_common` helper only after the first cloud
  adapter proves the repeated pieces are real: init/apply/destroy wrappers,
  output parsing, event streaming, generated-file paths.
- Include a dry-run `plan` preview that renders estimated resources and cost
  before the first mutating apply.

