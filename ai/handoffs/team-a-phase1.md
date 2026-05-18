# Team A Phase 1 Handoff

Branch: `team/core-config-state`
Target: `integration/mvp-platform`
Author: Team A (core config, state, events)
Status: ready for review and merge

This handoff is the §3 deliverable the team work plan requires: design
notes, plan, code, tests, usage examples, and verification notes for
the Team A foundation slice.

## 1. Scope of this slice

Implements the **config + validation + resolution** half of Team A's
Phase 1 deliverable (`ai/engineering/implementation_plan.md §3`). It
does **not** yet implement `.playground/` state, run records, or the
event bus — those are the next slice (§9 below).

What landed:

- `config/` tree (12 YAML files: Defaults, ProviderConfig,
  ArtifactSources, three NetworkProfiles, three VmRoles, two
  CommandPresets, one sample Lab).
- Cross-team contract freeze: `ai/architecture/shared_contracts.md`
  (eight runtime contracts + on-disk YAML kinds + invariants).
- Diagnostic ID registry: `ai/architecture/diagnostic_ids.md`.
- Python package scaffold under `src/playground/` (Python 3.12+;
  Pydantic 2 / ruamel.yaml / Typer surface declared but not yet wired).
- Typed models for every YAML kind, the loader, the cross-reference
  validator, and the resolver that produces `ResolvedLab`.
- 69 unit tests covering happy paths, every Diagnostic ID's negative
  case, and an end-to-end resolution against the committed config.

What is **NOT** here yet (and is the next Team A slice):

- `StateStore` / `RunStore` implementations and `.playground/`
  initialization (`shared_contracts.md §9.1 / §9.2`).
- `OperationRun` + `OperationEvent` Pydantic models.
- `EventBus` and the four built-in subscribers (JSONL, human log,
  summary, status snapshot).
- Retention policy enforcement.
- Per-key `source_map` precision in `ResolvedLab` (currently coarse).
- Runtime override apply path (the field exists on `ResolvedLab` but
  is always an empty list).

## 2. Commit narrative

The 14 commits on this branch land in three groups; each group is
self-contained and bisectable.

**Foundation (commits 1–3)**

1. `28ec2c5` — `config/` YAML skeleton.
2. `cd6bb6e` — Initial `shared_contracts.md` (eight contracts).
3. `280a111` — Python package scaffold + smoke import test.

**Review-driven hardening (commits 4–7)**

4. `8a1114a` — Blocker fixes: auxiliary shapes (Budget, RetentionPolicy,
   Plan, ApplyResult, DestroyResult, ResolvedDefaults, etc.); VM field
   reconciliation; drop broken `[project.scripts]`.
5. `6b478fc` — PRD conformance §11: `playground-<lab>-*` libvirt naming
   for legacy coexistence; legacy `ansible/site.yml` status; missing
   router ansible role as a warning; air-gap invariants.
6. `6f4b5a5` — Design refinements: `StateStore`/`RunStore` split (ISP);
   open `OperationEvent.producer` enum; defer system_design event
   vocabulary to the freeze doc; remove `tests/__init__.py` files.
7. `3095f74` — Nice-to-haves: `__version__` via `importlib.metadata`;
   reserved `src/playground/models/schemas/`; new
   `ai/architecture/diagnostic_ids.md` registry.

**Implementation (commits 8–14)**

8. `f67675c` — Doc: on-disk YAML kinds + 7-step resolver pipeline in
   `shared_contracts.md §3.2`.
9. `cc65b9d` — Fix `ping-network.yaml`: `target.role: any` →
   `target.any: true`.
10. `e004d1a` — Base models: `StrictModel`, `Metadata`,
    `ResourceEnvelope`, `Diagnostic`, `SourceLocation`.
11. `38ae25f` — `playground.models.kinds`: one strict model per YAML
    kind, `parse_resource()` dispatcher, parametrized walk over every
    committed YAML.
