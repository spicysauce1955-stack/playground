# Shared Contracts

This document fixes the eight cross-team contracts listed in
`ai/engineering/team_work_plan.md` §7. These shapes are the smallest set
that Team A (config/state/events), Team B (backends/runtime), and Team C
(CLI/TUI) must agree on before independent coding begins.

The shapes here are **logical** — they describe field names, types, and
invariants, not a concrete language binding. Team A owns these and
publishes the implementation under `src/playground/models/` and
`src/playground/events/`. Other teams consume them as imported types.

Any change to a field name, type, or invariant requires updating this
document and notifying the other teams. Additive changes (new optional
fields, new enum values that downstream tolerates) are safe; renames,
removals, or required-field additions are breaking.

## 1. Versioning

All public contracts carry an explicit version in their wire form:

```text
apiVersion: playground/v1
kind: <ContractName>
```

The `v1` track is open until first `integration/mvp-platform` → `main`
merge. Breaking changes after that require `v2`.

## 2. Diagnostic

A single problem report. Used by validation, doctor, planning, and
backend pre-flight.

Fields:

- `id` — short stable identifier (e.g. `config.reference.unknown_role`).
- `severity` — one of `error`, `warning`, `info`.
- `message` — human-readable, one sentence preferred.
- `source` — optional file location.
  - `path` — repo-relative path.
  - `line` — 1-based line number, optional.
  - `column` — 1-based column, optional.
- `key_path` — optional JSON-pointer-like path inside the file
  (e.g. `spec.vms[1].role`).
- `suggestion` — optional remediation hint, one sentence.
- `tags` — optional list of strings for filtering.

Invariants:

- `severity = error` blocks `apply`, `plan` may proceed if any consumer
  opts to "show errors anyway" but exit code remains non-zero.
- `id` namespaces are owned per area: `config.*`, `doctor.*`,
  `backend.*`, `state.*`.

Consumers: CLI/TUI (rendering), planner (early bail), runs (attach to
`OperationRun.diagnostics`).

## 3. ResolvedLab

The output of the resolver — a backend-neutral, fully expanded view of
one lab, ready for planning. It is the **input contract** to the
backend adapters.

Top-level fields:

- `api_version` — `playground/v1`.
- `lab_name` — string.
- `description` — string, optional.
- `tags` — list of strings.
- `backend` — string, currently always `local-libvirt`.
- `offline` — bool.
- `budget` — `Budget`.
- `defaults` — `ResolvedDefaults`.
- `providers` — map of provider name → opaque provider settings.
- `networks` — list of `ResolvedNetwork`.
- `vms` — list of `ResolvedVm`.
- `workloads` — list of `ResolvedWorkload`.
- `commands` — list of `ResolvedCommand`.
- `artifacts` — `ResolvedArtifacts`.
- `runtime_overrides` — list of `RuntimeOverride`, possibly empty.
- `source_map` — mapping of each top-level resource to the file/key it
  originated from, for diagnostics.

`ResolvedVm`:

- `name`, `role`, `image` (artifact ref), `vcpu`, `memory_mb`,
  `disk_gb`, `networks` (list of network names), `ssh.user`,
  `ssh.public_key_path`, `provisioners` (list of `{ansible_role: ...}`
  for now), `tags`, `provider_overrides`.

The user-authored YAML nests these under `resources: {vcpu, memory_mb,
disk_gb}`; the resolver flattens them onto `ResolvedVm`. Field names
are authoritative here; `ai/architecture/system_design.md §4` is a
shorter summary and defers to this section.

`ResolvedNetwork`:

- `name`, `intent` (`nat` | `isolated` | `routed`), `cidr`,
  `internet_access` (`true` | `false` | `configurable`), `dns.enabled`,
  `routes` (list, may be empty), `provider_overrides`, `tags`.

`ResolvedWorkload`:

- `name`, `type` (`container` | `compose` | `swarm`), `source` (image
  ref or compose path), `placement` (`{target_role, target_vm,
  target_tag}` — exactly one set or `auto`), `networks`, `ports`,
  `volumes`, `environment`, `resources`, `tags`.

