# Engineering Principles

These principles guide implementation choices across the playground project.
They are derived from `docs/product/requirements.md` and the current system
design.

## 1. Local-First, Portable Later

Build for the local Ubuntu/KVM/libvirt tower first. Keep the model portable
enough for cloud or remote providers later, but do not add cloud abstractions
before the local path is proven.

## 2. YAML Intent Before Backend Mutation

User-authored YAML is the source of reproducible lab intent. The platform must
load, validate, and resolve YAML intent before it mutates infrastructure.

## 3. Read-Only Proof Before Apply

Every new control-layer capability should prove itself read-only before it
drives OpenTofu, Ansible, Docker, or host mutation. The current next slice is
`validate`, `lab list`, and `lab show`.

## 4. Keep Backends Visible

OpenTofu and Ansible are not hidden implementation details. The platform may
render inputs, wrap commands, and capture outputs, but backend modules remain
visible, editable, and inspectable.

## 5. Backend-Neutral Core

Core config and resolved models describe lab intent in generic terms: labs,
VMs, networks, roles, workloads, artifacts, commands, budgets, and provider
settings. Provider-specific detail belongs in provider config or adapter code.

## 6. Project-Local Generated State

Generated state, rendered files, inventories, run records, logs, caches, and
artifacts belong under `.playground/`. User-authored config belongs outside
`.playground/` and is intended to be committed.

## 7. Idempotency Is Mandatory

Repeated `tofu apply`, Ansible runs, and future platform operations should avoid
unnecessary churn and must not degrade existing state.

## 8. Offline Readiness Is A Design Constraint

Remote artifact defaults are acceptable for online use, but artifact sources
must be configurable. When `offline: true`, validation and planning must reject
uncontrolled internet downloads before apply.

## 9. Diagnostics Over Crashes

User-facing config and validation failures should return structured diagnostics
with severity, path, message, and suggestion where possible. Exceptions are for
programmer errors or unrecoverable internal failures.

## 10. Trust The Operator

The operator is technical and trusted. Warn clearly about risky or
non-reproducible choices, but only block hard errors or policies explicitly set
to strict mode.

## 11. Small Sequential Slices

Work moves through the sequential pipeline in `docs/workflow.md`. Prefer small
vertical slices with clear acceptance criteria over large rewrites.

## 12. Tests Scale With Risk

Narrow changes need narrow tests. Cross-module contracts, config schema changes,
backend wrappers, and user-facing workflows require broader regression coverage.

## 13. Preserve The Working Baseline

The current `tofu/ -> ansible/ -> Redroid -> ADB` path is valuable. Do not break
it while building the Python control layer. Wrap or bridge it incrementally.

## 14. Document Decisions When They Shape Future Work

Architecture decisions that constrain future implementation should be recorded
in `docs/architecture_decisions.md`.
