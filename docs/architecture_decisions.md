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
