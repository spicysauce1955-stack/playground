---
name: playground-labs
description: "Provision throwaway Linux VMs (and optionally Docker / containerized Android) on the local host using the `playground` CLI. Each lab is a YAML file declaring VMs, networks, and workloads; `playground apply LAB` brings the world up, `playground destroy LAB` tears it down, and `playground exec --lab LAB --on VM -- CMD` runs a command inside a guest over SSH. Use this whenever the user wants to spin up a VM, get a Linux sandbox or test box, provision a lab, run something against a fresh Ubuntu, stand up a KVM/libvirt or VirtualBox or DigitalOcean cloud VM, or work with Redroid / containerized Android over ADB — even if they don't say 'playground' or 'lab' explicitly."
metadata:
  version: "1.1.0"
  last_updated: "2026-06-02"
  status: active
  task_type: tool-use
  prerequisites:
    - "playground CLI on PATH (`.venv/bin/playground` or system install)"
    - "either KVM/libvirt (default backend) or VirtualBox (alt backend) installed, or a DigitalOcean account + `$DIGITALOCEAN_TOKEN` for the `cloud-digitalocean` backend"
---

# playground-labs — provision throwaway VMs as a tool

You are a consumer of this tool. You do **not** edit `tofu/`, `ansible/`,
`src/`, or the lab schema. You drive the CLI, read its JSON, and treat
each lab as a black-box reproducible environment.

## When to use

Use this skill when the user (or your higher-level plan) needs:

- A short-lived Linux VM with SSH access to run tests against.
- A small cluster of VMs on a private network for cross-host scenarios.
- A Docker host for compose/swarm work that shouldn't pollute the host.
- A containerized-Android (Redroid) sandbox over ADB for app testing.

If the task is "edit infrastructure," "improve idempotency," or "fix the
backend" — **stop**. That's the platform team's job, not yours; route
back to the user.

## Quick start

```bash
# 1. Are prerequisites met? (one-time, but cheap to re-run)
playground doctor

# 2. What labs exist?
playground lab list

# 3. Bring a lab up. The default `generic-infra` lab gives you
#    three VMs (one generic, one docker-host, one router) on
#    three networks. Takes 3-8 minutes.
playground apply generic-infra

# 4. Where are the VMs?
playground status generic-infra --output json

# 5. Run something inside.
playground exec --lab generic-infra --on docker1 -- docker ps

# 6. Tear down when done.
playground destroy generic-infra
```

## The full CLI surface

| Command | What it does | When to reach for it |
| --- | --- | --- |
| `playground doctor` | Probe host for prereqs (libvirt, ansible, ssh key, …). | First, on every new machine. Re-run after suggestions. |
| `playground validate` | Static check of all lab YAMLs in `config/`. | After editing or adding a lab YAML. |
| `playground lab list` | Enumerate configured labs. | When you don't know what's available. |
| `playground lab show <lab>` | Resolved lab spec (post-defaults). | When you need to know what `apply` will actually provision. |
| `playground apply <lab>` | Provision VMs + run ansible + verify. Idempotent. | Standing up a lab, or re-converging after edits. |
| `playground status [<lab>]` | Observed VM state + IPs. Omit `lab` to list all labs at once. | After apply; whenever you need IPs. |
| `playground exec --lab <lab> --on <vm> -- <cmd>` | One-shot SSH into a VM. | Running arbitrary commands inside a guest. |
| `playground destroy <lab>` | `tofu destroy` the lab. | Clean teardown when apply state is consistent. |
| `playground suspend <lab>` | (cloud backends only) Destroy Droplets to stop billing; config preserved. | Pausing a cloud lab to cut costs; disk state is NOT preserved. |
| `playground resume <lab>` | (cloud backends only) Rebuild Droplets from config after a suspend. | Resuming a suspended cloud lab. |
| `playground reset <lab>` | Scrub-by-name (works even when tofu state is corrupted). | When `destroy` fails or state is wedged. |
| `playground runs list` | Past `apply` / `destroy` runs. | After a failed run, to find the run id. |
| `playground runs show <id>` | One run's steps, exit codes, log paths. | Diagnosing why a run failed. |
| `playground plan <lab>` | Backend-neutral dry-run summary. | Confirming what apply will do without committing. |