`ResolvedCommand`:

- `name`, `description`, `target` (`TargetSelector`),
  `command.shell`, `working_directory`, `environment`,
  `timeout_seconds`, `escalation.become`.

`TargetSelector` — exactly one of the following keys is set:

- `role: <role-name>` — every VM with this role.
- `vm: <vm-name>` — a single VM by name.
- `tag: <tag>` — every VM carrying this tag.
- `any: true` — every VM in the lab.

Invariants:

- All cross-references are resolved by name: references in the source
  YAML have been validated, and unknown references would have surfaced
  as `Diagnostic` entries before a `ResolvedLab` is produced.
- `runtime_overrides` are applied on top of the YAML-derived fields and
  re-flagged in `source_map` so plan/status can show them as
  temporary.
- `source_map` keys use dotted-path notation with indexed arrays:
  `spec.vms[0]`, `spec.networks[lab-private]` (string key when the
  list element has a stable `name`). Diagnostics produced after
  resolution must round-trip through this format.

### 3.1 Auxiliary shapes referenced above

`Budget`:

- `mode` — `strict` | `permissive`. `strict` blocks plan if limits are
  exceeded; `permissive` emits warnings.
- `max_vcpu`, `max_memory_mb`, `max_disk_gb`, `max_vms`,
  `max_containers` — integer limits.

`ResolvedDefaults`:

- `backend` — string.
- `offline` — bool.
- `vm` — `{image, resources: {vcpu, memory_mb, disk_gb}, ssh: {user,
  public_key_path}}`.
- `network` — `{profile}`.
- `retention` — `RetentionPolicy`.

`ResolvedArtifacts`:

- `vm_images` — map of artifact name → `{type, version, source,
  local_path, available_locally: bool, available_remote: bool}`.
- `tofu_providers` — map of name → `{version, source, local_path?}`.
- `ansible_collections` — map of name → `{version, source,
  local_path?}`.
- `docker_images` — map of name → `{image, registry, local_archive?,
  available_locally: bool, available_remote: bool}`.

A resolved artifact is the union of the source declared in
`config/artifacts/sources.yaml` and the observed cache state from
`.playground/cache/`. Backend adapters consume the resolved form and
do not re-read `sources.yaml`.

`RuntimeOverride`:

- `id` — short opaque string, unique per active lab.
- `target` — JSON-pointer-like key path into `ResolvedLab` (e.g.
  `vms[docker1].memory_mb`).
- `value` — new value.
- `reason` — optional human note.
- `created_at` — ISO 8601 UTC.

Runtime overrides live in `.playground/state/overrides/<lab>.json`
and are applied each time the resolver runs. Promoting an override to
permanent YAML is out of scope for v1 but the file format leaves room
for a `promoted_to: <yaml-path>` field later.

Consumers: planner (Team B), CLI plan view (Team C), state snapshot.

## 4. OperationRun

A single invocation of a mutating or inspecting operation (validate,
plan, apply, destroy, run-command, doctor, cache-prepare). Created
before any side effect, finalized at the end.

Fields:

- `run_id` — opaque string, lexicographically sortable by start time.
  Suggested format: `YYYYMMDDTHHMMSSZ-<6char>`.
- `lab` — lab name or `null` for lab-independent runs (doctor,
  cache-prepare-global).
- `operation` — one of `validate`, `plan`, `apply`, `destroy`,
  `status`, `doctor`, `run-command`, `cache-prepare`, `runs-show`.
- `status` — `pending`, `running`, `succeeded`, `failed`, `cancelled`.
- `start_time` — ISO 8601 UTC.
- `end_time` — ISO 8601 UTC or `null` while running.
- `backend_tools` — list of `{name, version}` actually invoked (e.g.
  `tofu`, `ansible-playbook`).
- `affected_resources` — list of `{kind, name, action}` where action is
  `create | update | delete | noop | unknown`.