12. `ce0a3cd` — `playground.config.{discovery,loader}`: never raises
    on user error, attaches `Diagnostic.source.path`/`key_path`.
13. `6b1f0d9` — `playground.validation.validator`: cross-reference
    walk, role-inheritance cycle detection, opt-in ansible role check.
14. `db31d42` — `playground.config.resolver` +
    `playground.models.resolved`: full 7-step pipeline.

## 3. Public surface for Teams B and C

These imports are the **stable** consumer surface. Adding to them is
safe; renaming or removing is a contract change.

```python
# Typed contract models — Team B + Team C consume.
from playground.models import (
    # Envelope and diagnostics
    Diagnostic, Severity, SourceLocation, Metadata, ResourceEnvelope,
    # On-disk kinds (user-authored YAML)
    Defaults, ProviderConfig, ArtifactSources, NetworkProfile, VmRole,
    CommandPreset, Lab,
    # Auxiliary shapes
    Budget, Resources, SshConfig, TargetSelector, RetentionPolicy,
    # Resolved (backend-neutral) model — Team B's input contract
    ResolvedLab, ResolvedVm, ResolvedNetwork, ResolvedWorkload,
    ResolvedCommand, ResolvedDefaults, ResolvedArtifacts,
    ResolvedArtifactImage,
    # Helpers
    parse_resource, KNOWN_KINDS,
)

# Pipeline entry points.
from playground.config import load_config, resolve_lab, LoadedConfig
from playground.validation import validate
```

**Not yet exported** (will arrive in the next slice):
`OperationRun`, `OperationEvent`, `EventBus`, `StateStore`, `RunStore`,
`Plan`, `ApplyResult`, `DestroyResult`, `ResourceStatus`. Their wire
shapes are frozen in `shared_contracts.md` — Team B can stub them
locally until they land.

## 4. Usage example (the full pipeline)

```python
from pathlib import Path
from playground.config import load_config, resolve_lab
from playground.validation import validate

# 1. Load the YAML tree. Never raises on user error.
loaded, load_diags = load_config(Path("config"))
print(f"load: {len(load_diags)} diagnostics")

# 2. Cross-reference check. Ansible-role check is opt-in.
val_diags = validate(loaded, ansible_roles_dir=Path("ansible/roles"))
errors = [d for d in val_diags if d.severity == "error"]
warnings = [d for d in val_diags if d.severity == "warning"]
print(f"validate: {len(errors)} errors, {len(warnings)} warnings")

# 3. Resolve a specific lab.
if not errors:
    resolved = resolve_lab(loaded, "generic-infra")
    for vm in resolved.vms:
        print(f"  {vm.name}: role={vm.role} vcpu={vm.vcpu} caps={vm.capabilities}")
```

Live output against the committed config (verified on Python 3.12.3,
2026-05-18):

```
load: 0 diagnostics
validate: 0 errors, 1 warnings
Resolved lab: generic-infra
  backend=local-libvirt offline=False
  networks: [('edge', 'nat', '10.20.10.0/24'),
             ('lab-private', 'isolated', '10.20.20.0/24'),
             ('routed-a', 'routed', '10.20.30.0/24')]
  vm node1: role=generic-node vcpu=1 mem=2048MB caps={}
  vm docker1: role=docker-host vcpu=2 mem=4096MB
              caps={'docker': True, 'compose': True, 'swarm': True}
  vm router1: role=router vcpu=1 mem=2048MB caps={'routing': True}
  commands: ['check-docker', 'ping-network']
```

The single warning is `config.reference.ansible_role_missing` against
`VmRole 'router'` — this is expected per `shared_contracts.md §11.3`;
Team B's Milestone 10 lands the `router` ansible role.

## 5. Diagnostic vocabulary in use

These IDs are emitted by the code on this branch (full registry:
`ai/architecture/diagnostic_ids.md`):