Append `--output json` to `apply`, `status`, and `runs *` for machine-
readable output. Use it whenever you'll parse the result.

## Picking a lab

Three labs ship in `config/labs/` (treat as menu options):

- **`generic-infra`** — 3 VMs on `local-libvirt`: a generic node, a
  docker-host, and a router. Three networks (edge / lab-private /
  routed). Good default for "I need Linux VMs."
- **`vbox-smoke`** — 1 VM on `local-vbox` (VirtualBox), docker-host
  role. Use when libvirt isn't available (e.g. a macOS host running
  this through a VM, no KVM). Reached via SSH on a NAT port-forward.
- **`cloud-smoke`** — 1 Droplet on `cloud-digitalocean` (DigitalOcean),
  docker-host role. Use when you need a real public-IP cloud host.
  Requires `export DIGITALOCEAN_TOKEN=<token>` before apply.
  Note: Redroid/nested-virt is not supported on DigitalOcean.

For a single throwaway VM with the least setup, prefer `vbox-smoke` (one
local VM, no cloud token) over the 3-VM `generic-infra`. Reach for
`generic-infra` when the task actually needs multiple VMs or a private
network; `cloud-smoke` only when a public IP / real cloud host matters
(and a token is set).

## Adding a new lab

If none of the existing labs fit, create one in `config/labs/<name>.yaml`.
Minimal shape (use `generic-infra.yaml` and `vbox-smoke.yaml` as
reference):

```yaml
apiVersion: playground/v1
kind: Lab
metadata:
  name: my-test         # must match filename without .yaml
  description: |
    One-line summary.
  tags: [test]

spec:
  backend: local-libvirt  # or local-vbox, or cloud-digitalocean
  offline: false

  budget:                 # capacity caps; permissive lets warnings ride
    mode: permissive
    max_vcpu: 4
    max_memory_mb: 8192
    max_disk_gb: 40
    max_vms: 2
    max_containers: 10

  networks:
    - name: lab-net
      profile: isolated   # or nat | routed
      cidr: 10.60.0.0/24

  vms:
    - name: node1
      role: generic-node  # see config/roles/ for the menu
      networks: [lab-net]

  commands:               # post-apply verify steps; pick from config/commands/
    enabled:
      - check-docker
```

**Always run `playground validate` after editing**; it catches schema
errors before apply does.

## Reading outputs

`playground status <lab> --output json` shape (one VM):

```json
{
  "lab": "generic-infra",
  "backend": "local-libvirt",
  "expected_vms": 3,
  "provisioned_vms": 3,
  "vms": [
    {"name": "docker1", "role": "docker-host",
     "ip": "10.20.10.42", "state": "provisioned",
     "ssh_host": "10.20.10.42", "ssh_port": 22}
  ]
}
```

`ssh_host` / `ssh_port` are the backend-neutral connection endpoint —
prefer them over `ip` when you SSH/SCP, because they're uniform across
backends: local-libvirt and cloud use the VM IP on port 22, but
local-vbox reports `127.0.0.1` with a per-VM NAT-forwarded port (its
`ip` is null). They're `null` until the VM is reachable.

States to handle:
- `provisioned` / `running` — VM exists, SSH should work.
- `missing` — declared in lab but not yet applied.
- Top-level `unknown_vms: [...]` — names present in observed/provider
  state but not declared in the lab (config drift); not a per-VM state.

Status with no argument (`playground status --output json`) returns
`{"labs": [...]}` listing every configured lab — useful for one-shot
discovery.

## Diagnostics — what to do

Diagnostics surface from `validate`, `doctor`, `apply`, `destroy`. They
have stable IDs (`namespace.category.code`). The most common ones a
consumer agent will hit:

