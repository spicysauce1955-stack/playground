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
provisioners must re-list them. `deployment-source` and
`deployment-target` re-list `docker` for exactly this reason
(history: implicit-via-hardcoded site.yml bit us when we
removed the hardcode).

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

## When to update this doc

- A new step is added to `execute_apply` (write its contract here).
- A new diagnostic ID prefix is introduced (link from the table
  in `docs/system_overview.md`).
- A new third-party tool joins the pipeline (record its defaults
  and gotchas under the pitfalls section).
- A bug surfaces that fits the recurring pattern but isn't on
  the pitfalls list — add it.
