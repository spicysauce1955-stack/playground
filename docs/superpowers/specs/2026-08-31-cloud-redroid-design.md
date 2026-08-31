# Redroid on cloud backends

**Date:** 2026-08-31
**Status:** Approved design, not yet implemented

## Problem

`redroid-host` labs only run on `local-libvirt`. Operators who want an
ADB-reachable Android instance must have a local KVM host with the right
kernel. The request is to run Redroid on a cloud VM like any other lab,
starting with `cloud-digitalocean`.

The repo's stated reason for the restriction is nested virtualization —
`PRD.md`, `docs/product/mvp_scope.md`, and `preflight/doctor.py` all tie
Redroid to nested-virt support. That reason is wrong, and correcting it is
part of this change.

## What Redroid actually requires

Redroid is a container. It needs `binder` in the host kernel's
`/proc/filesystems` and a mounted binderfs. It does not need KVM, VMX, or
nested virtualization.

Nested virt matters only on `local-libvirt`, and only indirectly: Redroid
runs inside a libvirt *guest*, so that guest's kernel must expose binder,
which is why `tofu/main.tf` uses `cpu { mode = "host-passthrough" }`. On a
cloud VM there is no nesting — Redroid runs directly on the instance kernel,
so the requirement is simply "the instance kernel can provide binder".

### Live probe, 2026-08-31

Verified on a throwaway DigitalOcean Droplet (`ubuntu-24-04-x64`,
`s-2vcpu-4gb`, nyc3, kernel `6.8.0-124-generic`), then destroyed:

| Step | Result |
|---|---|
| `binder` in `/proc/filesystems` on the fresh image | absent |
| `apt install linux-modules-extra-$(uname -r)`, `modprobe binder_linux` | loads |
| `mount -t binder binder /dev/binderfs` | ok (`binder`, `hwbinder`, `vndbinder`, `binder-control`) |
| `redroid/redroid:11.0.0-latest`, `--privileged`, `-p 5555:5555` | runs |
| `sys.boot_completed` | `1`, within 10s of container start |
| `adb` from the operator's laptop to `<public-ip>:5555` | `redroid11_x86_64`, Android 11, x86_64, 142 packages |

Two findings from the probe shape the design:

- Ubuntu Noble builds binder as a module (`CONFIG_ANDROID_BINDER_IPC=m`,
  `CONFIG_ANDROID_BINDERFS=m`) and ships it in `linux-modules-extra`, which
  the cloud image does not install. One `apt install` closes the gap.
- `ashmem_linux` does not exist on 6.8 — it was removed from staging in
  kernels >= 5.18, and Redroid uses memfd instead. The existing role's
  `ignore_errors: true` on that modprobe is already correct.

During the probe, port 5555 was reachable from the public internet with no
authentication, yielding a device shell to anyone who connected. That
observation drives the access decision below.

## Decisions

1. **Binder remediation lives in the existing `redroid` Ansible role**, not
   in a new role and not in DO cloud-init. Keeps one backend-neutral code
   path (principle 5) and avoids touching
   `tofu/cloud_digitalocean/cloud_init.cfg`, which silently discards itself
   on a single non-ASCII byte.
2. **ADB is reached over an SSH tunnel only.** The DO firewall stays
   SSH-only. ADB has no authentication, so it is never exposed publicly and
   no `adb_cidrs` knob is added.
3. **Capability mismatches warn, they do not block** (principle 10).

## Design

### 1. `ansible/roles/redroid` — self-healing binder

Probe, remediate, re-probe, then fail:

1. `grep -w binder /proc/filesystems` (existing task, unchanged).
2. When absent and `redroid_install_kernel_modules` (new default, `true`):
   install `linux-modules-extra-{{ ansible_kernel }}`.
3. `modprobe binder_linux devices=binder,hwbinder,vndbinder`.
4. Persist across reboot: `/etc/modules-load.d/binder.conf` for the module
   and `/etc/modprobe.d/binder.conf` for the `devices` parameter.
5. Re-probe. On continued failure, emit today's abort message naming the
   VmRole opt-out, extended to state that modules-extra was already tried.

`ashmem_linux` keeps its best-effort `ignore_errors: true`. The binderfs
mount already persists via `ansible.posix.mount: state: mounted`, which
writes fstab; the modprobe persistence in step 4 is new and fixes a latent
gap on every backend.

Idempotency (principle 7) comes from probing first: a kernel that already
exposes binder performs no work. `redroid_install_kernel_modules: false` is
the air-gap opt-out (principle 8), for labs whose packages come from a local
mirror or whose kernel already has binder built in.

