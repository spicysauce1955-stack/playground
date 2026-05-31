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

reset:
  tofu-destroy  → tag-sweep → clean-state-files
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

### vbox: no nested virt → Redroid won't work there

The libvirt path uses `cpu { mode = "host-passthrough" }` so guests get
binderfs for Redroid. VirtualBox doesn't pass that through, so a
`redroid-host` lab on `local-vbox` will fail the binder assertion.
Generic VM + Docker labs are the supported vbox use case
(`config/providers/local-vbox.yaml` records `nested_virtualization:
false`).

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
- A new third-party tool joins the pipeline (record its defaults
  and gotchas under the pitfalls section).
- A bug surfaces that fits the recurring pattern but isn't on
  the pitfalls list — add it.