| ID | Severity | Where |
| --- | --- | --- |
| `config.yaml.parse_failed` | error | loader, malformed YAML or non-mapping top-level |
| `config.schema.kind_missing` | error | loader, no `kind:` |
| `config.schema.unknown_kind` | error | loader, `kind:` not in `KNOWN_KINDS` |
| `config.schema.kind_mismatch` | warning | loader, kind doesn't match directory |
| `config.schema.validation_failed` | error | loader, Pydantic validation |
| `config.identity.duplicate_name` | error | loader, repeated name within a kind |
| `config.reference.unknown_role` | error | validator, `vm.role` resolves nowhere |
| `config.reference.unknown_network` | error | validator, `vm.networks[]` / `workload.networks[]` |
| `config.reference.unknown_command` | error | validator, `commands.enabled[]` |
| `config.reference.unknown_provider` | error | validator, `lab.backend` |
| `config.reference.unknown_network_profile` | error | validator, `lab.networks[].profile` |
| `config.reference.unknown_image` | error | validator, `vmrole.image` |
| `config.reference.ansible_role_missing` | warning | validator, opt-in |
| `config.role.inheritance_cycle` | error | validator, `extends` cycle |
| `config.role.unknown_extends` | error | validator, `extends` points nowhere |

## 6. Test surface

69 tests under `tests/unit/`, all green on Python 3.12.3:

```
tests/unit/test_package_import.py            2 tests
tests/unit/models/test_base.py               7 tests
tests/unit/models/test_diagnostic.py         5 tests
tests/unit/models/test_kinds.py             24 tests (12 parametrized
                                            YAML walks + 12 focused)
tests/unit/config/test_loader.py            10 tests
tests/unit/config/test_resolver.py           9 tests
tests/unit/validation/test_validator.py     10 tests
```

The most load-bearing test is
`tests/unit/models/test_kinds.py::test_every_committed_yaml_parses` —
a `pytest.mark.parametrize` walk over every file under `config/`. If
a YAML field drifts from the contract, this test fails before any
other test runs.

How to run:

```bash
uv venv .venv --python python3.12 --clear   # or python3 -m venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest -q                          # 69 passed
```

`pyproject.toml` declares all dev dependencies. The smoke import test
(`tests/unit/test_package_import.py`) verifies the seven Team A
subpackages import cleanly without any Pydantic-touching code, so the
test suite still works against the empty scaffold in case anyone
bisects through commits 3–9.

## 7. Cross-team contracts summary

| Contract | Doc | Code | Status |
| --- | --- | --- | --- |
| `Diagnostic` | `shared_contracts.md §2` | `playground.models.Diagnostic` | Final |
| `ResolvedLab` | `§3` + `§3.1` + `§3.2` | `playground.models.ResolvedLab` | Final |
| `OperationRun` | `§4` | — | Doc only; code next slice |
| `OperationEvent` | `§5` | — | Doc only; code next slice |
| `ResourceStatus` | `§6` | — | Doc only; Team B will reference |
| `ProviderAdapter` (incl. Plan/ApplyResult/DestroyResult) | `§7` | — | Doc only; Team B implements |
| `EventBus` | `§8` | — | Doc only; code next slice |
| `StateStore` / `RunStore` | `§9.1` / `§9.2` | — | Doc only; code next slice |
| CLI commands + exit codes + JSON output | `§10` | — | Doc only; Team C wires |
| YAML kinds + resolver pipeline | `§3.2` | `playground.config.resolver` | Final |
| Air-gap / legacy / coexistence invariants | `§11` | (enforced at resolver/validator time later) | Doc only |

The five items still listed as "doc only" are the Phase 1 / Phase 2
exit gap. Their shapes are frozen — Team B and Team C may build
against them as if they exist; the implementations land in the next
Team A slice and will preserve the documented field names.

## 8. Merge readiness checklist

Per `team_work_plan.md §8` the merge order is:

1. **Team A** merges config/state/event foundations into
   `integration/mvp-platform`. ← *this PR (partial: config + validation
   + resolver portion)*