- `diagnostics` — list of `Diagnostic`.
- `summary_path` — repo-relative path to the human summary.
- `logs_path` — repo-relative path to the JSONL event log.
- `exit_code` — integer, matches the CLI exit-code rules (§9).

Invariants:

- `run_id` is unique across `.playground/runs/`.
- `status` is monotonic: `pending → running → (succeeded|failed|cancelled)`.
- `end_time` is set when `status` leaves `running`.

Storage layout:

```text
.playground/runs/<run_id>/
  run.json           — this record
  summary.md         — human summary
  logs/events.jsonl  — append-only OperationEvent stream
  logs/<tool>.log    — optional raw tool logs
```

Consumers: every CLI command writes one, TUI run-viewer reads them.

## 5. OperationEvent

A single line in the event stream of one `OperationRun`. Both Team A
infra subscribers and Team B backend wrappers publish these; Team C
consumes them for live UI.

Fields:

- `event_id` — monotonic per-run integer.
- `run_id` — the parent run.
- `lab` — copied from run for ergonomics, may be `null`.
- `timestamp` — ISO 8601 UTC with millisecond precision.
- `level` — `debug` | `info` | `warn` | `error`.
- `event_type` — see enum below.
- `producer` — `core` | `backend.tofu` | `backend.ansible` |
  `backend.docker` | `doctor` | `cli`.
- `backend` — optional backend name (`local-libvirt`, etc).
- `resource_ref` — optional `{kind, name}` the event is about.
- `phase` — optional free-form phase tag (`init`, `apply`,
  `inventory`, `task`, `cleanup`).
- `message` — human-readable, one short line.
- `data` — optional JSON object for structured payload.

Event types (`event_type`):

- `run.started`, `run.finished`, `run.cancelled`.
- `phase.started`, `phase.finished`.
- `resource.planned`, `resource.applied`, `resource.failed`,
  `resource.observed`.
- `diagnostic.emitted`.
- `command.started`, `command.output`, `command.finished`.
- `progress` — for long-running operations with %/n-of-m payload.

Invariants:

- The first event in any run is `run.started`; the last is one of
  `run.finished` / `run.cancelled`.
- `event_id` increases by 1 per event within a run.
- JSONL writer is append-only and `flush()`-after-write so concurrent
  readers can tail.

Consumers: JSONL logger, human-log subscriber, run-summary subscriber,
status-snapshot subscriber, CLI streaming output, TUI run-viewer.

## 6. ResourceStatus

A point-in-time observation of one managed resource. Returned by
backend adapters (`status` operation) and embedded in
`OperationRun.affected_resources` extended views.

Fields:

- `kind` — `vm` | `network` | `workload` | `container` |
  `compose-stack` | `swarm-service` | `route`.
- `name` — string, unique within `(lab, kind)`.
- `state` — `unknown` | `absent` | `pending` | `running` | `stopped` |
  `failed` | `degraded`.
- `backend` — string, e.g. `local-libvirt`, `docker`.
- `provider_ids` — list of strings (libvirt domain UUID, docker
  container ID, etc.).
- `addresses` — list of `{network, ip, mac?}` for VMs/containers.
- `attributes` — free-form map for kind-specific data (e.g. Docker
  engine version, Swarm role).
- `last_observed` — ISO 8601 UTC.
- `notes` — optional human string.

Invariants:

- `state = absent` and `state = unknown` are distinct: `absent` is a
  positive confirmation the backend says it isn't there.
- `addresses` may be empty even for `running` if DHCP hasn't completed
  — consumers should treat empty as "not yet observed".

Consumers: CLI status view, TUI dashboard, state snapshot writer.

## 7. ProviderAdapter

The interface Team B implements per backend. Team A and Team C call
into it but do not subclass it.

Required operations (logical, language-agnostic):

- `plan(resolved_lab, run, event_bus) -> Plan` — produce a `Plan`
  describing intended changes; emits `phase.*` and `resource.planned`
  events; never mutates real resources.
