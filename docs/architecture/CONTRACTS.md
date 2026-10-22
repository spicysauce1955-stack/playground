# Layer contracts

This doc records the input/output contract for every layer in
`playground apply` and the cross-layer pitfalls that have already
bitten us once. Read this before adding a new step, a new lab
type, or a new third-party tool to the pipeline. The recurring
bug shape in this codebase is "library default wrong for fresh
state" or "implicit cross-layer dependency hidden by a hardcoded
value" — this doc exists to make those gaps visible up front.

## Pipeline overview

```
config/labs/<lab>.yaml
        |
        v
  config loader (src/playground/config/loader.py)
        |
        v
  LoadedConfig
        |
        v
  validator (src/playground/validation/validator.py)
        |  diagnostics: config.*
        v
  resolver (src/playground/config/resolver.py)
        |
        v
  ResolvedLab
        |
        v
+-------+-------+
|               |
v               v
tofu render     inventory render
(tfvars.py)     (inventory.py)
        |               |
        v               v
.playground/state/tofu/<lab>.tfvars.json
.playground/state/inventory/<lab>.ini
        |
        v
================ execute_apply ================
| tofu-apply         (cwd=tofu/)
| wait-for-vms-ready (TCP :22 then cloud-init status --wait)
| ansible-playbook   (ANSIBLE_CONFIG=ansible/ansible.cfg)
| verify-lab         (post-apply sanity battery; warning-only)
================================================
        |
        v
.playground/runs/<run-id>/{run.json, events.jsonl, logs/*.log}
```

## Per-layer contracts

### 1. Config loader (`src/playground/config/loader.py`)

**Input**: a directory of YAML files under `config/`.

**Output**: `LoadedConfig` (typed dataclass) + diagnostics list.

**Contract**:
- Every YAML must declare `apiVersion: playground/v1` and a valid
  `kind` from `playground.models.kinds.KNOWN_KINDS`.
- Duplicate `metadata.name` within a kind → diagnostic
  `config.identity.duplicate_name` (error).
- Parse-only; no cross-reference checks. The validator does those.

**Failure mode**: returns `LoadedConfig` with whatever parsed plus
diagnostics. Callers must check `_has_errors(diagnostics)` before
trusting the result.

### 2. Validator (`src/playground/validation/validator.py`)

**Input**: `LoadedConfig`.

**Output**: `list[Diagnostic]`.

**Contract**:
- Cross-reference checks: every name referenced exists (roles,
  networks, commands, providers, images, workload targets).
- Role-graph integrity: no cycles, no unknown `extends`.
- Lab-scoped DNS regex check.
- Backend-capability warnings (e.g., heterogeneous per-VM
  resources on local-libvirt).
- Diagnostic IDs are public contract — never rename without a
  deprecation plan. Full registry in `docs/system_overview.md`.

### 3. Resolver (`src/playground/config/resolver.py`)

**Input**: `LoadedConfig` + lab name.

**Output**: `ResolvedLab` (frozen Pydantic model, `extra="forbid"`).

**Contract**:
- Trusts the validator's invariants. Raises `KeyError` rather
  than silently producing broken models if a cross-reference is
  missing.
- Walks the role-extends chain root→leaf and deep-merges specs.
  `provisioners` uses **list-replace** semantics (child wins
  entirely). `capabilities` deep-merges as a dict.
- Populates `dns_domain` to `<lab-name>.lab` when the lab YAML
  omits `spec.dns_domain`.

### 4. Tofu render (`src/playground/backend/local_libvirt/tfvars.py`)

**Input**: `ResolvedLab`.

**Output**: dict serialized to
`.playground/state/tofu/<lab>.tfvars.json`.

**Contract**:
- `vm_names`: declaration-order list of lab VM names.
- `networks`: list of `{name, cidr}` from
  `lab.spec.networks`. One `libvirt_network` per entry.
- `vm_networks`: `{vm_name: [net_name, ...]}` from VM attachments.
- `vm_network_ips`: `{vm_name: {net_name: ip}}` for pinned IPs.
- `dns_domain`: always populated (resolver default).
- `vm_dns_hosts`: `{net_name: [{hostname, ip}, ...]}` derived from
  pinned-IP VMs. **Empty when no IPs are pinned** — see DNS
  pitfall below.

### 5. Tofu apply (`tofu/main.tf` via `apply.py`)

**Input**: `terraform.tfvars.json` from step 4.

**Output**: libvirt resources + tofu state at
`tofu/terraform.tfstate`. Also produces `tofu output -json`
emitting `vm_ips: {vm_name: ip}`.

**Contract**:
- Creates one `libvirt_network` per `var.networks` entry; sets
  `domain = var.dns_domain` and renders a `dns { enabled = true;
  hosts { ... } }` block when `var.vm_dns_hosts[network]` is
  non-empty.
- Creates one `libvirt_domain` per `var.vm_names` entry.
  `cpu { mode = "host-passthrough" }` is non-negotiable
  (Redroid needs binderfs).
- Cloud-init disk per VM: `cloud_init.cfg` templated with
  `vm_name`, `dns_domain`, `ssh_public_key`. Sets `hostname` +
  `fqdn` + `preserve_hostname: false`.

**Failure modes**: missing tofu binary, libvirtd unreachable,
permissions on the storage pool. Doctor covers the host
prereqs; `runtime.apply.tofu_binary_missing` covers the binary.

### 6. wait-for-vms-ready (`backend/local_libvirt/wait.py`)

**Input**: list of `VmTarget(name, ip, ssh_user)` derived from
`vm_ips` + `ResolvedVm.ssh.user`.

**Output**: `(StepResult, list[Diagnostic])`. Step exit 0 when
every VM passes both phases.

**Contract**:
- **Phase 1** — TCP :22 reachable via `socket.create_connection`
  with exponential backoff up to `DEFAULT_SSH_TIMEOUT_SECONDS`
  (300s). Cheap signal of "sshd listening."
- **Phase 2** — `ssh user@ip "cloud-init status --wait"` with
  subprocess timeout `DEFAULT_CLOUD_INIT_TIMEOUT_SECONDS` (600s).
  Blocks on the VM side until every cloud-init stage is done
  (incl. `package_upgrade`).
- VMs probed in parallel via `ThreadPoolExecutor`. Total wall
  time ≈ max(per-VM time), not sum.
- SSH invoked with `BatchMode=yes`, `StrictHostKeyChecking=accept-new`,
  `ConnectTimeout=10`. Never hangs on first-boot host-key prompt.

### 7. ansible-playbook (`apply.py` / `runner.py`)

**Input**: rendered inventory at
`.playground/state/inventory/<lab>.ini`, `ansible/site.yml`,
`ANSIBLE_CONFIG=ansible/ansible.cfg` env var (set by `runner.py`).

**Output**: `(StepResult, list[Diagnostic])` from streamed
subprocess.

**Contract**:
- Cwd is repo root (the `parent` of `ansible_dir`).
- **`ANSIBLE_CONFIG` MUST be wired explicitly** because the
  default discovery looks at `./ansible.cfg` relative to cwd. The
  file lives at `ansible/ansible.cfg`. The wiring is in
  `run_ansible_playbook(..., ansible_cfg=...)`.
- site.yml dispatches roles via `[needs_<provisioner>]` inventory
  groups derived from `ResolvedVm.provisioners`. Three plays stay
  on `hosts: playground` (extra_hosts, common, workload_*) because
  they're truly universal.
- Idempotent: re-running apply on a healthy lab should report
  `changed=0` across all tasks. (Move 4 will make this an
  enforceable assertion.)

### 8. verify-lab (post-Move 3)

**Input**: live lab (post-ansible-playbook).

**Output**: `(StepResult, list[Diagnostic])`. **Warning-only** —
failures attach `runtime.apply.verify_failed` but the run still
finishes `status=succeeded`.

**Contract**:
- For each VM: `ssh <user>@<ip> systemctl is-system-running` must
  return `running` or `degraded`, not `failed`.
- For each VM in `[needs_docker]`: `ssh <user>@<ip> docker ps`
  must exit 0.
- For each `lab.spec.commands.enabled` with `target: any`: run
  and assert exit 0.

### 9. playground reset (`backend/local_libvirt/scrub.py` + `runner.py`)

**Input**: `ResolvedLab`. Does NOT depend on tofu state.

**Output**: scrubbed libvirt + cleaned per-lab state files.

**Contract**:
- Three steps: `scrub-libvirt` (force destroy + undefine by name),
  `tofu-destroy` (best-effort), `clean-state-files` (per-lab state).
- Idempotent: a second reset on a clean lab is a no-op.
- **Never** touches `tofu/terraform.tfstate`,
  `ubuntu-noble.qcow2` (shared), or other labs' state.

### 10. playground doctor (`src/playground/preflight/doctor.py`)

**Input**: host environment + the playground repo.

**Output**: `list[Diagnostic]`. Read-only; never mutates state.

**Contract**:
- Each check is a pure function returning `list[Diagnostic]`.
- `runtime.doctor.*` namespace. IDs are public contract.
- Severity: `error` blocks apply; `warning` doesn't.

## Backend: local-vbox

A second backend (`spec.backend: local-vbox`) provisions VirtualBox VMs
with the `VBoxManage` CLI instead of OpenTofu + libvirt. The CLI/TUI
route to it through `playground.backend.dispatch`, which selects on
`ResolvedLab.backend`. The **configure half is shared verbatim** with
libvirt — `wait-for-vms-ready`, `ansible-playbook`, and `verify-lab` are
backend-neutral (they live under `backend/local_libvirt/` for historical
reasons but take an `ssh_port`, so vbox reuses them). Only the front half
differs.

