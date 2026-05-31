# Product Requirements Document

## Product Intent

The playground is a local-first lab platform for defining, operating, and
inspecting infrastructure experiments. The operator describes lab intent in a
YAML config tree, validates and resolves that intent through a Python control
layer, and eventually applies it through visible OpenTofu, Ansible, Docker, and
provider adapters.

The most accurate source of product intent is:

```text
docs/product/requirements.md
docs/product/user_stories.md
docs/product/mvp_scope.md
```

This document is the concise root PRD. If details conflict, prefer
`docs/product/requirements.md`, then current code/tests, then this summary.

## Primary User

The primary user is a technical operator who is comfortable with Linux,
virtualization, Docker, networking, Android, and security experiments. The
product should provide guardrails, diagnostics, and visibility, while still
trusting the operator to make advanced or risky lab choices.

## Core Goals

- Define reproducible named labs from YAML config trees.
- Operate one active lab at a time in the first version.
- Manage VMs, Docker workloads, and virtual networks first.
- Support host containers and VM-hosted containers.
- Keep network topology first-class, including NAT, isolated/no-internet, and
  routed networks.
- Keep generated state, logs, runs, cache, and artifacts project-local under
  `.playground/`.
- Support offline operation through configurable artifact sources and local
  caches.
- Keep backend modules visible and editable instead of hiding OpenTofu and
  Ansible behind opaque automation.
- Leave room for Android/Redroid, traffic capture, and security lab presets.

## Current Baseline

The working infrastructure baseline is:

```text
tofu/ -> ansible/ -> Docker/Redroid -> ADB
```

This baseline provisions local KVM/libvirt VMs with OpenTofu, configures them
with Ansible, installs Docker, and runs Redroid containers for Android
experiments.

The emerging Python control layer is:

```text
config/
src/playground/config/
src/playground/models/
src/playground/validation/
```

The two layers are not unified yet. The Python layer must prove read-only
validation and inspection before it automates or replaces backend operations.

## MVP Outcome

The operator can define a generic infra lab in YAML, validate it, inspect the
resolved model, see a plan, apply it on a local libvirt host, inspect VMs,
networks, Docker readiness, and destroy the lab with structured state and logs
retained locally.

## MVP Scope

Included:

- named lab definitions
- one active lab
- `local-libvirt` backend
- VM roles: `generic-node`, `docker-host`, `router`
- network profiles: `nat`, `isolated`, `routed`
- config validation and actionable diagnostics
- project-local `.playground/` state
- operation run/log model
- doctor/readiness checks
- offline artifact source model
- CLI-first operations

Deferred:

- full TUI
- cloud providers

  > Update (2026-05-31): a `cloud-digitalocean` backend has since shipped
  > (validated live); see `docs/architecture/cloud_digitalocean_design.md`.
- packet capture workflows
- Android device lifecycle automation beyond the current Redroid baseline
- full Docker Compose/Swarm execution if it expands the first slice too much

## Near-Term Product Direction

The next safe implementation slice is read-only CLI support:

```text
playground validate
playground lab list
playground lab show <name>
```

Backend automation should wait until validation and resolution cover required
defaults, workload placement, routing intent, budget totals, offline artifacts,
and source tracking.

## Non-Functional Requirements

- Extensible: add resource types, providers, roles, and presets without
  redesigning the core model.
- Inspectable: plans, logs, generated backend files, and state should be easy to
  locate.
- Recoverable: failed runs leave enough state and logs to continue or clean up.
- Idempotent: repeated apply/configure should avoid unnecessary churn.
- Portable: lab intent should be backend-neutral where possible.
- Offline-capable: offline mode must not depend on uncontrolled internet access.
- Conservative defaults: defaults should suit a local tower with limited
  resources.
- User-trusting: warn about risk, but do not block advanced usage unless strict
  mode is explicitly enabled.