- `apply(resolved_lab, plan, run, event_bus) -> ApplyResult` — execute
  the plan; emits `resource.applied`/`resource.failed` and finishes
  with a populated `ApplyResult.statuses`.
- `destroy(resolved_lab, run, event_bus) -> DestroyResult` — remove all
  managed resources for the lab.
- `status(resolved_lab, run, event_bus) -> list[ResourceStatus]` —
  observe current state; never mutates.
- `doctor(event_bus) -> list[Diagnostic]` — readiness checks for this
  backend on the local host.

`Plan` shape:

- `lab` — lab name.
- `backend` — backend name.
- `actions` — list of `PlanAction`.
- `rendered_inputs` — list of `{path, content_ref}` pointing into
  `.playground/state/rendered/`.
- `warnings` — list of `Diagnostic` (severity `warning` or `info`
  only; `error` should have aborted plan).
- `budget_check` — `{passes: bool, details: list[Diagnostic]}`.
- `created_at` — ISO 8601 UTC.

`PlanAction`:

- `kind` — one of `vm` | `network` | `workload` | `route` |
  `inventory` | `rendered-file`.
- `name` — string, unique within `(plan, kind)`.
- `action` — `create` | `update` | `delete` | `noop` | `unknown`.
- `before` — optional current state snippet (small JSON object) for
  display.
- `after` — optional desired state snippet.
- `reason` — short human string explaining why this action is needed.

`ApplyResult`:

- `plan` — the `Plan` that was applied (or its `created_at`+hash for
  identity).
- `started_at`, `finished_at` — ISO 8601 UTC.
- `succeeded` — bool; `true` only if every action reached its target
  state.
- `statuses` — list of `ResourceStatus` observed after apply.
- `action_outcomes` — list of `{name, action, outcome: "ok" | "failed"
  | "skipped" | "unknown", error_message?}` aligned 1:1 with
  `plan.actions`.
- `diagnostics` — list of `Diagnostic` accumulated during apply.

`DestroyResult`:

- `started_at`, `finished_at` — ISO 8601 UTC.
- `succeeded` — bool.
- `removed` — list of `{kind, name}` resources confirmed removed.
- `remaining` — list of `{kind, name, reason}` resources still
  present (should be empty on success).
- `diagnostics` — list of `Diagnostic`.

Invariants:

- All adapter methods accept and use the same `EventBus` instance;
  they do not own their own logging path.
- Adapters never write outside `.playground/` and the directories
  named in `rendered_inputs`.
- Adapters never read user YAML directly; they only see
  `ResolvedLab`.
- Inspection methods (`status`, `doctor`) MUST NOT mutate any
  resource and MUST NOT write outside `.playground/`.

## 8. EventBus

Team A's in-process pub/sub for `OperationEvent`. One bus instance per
`OperationRun`; subscribers receive every event published during the
run.

Required operations (logical):

- `publish(event)` — append-only, totally ordered by `event_id`.
- `subscribe(subscriber)` — register before the first publish; receive
  every subsequent event in order.
- `close()` — final notification; subscribers flush.

Built-in subscribers (Team A owns these):

- `JsonlLogSubscriber` — writes `logs/events.jsonl`.
- `HumanLogSubscriber` — writes `logs/human.log` with one-line
  formatted events.
- `RunSummarySubscriber` — accumulates into `summary.md` at close.
- `StatusSnapshotSubscriber` — updates `.playground/state/status/<lab>.json`
  on `resource.observed` and terminal `resource.*` events.

Concurrency:

- MVP is single-threaded with synchronous fan-out: publish blocks until
  all subscribers consume.
- Subscribers must not raise back into the publisher; they catch their
  own errors and emit a `diagnostic.emitted` event of severity
  `warning` instead.

## 9. StateStore

Team A's filesystem-backed state API. All consumers go through this —
no direct `.playground/` writes from other teams.

Logical operations:

- `init()` — create `.playground/` skeleton, write `.gitignore` entry,
  idempotent.
