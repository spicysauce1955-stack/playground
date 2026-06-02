# Playground bugs / issues found during barak-deploy cross-VM testing

Logged by an external agent (read-only on playground code) while driving the playground CLI
to provision VMs across all three backends (local-libvirt, local-vbox, cloud-digitalocean)
for barak-deploy's cross-VM ship-and-deploy tests. **The agent did not edit playground code**
— each item below is for the playground maintainer to triage and fix.

Environment: this host has /dev/kvm, virsh, VBoxManage, OpenTofu, ~21 GiB free RAM.
playground installed editable (`uv pip install -e .`), `playground` symlinked onto PATH.

Severity legend: **BUG** (incorrect behavior), **PAPERCUT** (works but surprising/awkward),
**NOTE** (observation / possible enhancement).

### Status as of 2026-06-01 (re-tested after your fixes)
- **BUG-1** — ✅ FIXED & verified: `plan vbox-smoke` no longer leaks the generic-infra warning
  (whole-config commands like `lab list` still show it, which is correct).
- **NOTE-2** — fixed per your commit (not re-exercised on vbox this round).
- **BUG-3** — ✅ provisioning FIXED & verified: `apply generic-infra` now brings all 3 VMs up
  with IPs and `status` reports `provisioned 3/3` (no wait-for-IP timeout, no orphans).
- **NOTE-3** — by design (suspend/resume cloud-only).
- **BUG-4** — ⚠️ **NEW / OPEN**: with the provisioning fixed, `apply generic-infra` now fails
  one step later — the VMs have **no working DNS**, so ansible's `apt update` fails. Details below.

### Status as of 2026-06-02 (re-tested again, after `git pull` — tree already up to date)
- **BUG-4 (DNS)** — ✅ **now LIVE-CONFIRMED FIXED**: `apply generic-infra` got past the apt
  stage on all 3 VMs this round (`docker1/node1/router1` all `apt`/Docker-installed; docker1
  reached `ok=12 changed=5`). No more `Failed to update apt cache`. The dnsmasq DNS fix works
  end-to-end.
- **BUG-6 (from_json)** — ⚠️ **OPEN**: re-confirmed still failing on docker1 (the only host
  with a workload). This is now the sole remaining `apply generic-infra` failure.
- **BUG-7 (exec quoting)** — 🆕 new, see below.
- **PAPERCUT-4 / PAPERCUT-5 / NOTE-6** — 🆕 new (exec `--on` flag, stale tofu-lock recovery,
  cloud-401 UX), see below.
- **cloud-digitalocean** — could not exercise: `apply cloud-smoke` returns DigitalOcean
  **`401 Unable to authenticate`**. The token is well-formed (`dop_v1_…`, 71 chars) but
  rejected → expired/revoked on our side, **not a playground bug**. No droplet was created
  (auth fails before creation), so no cost. See NOTE-6 for the UX ask.

> A consolidated, prioritized version of the 2026-06-02 items (with "what I need") is in
> **`PLAYGROUND-REQUEST-barak-deploy.md`** in this repo root.

### Verification 2026-06-02 (pulled your fix commits 4360317…07bbbc8, re-checked each item)
- **BUG-7 (exec quoting)** — ✅ **VERIFIED FIXED**: `exec --on docker1 -- bash -lc 'rm -rf
  /tmp/x && mkdir -p /tmp/x && echo OK:$(ls -d /tmp/x)'` now runs correctly (`OK:/tmp/x`); no
  more `rm: missing operand`.
- **PAPERCUT-4 (`--host` alias + `playground cp`)** — ✅ **VERIFIED FIXED**: `exec --host
  docker1` works; `playground cp local generic-infra:docker1:/path` copied a file and it read
  back correctly off the VM.
- **Deterministic exit code** — ✅ **VERIFIED FIXED**: a failed `apply generic-infra` now exits
  `1` (confirmed without pipe-masking).
- **Per-VM warning demotion** — ✅ **VERIFIED FIXED**: `status generic-infra` now prints 0
  `per_vm_resources_unsupported` lines (was spamming every command).