`ansible_kernel` is available without a `setup` call — the `extra_hosts` play
in `site.yml` gathers facts for all hosts before any role runs.

### 2. Provider capabilities become load-bearing

VmRole capabilities are already consumed (`planner/scheduling.py:90` reads
`capabilities['docker']` to pick a swarm manager). The `capabilities:` blocks
in `config/providers/*.yaml` are currently declarative only — nothing in
`src/` reads them.

Add a validator check, `_check_role_capabilities_vs_provider`: for each
resolved VM, any capability the role declares truthy that the selected
provider config **explicitly declares false** emits a warning,
`config.backend.capability_unsupported`, naming the VM, the capability, and
the backend. A capability the provider does not mention emits nothing — the
map is open, and undeclared is not the same as unsupported.

Provider configs then state the truth:

- `cloud-digitalocean.yaml` gains `redroid: true`, and keeps
  `nested_virtualization: false`. That pairing is the correction: Redroid
  works there precisely because it never needed nested virt.
- `local-vbox.yaml` gains `redroid: false`, so a redroid lab on VirtualBox
  warns at validate time instead of failing the binder assertion partway
  through an apply (`docs/architecture/CONTRACTS.md:509`).

### 3. `config/labs/redroid-cloud.yaml`

A committed example lab: one VM, `role: redroid-host`, `backend:
cloud-digitalocean`, with `spec.providers.cloud-digitalocean.size:
s-4vcpu-8gb`.

The DO backend applies a single global `size` to every Droplet — `size` is
one scalar in the `_TFVARS_KEYS` allowlist in
`src/playground/backend/cloud_digitalocean/tfvars.py`, not a per-VM value.
So the role's `resources:` block (4 vCPU / 8 GB / 60 GB) is advisory on this
backend and the size slug is the real control. `_check_backend_capability`
in the validator, which today warns about heterogeneous per-VM resources on
`local-libvirt` only, is extended to make the same point for
`cloud-digitalocean`.

### 4. `playground adb --lab L --on VM`

A new CLI verb that resolves the VM's public IP from project-local state,
opens a foreground `ssh -L <port>:127.0.0.1:5555 ubuntu@<ip>`, and prints the
matching `adb connect 127.0.0.1:<port>` line. On local port collision it
selects the next free port and reports the one it chose. The tunnel lives for
as long as the command runs; Ctrl-C closes it.

No firewall change: `tofu/cloud_digitalocean/main.tf` continues to open only
port 22.

### 5. Corrected framing

- `preflight/doctor.py:236,261` — the nested-virt warning stops naming
  `redroid-host` as its justification. The check remains valid for
  libvirt guests needing passthrough; only the stated reason changes.
- `docs/architecture/CONTRACTS.md` — new section stating what Redroid
  requires (binder module plus binderfs, not nested virt), which backends
  provide it, and the kernel-skew failure mode below.
- `docs/product/mvp_scope.md` — an update note that cloud Redroid landed,
  matching the existing `cloud-digitalocean` note.
- `CLAUDE.md` — the Redroid bullet updated to describe binder rather than
  nested virt.

## Testing

- **Unit:** the capability-mismatch check (match, explicit-false, absent);
  `redroid-cloud.yaml` loads and resolves; `_TFVARS_KEYS` unchanged.
- **Ansible:** `--check` against a live host; second full run asserts no
  changed tasks (principle 7).
- **Live on DigitalOcean:** `playground apply redroid-cloud`, tunnel via
  `playground adb`, assert `sys.boot_completed == 1`, re-run `apply` for
  idempotency, `playground destroy`, then confirm via `doctl` that no
  Droplets or firewalls remain. The DO backend has a history of bugs that
  only appear live.
- **Regression:** confirm the `local-libvirt` redroid path still works, and
  determine whether it was already broken (see the open question below).

## Risks

**Kernel/package skew is the primary risk.** `linux-modules-extra` must match
the running kernel exactly, and DO's cloud-init sets `package_upgrade: true`.
If that upgrade installs a newer kernel, the running kernel's modules-extra
package may be unavailable in the archive, or the newly installed modules may
belong to a kernel that requires a reboot. The probe did not hit this.
Mitigation: always target the running kernel via `ansible_kernel`, and on
failure abort with both versions named rather than proceeding to a
non-functional container.

**Cost.** `s-4vcpu-8gb` is roughly $0.07/hr. Teardown verification is part of
the test plan, not an afterthought.

