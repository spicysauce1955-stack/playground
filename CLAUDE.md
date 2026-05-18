# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

An Infrastructure-as-Code playground that provisions KVM/libvirt VMs with OpenTofu, configures them with Ansible, and runs containerized Android (Redroid) inside the guests. The full spec lives in `PRD.md` — re-read it before any non-trivial change, as it states non-negotiable constraints (air-gap readiness, no hardcoded secrets, idempotency, nested-virt passthrough).

The highest-signal product intent lives in `docs/product/requirements.md`,
followed by `docs/product/user_stories.md` and `docs/product/mvp_scope.md`.

## Two-stage deploy pipeline

The stages are coupled by a manual handoff and must be run in order:

1. **Provision (OpenTofu):** `cd tofu && tofu init && tofu apply -auto-approve` brings up `playground_net` (NAT, 10.0.10.0/24) and `var.vm_count` Ubuntu Noble VMs with cloud-init.
2. **Inject IPs:** `tofu output vm_ips` lists DHCP-assigned addresses. These must be written into `ansible/inventory.ini` under `[playground]` as `pg-node-N ansible_host=10.0.10.X ansible_user=ubuntu` — there is no dynamic inventory script yet.
3. **Configure (Ansible):** `cd ansible && ansible-playbook -i inventory.ini site.yml` runs the `docker` then `redroid` roles on every host in `[playground]`.
4. **Connect:** `adb connect <VM_IP>:5555` from the host.

Teardown: `cd tofu && tofu destroy -auto-approve`.

## Architecture notes that aren't obvious from a single file

- **The real OpenTofu root is `tofu/`.** Edit `tofu/main.tf` for infrastructure resources. Do not recreate the retired repo-root `main.tf` stub.
- **Nested virtualization is load-bearing.** `cpu { mode = "host-passthrough" }` in `tofu/main.tf` is required so Redroid containers inside the guest can access the binder/ashmem kernel features. Do not change it to a generic CPU mode.
- **Cloud-init wires SSH access.** `tofu/cloud_init.cfg` is a `templatefile` that injects `var.ssh_public_key_path` (default `~/.ssh/id_rsa.pub`) for the `ubuntu` user. SSH password auth is disabled — losing the key means recreating the VM.
- **Redroid role mounts binderfs.** `ansible/roles/redroid/tasks/main.yml` best-effort `modprobe`s `binder_linux`/`ashmem_linux`, asserts binderfs support, mounts `/dev/binderfs`, and runs the redroid container `--privileged` with port 5555 exposed. The image tag lives in `ansible/roles/redroid/defaults/main.yml`.
- **Docker role is order-sensitive.** Remove distro `docker.io`/`containerd` before installing `docker-ce` from Docker's apt repo, then add the SSH user to the `docker` group. Defaults live in `ansible/roles/docker/defaults/main.yml`. Re-running is idempotent; reordering breaks a fresh box.
- **Ansible collections are declared.** `ansible/requirements.yml` declares the external collections used by roles. Controller must run `ansible-galaxy collection install -r ansible/requirements.yml` before the first `site.yml` run.

## Variables worth knowing (`tofu/variables.tf`)

`vm_count` (default 1), `vm_memory` MB (4096), `vm_vcpu` (2), `ssh_public_key_path`, `ubuntu_image_url` (Noble cloud image). Override via `-var` or `terraform.tfvars`; never hardcode secrets in `.tf` files.

## Repo context: sequential workflow

Work happens sequentially on `main`. The old parallel-branch planning files
have been removed. Use normal Git history, `README.md`, `PRD.md`, `CODEX.md`,
`AGENTS.md`, `docs/workflow.md`, `docs/platform.md`,
`docs/engineering_principles.md`, `docs/architecture_decisions.md`,
`docs/roadmap.md`, and this file as the repo guidance.

Claude Code is useful for repo-local debugging, test failures, deep multi-file
refactors, and independent implementation review. Codex is documented in
`CODEX.md`. Do not recreate the old planning tree unless the repo explicitly
adopts that workflow again.

### Project-local Claude subagents (`.claude/agents/`)

- `planner` — Turns a request into a scoped work item with acceptance criteria and verification.
- `architect` — Checks module boundaries, coupling, data flow, and maintainability before coding.
- `iac-implementer` — Makes focused repo changes after scope and design are clear.
- `code-reviewer` — PRD-conformance + idempotency + secrets review on diffs under `tofu/` and `ansible/`. Use after edits, before commit.
- `debugger` — Localizes failures across the tofu → cloud-init → ansible → redroid → ADB pipeline. Use when a stage breaks.
- `qa-engineer` — Static + live + idempotency test matrix. Use before cutting a PR / release.
- `integrator` — Final status check, generated-artifact cleanup, and merge-readiness summary.

## Commands cheat sheet

```bash
# Validate / format Terraform before applying
cd tofu && tofu fmt && tofu validate

# Re-render a single VM without touching others
tofu apply -target=libvirt_domain.playground_node[0]

# Dry-run Ansible against the inventory
ansible-playbook -i ansible/inventory.ini ansible/site.yml --check --diff

# Run a single role (e.g. only docker)
ansible-playbook -i ansible/inventory.ini ansible/site.yml --tags docker
# (Add `tags:` to role tasks first — none are tagged today.)

# Lint Ansible (if ansible-lint is installed)
ansible-lint ansible/site.yml
```