- **NOTE-6 (cloud preflight)** — ✅ **partially verified**: `apply cloud-smoke` with no
  `$DIGITALOCEAN_TOKEN` now fails fast and friendly (`runtime.cloud.token_missing` + a
  regenerate-link suggestion) and exits `1`, before any tofu. ⚠️ I could NOT verify the
  *invalid-token* path (the authed `/v2/account` call hung on my host's network); please
  confirm a present-but-rejected token yields a fast friendly 401/403 with a timeout.
- **PAPERCUT-5 (stale-lock auto-recovery)** — fix commit present (`bfb6ccb`); not live-tested
  (didn't re-induce a stale lock).
- **BUG-6 (from_json)** — ✅ **the reported error is GONE**: the `Parse pg_workloads JSON
  payload` task now succeeds (docker1 reached `ok=16 skipped=1`, no `from_json` traceback).
  **BUT** it exposed a new failure one step later — see **BUG-8**. So `apply generic-infra`
  still fails overall, just for a different reason now.

---

## BUG-1 — config warnings from one lab leak into unrelated lab operations

**Severity:** BUG (noisy/misleading, not fatal)
**Found:** during `playground plan` across labs.

The `config.backend.per_vm_resources_unsupported` WARNING for `generic-infra` is emitted on
**every** CLI invocation regardless of which lab is targeted. Repro:

```bash
playground plan vbox-smoke
# prints, before the vbox-smoke plan:
#   WARNING config.backend.per_vm_resources_unsupported: lab 'generic-infra' declares
#   heterogeneous per-VM resources ... at config/labs/generic-infra.yaml ...
```

`playground plan barak-deploy-cross-vm`, `playground lab list`, etc. all print the same
`generic-infra` warning. Expected: warnings should be scoped to the lab being operated on
(or, for whole-config commands like `lab list`, attributed clearly per-lab). As-is an
operator working on `vbox-smoke` or `cloud-smoke` sees a warning about a different lab on
every command, which trains them to ignore warnings.

Likely cause: the CLI validates/loads the entire `config/labs/` tree up front and surfaces
all collected warnings globally instead of filtering to the requested lab.

---

## NOTE-2 — `status --output json` omits SSH/connection details for local-vbox

**Severity:** NOTE / PAPERCUT (cross-backend inconsistency)
**Found:** during local-vbox lifecycle testing (vbox-smoke).

`playground status vbox-smoke --output json` returns per-VM `{name, role, state}` but **no
reachable address**. The local-vbox VM is reached via a NAT SSH port-forward (here
`127.0.0.1:2222`, found only via `VBoxManage showvminfo ... | grep Forwarding`). By
contrast the local-libvirt status exposes VM IPs (the barak-deploy cross-VM harness reads
them from status JSON to SSH/SCP).

Consequence: a backend-agnostic consumer (like barak-deploy's cross-VM test, which
discovers targets from `status --output json`) cannot reach a vbox VM — it would need to
shell out to `VBoxManage` for the forwarded port. Consider surfacing a uniform connection
field (e.g. `ssh_host`/`ssh_port`, or `address`) across backends so `status` is a complete,
backend-neutral source of truth. (The vbox-smoke lab itself provisions correctly: VM boots,
ansible installs Docker, `docker info` works over SSH on port 2222.)

---

## BUG-3 — shipped lab `generic-infra` fails to apply (tofu wait-for-IP timeout on isolated/routed nets)

**Severity:** BUG (a committed example lab does not apply out of the box)
**Found:** during multi-network topology testing.

`playground apply generic-infra` fails. The lab declares three networks — `edge` (nat),
`lab-private` (**isolated**), `routed-a` (**routed**) — and the tofu libvirt provider's
`libvirt_domain` wait-for-IP times out for the VMs whose interfaces sit on the
isolated/routed networks (no DHCP lease / no gateway):

```
Error: couldn't retrieve IP address of domain id: ... context deadline exceeded
  with libvirt_domain.playground_node[1] / [2]
tofu apply failed; no VMs provisioned   →   apply exit 1
```

Two sub-issues:
1. **Apply fails** for the multi-network topology. The single-nat-network labs
   (`barak-deploy-cross-vm`) apply fine; the failure is specific to the isolated/routed
   networks where the provider can't get a DHCP IP. Either the lab shouldn't wait for IPs on
   non-DHCP interfaces, or the libvirt module needs `qemu-guest-agent` + agent-based IP
   discovery (the provider error itself suggests installing qemu-agent).
2. **Orphaned running domains on failure.** When tofu aborts mid-apply, the 3 `libvirt_domain`s
   are left **running** in libvirt while tofu state records "no VMs provisioned", so
   `playground status` correctly shows `0 of 3 provisioned` even though `virsh list` shows all
   3 running. GOOD: `playground destroy generic-infra` *does* clean these orphans up (verified
   — `virsh list` empty afterward), so it's recoverable; but a failed apply silently leaving
   running VMs is surprising. Consider rolling back created domains on apply failure, or
   warning the operator that orphans exist and `destroy` is needed.

## NOTE-3 — suspend/resume are cloud-only (local backends reject them)

**Severity:** NOTE (by design; worth documenting prominently)

`playground suspend generic-infra` (local-libvirt) returns:
`ERROR runtime.backend.verb_not_supported: backend 'local-libvirt' does not support 'suspend'
(only cloud backends do)`. Same for local-vbox presumably. So suspend/resume can only be
exercised against `cloud-digitalocean` (where they map to powering a Droplet off/on). Fine as
a design choice — just noting that the new suspend/resume verbs are untestable on the two
local backends, and on cloud a suspended Droplet still incurs storage cost.

---

## BUG-4 — generic-infra VMs come up with no working DNS → ansible `apt update` fails

**Severity:** BUG (apply fails at the ansible stage; lab unusable out of the box)
**Found:** 2026-06-01, re-testing generic-infra after the BUG-3 fix.
**Status:** FIXED — `tofu/main.tf` now emits `dns { enabled = true }` on every
libvirt network unconditionally (was only emitted when the lab pinned IPs, so
the dmacvicar provider defaulted to `<dns enable='no'>` and dnsmasq served no
DNS). dnsmasq now forwards to the host resolver and advertises itself as the
guest DNS server via DHCP, so systemd-resolved gets a working upstream. Guard
test: `tests/unit/backend/local_libvirt/test_main_tf_network_dns.py`. Needs a
live `apply generic-infra` to confirm end-to-end.

GOOD: the BUG-3 fix works — `playground apply generic-infra` now **provisions all 3 VMs with
IPs** and `status` correctly reports `provisioned 3/3` (no wait-for-IP timeout, no orphan
mismatch). But the apply now fails at the **ansible** stage:

```
fatal: [docker1]: FAILED! => Failed to update apt cache: unknown reason
fatal: [node1]:   FAILED! => Failed to update apt cache: unknown reason
fatal: [router1]: FAILED! => Failed to update apt cache: unknown reason
VMs were provisioned but Ansible configuration failed.
```

Root cause: **broken DNS on the VMs**. On docker1 (on the `edge`/nat network, 10.20.10.250):
- internet by IP works: `ping -c1 8.8.8.8` → 0% loss.
- but `/etc/resolv.conf` points at the systemd-resolved stub `nameserver 127.0.0.53` with **no
  working upstream**, so every name fails: `Temporary failure resolving 'archive.ubuntu.com'`.
- Manually `echo "nameserver 8.8.8.8" > /etc/resolv.conf` immediately fixes resolution.

So the nat network gives connectivity but the guests' resolver has no upstream nameserver
(libvirt dnsmasq DNS isn't wired into systemd-resolved, or cloud-init didn't set one). The
isolated/routed VMs (node1, router1) likely have no egress at all. Fix options: have the
libvirt nat network advertise a DNS server (dnsmasq) and ensure cloud-init/netplan points
systemd-resolved at it, or push a `nameserver` via cloud-init. (For my deploy test I worked
around it by setting a nameserver on docker1 by hand — but the shipped lab should not need
that.)

---

<!-- Append findings below as they are discovered. -->

## BUG-6 — `workload_container` ansible role: `pg_workloads | from_json` fails when value is already a list

> Numbering note: renamed from BUG-5 → **BUG-6** to avoid clashing with your own
> `cd80706 "Fix BUG-5: playground exec works on all backends"`, which is a *different* bug.

**Found:** 2026-06-02, during `playground apply generic-infra` (barak-deploy skill-qualification fleet).
**Status:** ⚠️ **OPEN** — re-confirmed still failing on 2026-06-02 after `git pull`
(working tree was already up to date: 18 ahead of origin, 0 behind). This is the only
remaining `apply generic-infra` failure.

`playground apply generic-infra` provisions all 3 VMs (tofu OK), but Ansible fails 1 task on
`docker1` (the `docker-host` with the `demo-compose` workload):

```
TASK [workload_container : ...]
fatal: [docker1]: FAILED! => {"msg": "Unexpected templating type error occurred on
  ({{ pg_workloads | from_json }}): the JSON object must be str, bytes or bytearray, not list.
  the JSON object must be str, bytes or bytearray, not list"}
PLAY RECAP: docker1 : ok=12 changed=5 unreachable=0 failed=1 ...
```

**Cause:** the inventory passes `pg_workloads` to the host as an already-parsed YAML/JSON
**list** (see `generic-infra.ini`: `pg_workloads='[{"name":"demo-compose",...}]'` — Ansible
parses the single-quoted value into a list before the role sees it). The role then does
`pg_workloads | from_json`, which requires a *string*, not a list → templating error.

**Impact:** `apply` reports overall failure ("Ansible configuration failed") even though the
VMs are up and Docker is installed; the only casualty is the demo workload on the docker-host.
For my use I didn't need the demo workload, so I proceeded — but `apply` exits non-zero, which
would block CI / anyone who needs the workload.

**Suggested fix:** in the `workload_container` (and likely `workload_compose`) role, guard the
parse — `pg_workloads if (pg_workloads is iterable and pg_workloads is not string) else (pg_workloads | from_json)` — or stop JSON-encoding the inventory value if the role expects a
list. The other roles that already do `Parse pg_workloads JSON payload` succeeded on node1/router1
(which had no workloads), so the failure is specific to a host that actually has a workload list.

**Also (BUG):** `playground apply` printed `rc=0` on a run where tofu/ansible failed in one
invocation and `rc=1` in another for the same underlying failure — the exit code isn't
consistent with the failure. Make `apply` (and `destroy`/`reset`) exit non-zero
deterministically on any tofu/ansible failure; automation gates on the exit code.

---

## BUG-7 — `playground exec` does not preserve argument quoting

**Severity:** BUG (remote commands run differently than written)
**Found:** 2026-06-02, scripting remote setup over `playground exec`.

A quoted compound remote command is split and re-parsed by the remote shell, so the quoting is
lost. Repro:

```bash
playground exec --lab generic-infra --on docker1 -- bash -lc 'rm -rf /tmp/x && mkdir /tmp/x'
#   -> remote runs `bash -lc rm -rf /tmp/x && mkdir /tmp/x`
#   -> `bash -lc rm` runs the script "rm" (no args) => "rm: missing operand"
```
A `sh -c 'cat > /path'` redirect is mangled the same way. Effect: you cannot reliably pass a
quoted shell one-liner or a redirect through `exec`.

**Workaround we used:** decompose every multi-step action into separate single-token `exec`
calls (`exec … -- rm -rf /tmp/x`, then `exec … -- mkdir /tmp/x`), and use `tee` instead of
`sh -c 'cat >'` for file writes (verified binary survives via sha256 round-trip).

**Suggested fix:** quote each remote arg so the remote shell receives the exact tokens (e.g.
`shlex.quote` per arg before joining), OR document that `exec` builds a remote command line
with no quoting guarantee — and ship a file-push primitive (see PAPERCUT-4) so streaming files
through `exec -- tee` isn't necessary.

## PAPERCUT-4 — `exec` flag is `--on` (not `--host`), and there is no file-transfer command

**Found:** 2026-06-02.

- The natural guess `playground exec --host H …` errors with `Missing option '--on'`. A
  `--host` alias (or naming `--on` in the error/examples) would save a round-trip.
- There is no `playground cp`. Getting install bundles / control-wrapper tars onto a VM meant
  `playground exec --on H -- tee /remote/path < localfile` plus a manual sha256 check. A
  first-class `playground cp <local> <lab>:<host>:<remote>` (and reverse) would remove the most
  common post-provision chore.

## PAPERCUT-5 — stale tofu state-lock after an interrupted apply isn't auto-recovered

**Found:** 2026-06-02. An `apply` that was killed mid-run left a tofu state lock; subsequent
`apply` and `reset` both kept failing:

```
Error: Error acquiring the state lock ... resource temporarily unavailable
  Lock Info: ID ...  Who: user@host  Operation: OperationTypeApply
```
`playground reset generic-infra` did **not** clear it; we had to `cd tofu && tofu force-unlock
<id>` by hand (which then said "LocalState not locked", i.e. it had cleared — but the earlier
runs didn't recover on their own). Suggest: have `reset` (or `apply --force`) detect a stale
lock whose owning PID is gone and clear it, so users don't drop into raw tofu.

## BUG-8 — `workload_compose: Stage compose files` fails: staged compose file is missing (exposed by the BUG-6 fix)

**Severity:** BUG (a committed example lab still doesn't `apply` clean)
**Found:** 2026-06-02, verifying the BUG-6 fix on `apply generic-infra`.

With BUG-6 fixed, the `workload_compose` role now parses `pg_workloads` and proceeds — and
fails at the next step, `Stage compose files on the target`, because the staged source file it
was told to copy does not exist:

```
TASK [workload_compose : Stage compose files on the target]
failed: [docker1] (item=demo-compose) => {"msg": "Could not find or access
  '.playground/state/workloads/generic-infra/docker1/demo-compose.yaml' ...
  If you are using a module and expect the file to exist on the remote, see the remote_src option"}
PLAY RECAP: docker1 : ok=16 changed=0 failed=1 skipped=1
```

The inventory hands the host `staged_source:
.playground/state/workloads/generic-infra/docker1/demo-compose.yaml`, but nothing ever
*creates* that staged file from the lab's `source: ./compose/demo.yaml`. The `copy`/`template`
task then searches the ansible role search-path for it and fails.

**Impact:** same net effect as BUG-6 — `apply generic-infra` still fails overall (now on the
docker-host's compose workload), even though all 3 VMs are up and Docker is installed. This is
the "fix one, expose the next" chain: BUG-3 → BUG-4 → BUG-6 → **BUG-8**. (For our use we don't
need the demo workload, so we proceed; but the shipped lab still doesn't go green end-to-end.)

**Suggested fix:** render/stage the compose source to `staged_source` before the "Stage compose
files" task (the path implies a staging step that isn't running), or point the copy at the
real `source` with `remote_src: false` from the lab dir. Worth an end-to-end test that
`apply generic-infra` reaches `failed=0` on all hosts (a green example lab is the contract).

## NOTE-6 — cloud `apply` surfaces an auth failure as a raw tofu traceback (no preflight)

**Found:** 2026-06-02. `playground apply cloud-smoke` with an invalid/expired
`DIGITALOCEAN_TOKEN` fails *after* tofu starts creating the droplet, with a raw OpenTofu
`401 Unable to authenticate` block. This is our token (expired/revoked), **not a playground
bug** — but the failure mode is unfriendly and `playground doctor` passes despite the
credential being unusable. Ask: add a cloud-credential **preflight** (one cheap authed call,
e.g. `GET /v2/account`) to `doctor`/`plan`/`apply` that fails fast with an actionable message
and distinguishes 401 (expired/revoked → regenerate) from 403 (wrong scope).