2. Team C merges CLI skeleton on top.
3. Team B merges backend render/doctor logic on top.
4. Team C updates views to consume Team B status/plan APIs.
5. Cross-team integration tests.
6. `integration/mvp-platform` → `main`.

Self-check against the §3 "must provide" list:

- [x] Design notes (`ai/architecture/shared_contracts.md`,
      `diagnostic_ids.md`, `system_design.md`,  `config_design.md`).
- [x] Implementation plan (`ai/engineering/implementation_plan.md`,
      updated `task_breakdown.md`).
- [x] Code (`src/playground/{models,config,validation}/`).
- [x] Tests (`tests/unit/`, 69 passing).
- [x] Usage examples (this file §4).
- [x] Run logs / verification notes (this file §4 + §6).

Self-check against `ai/engineering/team_work_plan.md §4` Acceptance
criteria:

- [x] "`playground validate` can validate sample configs." — `validate`
      API exists; CLI is Team C's wiring layer, but the underlying
      function is callable.
- [x] "A valid lab resolves into a normalized model." — `resolve_lab`
      produces `ResolvedLab` for `generic-infra`.
- [x] "Invalid references produce file/key/suggested-fix diagnostics." —
      every cross-reference Diagnostic carries `source.path`,
      `key_path`, and a `suggestion`.
- [ ] "Operation runs can be created without backend mutation." —
      pending next slice.
- [ ] "Fake long-running operations publish events consumed by multiple
      subscribers." — pending next slice.
- [ ] "`.playground/` is initialized and ignored by Git." — `.gitignore`
      ignores it; the init API is pending.

The three unticked items are the explicit scope of the next slice. The
recommendation is to merge this branch into `integration/mvp-platform`
now so Team B and Team C unblock against the stable contracts, rather
than wait for the state/events code to land.

## 9. Next slice (Team A, planned)

In commit order:

1. `playground.models.runs`: `OperationRun`, `OperationEvent`,
   `Severity` extensions per `shared_contracts.md §4–§5`. Tests.
2. `playground.state.store`: `StateStore` (init, active lab, status
   snapshot) atop atomic-replace file writes. Tests with `tmp_path`.
3. `playground.state.runs`: `RunStore` (`create_run`, `finalize_run`,
   `list_runs`, `iter_run_events`, retention). Tests.
4. `playground.events.bus`: synchronous `EventBus` + the four built-in
   subscribers (JSONL, human, summary, status snapshot). Tests with a
   fake long-running operation that publishes through every event
   type.
5. `playground.config.resolver` precision: thread `DiscoveredFile`
   through to populate `ResolvedLab.source_map` with per-key origins.
6. Retention policy enforcement and dry-run cleanup.

Estimated commits: 6–8. Estimated tests added: ~50.

## 10. Known cosmetic items (not blocking merge)

- `pyproject.toml` declares Textual as an optional `[tui]` extra but
  the TUI namespace is Team C's; the dep declaration is shape-only.
- The `Plan` / `ApplyResult` / `DestroyResult` shapes are documented in
  `shared_contracts.md §7` but not yet Pydantic models — Team B can
  either stub them locally or wait for the next Team A slice.
- `ResolvedLab.source_map` currently only carries one entry
  (`spec → config/labs/<name>.yaml`); per-key precision is item §9.5
  above.
- The validator's ansible-role check is opt-in via parameter; once
  Team B's adapter integrates the validator we'll likely want a
  module-level config knob so the CLI can flip it without touching
  argument shape.

## 11. Contact points

- Doc owner for `ai/architecture/shared_contracts.md`: Team A.
- Diagnostic ID registry owner: Team A. Add new IDs by appending
  under the owning prefix in `ai/architecture/diagnostic_ids.md`; no
  contract bump needed for additions.
- For any change to a Pydantic model's field name or required-ness,
  open an issue against this branch (or the integration branch
  post-merge) rather than editing locally on a feature branch —
  silent renames break the freeze.
