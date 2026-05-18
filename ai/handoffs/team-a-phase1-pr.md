# Pull Request: Team A Phase 1 — config skeleton, contracts, validation, resolution

**Source:** `team/core-config-state`
**Target:** `integration/mvp-platform`
**Type:** foundation; no runtime behavior change to existing `tofu/` or `ansible/` modules.

This is the draft body for the GitHub PR that opens when this branch is
ready to merge. Paste from "## Summary" downward into the `gh pr create
--body` payload (or the GitHub UI).

---

## Summary

- Lands Team A's Phase 1 foundation per `ai/engineering/team_work_plan.md §4`: the `config/` YAML tree, the shared cross-team contract document (`ai/architecture/shared_contracts.md`), the diagnostic ID registry, and the Pydantic models + loader + cross-reference validator + resolver that turn user YAML into a `ResolvedLab`.
- Freezes the 8 cross-team contracts (Diagnostic, ResolvedLab, OperationRun, OperationEvent, ResourceStatus, ProviderAdapter, EventBus, StateStore + RunStore split) plus auxiliary shapes (Budget, RetentionPolicy, Plan, ApplyResult, DestroyResult, RuntimeOverride, TargetSelector, ResolvedDefaults, ResolvedArtifacts) and the CLI command / exit-code / JSON-output contract.
- Adds PRD-conformance invariants: libvirt resource naming (`playground-<lab>-*`) for legacy coexistence with the existing `tofu/main.tf:playground_net`, air-gap mode enforcement, missing-ansible-role warning policy.
- 69 unit tests passing on Python 3.12.3 — including a parametrized walk over every committed YAML and a full end-to-end resolution of the `generic-infra` lab.

Detailed handoff: `ai/handoffs/team-a-phase1.md` (read this before reviewing the code).

## What landed

| Area | Files | Verified |
| --- | --- | --- |
| YAML config tree | `config/{defaults,providers,artifacts,networks,roles,commands,labs}/*.yaml` | parametrized parse test (12/12) |
| Cross-team contracts | `ai/architecture/shared_contracts.md` (12 sections) | doc-only |
| Diagnostic registry | `ai/architecture/diagnostic_ids.md` | 15 IDs registered, all emitted by code |
| Python package | `pyproject.toml`, `.gitignore`, `src/playground/` (7 subpackages) | `pip install -e ".[dev]"` then `pytest` |
| Models | `src/playground/models/{base,diagnostic,kinds,resolved}.py` | 36 unit tests |
| Loader + discovery | `src/playground/config/{discovery,loader}.py` | 10 unit tests |
| Validator | `src/playground/validation/validator.py` | 10 unit tests |
| Resolver | `src/playground/config/resolver.py` | 9 unit tests |

## What is intentionally NOT in this PR

- `StateStore` / `RunStore` filesystem code under `.playground/`.
- `OperationRun` / `OperationEvent` Pydantic models.
- `EventBus` and the four built-in subscribers.
- Retention enforcement.
- Per-key `source_map` precision (currently coarse; deferred to milestone 5.5).
- Anything from Team B (provider adapter, doctor) or Team C (CLI/TUI).

These are the next Team A slice; their contracts are frozen so Team B and Team C can start in parallel.

## How to verify locally

```bash
# 1. Install (uv recommended; falls back to python3 -m venv when available)
uv venv .venv --python python3.12 --clear
uv pip install --python .venv/bin/python -e ".[dev]"

# 2. Run the suite — expect 69 passed
.venv/bin/pytest -q

# 3. End-to-end smoke against the committed config
.venv/bin/python - <<'PY'
from pathlib import Path
from playground.config import load_config, resolve_lab
from playground.validation import validate

loaded, ld = load_config(Path("config"))
vd = validate(loaded, ansible_roles_dir=Path("ansible/roles"))
print(f"load: {len(ld)}  validate errors: {sum(1 for d in vd if d.severity=='error')}")
for d in vd:
    print(f"  [{d.severity}] {d.id}: {d.message}")

r = resolve_lab(loaded, "generic-infra")
for v in r.vms:
    print(f"  vm {v.name}: role={v.role} vcpu={v.vcpu} caps={v.capabilities}")
PY
```