### vbox apply pipeline

```
ResolvedLab (backend=local-vbox)
        |
        v
  build_vbox_plan (plan.py)   # pure: per-VM NICs, MACs, static IPs
        |
        v
================ execute_apply (runner.py) ================
| vbox-create        ensure base VDI (image.py: download qcow2 +
|                    qemu-img convert), then per VM: clonemedium,
|                    modifyvm (NAT NIC1 + --natpf1 ssh; intnet NIC
|                    per lab network), attach disk + NoCloud seed
|                    ISO (cloudinit.py), startvm --headless
| wait-for-vms-ready  (SHARED) 127.0.0.1:<host_port> per VM
| ansible-playbook    (SHARED) inventory has ansible_port=<host_port>
| verify-lab          (SHARED, warning-only)
==========================================================
```

### vbox-create contract (`backend/local_vbox`)

**Input**: `VboxPlan` from `build_vbox_plan`.

**Output**: N running VirtualBox VMs named `<lab>-<vm>`; returns
`vm_ips` (every VM → `127.0.0.1`) and `ssh_ports` (per-VM NAT host
port). On any failure, partially-created VMs are rolled back
(`unregistervm --delete`).

**Contract**:
- Base disk: the `ubuntu-noble` artifact (qcow2) is downloaded once and
  converted to a VDI with `qemu-img`, cached under
  `.playground/cache/artifacts/vm-images/...`. Per-VM disks are
  `clonemedium` copies, resized to the lab's `disk_gb`.
- NIC1 is **NAT** with `--natpf1 ssh,tcp,127.0.0.1,<host_port>,,22`.
  That is the SSH/management plane. Host ports are picked free at apply
  time (not in the plan).
- Each lab network adds an **internal-network** NIC (`--intnet<i>
  <lab>-<net>`) with a static IP set via the NoCloud `network-config`,
  matched by MAC. VirtualBox internal networks have no DHCP, hence
  static.
- cloud-init `user-data` mirrors `tofu/cloud_init.cfg` (hostname/fqdn,
  SSH key for `ssh.user`, package update/upgrade, no password auth).
- `playground reset` for vbox = `scrub-vbox` (delete every VM whose name
  starts with `<lab>-`) + `clean-state-files`. Never touches the cached
  base image.

## Backend: cloud-digitalocean

A third backend (`spec.backend: cloud-digitalocean`) provisions Droplets via
OpenTofu's `digitalocean` provider. The CLI/TUI route through
`playground.backend.dispatch` as with the other backends. The **configure half
is identical shared code** (`wait-for-vms-ready` → `ansible-playbook` →
`verify-lab`; these live under `backend/local_libvirt/` for historical reasons
but take `ssh_port=22` because Droplets have routable public IPs — no NAT
port-forward needed). Only the provisioning half and lifecycle verbs differ.

### cloud-digitalocean apply pipeline

```
ResolvedLab (backend=cloud-digitalocean)
        |
        v
  build_do_plan (plan.py)   # pure: Droplet size/region/tags/names
        |
        v
  render_do_tfvars (tfvars.py)  # pure; token NEVER included
        |
        v
  _prepare_tofu_dir             # copy tofu/cloud_digitalocean/*.tf +
                                # cloud_init.cfg into
                                # .playground/state/cloud-digitalocean/<lab>/
        |
        v
================ execute_apply / execute_resume (runner.py) ================
| cloud-preflight  token, size/region per distinct size, VPC name/range
|                  conflict (runtime.cloud.vpc_conflict), pre-VPC lab
|                  (runtime.apply.pre_vpc_lab) -- all before any tofu run
| tofu-init        (.playground/state/cloud-digitalocean/<lab>/ as cwd)
| tofu-apply       (-var-file=<lab>.tfvars.json)
| fetch-vm-ips     (tofu output -json → vm_ips map)
| render-inventory (pure; ssh_port=22 for all VMs)
| wait-for-vms-ready  (SHARED) public IP:22 per Droplet
| ansible-playbook    (SHARED)
| verify-lab          (SHARED, warning-only)
============================================================================
```

### cloud-digitalocean destroy / suspend / reset

```
destroy / suspend:
  tofu-destroy  → tag-sweep (list+delete by tag lab:<lab>, re-list survivors)
                → vpc-sweep (best-effort: delete an orphaned <lab>-vpc; warnings only)

reset:
  tofu-destroy  → tag-sweep → vpc-sweep → clean-state-files
  (clean-state-files removes per-lab dir + inventory; run logs are kept)
```

### cloud-digitalocean contract

**Per-lab state directory**: `.playground/state/cloud-digitalocean/<lab>/`
holds the copied `.tf` sources, `cloud_init.cfg`, `<lab>.tfvars.json`, and
`terraform.tfstate`. Each lab has its own directory so concurrent cloud labs
don't clash (unlike the single `tofu/terraform.tfstate` shared by
local-libvirt).

**Token**: passed to `tofu` via the `DIGITALOCEAN_TOKEN` environment variable
inherited by the subprocess. The `provider "digitalocean" {}` block in
`tofu/cloud_digitalocean/versions.tf` has no `token =` field. The
`render_do_tfvars` allowlist (`_TFVARS_KEYS`) excludes any token-like key.
Token must not appear in any tfvars file, log event, Diagnostic, or run record.

**`vm_ips` output shape**: `tofu output -json` must emit
`vm_ips: {vm_name: ip_string}` — a flat map keyed by the bare VM name (e.g.
`"node1": "203.0.113.10"`). This shape is a hard contract consumed by
`fetch_vm_ips` and reused unchanged by `render_inventory` and `verify_lab`.

**Lifecycle verbs** beyond apply/destroy:

- `suspend` — destroys all Droplets to stop billing. **Powered-off Droplets
  still bill on DigitalOcean**, so suspend uses `tofu destroy` (not a
  power-off API call). Publishes a `log_line` warning before any mutation.
  Per-lab state (tfvars, tfstate) is **preserved** so `resume` can rebuild.
- `resume` — re-provisions from config (`execute_apply` path with
  `operation="resume"`). Publishes a `log_line` warning before mutation: VM
  disk changes are NOT preserved (no snapshot). `local-libvirt` and
  `local-vbox` return `runtime.backend.verb_not_supported` for suspend/resume.
- `reset` — best-effort teardown + `clean-state-files` (removes per-lab dir,
  inventory, workload staging); run logs are kept. Idempotent: missing paths
  are silently skipped.

**Tag sweep**: destroy/suspend/reset always run a tag-sweep after
`tofu destroy`. It lists Droplets by `lab:<lab>` tag, deletes each, then
re-lists. If any survivors remain the operation exits `status=failed` with
`runtime.<operation>.orphaned_resource` diagnostics containing the
DigitalOcean console URL for each orphan. This prevents reporting success
while paid compute is still running.

**`query_status`**: the source of truth is the live DO API (Droplets tagged
`lab:<lab>`), not tofu state. Stale state cannot cause a false "no compute"
reading.

**ssh_port**: always 22. Droplets receive public IPv4 directly; no NAT
port-forward is involved. `wait_for_vms_ready` and `verify_lab` receive
`ssh_ports=None`, which both functions treat as "use port 22 for all VMs".