| ID prefix | Meaning | Your move |
| --- | --- | --- |
| `runtime.doctor.*` | Host prereq problem. | Follow the `suggestion` field. Re-run doctor until clean. |
| `runtime.apply.libvirt_domain_crashed` | A VM crashed at boot — typically nested-virt failure. | Read the suggestion (escalation ladder) and `docs/architecture/nested_virtualization.md`. |
| `runtime.doctor.xsltproc_missing` | `xsltproc` not on PATH; needed by labs that use `cpu_features_disable`. | `sudo apt install -y xsltproc` (or another libxslt package) and re-run doctor. |
| `runtime.apply.wait_ssh_timeout` / `wait_cloud_init_timeout` | VM came up but never accepted SSH / cloud-init never finished. | `playground runs show <id>` for logs; `virsh console <vm>` to inspect. |
| `runtime.apply.verify_failed` | Apply succeeded but the post-verify probe found something off (warning-only — the lab is still up). | Investigate; lab may still be usable. |
| `runtime.apply.not_idempotent` | Apply twice changed state on the second pass. | Report to the platform team — likely a role bug. |
| `runtime.backend.unsupported` | The lab's `spec.backend` value isn't a backend you have installed. | Pick a different lab or install the backend. |
| `runtime.backend.verb_not_supported` | `suspend`/`resume` called on a local backend (local-libvirt or local-vbox). | Use `destroy`/`apply` instead; suspend/resume are cloud-only. |
| `runtime.doctor.cloud_token_missing` | `$DIGITALOCEAN_TOKEN` not set when using `cloud-digitalocean`. | `export DIGITALOCEAN_TOKEN=<token>` and re-run. |
| `config.reference.*` | YAML refers to something that doesn't exist (role, network, command). | Fix the lab YAML, re-validate. |
| `config.backend.tcg_mode_slow` | Lab opts into TCG (qemu) mode — boots 10-100× slower. | Expect long apply, raise `wait_*_timeout_seconds` if needed. |

Full registry: `docs/system_overview.md` (table near the bottom).
Failure-mode deep dive: `docs/architecture/nested_virtualization.md`.

## Common workflows

### Reproducible test box: apply, run a test, destroy

```bash
playground apply generic-infra
playground exec --lab generic-infra --on docker1 -- bash -c 'curl -fsS https://example.com'
RESULT=$?
playground destroy generic-infra
exit $RESULT
```

The exit code of `playground exec` is the remote command's exit code,
so you can chain it directly.

### Apply failed — what happened?

```bash
# Newest first; status of each run.
playground runs list --output json

# Per-step logs + diagnostics for one run.
playground runs show <run-id> --output json
```

If `destroy` fails too, `playground reset <lab>` does a scrub-by-name —
it works even when tofu state is corrupted.

### "I just need a fresh Ubuntu in 30s"

You can't. Default apply is 3–8 minutes (VM boot + cloud-init +
ansible). Set expectations with the user. If they need faster, suggest
a container instead — this tool is for full-VM scenarios.

### Talking to a Redroid (Android) VM

If the lab uses the `redroid-host` role, apply boots the container and
exposes ADB on `<vm-ip>:5555`. From the host:

```bash
adb connect $(playground status <lab> -o json | jq -r '.vms[0].ip'):5555
adb devices
```

Redroid requires nested-virt features on the L1 host — if `apply`
fires `runtime.apply.libvirt_domain_crashed`, see
`docs/architecture/nested_virtualization.md`.

## Hard rules

- **Never edit `tofu/`, `ansible/`, `src/playground/`, or
  `config/providers/`** — those are the platform implementation. If a
  knob you need isn't exposed in the lab YAML, report it back; don't
  reach into the internals.
- **Always `validate` before `apply`** after editing a lab. The
  validator catches >90% of mistakes before they cost you a 5-minute
  apply.
- **Apply is idempotent — destroy is the cleanup.** Don't try to fix
  apply state by deleting files under `.playground/state/`. Use
  `playground destroy` then `playground reset` if destroy fails.
- **`apply` and `destroy` mutate the host's libvirt / VirtualBox
  state.** Confirm with the user before running these if you're
  unsure they want that side effect.

## Quick reference card

```
playground doctor                          # host prereqs
playground validate                        # lint all labs
playground lab list                        # menu
playground apply <lab>                     # bring up (~5 min)
playground status [<lab>] -o json          # IPs + state
playground exec --lab <lab> --on <vm> -- <cmd>   # SSH
playground destroy <lab>                   # tear down
playground reset <lab>                     # scrub when destroy fails
playground runs list | runs show <id>      # post-mortem
```

Everything else is internal — you don't need it.
