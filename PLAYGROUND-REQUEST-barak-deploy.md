# Request to the playground developer — from the barak-deploy team

**Date:** 2026-06-02
**From:** barak-deploy maintainers (using `playground` as a black-box VM provisioner)
**Context:** We drive `playground` to stand up real VMs (libvirt + DigitalOcean) and deploy
containerized + non-docker workloads onto them via barak-deploy's opener/tunneler flow. This
round was a 5-project / 3-machine qualification: `generic-infra` (libvirt: docker1, router1,
node1) for the local machines and `cloud-smoke` (DigitalOcean: node1) for the cloud machine.

We hit a few things that cost us time or blocked the cloud half entirely. None of this is
meant as criticism — playground did the heavy lifting and the local fleet came up fine. Below
is what we experienced and what we need, prioritized.

---

## BLOCKING — we could not run the cloud half

### 1. Cloud `apply` fails with a raw `401` deep inside `tofu apply` (likely an invalid token, but the failure mode is bad)

**What we saw** (`playground apply cloud-smoke`, reproduced twice):
```
digitalocean_droplet.node[0]: Creating...
│ Error: Error creating droplet: POST https://api.digitalocean.com/v2/droplets:
│   401 (request "...") Unable to authenticate you
tofu-apply failed. Droplets may have been created; run `playground destroy cloud-smoke` ...
```
The `DIGITALOCEAN_TOKEN` in `.env` is well-formed (`dop_v1_…`, 71 chars), so this is almost
certainly an **expired/revoked token on our side** — not a playground defect. We will refresh
it. BUT the failure mode is rough:

- The auth failure only surfaces *after* tofu starts creating resources, as a raw OpenTofu
  traceback. There's no early, friendly "your DigitalOcean token was rejected (401) — generate
  a new one at … and set DIGITALOCEAN_TOKEN" message.
- `playground doctor` passes even though the cloud credential is unusable, so there's no way to
  catch this before an `apply`.

**What we need:**
- A **cloud-credential preflight**: before `tofu apply` on a cloud backend, make one cheap
  authenticated call (e.g. `GET /v2/account`) and fail fast with a clear, actionable message on
  401/403 — distinguishing "token rejected (401 → expired/revoked)" from "token lacks scope
  (403)". Ideally fold this into `playground doctor` and `playground plan` for the cloud
  backend so it's catchable without spending an apply.

---

## HIGH — these cost us real time / produce misleading signals

### 2. `apply` exits `0` (success) on a failed run

**What we saw:** every failed `apply` we hit (stale-lock failure, ansible failure, the cloud
401) ended with the shell reporting `rc=0`, even though the run record says `apply failed`. In
one early run it was `rc=1`, in later identical-failure runs it was `rc=0` — i.e. inconsistent.

**Impact:** any script or CI gating on `playground apply` exit status will treat a broken
provision as success. We had to grep stdout for `apply failed` to know the truth.

**What we need:** `apply` (and `destroy`/`reset`) should exit **non-zero deterministically**
whenever tofu or ansible fails. Exit code is the contract automation relies on.

### 3. `workload_container` ansible role: `pg_workloads | from_json` fails when the value is already a list

**What we saw** (`playground apply generic-infra`, every time; only on the host that actually
has a workload — `docker1`):
```
fatal: [docker1]: FAILED! => {"msg": "Unexpected templating type error occurred on
  ({{ pg_workloads | from_json }}): the JSON object must be str, bytes or bytearray, not list"}
PLAY RECAP: docker1 : ok=12 changed=5 ... failed=1
```
The rendered inventory passes `pg_workloads` as an already-parsed list
(`generic-infra.ini`: `pg_workloads='[{"name":"demo-compose",...}]'`), and the role then runs
`| from_json` on it. `node1`/`router1` (no workloads) pass the "Parse pg_workloads JSON
payload" task fine, so the failure is specific to a host that actually has a workload list.

**Impact:** `apply generic-infra` always reports overall failure, even though all 3 VMs are up
and Docker is installed. Combined with (2), this is easy to misread. We worked around it
(Docker installs before the failing step, which is all we needed), but it would block anyone
who needs the declared workload, and it makes `apply` look broken.

