# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

An Infrastructure-as-Code playground that provisions KVM/libvirt VMs with OpenTofu, configures them with Ansible, and runs containerized Android (Redroid) inside the guests. The full spec lives in `PRD.md` — re-read it before any non-trivial change, as it states non-negotiable constraints (air-gap readiness, no hardcoded secrets, idempotency, nested-virt passthrough).

## Two-stage deploy pipeline

The stages are coupled by a manual handoff and must be run in order:

1. **Provision (OpenTofu):** `cd tofu && tofu init && tofu apply -auto-approve` brings up `playground_net` (NAT, 10.0.10.0/24) and `var.vm_count` Ubuntu Noble VMs with cloud-init.
2. **Inject IPs:** `tofu output vm_ips` lists DHCP-assigned addresses. These must be written into `ansible/inventory.ini` under `[playground]` as `pg-node-N ansible_host=10.0.10.X ansible_user=ubuntu` — there is no dynamic inventory script yet.
3. **Configure (Ansible):** `cd ansible && ansible-playbook -i inventory.ini site.yml` runs the `docker` then `redroid` roles on every host in `[playground]`.
4. **Connect:** `adb connect <VM_IP>:5555` from the host.

Teardown: `cd tofu && tofu destroy -auto-approve`.

## Architecture notes that aren't obvious from a single file

- **There are two `main.tf` files.** `/main.tf` at the repo root is a stub left over from early scaffolding and only declares the network. The real, complete OpenTofu config is in `tofu/main.tf` (volumes, cloud-init disks, VM domains, host-passthrough CPU). Always edit under `tofu/` unless you're intentionally retiring the stub.
- **Nested virtualization is load-bearing.** `cpu { mode = "host-passthrough" }` in `tofu/main.tf` is required so Redroid containers inside the guest can access the binder/ashmem kernel features. Do not change it to a generic CPU mode.
- **Cloud-init wires SSH access.** `tofu/cloud_init.cfg` is a `templatefile` that injects `var.ssh_public_key_path` (default `~/.ssh/id_rsa.pub`) for the `ubuntu` user. SSH password auth is disabled — losing the key means recreating the VM.
- **Redroid role mounts binderfs.** `ansible/roles/redroid/tasks/main.yml` modprobes `binder_linux`/`ashmem_linux` (ignored on failure since modern kernels may bake them in), mounts `/dev/binderfs`, then runs the `redroid/redroid:11.0.0-latest` container `--privileged` with port 5555 exposed. Privileged + binderfs are both required; removing either breaks Android boot inside the container.
- **Docker role is order-sensitive.** It removes distro `docker.io`/`containerd` before installing `docker-ce` from Docker's apt repo, and adds the `ubuntu` user to the `docker` group. Re-running is safe (idempotent) but reordering will break a fresh box.

## Variables worth knowing (`tofu/variables.tf`)

`vm_count` (default 1), `vm_memory` MB (4096), `vm_vcpu` (2), `ssh_public_key_path`, `ubuntu_image_url` (Noble cloud image). Override via `-var` or `terraform.tfvars`; never hardcode secrets in `.tf` files.

## Repo context: multi-agent workflow

`setup.md`, `.antigravity/`, `.cursor/`, `.opencode.json`, and the `ai/` directory tree describe a multi-tool routing scheme (OpenCode for architecture, Antigravity for implementation, Cursor for review, Codex for alternative patches). Per `setup.md`, **Claude Code's role here is the local repo-debugging specialist** — invoked for difficult repo-local fixes, test failures, deep multi-file refactors, and independent implementation review, not as the default driver. The `ai/` subdirectories (`global_context.md`, `architecture/`, `engineering/`, `handoffs/`, etc.) are placeholders for that workflow and are currently empty.

### Project-local Claude subagents (`.claude/agents/`)

- `code-reviewer` — PRD-conformance + idempotency + secrets review on diffs under `tofu/` and `ansible/`. Use after edits, before commit.
- `debugger` — Localizes failures across the tofu → cloud-init → ansible → redroid → ADB pipeline. Use when a stage breaks.
- `qa-engineer` — Static + live + idempotency test matrix. Use before cutting a PR / release.

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