- `get_active_lab() -> str | None`.
- `set_active_lab(name)`.
- `read_status_snapshot(lab) -> list[ResourceStatus]`.
- `write_status_snapshot(lab, statuses)`.
- `create_run(operation, lab) -> OperationRun` — allocates `run_id`,
  writes initial `run.json`, returns the in-flight handle.
- `finalize_run(run, status, exit_code)` — writes terminal `run.json`.
- `list_runs(filter?) -> list[OperationRun]`.
- `get_run(run_id) -> OperationRun`.
- `iter_run_events(run_id) -> Iterable[OperationEvent]`.
- `apply_retention(policy, dry_run=False) -> RetentionReport`.

`RetentionPolicy`:

- `runs.keep_last` — integer, minimum number of recent runs to keep.
- `runs.max_age_days` — integer, drop runs whose `end_time` is older
  than this many days, subject to `keep_last`.
- `logs.keep_per_run` — bool; when `false`, prune per-run logs after
  the summary has been written.
- `logs.compress_after_days` — integer; gzip per-run JSONL/raw logs
  older than this.

`RetentionReport`:

- `policy` — the `RetentionPolicy` applied.
- `dry_run` — bool.
- `actions` — list of `{path, action: "delete" | "compress" | "keep",
  reason}`.
- `freed_bytes` — integer, estimated when `dry_run=true`.

Filesystem layout:

```text
.playground/
  state/
    active-lab.json
    rendered/           # backend-rendered inputs (Team B writes here via adapter)
    tofu/               # provider-local OpenTofu state (configured by adapter)
    status/<lab>.json
  cache/
    artifacts/...
  runs/<run_id>/...
  logs/                 # only operator-facing rollups; per-run logs live under runs/
```

Invariants:

- `.playground/` is git-ignored.
- All writes are atomic-replace (write tmp + rename).
- `read_*` functions never block on writers (only complete records are
  surfaced).

## 10. CLI Command Names And Exit Codes

Final command names for MVP (from `ai/product/mvp_scope.md` §MVP
Commands; reproduced here as the contract):

```text
playground doctor
playground validate [--lab LAB]
playground lab list
playground lab select LAB
playground plan [--lab LAB]
playground apply [--lab LAB]
playground status [--lab LAB]
playground stop [--lab LAB]
playground destroy [--lab LAB]
playground run COMMAND_OR_PRESET [--target SELECTOR]
playground cache prepare [--lab LAB]
playground runs list
playground runs show RUN_ID
```

Global flags (Team C owns wiring, Team A owns semantics):

- `--lab LAB` — override active lab for this invocation only; not
  persisted.
- `--output human|json` — output mode; default `human`.
- `--no-color` — disable ANSI.
- `--quiet` / `--verbose` — adjust event level shown.

Exit codes:

- `0` — success.
- `1` — generic failure (unhandled error, raised exception).
- `2` — usage error (bad flags, unknown subcommand).
- `3` — validation/diagnostic error (any `Diagnostic` with severity
  `error` for the requested operation).
- `4` — doctor failure (any required check failed).
- `5` — backend operation failure (adapter reported failure).
- `6` — state/IO error (cannot write `.playground/`, lock contention).
- `130` — interrupted (Ctrl-C).

JSON output contract:

- All commands in `--output json` produce a single top-level JSON
  object on stdout.
- Required keys: `ok` (bool), `operation` (string), `run_id` (string
  or null), `data` (object).
- Diagnostics live under `data.diagnostics`.
- Tool/raw output goes to stderr or to `.playground/runs/<id>/logs/`,
  never into the JSON payload.

## 11. Open Items

- Whether `ResolvedLab` is exported as JSON Schema or as a typed model
  with derivable schema.
- Whether `OperationEvent.data` payloads get per-event-type schemas or
  remain free-form for MVP.
- Whether `ProviderAdapter` is async by default in the Python
  implementation, or sync with an explicit thread for backend
  subprocesses.

These are flagged in `ai/engineering/implementation_plan.md` Phase 1
exit criteria and `ai/architecture/config_design.md` §10.