Expected output: zero load diagnostics, zero validate errors, one expected warning (`config.reference.ansible_role_missing` for the `router` role — Team B Milestone 10 fills the gap), three resolved VMs with role-inherited capabilities.

## Review focus areas

For an architecture reviewer:

- `ai/architecture/shared_contracts.md` — particularly §3.2 (on-disk YAML kinds + 7-step resolver pipeline), §9.1/§9.2 (StateStore/RunStore split for ISP), §11 (PRD-conformance invariants).
- `src/playground/models/kinds.py` — `extra="forbid"` on every spec model, `model_validator` enforcement on `TargetSelector` and `WorkloadPlacement`, the discriminated-union dispatch in `parse_resource`.
- `src/playground/config/resolver.py::_flatten_role` — deep-merge semantics; lists replace, capability maps recurse, `extends` chain walked root → leaf.

For a code reviewer:

- Every Diagnostic emitted by `loader.py` and `validator.py` carries a registered ID. Net new IDs would need an entry in `ai/architecture/diagnostic_ids.md`.
- `discovery.py` filters hidden directories under `config_dir` only — earlier version filtered all path parts and was broken by parent paths like `.superset/` (fixed in `ce0a3cd`).
- The validator's ansible-role check is opt-in by parameter so unit tests don't depend on the real `ansible/` tree.

For a PRD reviewer:

- The new lab CIDRs (`10.20.x.0/24`) intentionally differ from the legacy `10.0.10.0/24` in `tofu/main.tf` — coexistence rule is documented in `shared_contracts.md §11.1`. No tofu/ansible files were touched by this PR.
- Air-gap (`offline: true`) contract is documented in `shared_contracts.md §11.4` with the blocking `artifact.offline_violation` diagnostic. Enforcement code lands with the next slice; the YAML's `local_path` fields are already populated.

## Test plan

- [x] `pytest` green on Python 3.12.3 (69/69).
- [x] `python -c "import playground; print(playground.__version__)"` against installed package returns a real version string.
- [x] `discover_config_files(Path("config"))` yields all 12 committed YAMLs.
- [x] `load_config(Path("config"))` returns zero diagnostics against the committed tree.
- [x] `validate(...)` returns zero errors and exactly one expected warning.
- [x] `resolve_lab(..., "generic-infra")` round-trips with VM resources from per-VM override, role chain, and defaults respectively.
- [ ] `tofu fmt -check && tofu validate` in `tofu/` — skipped because this PR does not touch `tofu/`; `git diff main..HEAD -- tofu/` is empty.
- [ ] `ansible-lint ansible/site.yml` — same skip; `git diff main..HEAD -- ansible/` is empty.

## Risk and rollback

- No runtime behavior change to existing infrastructure. The `tofu/` and `ansible/` trees are untouched; `playground apply` does not exist yet on any branch.
- Rollback is a single `git revert` of the merge commit; nothing in this PR has external side effects.

## Follow-up issues to file after merge

1. **Team A — state/events slice.** Implement `StateStore`, `RunStore`, `EventBus`, and the four subscribers (`JsonlLogSubscriber`, `HumanLogSubscriber`, `RunSummarySubscriber`, `StatusSnapshotSubscriber`). Contracts frozen in `shared_contracts.md §4`, `§5`, `§8`, `§9`.
2. **Team A — source_map precision.** Thread `DiscoveredFile` from loader into resolver so `ResolvedLab.source_map` carries per-key origins.
3. **Team A — retention enforcement.** `RetentionPolicy` model exists; `RunStore.apply_retention` does not.
4. **Team B — local-libvirt adapter.** First consumer of `ResolvedLab`. Should implement `ProviderAdapter` per `shared_contracts.md §7` and emit `OperationEvent` through `EventBus` once it lands.
5. **Team B — router ansible role.** Currently a documented gap (`config.reference.ansible_role_missing` warning); see `task_breakdown.md` Milestone 10.
6. **Team C — CLI wiring.** `pyproject.toml` intentionally has no `[project.scripts]` entry; Team C adds `playground.cli.main:app` and re-enables the console script.

## gh CLI command (for the actual PR)

```bash
gh pr create \
  --base integration/mvp-platform \
  --head team/core-config-state \
  --title "Team A Phase 1: config + contracts + validation + resolution" \
  --body-file ai/handoffs/team-a-phase1-pr.md
```