**What we need:** guard the parse — accept either a JSON string or an already-decoded list,
e.g. `pg_workloads if (pg_workloads is not string and pg_workloads is iterable) else
(pg_workloads | from_json)` — in `workload_container` (and check `workload_compose`, which has
the same "Parse … JSON payload" task).

### 4. `playground exec` does not preserve argument quoting

**What we saw:** `playground exec --lab L --on H -- bash -lc 'rm -rf /tmp/x && mkdir /tmp/x'`
ran the remote command as if the quotes were stripped — `rm` received no argument
(`rm: missing operand`), because the single-quoted compound string was split and the remote
shell re-parsed the tokens. A `sh -c 'cat > file'` redirect was similarly mangled.

**Impact:** you can't reliably pass a quoted shell one-liner or a redirect through `exec`. We
had to decompose every multi-step remote action into separate single-token `exec` calls
(`exec … -- rm -rf /tmp/x`, then `exec … -- mkdir /tmp/x`, …), and use `tee` instead of
`sh -c 'cat >'` for file writes.

**What we need (either is fine):**
- Preserve argv exactly (quote each remote arg so the remote shell sees the same tokens), **or**
- Document clearly that `exec` joins args into a remote command line (no quoting guarantee), and
  provide a first-class **file-push primitive** (see item 6) so we don't have to stream files
  through `exec … -- tee …`.

---

## MEDIUM — friction / discoverability

### 5. `exec` flag is `--on` / `--lab`, not `--host`

The natural guess (`--host`) errors with `Missing option '--on'`. Minor, but it cost a couple
of round-trips. A `--host` alias, or naming it in the error/examples, would help.

### 6. No file-transfer command

To get an install bundle and control-wrapper tars onto a VM we piped them through
`playground exec --on H -- tee /path < localfile` and verified with a sha256 round-trip
(binary survived, good). A real `playground cp <local> <lab>:<host>:<remote>` (and back) would
remove a whole class of awkwardness — this is the single most common thing we do after
provisioning.

### 7. Stale tofu state-lock after an interrupted apply is hard to recover

An `apply` we started (then a sandbox killed) left a tofu state lock; subsequent `apply`/`reset`
kept failing with `Error acquiring the state lock … resource temporarily unavailable` citing a
lock ID and `Who: user@host` from the dead process. `playground reset` did not clear it; we had
to `cd tofu && tofu force-unlock <id>` by hand (which then reported "LocalState not locked", so
it had already cleared — but the earlier runs didn't recover on their own).

**What we need:** have `playground reset` (or a `--force` on `apply`) detect and clear a stale
lock whose owning PID is gone, so users don't have to drop into raw tofu.

---

## LOW — noise

### 8. The per-VM-resources warning prints on *every* command for `generic-infra`

```
WARNING config.backend.per_vm_resources_unsupported: lab 'generic-infra' declares heterogeneous
per-VM resources, but the local-libvirt backend applies global var.vm_memory/var.vm_vcpu ...
```
It fired on `apply`, `status`, `exec`, `destroy` — every invocation, twice on some. It's
useful once, but as a banner on read-only/`exec` calls it's pure noise and made log-grepping
harder. Suggest: emit it on `apply`/`plan` only, or gate it behind a verbosity flag.

---

## What worked well (so you know what not to touch)

- `playground apply generic-infra` brought up all 3 libvirt VMs reliably; Docker + compose v2
  were present on the `docker-host`; Python 3.12 on every VM.
- `playground exec` connectivity itself was solid (once we learned its quoting behavior).
- `playground destroy` + `status` gave us a clean, confidence-inspiring teardown (0 VMs / 0
  droplets every time) — important to us because the cloud VM costs money.
- The `cloud-smoke` lab definition and the run-record/`.playground/runs/...` logs were easy to
  navigate when diagnosing failures.

---

## Summary of asks, in priority order

1. **Cloud-credential preflight** (fail fast + clear message on 401/403; add to `doctor`).
2. **`apply`/`destroy` exit non-zero on failure**, deterministically.
3. **Fix `workload_container` `from_json`** (accept list-or-string).
4. **`exec` quoting**: preserve argv, or document + ship a file-push primitive.
5. `--host` alias for `exec`; stale-lock auto-recovery in `reset`; demote the per-VM warning to
   `apply`/`plan` only.

Happy to provide full run records (`.playground/runs/…`) or repro steps for any of these.
Thanks — playground saved us a lot of manual VM wrangling.
