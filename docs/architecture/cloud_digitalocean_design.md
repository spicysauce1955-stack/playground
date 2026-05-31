# Design: `cloud-digitalocean` backend

Date: 2026-05-31
Status: **proposed** — for review, no code written yet.

Sources:
- `docs/product/cloud_backend_user_needs.md` (operator needs)
- `docs/product/cloud_digitalocean_prd.md` (authoritative scope)
- `docs/research/cloud_vm_backends_2026-05-31.md` (research, config sketch)
- `docs/architecture/CONTRACTS.md` (pipeline layer contracts)

This document was produced by the `planner` and `architect` subagents and
synthesized after two operator decisions (recorded under "Settled
decisions" below). It is the design hand-off; implementation has not
started.

---

## 1. Intent

Add a fourth backend value, `cloud-digitalocean`, that runs existing
YAML-defined labs on short-lived DigitalOcean Droplets while preserving
the `OpenTofu → Ansible → workload` pipeline and the inspectable backend
model. It is a **narrow, working** DO backend, not a generic cloud
abstraction (PRD non-goal).

## 2. Structural shape

The backend follows the **`local-vbox` template exactly**: the new
package owns only the *front half* of the pipeline; the *back half* is
reused verbatim.

```
ResolvedLab (backend=cloud-digitalocean)
        |
        v
  build_do_plan (plan.py)        # pure: droplet names, tags, cost
        |
        v
================ execute_apply (runner.py) ================
| render tfvars + main.tf  → .playground/state/cloud-digitalocean/<lab>/
| tofu init / apply        (DO provider; token via env, never HCL)
| read `vm_ips` output     (same shape as libvirt → reuse fetch_vm_ips)
| wait-for-vms-ready  (SHARED, ssh_port=22, real public IPs)
| ansible-playbook    (SHARED)
| verify-lab          (SHARED, warning-only)
==========================================================
```

- **Back half is imported, not copied**, exactly as
  `local_vbox/runner.py` does today:
  `wait_for_vms_ready`, `run_ansible_playbook`, `verify_lab`,
  `render_inventory`/`fetch_vm_ips` from `backend/local_libvirt/*`.
- Cloud VMs have routable public IPs, so `ssh_port` stays `22` (no NAT
  port-forward like vbox).
- The DO OpenTofu root emits its `vm_ips` output in the **same shape**
  libvirt does (`{ vm_name => ipv4 }`) so `fetch_vm_ips` in
  `local_libvirt/inventory.py` is reused unchanged.

### Module layout

New:

```
src/playground/backend/cloud_digitalocean/
    __init__.py          # exports execute_apply/destroy/reset/suspend/resume, query_status
    runner.py            # lifecycle orchestration; mirrors local_vbox/runner.py
    plan.py              # build_do_plan(resolved) -> DoPlan; pure, no I/O
    tfvars.py            # render_do_tfvars(resolved) -> dict; pure, key allowlist
    tofu_templates.py    # generate_main_tf(DoPlan) -> HCL text
    status.py            # query_status: join tofu state + live DO API by tag
    pricing.py           # DROPLET_PRICES lookup table, last-updated dated
    cloudinit.py         # render user-data (mirrors local_vbox/cloudinit.py)

config/providers/cloud-digitalocean.yaml
config/labs/cloud-smoke.yaml

tofu/cloud_digitalocean/{versions,variables,main,outputs}.tf
    # separate root from committed tofu/; provider auth via env only
```

Generated, git-ignored, per-lab:

```
.playground/state/cloud-digitalocean/<lab>/
    main.tf  <lab>.tfvars.json  terraform.tfstate  .terraform/
.playground/state/inventory/<lab>.ini
.playground/runs/<run-id>/{run.json, events.jsonl, logs/*.log}
```

Modified:

- `src/playground/backend/dispatch.py` — add `DIGITALOCEAN` constant +
  `SUPPORTED_BACKENDS` entry; route the 4 existing verbs; add new
  `execute_suspend` / `execute_resume` dispatch functions.
- `src/playground/backend/local_libvirt/apply.py` — add a backend-neutral
  `run_tofu_init` wrapper (the generated DO root needs `init`; the
  committed `tofu/` root is pre-initialized so libvirt never needed it).
- `src/playground/planner/plan.py` — add a shared `CostEstimate` model +
  optional `cost_estimate` field on `Plan` (see Settled decision #1).
- `src/playground/models/status.py` — add optional `provider_id` /
  `provider_state` to `VmStatus` (additive; existing consumers checking
  only `state` are unaffected).
- `src/playground/cli/main.py` — add `suspend` / `resume` commands; add
  `--backend` to `doctor`; render the cost line in `plan` output.
- `src/playground/preflight/doctor.py` — add `check_cloud_digitalocean_*`
  group, gated on `--backend cloud-digitalocean`.
- `docs/architecture/CONTRACTS.md` — add a "Backend: cloud-digitalocean"
  section + new cross-layer pitfalls.
- `pyproject.toml` — add `httpx` to runtime dependencies (see Settled
  decision #2).

**Must NOT change:** `tofu/main.tf` (committed libvirt root), the
`Budget` model in `models/kinds.py`, and any `local_libvirt` / `local_vbox`
backend file (the shared back half is reused, not edited).

## 3. Provider config

```yaml
apiVersion: playground/v1
kind: ProviderConfig
metadata:
  name: cloud-digitalocean
spec:
  driver: cloud-digitalocean
  region: nyc3                       # committed default; override per lab
  image: ubuntu-24-04-x64
  size: s-1vcpu-1gb                  # cheap smoke default
  token_env: DIGITALOCEAN_TOKEN      # NAME of env var, never the value
  ssh_public_key_path: ~/.ssh/id_rsa.pub
  firewall:
    ssh_cidrs: []                    # empty → warn (SSH open to all)
  capabilities:
    nested_virtualization: false
    privileged_containers: true
```

`ProviderConfigSpec` is `extra="allow"`, so no generic-model change is
needed to carry these keys or lab-level overrides under
`spec.providers.cloud-digitalocean`.

## 4. Lifecycle commands

| Command | Behavior |
|---|---|
| `doctor --backend cloud-digitalocean` | token-env present; token-not-committed (git scan); `tofu` installed; SSH key readable; state dir writable; firewall-CIDR warning. Redacts secrets. |
| `plan <lab>` | provider/region/size/image/VM count/resource names/tags/SSH exposure + **advisory** hourly & monthly cost. No mutation. |
| `apply <lab>` | render tfvars + `main.tf` under per-lab state path; `tofu init/apply`; tag every resource; reuse wait → ansible → verify. |
| `status <lab>` | **join** local tofu state with a **live DO API tag query**; distinguish "state says X" from "provider reports X". |
| `suspend <lab>` | **destroy** Droplets (power-off still bills) + preserve local state/run history; tag-sweep for survivors; idempotent. |
| `resume <lab>` | rebuild from config (`tofu apply`) + re-run readiness/ansible; emits an explicit "disk changes not preserved" event before mutating. |
| `destroy <lab>` | `tofu destroy` + tag-sweep; idempotent; report failures with console URLs. |
| `reset <lab>` | best-effort `tofu destroy` then unconditional tag-based orphan sweep. |

### suspend/resume dispatch shape

`suspend`/`resume` are **new verbs**. Add them to `dispatch.py` with
explicit routing; local backends return a
`runtime.backend.verb_not_supported` error diagnostic rather than raising
or growing methods they can't support. No capability ABC/protocol —
premature for three backends.

```python
def execute_suspend(*, resolved, state_dir, bus):
    if resolved.backend == DIGITALOCEAN:
        return cloud_digitalocean.execute_suspend(...)
    return None, [_verb_not_supported("suspend", resolved.backend)]
```

## 5. Settled decisions (operator, 2026-05-31)

1. **Cost model lives in the shared `Plan` model.** Add a `CostEstimate`
   value object (`hourly_usd`, `monthly_usd`, `note`, `advisory=True`) and
   an optional `cost_estimate: CostEstimate | None` on
   `planner/plan.py:Plan`. Local backends leave it `None`; the DO adapter
   populates it from `pricing.py`. The CLI `plan` renderer shows the cost
   line when present. The shared `Budget` model is **not** given a money
   dimension yet — a per-provider `cost_budget_usd` warning (open-keyed in
   the DO provider config) covers budget checks until a second cloud
   backend proves the cross-provider shape.

2. **The first slice makes live DigitalOcean API calls.** Add `httpx` as a
   runtime dependency. `status` joins local tofu state with a live
   tag-based Droplet query (`GET /v2/droplets?tag_name=playground-lab:<lab>`)
   so it can report `provider_state` and flag divergence
   (`runtime.status.provider_disagrees`). `reset`/`suspend`/`destroy` run an
   unconditional tag-based orphan sweep after `tofu destroy` so stranded,
   still-billing resources are surfaced (not silently left). The DO API
   surface is kept small (status read + tag sweep); no `doctl` or full
   Ansible collection dependency.

## 6. Implementation slices (ordered)

1. **Static config + cost line** — provider YAML + `cloud-smoke` lab;
   `CostEstimate` on `Plan`; `validate`/`plan` pass with no libvirtd, no
   token. Touches `planner/plan.py`, `cli/main.py`.
2. **DO OpenTofu root** — `tofu/cloud_digitalocean/*.tf`: one
   `digitalocean_droplet` per `var.vm_names`, cloud-init user-data,
   `digitalocean_firewall`, ownership tags; `vm_ips` output in libvirt
   shape. `tofu validate`-clean. No token in any `.tf`.
3. **Adapter package** — `plan.py`, `tfvars.py`, `tofu_templates.py`,
   `cloudinit.py`, `status.py`, `pricing.py`, `runner.py`. Reuse the shared
   back half + the new `run_tofu_init`.
4. **Dispatch + CLI** — `cloud-digitalocean` routing; new `suspend`/`resume`
   commands; `--backend` on `doctor`.
5. **Doctor** — five `check_cloud_digitalocean_*` checks under
   `runtime.doctor.*`, secret-redacting.
6. **Tests** — unit suite with no token / no network / no `tofu init`;
   `httpx` mocked; a live smoke test gated behind `PLAYGROUND_DO_SMOKE=1`.

## 7. Acceptance criteria (traced to PRD)

- `cloud-smoke` validates without local-libvirt requirements.
- Missing `DIGITALOCEAN_TOKEN` → actionable doctor error.
- Token value in a tracked file → error diagnostic.
- `plan cloud-smoke` shows region/size/image/resource names/SSH
  exposure + cost estimate; no mutation.
- `apply` creates a tagged Droplet, generated inventory under
  `.playground/`, runs existing Ansible roles; state under
  `.playground/state/cloud-digitalocean/cloud-smoke/` (not `tofu/`).
- `status` reports actual provider state + SSH reachability; distinguishes
  local-state intent from provider reality.
- `suspend` removes Droplet compute, keeps local run history; idempotent.
- `resume` recreates from config, re-runs provisioning; states disk
  changes are not preserved.
- `destroy` removes all lab-owned DO resources; idempotent.
- Re-running suspend/destroy after resources are gone → no-op / warning,
  not error.
- **No command prints the API token.**

## 8. Risks & mitigations

1. **Tofu state diverges from provider reality** (Droplets deleted in
   console, billing enforcement, etc.). → `status` always consults the
   live API, not only state; `VmStatus.provider_state` + a
   `provider_disagrees` diagnostic make the gap visible.
2. **Token leak into a generated/logged artifact** — highest at the
   tfvars/HCL boundary. → DO provider authenticates via the **environment
   only** (no `token` attribute in HCL); `render_do_tfvars` uses an
   explicit key **allowlist** and never serializes the raw provider dict;
   doctor scans config + tracked files for token-shaped values; a unit
   test asserts the token env name never appears in rendered output.
3. **Suspended lab leaves orphaned, billing Droplets** if the
   destroy→sweep sequence is non-atomic. → suspend runs the tag sweep
   **unconditionally** after `tofu destroy` regardless of its exit code;
   surviving tagged compute is reported as
   `runtime.suspend.orphaned_resource` with ID + console URL, and suspend
   does **not** finish `succeeded` while paid compute survives.

## 9. Coupling points to watch

- **tfvars keys ↔ `main.tf` variables.** Name drift fails at `tofu apply`
  with an unhelpful error. → unit test asserts every key from
  `render_do_tfvars` has a matching `variable "<key>"` in the generated
  HCL.
- **`fetch_vm_ips` reuse.** The DO tofu output must be named `vm_ips` and
  be `dict[str, str]`, or the reused parser emits
  `config.inventory.tofu_parse_failed`. Documented as a CONTRACTS.md
  constraint so template edits don't break it silently.

## 10. Verification strategy

- **Unit (no token / no network / no `tofu init`):** `build_do_plan`
  resource names/tags/cost; tfvars↔HCL key parity; cloud-init contents;
  token-name-never-leaked; runner subprocess sequence & run-record shape;
  all doctor checks; `httpx` mocked.
- **Static:** `tofu fmt -check`/`validate` on the DO root (CI, needs
  `init` connectivity); `grep` asserts no token pattern in `tofu/` or
  `.playground/`; `playground validate`/`plan cloud-smoke` with token
  unset.
- **Live smoke (`PLAYGROUND_DO_SMOKE=1`, spends money, off CI):**
  doctor → plan → apply → status → suspend → resume → destroy → status,
  then confirm zero tagged Droplets remain. Runner never logs the token.

## 11. Deferred (not in first slice)

Multi-provider abstraction; snapshot-based suspend; Redroid/nested-virt on
DO; dynamic inventory; extracting the shared back half to `backend/shared/`
(3rd import from the mis-named package is tolerable — revisit at the 4th
backend); a `tofu_common` helper; live pricing API; money dimension on the
shared `Budget` model; `.env` auto-loading; auto-registering SSH keys in
DO; interactive destroy confirmation.
