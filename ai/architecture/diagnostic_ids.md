# Diagnostic ID Registry

`Diagnostic.id` namespaces are reserved per area so teams don't collide
on identifiers. The full `Diagnostic` shape lives in
`ai/architecture/shared_contracts.md §2`. This document lists the
reserved prefixes and the known IDs in each.

Adding a new ID is a documentation change, not a contract change:
append it under the owning prefix in this file. Reusing an existing
ID with new semantics IS a contract change — bump `apiVersion` or
pick a new ID.

Severity is conventionally suggested below but the runtime severity
is whatever the producer assigns.

## Reserved prefixes

| Prefix | Owner | Examples |
| --- | --- | --- |
| `config.*` | Team A — config loader, validator, resolver. | `config.reference.unknown_role`, `config.schema.missing_field` |
| `state.*` | Team A — `.playground/` filesystem, runs, retention. | `state.write_failed`, `state.run.id_collision` |
| `event.*` | Team A — event bus / subscribers. | `event.subscriber_failed` |
| `doctor.*` | Team B — host/backend readiness checks. | `doctor.binary_missing`, `doctor.kvm.unavailable` |
| `backend.<adapter>.*` | Team B — per-adapter (e.g. `backend.tofu.*`, `backend.ansible.*`, `backend.docker.*`). | `backend.tofu.plan_failed`, `backend.ansible.role_missing` |
| `artifact.*` | Team B — artifact resolver / cache. | `artifact.offline_violation`, `artifact.checksum_mismatch` |
| `cli.*` | Team C — CLI surface (flag parsing, output formatting). | `cli.flag.unknown`, `cli.output.format_error` |
| `tui.*` | Team C — TUI surface. | `tui.layout.unavailable` |

## Known IDs

The following IDs are explicitly referenced by `shared_contracts.md`
or by current task breakdowns and should not be renamed without
updating all three docs.

### config.*

- `config.reference.unknown_role` — a Lab references a role name that
  is not defined under `config/roles/`. **severity:** `error`.
- `config.reference.unknown_network` — a Lab/VM references a network
  name not defined in `spec.networks`. **severity:** `error`.
- `config.reference.unknown_command` — a Lab `commands.enabled` entry
  references a preset not defined under `config/commands/`.
  **severity:** `error`.
- `config.reference.ansible_role_missing` — a role preset references
  an ansible role that does not exist under `ansible/roles/`. See
  `shared_contracts.md §11.3`. **severity:** `warning`.
- `config.schema.missing_field` — required field absent. **severity:**
  `error`.
- `config.schema.unknown_field` — unrecognized key under a known
  `kind`. **severity:** `warning`.
- `config.identity.duplicate_name` — two objects share `metadata.name`
  within the same `kind`. **severity:** `error`.

### state.*

- `state.write_failed` — atomic-replace failed (disk full, permission).
  **severity:** `error`.
- `state.run.id_collision` — generated `run_id` already exists.
  **severity:** `error`.
- `state.retention.skipped` — retention policy would delete a run
  outside the safe set. **severity:** `warning`.

### artifact.*

- `artifact.offline_violation` — offline mode required local artifact
  but resolver attempted (or would attempt) a network fetch. See
  `shared_contracts.md §11.4`. **severity:** `error`.
- `artifact.checksum_mismatch` — cached file checksum disagrees with
  declared source. **severity:** `error`.

### backend.*

- `backend.tofu.plan_failed` — `tofu plan` returned a non-zero exit
  code. **severity:** `error`.
- `backend.ansible.role_missing` — runtime equivalent of the
  validator-time `config.reference.ansible_role_missing`, raised when
  apply actually invokes ansible. **severity:** `error`.

### doctor.*

- `doctor.binary_missing` — required binary not on PATH.
  **severity:** `error` for required, `warning` for optional.
- `doctor.kvm.unavailable` — KVM CPU capability or modules absent.
  **severity:** `error`.

This list is not exhaustive — new IDs join the appropriate prefix on
first use. The point of this file is to prevent two teams inventing
the same identifier with conflicting meanings.
