# Architecture Decisions

This file records durable architecture decisions. It is intentionally compact:
add a new entry when a decision constrains future implementation, not for every
small task.

## ADR-0001: Sequential Workflow With Claude And Codex

Status: accepted

Decision:

- Work sequentially on `main` unless the user explicitly asks for a short task
  branch.
- Use the pipeline in `docs/workflow.md`: plan, design, implement, test,
  review, integrate.
- Use Claude and Codex subagents as scoped specialists and quality gates, not as
  parallel delivery teams.

Rationale:

- The previous team/parallel workflow created coordination overhead and
  coupling.
- The project is moving faster with one clear source of work and targeted
  specialist reviews.

Consequences:

- `AGENTS.md`, `CODEX.md`, and `CLAUDE.md` are active agent guidance.
- The old `ai/`, Antigravity, Cursor, and OpenCode workflow files are not
  active project guidance.

## ADR-0002: Python Control Layer With Visible OpenTofu And Ansible Backends

Status: accepted

Decision:

- Python is the control-layer implementation language.
- OpenTofu and Ansible remain visible backend modules under `tofu/` and
  `ansible/`.
- The Python layer may validate config, render inputs, wrap commands, and
  capture outputs, but should not hide or prematurely rewrite the backend
  modules.

Rationale:

- The current backend path is already useful and inspectable.
- Visible backend modules make debugging, review, and operator override easier.

Consequences:

- Backend automation must be incremental.
- Early Python work focuses on read-only config and CLI behavior.

## ADR-0003: YAML Config Tree As Primary Lab Intent

Status: accepted

Decision:

- User-authored lab intent lives in YAML under `config/`.
- Presets for roles, networks, providers, commands, artifacts, and labs are
  editable from day one.
- Runtime overrides are temporary by default and later live under `.playground/`.

Rationale:

- The operator wants high configurability and reproducibility.
- YAML is readable, versionable, and easy to inspect.

Consequences:

- Config validation quality is central to product quality.
- Backend-specific settings must stay separated from generic lab intent.

## ADR-0004: Read-Only CLI Before Backend Automation

Status: accepted

Decision:

- Implement `playground validate`, `playground lab list`, and
  `playground lab show <name>` before plan/apply/status/destroy wrappers.

Rationale:

- The config plane currently describes richer intent than the backend can
  execute.
- Read-only CLI proves loader, validation, diagnostics, and resolution without
  risking real infrastructure.

Consequences:

- Backend automation waits for validation hardening and resolved-model
  completion.
- TUI work waits for stable CLI/core behavior.

## ADR-0005: `.playground/` Is The Generated State Root

Status: accepted

Decision:

- Generated state, run records, logs, rendered files, inventories, caches, and
  artifacts live under `.playground/`.
- `.playground/` is Git-ignored.
- User-authored config remains outside `.playground/`.

Rationale:

- Project-local generated state makes cleanup, inspection, and portability
  straightforward.
- Separating authored config from generated state reduces accidental commits and
  makes failures easier to inspect.

Consequences:

- Cleanup commands must never remove user-authored config.
- Backend wrappers should render generated files under `.playground/`, not into
  committed config unless explicitly requested.

## ADR-0006: cloud-digitalocean As The First Cloud Backend

Status: accepted

Context:

The platform had two local backends (`local-libvirt`, `local-vbox`). A cloud
backend was deferred in the original MVP scope. As of 2026-05-31 a
`cloud-digitalocean` backend was shipped and validated live end-to-end.
Several design choices had to be made to keep the cloud path safe and
consistent with existing platform constraints.

Decision:

1. **Per-lab isolated tofu state.** Each lab's OpenTofu root and state file
   live under `.playground/state/cloud-digitalocean/<lab>/` (generated,
   git-ignored). This avoids the global-state pitfall where two labs running
   against the committed `tofu/` root would collide. The committed `tofu/`
   remains the local-libvirt root only.

2. **Credentials from environment only.** `$DIGITALOCEAN_TOKEN` is the sole
   credential path. It is never written to HCL, tfvars files, or log output.
   The runner checks for the env var before any tofu invocation and emits
   `runtime.doctor.cloud_token_missing` if absent.

3. **`suspend` destroys Droplets; `resume` rebuilds from config.** Powered-off
   DigitalOcean Droplets still incur full hourly billing. `suspend` is therefore
   implemented as a destroy of the Droplets (not a power-off), and `resume`
   is a fresh apply from the lab config. Disk state is not preserved across a
   suspend/resume cycle. This is prominently documented; operators who need
   persistence must use `apply`/`destroy` manually.

4. **cloud-init readiness gate is advisory.** DigitalOcean's vendor first-boot
   script runs inside cloud-init after the user-data stage completes, which
   dirties `cloud-init status --wait` with a non-`done` result even when the
   user payload has fully run. The `wait-for-vms-ready` step therefore treats
   cloud-init status as advisory for cloud labs; Ansible is the real gate
   (gather_facts success implies SSH + Python are available).

5. **cloud-init user-data must be ASCII-only.** DigitalOcean's ConfigDrive
   implementation does not accept non-ASCII bytes in user-data. Templates and
   SSH public key values must be validated for ASCII before injection.

6. **Redroid/nested-virt not supported on DigitalOcean.** DO Droplets do not
   expose nested virtualization features required by the binder kernel module.
   Labs targeting the `cloud-digitalocean` backend must not include the
   `redroid-host` role; the validator emits a warning if they do.

Consequences:

- A `dispatch.py` backend registry routes `ResolvedLab.backend` to the
  correct adapter; all three backends (local-libvirt, local-vbox,
  cloud-digitalocean) share the same CLI surface.
- Cloud labs add `suspend` and `resume` verbs; local backends reject them
  with `runtime.backend.verb_not_supported`.
- `playground doctor` gains a `runtime.doctor.cloud_token_missing` check for
  the cloud backend path.
- The per-lab state directory convention (`.playground/state/<backend>/<lab>/`)
  is the standard pattern for any future cloud backend.
