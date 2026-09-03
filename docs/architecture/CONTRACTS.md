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
- Provisioning (the `capture` Ansible role) installs `tcpdump`, the
  wrapper, and the instanced unit, and converges to unit
  enabled-but-**stopped**. It never starts, stops, or restarts a
  session — `playground apply` on a lab that is mid-capture leaves the
  recording untouched. Sessions start only via `playground capture
  start`.
- `spec.capture.enabled: false` does not skip the `needs_capture` play —
  the host is still a member of that group. It skips *inside* the role,
  via its own `ansible.builtin.meta: end_host` guard, evaluated right
  after `pg_capture` is parsed and before the `tcpdump` package task
  runs. Net effect is the same (no package, no unit) but the skip point
  is the role's guard, not group membership — a detail that matters if
  you ever go looking for a `when:` on the play itself and don't find
  one.
- `local-vbox` is excluded by construction, not by a runtime check on
  that backend: `capture_vm_names()` only returns VMs whose role
  declares `capabilities.capture: true`, and `redroid-host`'s capture
  capability is meaningless without a Redroid container to attach
  to — `local-vbox`'s `ProviderConfig` declares `redroid: false`
  because Redroid is unverified there. Targeting also short-circuits on
  `resolved.backend` before ever asking about capabilities: any lab on
  `local-vbox` (or any future non-Redroid backend) gets
  `config.capture.backend_unsupported` naming the two backends that do
  support it.

### Two Ubuntu tcpdump defaults that break a fresh guest

Both are the recurring "library default wrong for fresh state" shape.

1. **Privilege drop.** Debian/Ubuntu `tcpdump` `setuid()`s to the
   unprivileged `tcpdump` user unless told otherwise, and then cannot
   write into the root-owned capture directory. `-Z root` is required.
   (The capture directory itself is `0755`, not `0750` — see below —
   but ownership is still `root:root`, and the untold `tcpdump` user has
   no write access to it either way.)
2. **AppArmor confinement.** Ubuntu ships
   `/etc/apparmor.d/usr.sbin.tcpdump`, which confines where `tcpdump`
   may write; `/var/lib/playground/` is outside it. The failure
   presents as `Permission denied` on a directory whose ownership and
   mode look correct. The shipped profile ends with
   `#include <local/usr.sbin.tcpdump>`, so the role writes a local
   override at `/etc/apparmor.d/local/usr.sbin.tcpdump` and reloads with
   `apparmor_parser -r` (tolerated with `failed_when: false` — a host
   with AppArmor disabled has no profile to reload, and that is not a
   capture failure). Do not disable confinement instead.

The capture directory is created `0755`, not `0750`: tcpdump's savefiles
land as `0644 root:root` regardless of the parent directory's mode, so a
tighter parent would block traversal while the files underneath stay
world-readable anyway. `capture status`'s `find` calls and `capture
fetch`'s `scp` are deliberately unprivileged — only `start`/`stop`/
`clean` go through `sudo -n` — so the unprivileged SSH user genuinely
needs to traverse this directory to read anything back.

### `-C` semantics are inherited, and the ceiling is per session-start

`max_file_mb` maps to `tcpdump -C`, which counts units of **1,000,000
bytes, not MiB**. `-C` with `-W` is a **ring within one running
tcpdump**: the oldest file is overwritten once `max_files` is reached,
so a session that outlives its budget keeps the most recent
`max_file_mb * max_files` and discards the beginning.

That ceiling is **per session-start, not a global cap on the
directory**. Two facts compound: `tcpdump` applies `strftime()` to `-w`
only when `-G` is set — under `-C`/`-W` alone it writes the name
verbatim and appends a bare rotation counter (verified live: `-w
'%Y%m%d-%H%M%S.pcap' -C 1 -W 3` produced a file literally named
`%Y%m%d-%H%M%S.pcap0`) — so the wrapper computes the timestamp itself
with `date -u` before invoking `tcpdump`, and `-W`'s ring counts files
*per base name*. Every unit start (including every Redroid container
restart, since `Restart=always` re-execs the wrapper) mints a new
stamp, hence a new base name, hence a new ring. N restarts therefore
permit up to N × (`max_file_mb` × `max_files`) on disk, not one fixed
ceiling. This was a deliberate trade, not an oversight: one fixed
filename would keep a true global cap, but would silently TRUNCATE the
prior session's data on every container restart — losing data and
hiding that it was lost. `capture status` reports the file count so a
wrapping (or multiplying) session is visible. Because the on-disk name
is always `<stamp>.pcap<N>`, every CLI glob that touches these files is
`*.pcap*`, never `*.pcap` — a bare `*.pcap` glob matches nothing that
tcpdump actually wrote.

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
- A new third-party tool joins the pipeline (record its defaults
  and gotchas under the pitfalls section).
- A bug surfaces that fits the recurring pattern but isn't on
  the pitfalls list — add it.