**Image drift.** A future Ubuntu image that builds binder differently, or a
non-Ubuntu image, breaks the apt-based remediation. The abort message names
the opt-out, so the failure is legible rather than silent.

## Open question to resolve during implementation

Ubuntu Noble ships binder in `linux-modules-extra`, and `local-libvirt`
guests boot the same Noble cloud image. The current `redroid` role never
installs that package, which suggests the role may already be failing its
binder assertion on `local-libvirt` — meaning this change fixes an existing
break rather than only adding a backend. This is a hypothesis from the DO
probe, not a verified claim; verify it on a libvirt guest early, because a
confirmed answer changes how the change is described and may warrant its own
note in `docs/roadmap.md`.

## Live validation, 2026-08-31

Run against DigitalOcean `s-4vcpu-8gb` / `ubuntu-24-04-x64` in nyc3, Droplet
`redroid-cloud-droid1`, destroyed afterwards.

| Step | Result |
|---|---|
| `playground validate` / `plan` | 0 errors, 0 warnings from `redroid-cloud`; cost estimate ~$0.0714/hr |
| `playground apply redroid-cloud` | exit 0; every pipeline step green (cloud-preflight, tofu-init, tofu-apply, wait-for-vms-ready, ansible-playbook, verify-lab) |
| redroid role, first apply | probe found binder missing -> installed modules-extra -> loaded `binder_linux` -> re-probe passed -> abort tasks skipped. `ashmem_linux` failed and was ignored, correct on kernel 6.8 |
| Android boot via `playground adb` | `sys.boot_completed=1`, `ro.product.model=redroid11_x86_64`, 142 packages |
| Port 5555 from the public internet | filtered (port 22 open) — the tunnel-only decision holds |
| Second apply (idempotency, principle 7) | `changed=0` |
| **Graceful reboot (first attempt)** | **FAILED — see below** |
| Graceful reboot after fix | ssh back in 40s, `is-system-running=running`, 0 failed units, binder present, binderfs mounted, container up, `sys.boot_completed=1` in ~5s |

### Two bugs found, both reboot-only

Neither appeared in static tests, in the first apply, or in the idempotency
re-run. Both are now covered by regression tests in
`tests/unit/ansible/test_redroid_binder_remediation.py`.

**Bug 1 — binderfs in `/etc/fstab` bricks the boot (high severity).**
`ansible.posix.mount: state: mounted` wrote
`binder /dev/binderfs binder defaults 0 0`. Systemd attempts fstab mounts at
`local-fs.target`, before `systemd-modules-load.service` loads
`binder_linux`, and `/dev` is a devtmpfs so the mountpoint is gone at boot.
The mount failed, the boot degraded to `maintenance`, and sshd never
started — the Droplet was unreachable until a hard power-cycle. Removing
that single line restored a clean `running` boot, which isolated it from
Bug 2.

This line predated this work; the change to the role is what made it
reachable, because previously the role aborted at the binder assertion
before ever mounting.

*Fix:* a `redroid-binderfs.service` unit ordered
`After=systemd-modules-load.service` / `Before=docker.service` performs
modprobe -> mkdir -> mount, and is enabled so it runs every boot. The role
now uses `ansible.posix.mount` only as `state: absent_from_fstab`, to clean
up hosts already carrying the bad entry.

**Bug 2 — kernel/package skew, the risk this spec predicted.** Applied on
`6.8.0-124-generic`; cloud-init's `package_upgrade: true` had staged
`6.8.0-138-generic`, which the reboot booted into:
`modprobe: FATAL: Module binder_linux not found in /lib/modules/6.8.0-138-generic`.
Ubuntu cloud images ship `linux-image-virtual`, which carries no
modules-extra for any kernel.

*Fix:* install `linux-modules-extra-<kernel>` for every directory in
`/lib/modules` rather than for `ansible_kernel` alone. Per-kernel installs
are best-effort; the binder re-probe remains the gate for the running
kernel. Confirmed live: the loop reported `ok` for `6.8.0-124-generic` and
`changed` for `6.8.0-138-generic`.

### Correction to this spec's earlier text

The design section originally described persistence as
"`/etc/modules-load.d/` and `/etc/modprobe.d/`". Those files are still
written, but they are not sufficient on their own — `modules-load.d` cannot
run early enough to satisfy an fstab mount, and it silently no-ops when the
booted kernel has no matching module. The systemd unit is what actually
makes the stack reboot-safe.