**VPC per lab**: `digitalocean_vpc.lab` (`<lab>-vpc`, `ip_range` = the
lab network's CIDR normalised to its network address); every Droplet sets
`vpc_uuid`. The validator requires exactly one network on this backend
(`config.network.do_single_network`), an RFC1918 IPv4 CIDR with a /16–/28
prefix (`config.network.do_vpc_cidr_invalid`), and warns that pinned
per-VM IPs are ignored (`config.network.do_ip_pin_ignored`). The range
must not overlap another VPC in the account (including DO's per-region
default VPCs, e.g. `10.108.0.0/20` in nyc3); cloud-preflight checks this
before tofu runs (`runtime.cloud.vpc_conflict`), and the validator warns
when two DO labs declare overlapping CIDRs (`config.network.do_cidr_overlap`). `tofu destroy` removes the VPC after the Droplets; teardown
also runs a best-effort `vpc-sweep` after the tag sweep (find
`<lab>-vpc`, delete, three attempts 5 s apart, warnings only:
`runtime.<op>.orphaned_vpc`), because an orphaned VPC blocks re-creating
the lab under the same name / range.

**Firewall**: `<lab>-fw` is attached by `droplet_ids` only (see "Lab egress isolation" →
"Backend: cloud-digitalocean (isolation)"). Inbound:
SSH from `firewall.ssh_cidrs`, plus all tcp/udp/icmp from the VPC CIDR
(VM-to-VM on any port). Outbound: open, or lab-droplets-only while
isolated (see "Lab egress isolation").

**Addresses in `status`**: `VmStatus.ip` is the Droplet's private VPC
IPv4 (`None` when absent, never the public one); `VmStatus.public_ip` is
the public IPv4; `ssh_host` = the public IP, `ssh_port` = 22. Anything
that *connects* uses `ssh_host`. Local backends never set `public_ip`, so
their JSON is unchanged. The tofu `vm_ips` output stays the public map
(the configure half depends on it); `vm_private_ips` is the private map.

**Per-VM size**: `spec.vms[].provider_overrides.cloud-digitalocean.size`
> `spec.providers.cloud-digitalocean.size` > `DEFAULT_SIZE`. `DoPlan.vm_sizes`
maps every VM to its slug, rendered as the `vm_sizes` tfvar; the
Droplet takes `lookup(var.vm_sizes, <vm>, var.size)`. The cost estimate is
the sum over VMs (`None` if any slug is unpriced), the size/region
preflight runs once per distinct size, and the plan shows
`s-1vcpu-2gb x5, s-2vcpu-4gb x1` when sizes differ. Validator:
`config.backend.do_vm_size_invalid` (error: override is not a non-empty
string), `config.backend.do_size_below_resources` (warning: a parseable
`s-<n>vcpu-<m>gb` slug provides fewer vCPUs / less memory than the VM's
declared resources).

**Upgrade note**: labs provisioned before the VPC change have no VPC, and
`vpc_uuid` forces Droplet replacement, so `apply` / `resume` refuse them
(`runtime.apply.pre_vpc_lab`): run `playground destroy <lab>` then
`playground apply <lab>` deliberately. The new validator errors
(`do_single_network`, `do_vpc_cidr_invalid`, `do_vm_size_invalid`) are
downgraded to warnings for destroy / reset / suspend, so an old lab can
always be torn down.
Until re-applied, their firewall keeps the account-wide tags and makes
`isolate` on any other lab fail with `runtime.isolate.foreign_firewall`.

## Workload: `android_app`

`type: android_app` is the first workload type whose staged artifact is
not always a single file, and whose Ansible role talks to something
other than Docker/`docker compose`/`docker stack` on the guest (a running
Redroid device, over `adb`). Read this section, and update it, before
changing `stage_workload_files`, `workload_to_ansible_payload`, or
`ansible/roles/workload_android_app`.

### `stage_workload_files`: file-or-directory in, same shape out

`workload.source` for `android_app` may point at either:

- a single `*.apk` file, or
- a directory of split APKs (one base APK + `split_config.*`/`config.*`
  siblings; validated by `playground.android.splits.find_base_apk`).

`stage_workload_files` (`src/playground/planner/scheduling.py`) mirrors
whichever shape it was given: a file source is copied to
`stage_dir/<vm>/<workload><suffix>`; a directory source is copied to
`stage_dir/<vm>/<workload>/` (contents only, one `shutil.copyfile` per
`*.apk`, with the destination directory `rmtree`'d first so a split
dropped from the lab config doesn't linger). Both branches write into the
**same return type** as every other workload type —
`dict[str, dict[str, Path]]`, `{vm_name: {workload_name: staged_path}}` —
so `render_inventory` and the compose/swarm code paths need no changes to
carry a directory instead of a file; they just don't stat what's on the
other end of the path.

`.aab` / `.apks` (Android App Bundles, not installable APKs) and a
single non-`.apk` file (typically an un-extracted `.xapk`/`.apkm`, which
are ZIPs of already-built splits) are both rejected at this layer with a
diagnostic, before staging — not left for the role to fail on.

### The role discriminates file-vs-directory by the STAGED PATH's suffix — this is load-bearing

`ansible/roles/workload_android_app/tasks/main.yml` never inspects
`item.staged_source` on disk to decide which copy/install task to run; it
checks `(item.staged_source | lower).endswith('.apk')`. Staging preserves
the source's shape (see above), so this is a safe inference **as long as
every file source stage_workload_files accepts ends in `.apk`** — which is
exactly what the `config.workload.apk_bundle_not_extracted` diagnostic in
`stage_workload_files` guarantees by rejecting any other single-file
source before it reaches staging.

If that guard is ever relaxed (e.g. to "cosmetically" accept any file
extension), a single-file source whose name does not end in `.apk` will
still be staged as a *file*, but the role will route it into the
split-directory branch (`copy: src: "{{ item.staged_source }}/"`), and
`ansible.builtin.copy` will fail because you cannot glob a trailing `/`
onto a plain file. Do not remove the suffix guard in
`stage_workload_files` without also changing how the role tells file and
directory sources apart.

### The `android` payload key and the fields the role consumes

`workload_to_ansible_payload` (`src/playground/planner/scheduling.py`)
adds an `"android"` key — `workload.android.model_dump()` — to the
per-workload dict whenever `workload.android is not None`; it is omitted
entirely (not `null`) for a `container`/`compose`/`swarm` workload, or an
`android_app` workload with no `android:` block declared (the role warns
and skips those by name rather than crashing on `item.android.package`).

`AndroidAppOptions` (`src/playground/models/kinds.py`) has five fields,
and the role consumes all five:

| field         | type          | role usage                                                                 |
|---------------|---------------|------------------------------------------------------------------------------|
| `package`     | `str`         | idempotency check (`pm list packages`), `pm grant`, `pidof`, `am start -n`   |
| `launch`      | `bool`        | gates the "ensure a process exists" launch tasks                            |
| `activity`    | `str \| None` | when set, `am start -n <package>/<activity>`; when unset, launcher discovery via `cmd package resolve-activity` with a `monkey` fallback |
| `permissions` | `list[str]`   | looped `pm grant <package> <perm>`, tolerant of failure (`failed_when: false`) |
| `reinstall`   | `bool`        | forces `adb install -r` / `install-multiple -r` unconditionally on every apply |

`pg_apk_root` (default `/opt/playground/apks`) is where staged files land
on the guest, one subdirectory per workload name.

### The guest needs `adb`; it comes from the redroid role, not this one

`workload_android_app` runs `adb` commands against `127.0.0.1:5555`
**on the guest itself** — it never opens a network path to the device and
has no `adb`-installation task of its own. `adb` is installed by the
`redroid` role's `redroid_install_adb` task (default `true`, Ubuntu
universe package `adb`), which then asserts `adb version` succeeds and
fails the play by name if it doesn't. Setting `redroid_install_adb: false`
without providing `adb` some other way therefore breaks every
`android_app` workload on that VM, not just Redroid itself — `verify-lab`
and any `type: android_app` role tasks will fail at the same "no adb on
PATH" assertion.

### ADB has no authentication — reachability, not exposure, is the control

Same rule as the redroid contract above: the redroid container binds
`0.0.0.0:5555` regardless of workload type, so `android_app` inherits
whatever reachability that backend has (see "ADB has no authentication"
under the Redroid section: DO's firewall opens 22 only; local-libvirt's
NAT network has no firewall in front of it; local-vbox does not forward
5555 to the host by default). The `workload_android_app` role itself
never exposes anything new — it drives `adb` from inside the guest over
SSH-executed Ansible tasks, the same mechanism `playground app` uses.
Operators reach a device with `playground adb --lab <lab> --on <vm>`
(SSH tunnel to the guest's 5555) or `playground app <verb> --on <vm>`
(guest-side `adb`, no tunnel) — never by opening 5555 to a network.

## Capture: Redroid device traffic

**Input**: `ResolvedLab.capture` (a `CaptureOptions`), carried to Ansible
as the `pg_capture` group var under `[playground:vars]`.
`ansible/roles/capture/defaults/main.yml` only declares the fallback
`pg_capture: '{}'`, so a hand-run play without the var is a no-op rather
than an undefined-variable failure. The actual parse -- the "string or
already-decoded dict" guard, needed because `from_json` only accepts a
string while Ansible sometimes auto-parses the .ini value into a dict
already -- is the "Parse pg_capture JSON payload" task in
`ansible/roles/capture/tasks/main.yml`, the same shape
`workload_container`'s "Parse pg_workloads JSON payload" task in
`ansible/roles/workload_container/tasks/main.yml` uses for `pg_workloads`.

**Output**: `.pcap` files at `/var/lib/playground/capture/<vm>/` on the
guest; fetched to `.playground/runs/<run-id>/artifacts/capture/<vm>/`.

**Contract**:
- The capture point is the Redroid container's OWN network namespace,
  entered with `nsenter --target <pid> --net`. Only `--net` is entered —
  the mount namespace stays the guest's — so `tcpdump` is the guest's
  binary, the pcap lands on the guest's disk (not inside a container
  whose writes vanish when it is recreated), and `-i any` inside that
  namespace is exactly one device's traffic.
- **The container PID is resolved at unit-start time, never at
  provision time.** The wrapper (`capture-start`) does the `docker
  inspect` itself, on every start. The Redroid container runs
  `restart_policy: unless-stopped`, so a PID baked in by Ansible at
  provision time would go stale the first time the container restarts.
- The wrapper DISCOVERS the container by the `redroid_` name prefix
  (`capture_container_prefix`) and fails loudly — exit 1, before
  `nsenter` runs — on zero or on more than one match. It deliberately
  does not duplicate the redroid role's
  `redroid_{{ inventory_hostname | regex_replace(...) }}` naming
  expression; that would be a second copy of a naming convention living
  in a second role, the exact "implicit cross-layer dependency hidden by
  a hardcoded value" shape this doc warns about.
- **A Redroid container restart does NOT kill tcpdump, and the unit does
  NOT restart on its own — an earlier version of this doc, the unit's
  own comment, and the design spec all asserted the opposite, and it was
  measured false on a live guest.** `nsenter --target <pid> --net` moves
  tcpdump INTO the container's network namespace, and tcpdump's own
  presence there keeps that namespace alive after every one of the
  container's own processes has exited. So on a `docker restart`,
  tcpdump keeps running untouched (measured: MainPID unchanged across a
  restart that changed the container's own PID), `Restart=always` never
  fires because nothing died, and `systemctl is-active` keeps reporting
  `active` while tcpdump sits in an orphaned, disconnected namespace
  capturing nothing — 5 pings inside the new container's netns produced
  zero new captured bytes. The fix is that `capture-start.j2` is now a
  **watchdog**, not a one-shot `exec` into tcpdump: it launches tcpdump
  in the background, records the container's netns identity at start,
  and runs two checks at two cadences. Every ~1 second it checks
  tcpdump's OWN liveness — decoupled from the netns check, and load-
  bearing for a different reason: a tcpdump that dies immediately (a bad
  `-i`, an AppArmor denial, an unwritable savefile path) must be caught
  well inside `capture start`'s 2-second `is-active` probe window (see
  below), or that probe reads a lying `active` for a capture that
  already died — the same "unit says active while nothing is captured"
  shape as the netns bug, just triggered by an instant death instead of
  a restart. Every ~5th tick (roughly every 5 seconds — this one forks
  `docker` twice, so it is not run every tick) it polls the container's
  CURRENT netns, and when it differs from the one seen at this start, it
  stops tcpdump itself and exits NON-ZERO — THAT is what makes
  `Restart=always` re-exec the wrapper against the new PID. Self-healing
  is NOT one poll interval: it is up to the ~5s netns-detection latency
  PLUS `RestartSec=5` before the new tcpdump attaches, so **up to
  roughly 10 seconds** of lost traffic in the worst case. A container
  that is transiently unresolvable mid-restart (as opposed to one whose
  netns has actually changed) does not end the loop — it may simply be
  coming back, and the two `docker` calls inside that check are wrapped
  in `timeout 10` so a wedged daemon cannot block staleness detection
  indefinitely. `playground capture status` additionally compares a
  netns marker the wrapper writes at every start
  (`/run/playground-capture/<vm>.netns` — under `/run`, not the capture
  directory, so it is not swept up by `capture fetch`) against the
  container's current netns and reports `state=stale` on a mismatch, as
  a safety net for a crashed watchdog or a future regression — not as
  the primary defence, which is the watchdog's own poll loop.
- Provisioning (the `capture` Ansible role) installs `tcpdump`, the
  wrapper, and the instanced unit, and converges to unit
  installed-but-**stopped** — it is never `enable`d. The `capture` role
  itself never starts, stops, or restarts a session, and never touches a
  recording already in progress. That guarantee is scoped to this role,
  though: `ansible/site.yml` runs the `redroid` role's play before the
  `capture` play, so `playground apply` on a lab that is mid-capture CAN
  still interrupt the session — if that earlier play recreates the
  Redroid container, the running session ends up stale (see the watchdog
  paragraph below) and, once the watchdog notices and restarts the unit,
  a new `${stamp}` pcap base name begins. Sessions start only via
  `playground capture start`. Not enabling is deliberate (an
  enabled instance would start recording at boot, violating "provisioning
  never starts a session"), and it has a consequence nobody wrote down
  before now: **a guest reboot does not resume a capture that was
  running before it.** The operator's session record still claims the
  capture is running; `capture status` will show `state=inactive`. The
  recovery is `capture stop` (a no-op against the guest that only clears
  the stale local record) followed by `capture start`.
- **Confirmed live, not just claimed:** a re-apply against an already
  converged capture-enabled host reported `changed=0` across all 41
  tasks in the play, did not recreate the Redroid container, and did not
  disturb a running capture session — same container id, same unit
  `MainPID`, capture still recording throughout the re-apply.
- `spec.capture.enabled: false` does not skip the `needs_capture` play —
  the host is still a member of that group. It skips *inside* the role,
  via its own `ansible.builtin.meta: end_host` guard, evaluated right
  after `pg_capture` is parsed and before the `tcpdump` package task
  runs. Net effect is the same (no package, no unit) but the skip point
  is the role's guard, not group membership — a detail that matters if
  you ever go looking for a `when:` on the play itself and don't find
  one.
- **Every `redroid-host` now provisions capture tooling by default.**
  `config/roles/redroid-host.yaml` declares `capabilities.capture: true`
  and lists the `capture` role in `provisioners`, and
  `CaptureOptions.enabled` defaults to `true` — so any lab using this
  role, including the previously capture-free `redroid-cloud`, now
  installs `tcpdump` and hard-fails at provisioning time if the apt
  archive is unreachable (the same "abort loudly rather than silently
  degrade" precedent as the redroid role's own `adb` probe/abort pair).
  This is not a defect, but it is a behaviour change worth knowing about
  before re-applying an existing lab. `spec.capture.enabled: false` in
  the lab's spec is the opt-out for an air-gapped or lean guest.
- `local-vbox` is excluded by construction, not by a runtime check on
  that backend: `capture_vm_names()` only returns VMs whose role
  declares `capabilities.capture: true`, and `redroid-host`'s capture
  capability is meaningless without a Redroid container to attach
  to — `local-vbox`'s `ProviderConfig` declares `redroid: false`
  because Redroid is unverified there.
- **Capturability is a property of the TARGET, not of `spec.backend`.**
  An earlier version of `capture/targets.py` short-circuited on
  `resolved.backend` before asking about capabilities at all and emitted
  `config.capture.backend_unsupported`; that diagnostic no longer exists
  anywhere in `src/`, and this doc described it long after it was
  deleted. The reason it had to go is the physical-handset work: a phone
  plugged into the machine running a `local-vbox` lab has nothing to do
  with the backend that lab's VMs use, and a backend allowlist would
  have refused it for a reason that is not about the target. Each
  candidate is now checked for its OWN `capture` capability. Physical
  device slots are still refused, but by name and for the honest reason:
  naming one with `--on` gives `config.capture.device_unsupported`
  ("capture from a physical handset ships in Plan 2"), which requires
  device slots to be recognized candidates first — that is why
  `resolve_capture_targets` checks `resolved.devices` ahead of every
  other branch.

### Two Ubuntu tcpdump defaults that break a fresh guest

Both are the recurring "library default wrong for fresh state" shape.

1. **Privilege drop — this is the load-bearing one, confirmed and
   strengthened by live measurement.** Debian/Ubuntu `tcpdump`
   `setuid()`s to the unprivileged `tcpdump` user unless told otherwise,
   and then cannot write into the root-owned capture directory. `-Z
   root` is required. Measured live: `sudo -u tcpdump touch` inside the
   `0755` root-owned capture directory is denied, and running tcpdump
   WITHOUT `-Z root` under `-C 1 -W 3` fails immediately with
   `tcpdump: /var/lib/playground/capture/droid1/rot.pcap0: Permission
   denied` — not at the first rotation, but on the very first file.
   That is a `-C`-specific nuance: under `-C`, even the FIRST numbered
   savefile is opened AFTER the privilege drop, so the failure is
   immediate; without `-C`, tcpdump opens its one savefile BEFORE
   dropping privileges and would succeed. (The capture directory itself
   is `0755`, not `0750` — see below — but ownership is still
   `root:root`, and the unprivileged `tcpdump` user has no write access
   to it either way.)
2. **AppArmor confinement — NOT the load-bearing protection here,
   contrary to what an earlier version of this doc claimed.** Ubuntu
   ships `/etc/apparmor.d/usr.sbin.tcpdump` confining where `tcpdump`
   may write, and the role does install a local override at
   `/etc/apparmor.d/local/usr.sbin.tcpdump` (reloaded with
   `apparmor_parser -r`, tolerated with `failed_when: false` for a host
   with AppArmor disabled) as defence-in-depth. But measured live on
   Noble with the tcpdump profile in **enforce** mode: the STOCK
   profile, with no override of ours in place, already permits
   `**.pcap*` writes anywhere on the filesystem — `.pcap`, `.pcap0`,
   `.pcap00`, and `.pcap39` all wrote successfully to an arbitrary
   directory (`/var/lib/nogrant/`) outside our override's grant, while a
   `.dat` file in the same directory was denied with `Permission
   denied`. So the override is protection against a future filename
   that doesn't match `**.pcap*`, not the reason capture works today —
   downgrade any claim that AppArmor is the highest-probability
   live-only failure; `-Z root` above is the one that actually gates
   whether writes succeed at all. Do not disable confinement regardless
   — it is real, correctly scoped, defence-in-depth, just not the thing
   standing between a fresh guest and a working capture.

The capture directory is created `0755`, not `0750`: tcpdump's savefiles
land as `0644 root:root` regardless of the parent directory's mode, so a
tighter parent would block traversal while the files underneath stay
world-readable anyway. `capture status`'s `find` calls and `capture
fetch`'s `scp` stay deliberately unprivileged — `start`/`stop`/`clean`
go through `sudo -n`, and so, now, does the ONE step of `capture
status`'s stale-capture check that reads another (typically root-owned)
process's `/proc/<pid>/ns/net` symlink, which an unprivileged reader
cannot otherwise resolve — so the unprivileged SSH user genuinely needs
to traverse this directory to read anything back.

### `-C` semantics are inherited, and the ceiling is per session-start

`max_file_mb` maps to `tcpdump -C`, which counts units of **1,000,000
bytes, not MiB**. `-C` with `-W` is a **ring within one running
tcpdump**: the oldest file is overwritten once `max_files` is reached,
so a session that outlives its budget keeps the most recent
`max_file_mb * max_files` and discards the beginning.

That ceiling is **per session-start, not a global cap on the
directory**. Two facts compound: `tcpdump` applies `strftime()` to `-w`
only when `-G` is set — under `-C`/`-W` alone it writes the name
verbatim and appends a rotation counter, ZERO-PADDED to the width of
`-W`'s argument (verified live: `-w '%Y%m%d-%H%M%S.pcap' -C 1 -W 3`
produced a file literally named `%Y%m%d-%H%M%S.pcap0`; with this role's
default `max_files: 40` the real names observed on a live guest are
`<stamp>.pcap00` through `<stamp>.pcap39`, e.g.
`20260903-171939.pcap00` — NOT `<stamp>.pcap0` … `<stamp>.pcap1` as an
earlier version of this doc claimed) — so the wrapper computes the
timestamp itself with `date -u` before invoking `tcpdump`, and `-W`'s
ring counts files *per base name*. Every unit start (including every
restart the watchdog triggers after noticing the container moved to a
new network namespace — see "A Redroid container restart does NOT kill
tcpdump" above) mints a new stamp, hence a new base name, hence a new
ring. N restarts therefore permit up to N × (`max_file_mb` ×
`max_files`) on disk, not one fixed ceiling. This was a deliberate
trade, not an oversight: one fixed filename would keep a true global
cap, but would silently TRUNCATE the prior session's data on every
container restart — losing data and hiding that it was lost. `capture
status` reports the file count so a wrapping (or multiplying) session is
visible. Because the on-disk name is always `<stamp>.pcap<NN...>` at
whatever width `-W` implies, never a single un-padded digit, every CLI
glob that touches these files is `*.pcap*`, never `*.pcap` or `*.pcap?`
— a bare `*.pcap` glob matches nothing that tcpdump actually wrote, and
a single-`?` glob stops matching the moment `max_files` needs two
digits.

### The unit cannot self-report a wrapper failure; `capture start` verifies it

The instanced unit is `Type=exec`, under which `systemctl start`
completes when `/bin/sh` execs — not when the wrapper's own final `exec
nsenter ... tcpdump` succeeds. A wrapper that dies immediately (no
Redroid container found, an AppArmor denial) still makes the start job
report exit 0. `playground capture start` therefore does not trust that
exit code: it starts the unit, sleeps into the unit's `RestartSec=5`
gap, and requires `systemctl is-active` to report exactly `active`
before it records a session. The unit's `[Unit]` section also sets
`StartLimitIntervalSec=300` / `StartLimitBurst=20`, wider than systemd's
default start-limit window — at the default, `Restart=always` with
`RestartSec=5` would never trip (5 starts per 10s does not fire at a 5s
interval), so a permanently broken wrapper would retry forever instead
of ever reaching `failed`, where `is-active` (and so `capture status`)
can actually surface it. The wider window still absorbs a normal
Redroid restart (1-2 starts) while a genuinely broken wrapper burns all
20 within roughly 100 seconds.

### Android 14 moves the CA store — a constraint on the image tag

Not needed for passive capture, but load-bearing for the TLS-interception
follow-up: bind-mounting a MITM CA into `/system/etc/security/cacerts`
works because the pinned image is Android 11. **Android 14+ moved the
trust store into the Conscrypt APEX**, so raising `redroid_image`'s tag
past 13 would invalidate that approach entirely. This is a second
constraint on `redroid_image`, alongside "must stay date-stamped".

## Physical Android devices (`spec.devices`)

A USB-attached handset is a lab target that NO backend provisions. That
one fact drives every contract below; see
`docs/superpowers/specs/2026-09-08-physical-android-devices-design.md`
for the design.

### The device phase in `execute_apply`'s caller, not in a backend

`playground apply` runs adoption (`cli/main.py::_adopt_devices_or_exit`
-> `devices/adopt.py::adopt`) **after** the backend's `execute_apply`
returns success, in the CLI layer, for every backend alike. It is not a
step inside `local_libvirt`/`local_vbox`/`cloud_digitalocean`, because
none of them creates or destroys a phone — a `local-libvirt` lab may own
a handset exactly as a `cloud-digitalocean` one may.

Contract of that phase, in order:

- **It only runs when `resolved.devices` is non-empty.** A device-less
  lab must shell out to `adb` exactly as often as it did before devices
  existed: never. Two tests pin this
  (`test_a_device_less_lab_never_touches_adb`, and the apply-side
  equivalent); treat them as a regression fence, not as coverage
  padding.
- Input is `list_attached()` (one `adb devices -l` on the CONTROLLER,
  never inside a guest) plus the lab's `ResolvedDevice` slots — pure
  intent, no probed facts, per "config declares intent, state holds
  facts".
- Output is a persisted binding per slot under
  `<state_dir>/state/devices/<lab>/<slot>.json`, plus diagnostics. A
  serial is never written into a committed lab file, for the same reason
  a VM IP never is.
- An absent OPTIONAL handset is a warning (`required: false` ->
  `state="missing"`); an absent REQUIRED one, an unauthorized one, an
  unmet `require:`, an ambiguous match, or a rebind-required slot is an
  error that exits non-zero.
- **`--rebind` is the only thing in the codebase that retargets a slot**,
  and it lives on `apply`. Nothing else — not `status`, not `app`, not
  `adb` — may rebind, and every `device.rebind_required` refusal names
  `playground apply <lab> --rebind` rather than a bare flag, because
  naming a flag the current verb does not accept is a dead end (it was
  one, for a while: the diagnostics named `--rebind` before any command
  defined it).
- **Ordering caveat, deliberately unresolved:** adoption runs AFTER the
  backend apply, so a missing REQUIRED handset fails only once the VMs
  are already up. Moving it earlier is a real design decision (it would
  make `apply` probe USB before provisioning anything), not a bug to fix
  in passing.

### `query_status`'s device half is backend-neutral, read-only, and never binds

`backend/dispatch.py::query_status` routes the VM-shaped half to the
matching adapter and then fills `LabStatus.devices` itself, via
`_device_statuses`. Three properties are load-bearing:

1. **Read-only.** It calls `read_binding`, NOT `resolve_binding` — the
   latter auto-binds and persists an unbound slot with exactly one
   matching candidate, which is precisely the mutation a status query
   must never cause. It also does not re-`probe_facts`: facts come from
   the registry as recorded at the last adoption, so status stays as
   cheap as any other status query.
2. **`state_dir` is an input to the ANSWER, not just a place to write.**
   A slot's binding lives under it, so every device-targeting verb must
   thread the operator's `--state-dir` through to `query_status`. `app`
   and `adb` did not, and `playground apply L --state-dir /srv/pg`
   followed by `playground adb --lab L --on pixel7` therefore reported a
   plugged-in phone as `device.not_connected` while `status --state-dir
   /srv/pg` called it connected. This is the recurring "library default
   wrong for a non-default caller" shape below.
3. **`device.adb_unavailable` is downgraded to a WARNING here, not
   dropped.** `list_attached()` returns it as an error, but
   `query_status` is shared by `status`, `exec`, `cp`, `app`, `adb` and
   `capture`; propagating an error would hard-fail all of them over a
   missing `adb` that most of them do not need. Downgrading means every
   caller must PRINT surviving warnings after `_exit_on_errors` (which
   only raises on errors) — otherwise a device lab reads as "nothing
   attached" with no cause named anywhere.

`DeviceStatus.rebind_candidates` is the swapped-cable channel: when the
bound serial is absent and a DIFFERENT attached handset satisfies the
slot's selector, `_device_statuses` records those serials (via the pure
`match_candidates`) and `android/targets.py::device_status_diagnostic`
turns them into `device.rebind_required`. Carrying the fact on the
status object rather than as a diagnostic out of `query_status` is what
lets `playground status` report a swapped cable without `_exit_on_errors`
aborting the status command itself. Reporting is not binding: the
persisted binding is untouched by any of this.

### Only three paths ever write under `config/`, and one of them is a note

`config/` is tracked by git; `.gitignore` excludes only `.playground/`.
So anything that writes a `DeviceProfile` or a note into `config/` puts
churn in someone's `git status`. Exactly three paths may do it, each an
explicit human action: answering the discovery prompt, `playground
device save`, and WRITING a shared note (`device note` creates the
profile a shared note needs). Everything else — every adb poll, every
`device list`, every `device note --show`/`--clear` — is read-only. That
is a contract, not an implementation detail: `note_command` created the
profile before it looked at which sub-verb was asked for, so a plain
READ produced a committed file.

**Notes default to the SHARED, committed surface**
(`config/devices/<name>.md`); `--private` is the gitignored,
serial-keyed one (`<state_dir>/state/devices/<serial>/notes.md`).
Because the safe choice is the one behind a flag, the CLI has to say so
in three places — the `device note` help, the group help, and the
success line of every shared write — and this paragraph is the third
place the design names: the docs. Neither surface encrypts anything;
`--private` only keeps the file out of git and off a teammate's
machine, and the credential heuristic in `devices/notes.py` is advisory
(it warns, never refuses — the motivating use case is itself
credential-shaped). The private surface is keyed by the REAL serial in
every reader, so a `--private` write against a model-only profile must
resolve the serial through the registry or refuse
(`config.device.note_serial_required`): writing it under the profile
name produced a note that was saved and then permanently invisible.

## Lab egress isolation (isolate / connect)

`playground isolate <lab>` cuts, after provisioning, every lab VM's egress
to anything outside the lab's own network(s); `playground connect <lab>`
undoes it. The cut is enforced OUTSIDE the guests (a VM cannot undo it) and
holds across a DHCP renewal and a reboot. Implemented for `local-libvirt`
and `cloud-digitalocean`; `local-vbox` is unsupported.

### Intent (backend-neutral)

- **Blocked:** TCP/UDP/ICMP from lab VMs to anything outside the lab
  networks: the internet and DNS forwarded to outside resolvers. The
  cloud metadata service is NOT blocked on DigitalOcean (see below). A lookup of an outside
  name from a VM must fail.
- **Kept:** all VM-to-VM traffic on any port, and SSH from this machine to
  every VM (`exec` / `cp` / SSH tunnels / `adb`).

### Dispatch, not backend, owns the record

`backend/dispatch.py` routes `execute_isolate` / `execute_connect` (same
kwarg style as the other verbs; `tofu_dir` / `config_dir` are accepted for
a uniform signature and unused). Backend entry points live in
`local_libvirt/isolation.py` and `cloud_digitalocean/isolation.py`, take
`(resolved, state_dir, bus)`, never raise, and return an `OperationRun`
(`operation` = `isolate` / `connect`) plus diagnostics under
`runtime.isolate.*` / `runtime.connect.*`. **They never read or write the
isolation record**; dispatch does.

The record is `<state_dir>/isolation/<lab>.json`
(`state/isolation.py`: `lab`, `backend`, `isolated_at` ISO UTC,
`lab_cidrs`, `run_id`, `state` = `isolating` | `isolated`, default
`isolated` so older files parse), written atomically. **Fail closed:** the
record is written (`isolating`) BEFORE the backend runs; if that write
fails, dispatch aborts with error `runtime.isolate.record_write_failed`
without calling the backend.

| Event | Record |
| --- | --- |
| `isolate` starts | the prior record (if any) is read first, then `isolating` is written (overwritten on re-isolate) |
| `isolate` run `succeeded` | updated to `isolated` (new run id and time); an update failure is only a warning (`runtime.isolate.record_update_failed`), the `isolating` record still guards |
| `isolate` run `failed`, no prior record | cleared when the run contains no mutating step (`attach-filter` on libvirt, `tofu-apply` on DO; whatever the exit codes), so preflight / init / plan / plan-time refusals change nothing; otherwise kept as `isolating` (lab may be partially isolated; warning `runtime.isolate.partially_isolated` names `connect` / `isolate`) |
| `isolate` run `failed`, prior record | the prior record is restored exactly as it was (the previous isolation is still in force); `partially_isolated` is only added when a mutating step ran |
| `connect` run `succeeded` | cleared |
| `destroy` / `reset` / `suspend` run not `failed` | cleared (the resources carrying the enforcement are gone; resume is a fresh, connected apply); on libvirt the nwfilter is removed only if a record existed before the teardown |
| `connect` / `destroy` / `reset` / `suspend` run `failed` | untouched |

An unreadable (corrupt) record counts as isolated: the guard must fail
closed, and so does a record that cannot be read (`OSError`): the guard
reports it as isolated with the error in the message.

### The apply guard: `runtime.apply.lab_isolated`

`execute_apply` and `execute_resume` return `(None, [error])` while the
record exists in either state (the message calls out a half-finished
`isolating`), before any backend code runs. The suggestion names
`playground connect <lab>`. Reasons: Ansible needs internet, and
re-rendering the DO tfvars must never silently reopen egress. The
operator workflow is `connect`, `apply`, `isolate`. The CLI and TUI
already treat a `None` run as "pre-flight rejected", so no extra
presentation code is needed. The guard is backend-independent (it also
covers `local-vbox`, which can never have a record).

### Idempotency

`isolate` on an already-isolated lab succeeds; `connect` on a lab that was
never isolated succeeds. `isolate` on an unprovisioned lab fails with
`runtime.isolate.not_provisioned`.

### local-vbox: unsupported

Both verbs return `(None, [runtime.backend.verb_not_supported])`.
`verb_not_supported_diagnostic` has a dedicated wording for these verbs
(no egress-isolation mechanism implemented); its suspend/resume wording
("only cloud backends do") must not be reused for them.

### DNS caveat (local-libvirt)

While isolated, guest DNS queries to the lab gateway (libvirt's dnsmasq)
are dropped: dnsmasq forwards upstream and libvirt cannot switch that off
on a live network. Consequence: lab-internal names served by dnsmasq
also stop resolving while isolated; use IPs or `extra_hosts`. VM-to-VM DNS
(a VM running its own resolver) is not blocked.

### Drop, not reject (local-libvirt)

The nwfilter rules sit at the ebtables layer (`<ip>` / `<ipv6>` rules),
which can only DROP. iptables-level nwfilter rules were avoided on
purpose (they need `br_netfilter`, which is not guaranteed). Outside
connections therefore time out instead of failing fast. The CLI says so.

### Backend: local-libvirt (isolation)

- Mechanism: a libvirt nwfilter `playground-isolate-<lab>` (name
  sanitised), chain `root`, ebtables layer only, every rule
  `direction='out'` (traffic sent BY the guest), referenced through a
  `<filterref>` from every interface on a lab network. libvirtd enforces
  it on the host (no sudo, no host nftables) and it is persisted in the
  domain config. Verified live on libvirt 10.0 with real guests: a DHCP
  renewal brings the default route back but egress stays cut, and the cut
  survives a reboot.
- Rule set, repeated per lab gateway `G` / per lab CIDR `C`:

  | Prio | Action | Match |
  | --- | --- | --- |
  | 50 | accept | ARP |
  | 100 | drop | tcp + udp dport 53 to `G` (dnsmasq forwards upstream) |
  | 110 | accept | udp dport 67 to `G` (unicast DHCP renew) |
  | 120 | accept | tcp sport 22 to `G` (replies to host-initiated SSH) |
  | 130 | accept | icmp to `G` |
  | 150 | drop | everything else to `G` |
  | 160 | drop | the network and broadcast address of `C` |
  | 200 | accept | ip to `C` (VM-to-VM, any port) |
  | 210 | accept | udp dport 67 to 255.255.255.255 (DHCP discover / rebind) |
  | 900 | drop | all other IPv4; all IPv6 |

- **The host is outside the lab.** `G` is the host's bridge address and
  lies inside `C`, so the gateway rules sort before the CIDR accept;
  without them anything the host listens on at `0.0.0.0` (docker swarm,
  rabbitmq, a registry, a proxy) would be an egress path, and the subnet
  broadcast would reach host UDP sockets. Consequence: host→VM
  connections other than SSH (e.g. curling a VM port from the host) do
  not work while isolated; use an SSH tunnel. `exec` / `cp` / ansible /
  `adb` ride sshd and keep working. IPv6 is dropped entirely, VM-to-VM
  included.
- **Scoped from libvirt, not the YAML.** `C` and `G` come from
  `virsh net-dumpxml` of each lab network; a CIDR that differs from the
  lab config fails with `runtime.isolate.network_drift` (re-apply first).
  Every domain in `virsh list --all` with an interface on a lab network
  is isolated, declared in the lab or not (`undeclared_domain` warning);
  an isolated domain with any interface NOT on a lab network fails with
  `runtime.isolate.non_lab_interface` before anything changes.
- Steps: `isolate` = inspect-lab → define-nwfilter → attach-filter →
  verify-isolation; `connect` = inspect-lab → detach-filter →
  undefine-nwfilter. Running domains get `update-device --live` (live
  XML) and `--config` (`dumpxml --inactive`); shut-off domains get
  `--config` only. verify re-lists running domains and requires a live
  binding (`virsh nwfilter-binding-list`) for each. Per-run XML is kept
  under `runs/<id>/xml/`.
- libvirt facts this code depends on (verified live): `nwfilter-define`
  of an existing name **without** its UUID fails ("already exists with
  uuid"), so a refresh reuses the UUID and is skipped when the rules are
  unchanged; `update-device --live` accepts a filterref change on a
  running domain; `nwfilter-undefine` of a referenced filter fails
  ("nwfilter is in use"), so `connect` detaches every filterref first.
- A half-isolated lab (crash mid-run) is completed by re-running
  `isolate`. An interface already carrying a different filter is switched
  to ours (`filter_replaced` warning; libvirt allows one filterref per
  interface) and is NOT restored by `connect`. Domains tofu creates never
  carry one.
- `destroy` / `reset` call `local_libvirt.remove_isolation_filter(lab)`
  best-effort after a non-failed run; its warnings are surfaced by the CLI
  and never fail the teardown.

### Backend: cloud-digitalocean (isolation)

- Mechanism: the lab Cloud Firewall, driven by the tofu variable
  `egress_isolated` (default false) so HCL and tofu state stay the single
  source of truth. Open: outbound tcp/udp/icmp to `0.0.0.0/0, ::/0`.
  Isolated: outbound only to `destination_droplet_ids` = the lab's own
  Droplets, with no addresses. DO firewalls are stateful, so replies to
  inbound SSH still flow.
- **Not the VPC CIDR.** In a custom VPC, DigitalOcean gives droplets a
  resolver at an address *inside* the VPC range (`10.97.0.254` for
  `10.97.0.0/24`; `resolvectl dns` shows it) that forwards outside
  lookups, so a CIDR-scoped rule leaves outside names resolving. Verified
  live: droplet-id destinations make that lookup fail while VM-to-VM on
  any port and inbound SSH keep working.
- **The metadata service is NOT blocked** (verified live).
  `169.254.169.254` is link-local, answered by the hypervisor, and outside
  Cloud Firewall's reach. It is not a path to the internet; if a test
  needs it unreachable, block it in-guest.
- **The firewall targets Droplets by `droplet_ids` only, never `tags`.** A
  DO firewall's `tags` *select the Droplets it applies to*, the ownership
  tags include the account-wide `playground`, and DO unions allow rules
  across firewalls, so one tagged open firewall silently defeats isolation
  on every other lab (seen live with a firewall created before this
  change; isolate now catches it, see below).
- `isolate` / `connect` flip only `egress_isolated` in the per-lab
  `<lab>.tfvars.json` written by the last apply (no re-render from config,
  no re-copy of the `.tf` sources). Steps: preflight → set-egress-isolated →
  tofu-plan → tofu-apply → verify-firewall.
  - **Scoped plan:** `tofu plan -out` + `tofu show -json`; anything other
    than an in-place update of `digitalocean_firewall.lab` (or no change)
    is refused with `runtime.<op>.unexpected_changes`, so isolate never
    recreates a hand-deleted Droplet or applies config drift. Then
    `tofu apply <planfile>`. The tfvars file is restored if init, plan or
    apply fails.
  - **Verify by ID, after it settles:** the firewall id and Droplet ids
    come from the per-lab tfstate (output `firewall_id` too); the firewall
    is read with `GET /v2/firewalls/{id}` and polled (60 s, every 3 s)
    until `status == "succeeded"` with no `pending_changes`.
  - isolate requires: the lab set (state Droplet ids ∪ Droplets tagged
    `lab:<lab>`) is attached to our firewall (`uncovered_droplet`
    otherwise; an empty or short tag listing fails); every outbound rule
    targets exactly that set and nothing else; every OTHER firewall that
    applies to a lab Droplet (by `droplet_ids` or tag overlap, same name
    or not) allows no other outbound destination
    (`runtime.isolate.foreign_firewall`, naming it). connect requires our
    firewall open again.
  - API unreachable or not settled in time → the run fails; it never
    claims isolation it could not confirm.
- Pre-checks: no per-lab state, or no firewall in the tfstate (destroy
  leaves tfvars behind) → `runtime.isolate.not_provisioned`; a per-lab
  `variables.tf` without `egress_isolated` (lab provisioned before this
  feature) → `runtime.isolate.needs_reapply`. `connect` on such a lab is a
  no-op success ("nothing to restore").

### Checking isolation from inside a guest

The guest's own resolver cache (systemd-resolved) keeps answering names
it already resolved until their TTL runs out, so a "lookup of an outside
name must fail" check has to bypass it: `resolvectl flush-caches` first,
or `resolvectl query --cache=no <name>`. A send to `/dev/udp/...` or
`nc -u` "succeeds" whether or not anything arrives; probe UDP with a
request that needs an answer (e.g. `dig @<server>`).

### Threat model and known gaps

- The guest is assumed non-adversarial: the point is a non-fragile air
  gap for testing, not a jail. On libvirt the tcp sport 22 accept to the
  gateway is stateless, so a root guest could SNAT to source port 22 and
  reach host services on the gateway address.
- There is no cross-command lock: an `apply` and an `isolate` racing on
  the same lab (CLI + TUI) are not serialized. Nothing else in the CLI
  locks either.
- local-libvirt shares one global tofu state: applying another libvirt
  lab destroys the first one's domains and leaves its record and filter
  stale (`playground reset` / `connect` clean up).

## Cross-layer pitfalls (things future-you will hit)

These are the gotchas we've already paid for. Each one cost
~half a day of debugging the first time around.

### Library defaults are wrong for fresh state

Stock `ansible.cfg` has `host_key_checking=True`, no
`ControlMaster`, no `pipelining`. **Every** one of these fails
on a fresh VM with no entries in `~/.ssh/known_hosts`. Shipped
config is at `ansible/ansible.cfg`; the runner wires
`ANSIBLE_CONFIG` explicitly because Ansible's auto-discovery
looks at cwd, not at `ansible/`.

Lint via `playground doctor` — `runtime.doctor.ansible_cfg_*`.

### dmacvicar/libvirt's `dns {}` block needs explicit `enabled = true`

Without it, the provider's `getDNSEnableFromResource` returns
`"no"`, libvirtxml emits `<dns enable='no'>`, and libvirt
disables dnsmasq DNS entirely. The host records you populated
are silently ignored. **Always** set `enabled = true` inside
the `content` block when populating `hosts`.

### dmacvicar/libvirt + bridge networks need `qemu_agent = true`

We currently use NAT, so this is sidestepped. If we ever
migrate to bridge mode, the README is explicit: "set
`qemu_agent = true` or wait_for_lease hangs." Track via
`runtime.doctor.tofu_*` if/when added.

### Cloud-init has two boundaries, not one

"sshd listening" ≠ "cloud-init done". sshd comes up after the
Network stage (~30-90s on Noble); `package_upgrade` holds the
apt lock for another 1-3 minutes during the Final stage. If
ansible runs an `apt install` between those, it races the lock.
`wait-for-vms-ready` gates on `cloud-init status --wait` for
this exact reason.

### Hardcoded play in site.yml = hidden role-system bypass

site.yml MUST dispatch via `[needs_<provisioner>]` groups
derived from VmRole `provisioners`. Hardcoding
`roles: [docker, redroid]` on `hosts: playground` (as we did
historically) silently applies roles to every VM regardless of
VmRole, and creates implicit cross-layer dependencies that
fail mysteriously when refactored. Three plays may stay on
`hosts: playground` (extra_hosts, common, workload_*) because
they're truly universal — but every other role must be
provisioner-dispatched.

### VmRole `provisioners` is list-replace, not list-merge

A child role's `provisioners: [foo]` does NOT inherit the
parent's `provisioners: [bar]`. The resolver's
`_deep_merge_spec` explicitly list-replaces this field. Any
VmRole that extends another and needs the parent's
provisioners must re-list them — e.g., a custom role
extending `docker-host` must re-list `docker` itself or
docker won't be installed. (History: a hardcoded
`hosts: playground` play in site.yml used to install docker
universally, hiding this; removing the hardcode surfaced the
implicit dependency for VmRoles that extended `docker-host`
without re-listing.)

### AppArmor on Ubuntu: stock files don't prove virt-aa-helper works

`/etc/apparmor.d/libvirt/libvirt-qemu` ships on every libvirt
install — its presence proves nothing. The signal that
virt-aa-helper is broken is **orphan profiles**: files
matching `libvirt-<uuid>` (with a hyphen in the UUID portion)
without a sibling `.files` companion. Doctor's
`apparmor_orphan_profiles` check looks for exactly this.

`security_driver = "none"` in `/etc/libvirt/qemu.conf` is the
opt-out; it silences the doctor's apparmor check entirely.

### libvirt-qemu must traverse the pool path

libvirt-qemu (the user libvirtd runs domains as) needs read +
execute on the storage pool path AND every directory ancestor.
A pool inside `$HOME` with mode `0700` fails silently —
domains start but qemu can't read the disks. Doctor's
`pool_path_unreadable` check walks the ancestor chain.

### Tofu state is global, not per-lab

`tofu/terraform.tfstate` is a single shared file. `playground
apply <lab-B>` after a previous `apply <lab-A>` overwrites the
state to match lab-B. Concurrent labs on one host don't work
today. `playground reset` is the recovery path for state that
gets out of sync with reality.

### vbox: VirtualBox can't boot the qcow2 cloud image directly

The Ubuntu cloud image ships as qcow2, which VirtualBox doesn't read.
The vbox backend converts it to a VDI with `qemu-img convert` and clones
per-VM copies. `qemu-img` (apt: `qemu-utils`) is therefore a hard
dependency of the vbox path — `playground doctor` warns
(`runtime.doctor.qemu_img_missing`) when it's absent. The conversion is
cached, so it only happens on the first apply.

### vbox: reachability is 127.0.0.1:<port>, not a routable VM IP

With NAT + port-forward, every VM's SSH endpoint is `127.0.0.1` on a
distinct host port. The shared `wait`/`verify`/`inventory` code carries
an `ssh_port` (defaulting to 22, so libvirt is unaffected). `playground
status` shows `127.0.0.1` for vbox VMs — that's expected, not a bug.
VM-to-VM traffic does **not** go over NAT; it uses the per-network
intnet NICs with static IPs.

### vbox: a NoCloud network-config replaces the image default entirely

If the seed ISO contains a `network-config`, it fully supersedes the
cloud image's default netplan — so it must list **every** NIC (the NAT
NIC as DHCP included), or an omitted NIC comes up unconfigured. The
backend therefore omits `network-config` for NAT-only VMs (letting the
image default DHCP all NICs) and only emits it when there's an intnet
NIC needing a static IP. NICs are matched by MAC (no `set-name`) to
avoid depending on guest interface enumeration.

### Redroid needs binder, not nested virtualization

The recurring misstatement in this repo was that Redroid requires nested
virt. It does not. `binder` is a KERNEL MODULE
(`CONFIG_ANDROID_BINDER_IPC=m`), not a CPU feature, and no CPU-level
passthrough setting provides it. Redroid is a container: it needs `binder`
in `/proc/filesystems` on the kernel it runs on, plus a mounted binderfs.
`cpu { mode = "host-passthrough" }` in `tofu/main.tf` is unrelated to
binder — it is a PRD constraint that exposes host CPU virt extensions to
the guest for nested hypervisors and virt-accelerated workloads. Proven
live: Redroid runs on a DigitalOcean Droplet, which has no nested
virtualization at all (no libvirt guest, no `host-passthrough` in the
picture); the Ansible role loading the `binder_linux` module is what makes
it work, on any backend whose guest kernel can load that module.

On `local-libvirt`, Redroid runs inside a libvirt *guest*; that guest's own
kernel (whatever `cpu_mode` it boots with) is what the redroid role targets
— `host-passthrough` neither helps nor hinders binder. On a cloud VM there
is no nesting at all: Redroid runs directly on the instance kernel.

Ubuntu builds binder as a MODULE (`CONFIG_ANDROID_BINDER_IPC=m`,
`CONFIG_ANDROID_BINDERFS=m`) and ships it in
`linux-modules-extra-<kernel>`, which cloud images do not install. The
`redroid` Ansible role installs it for **every kernel in `/lib/modules`**,
loads `binder_linux` with `devices=binder,hwbinder,vndbinder`, and mounts
binderfs through a `redroid-binderfs.service` systemd unit. Set
`redroid_install_kernel_modules: false` to opt out on air-gapped hosts.

Verified live on DigitalOcean `ubuntu-24-04-x64` (2026-08-31): apply is
idempotent (`changed=0` on re-run), Redroid 11 boots
(`sys.boot_completed=1`), ADB works through `playground adb`, and the whole
stack survives a graceful reboot.

### Redroid image tags: pin the date-stamped one

`redroid/redroid:<version>-latest` is a MOVING tag -- upstream repoints it
on every new build of that version. A lab pinned to it is not reproducible:
two applies weeks apart can boot different Android builds from identical
committed config, and an app-test result cannot be attributed to a specific
build.

`ansible/roles/redroid/defaults/main.yml` pins the date-stamped tag
(`11.0.0-240527`) that `11.0.0-latest` resolved to on 2026-08-31 --
identical digest, so the pin changed nothing at the time it was made.
`tests/unit/ansible/test_redroid_image_pin.py` fails if a `-latest` tag is
reintroduced. Choose new versions from
https://hub.docker.com/r/redroid/redroid/tags, always a date-stamped tag.

### Two live-only failures this role exists to avoid

Both were invisible to static tests and to a first apply. They only
appeared on the first reboot.

**1. NEVER put binderfs in `/etc/fstab`.** This is an availability bug, not
a cosmetic one. `ansible.posix.mount: state: mounted` writes
`binder /dev/binderfs binder defaults 0 0`. Systemd attempts fstab mounts at
`local-fs.target` — *before* `systemd-modules-load.service` has loaded
`binder_linux` — and `/dev` is a devtmpfs, so the mountpoint does not exist
yet either. The mount fails, the boot degrades to `maintenance`, and **sshd
never starts**: the VM is unreachable until a hard power-cycle
(`doctl compute droplet-action power-cycle`). `redroid-binderfs.service`
does the same work ordered `After=systemd-modules-load.service` and
`Before=docker.service`. The role also runs
`ansible.posix.mount: state: absent_from_fstab` to clean up hosts that
already carry the bad entry. A unit test
(`test_binderfs_is_never_persisted_in_fstab`) fails if anyone reintroduces
it.

**2. Kernel/package skew is real, not theoretical.** DigitalOcean's
cloud-init sets `package_upgrade: true`, which stages a NEWER kernel than
the one running at apply time. Observed: applied on `6.8.0-124-generic`,
rebooted into `6.8.0-138-generic`, and
`modprobe: FATAL: Module binder_linux not found in /lib/modules/6.8.0-138-generic`.
Ubuntu cloud images ship `linux-image-virtual`, which deliberately carries
no modules-extra. Installing for the running kernel alone is therefore not
enough — the role loops over every directory in `/lib/modules`. Per-kernel
installs are best-effort (`failed_when: false`); the binder re-probe is the
real gate for the running kernel.

**ADB has no authentication.** The redroid container is run with
`ports: ["5555:5555"]`, which binds `0.0.0.0:5555` inside the guest —
that is exposure at the guest-network level regardless of backend, and
this doc is not the place to claim it never happens. What varies is who
can *reach* that guest address:

- `cloud-digitalocean`: the firewall (`tofu/cloud_digitalocean/main.tf`)
  opens port 22 only, so 5555 is unreachable from the public internet —
  the Droplet's own guest-level bind is mitigated entirely by the cloud
  firewall.
- `local-libvirt`: there is no firewall layer in front of the NAT network
  (10.0.10.0/24) — the libvirt host and every other VM on that NAT network
  can reach `<vm-ip>:5555` directly. Do not put a `redroid-host` lab on a
  shared or untrusted host network.
- `local-vbox`: NAT + port-forward means 5555 is not forwarded to the host
  by default (only the SSH port is), but VMs on the same internal network
  NIC can still reach it guest-to-guest.

In all cases, prefer `playground adb --lab <lab> --on <vm>` (forwards a
local port over SSH) or guest-side `adb` over relying on 5555 being
unreachable.

### vbox: Redroid support is UNVERIFIED, not proven-impossible

`config/providers/local-vbox.yaml` records `redroid: false`, but per
"Redroid needs binder, not nested virtualization" above, the honest reason
is that nobody has tested it — not that VirtualBox is architecturally
incapable. Redroid only needs the guest kernel to load `binder_linux` (via
`linux-modules-extra-<kernel>`, same as any other guest) and to run a
`--privileged` container; neither of those is inherently blocked by
VirtualBox. `nested_virtualization: false` for vbox is accurate on its own
terms (VBox doesn't expose KVM nested-virt to guests), but it is not the
reason Redroid is disabled — do not conflate the two capabilities.

Until someone runs the redroid role against a `local-vbox` guest and
confirms binder loads and the container starts, treat `redroid: false`
there as "untested," and do not flip it to `true` without a live
verification note (in the style of the DigitalOcean one above). Generic VM
+ Docker labs remain the supported, verified vbox use case.

### libvirt: nested-virt fails when L0 refuses VMX passthrough

When this L1 host is itself inside an L0 hypervisor that doesn't
permit nested VMX, the playground guest starts and immediately pauses
with `paused (unknown)` and `kvm_intel: vmread/vmwrite failed` in
dmesg. The misleading top-level symptom is tofu's `wait_for_lease`
timing out after 5 minutes. The escape hatches are
`spec.providers.local-libvirt`'s `cpu_mode` + `cpu_features_disable`
(rung 1) and `domain_type: qemu` (rung 2; TCG software emulation).

See [`nested_virtualization.md`](nested_virtualization.md) for the
escalation ladder, symptom → rung mapping, and how to verify each
knob landed.

### cloud-digitalocean: token is env-only — never HCL, tfvars, logs, or diagnostics

The `DIGITALOCEAN_TOKEN` value must never appear in any `.tf` file,
`tfvars.json`, log event, Diagnostic message, or run record. The
`provider "digitalocean" {}` block has no `token =` field — the
provider reads it from the environment automatically. The
`render_do_tfvars` key-allowlist (`_TFVARS_KEYS`) enforces this on
the Python side; a unit test asserts the allowlist equals the
variables declared in `variables.tf`. When adding new provider-config
keys, add them to `_TFVARS_KEYS` only if they belong in `variables.tf`
— never add a key that carries secret material.

### cloud-digitalocean: tofu state is per-lab, not global

Unlike local-libvirt (single `tofu/terraform.tfstate`), cloud state lives
under `.playground/state/cloud-digitalocean/<lab>/terraform.tfstate`. Each
lab is isolated: `playground apply lab-A` and `playground apply lab-B` can
run on the same machine without overwriting each other's state. If you move
or rename the per-lab directory the state is lost and `tofu apply` will try
to create all resources again — use `playground reset` to wipe and start
clean.

### cloud-digitalocean: suspend≠power-off; the tag sweep is the safety net

DigitalOcean bills powered-off Droplets at the same rate as running ones.
`playground suspend` therefore runs `tofu destroy` (full deletion), not a
power-off. After destroy, the tag sweep (`list → delete → re-list`) catches
any Droplets the provider missed (partial-apply, provider bug, race).
If survivors remain, `execute_suspend` / `execute_destroy` exit `status=failed`
with `runtime.<operation>.orphaned_resource` diagnostics and the console URL.
**Never paper over survivor diagnostics with a no-op** — stranded Droplets
accrue charges silently.

### cloud-digitalocean: `vm_ips` output shape is a cross-layer contract

`tofu output -json` must emit `vm_ips` as a flat `{vm_name: ipv4_string}` map
(e.g. `{"node1": "203.0.113.10"}`). `fetch_vm_ips` in `inventory.py` extracts
`data["vm_ips"]["value"]` and validates every key and value is a string. If
`outputs.tf` changes this shape (e.g. wraps it in a nested object or renames
`vm_ips`) `fetch_vm_ips` will return `({}, [diagnostic])`, and apply will fail
at the "fetch-vm-ips" step with a clear error. Always keep `outputs.tf`'s
`vm_ips` shape in sync with `fetch_vm_ips`.

## When to update this doc

- A new step is added to `execute_apply` (write its contract here).
- A new diagnostic ID prefix is introduced (link from the table
  in `docs/system_overview.md`).
- A new verb mutates per-lab state outside a backend (the isolation
  record is the model: dispatch owns it, backends do not).
- A new third-party tool joins the pipeline (record its defaults
  and gotchas under the pitfalls section).
- A bug surfaces that fits the recurring pattern but isn't on
  the pitfalls list — add it.
