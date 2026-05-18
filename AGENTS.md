# AGENTS.md

Codex reads this file as the project-level operating guide.

## Operating Model

Work sequentially on `main` unless the user explicitly asks for a branch.

Default pipeline:

```text
plan -> design -> implement -> test -> review -> integrate
```

Use subagents as scoped specialists and quality gates. Do not recreate the old
parallel ownership workflow. A subagent must have a clear input, a narrow scope,
and a concrete expected output.

Use the full pipeline for:

- infrastructure changes under `tofu/` or `ansible/`
- cross-module Python changes
- changes that alter config schema, validation, or resolved models
- changes that affect the deploy pipeline documented in `README.md`

Use a lightweight path for small work:

- docs/comment-only edits: implement -> review
- test-only edits: implement -> test -> review
- obvious one-file fixes: implement -> test -> review -> integrate

Planning state lives in:

- `docs/workflow.md` for process
- `docs/platform.md` for durable design constraints
- `docs/roadmap.md` for the current sequential task queue

## Codex Subagents

Project-scoped custom agents live in `.codex/agents/`.

Preferred usage:

- `planner`: turn an idea into a scoped work item and acceptance criteria.
- `architect`: check boundaries, data flow, coupling, and risk before coding.
- `iac_engineer`: implement focused OpenTofu, Ansible, Python, or config changes.
- `tester`: add or run focused tests and investigate failures.
- `reviewer`: PR-style review for bugs, regressions, security, and missing tests.
- `integrator`: final cleanup, status check, merge readiness, and handoff summary.

Claude role equivalents:

| Pipeline stage | Codex agent | Claude agent |
| --- | --- | --- |
| Plan | `planner` | `planner` |
| Design | `architect` | `architect` |
| Implement | `iac_engineer` or built-in `worker` | `iac-implementer` |
| Test | `tester` | `qa-engineer` |
| Review | `reviewer` | `code-reviewer` |
| Integrate | `integrator` | `integrator` |

Use built-in Codex agents when they fit better:

- `explorer` for read-heavy codebase mapping.
- `worker` for bounded implementation tasks.
- `default` for ordinary single-threaded work.

## Repo Priorities

- Preserve the OpenTofu -> Ansible -> Redroid pipeline documented in `README.md`
  and `CLAUDE.md`.
- Re-read `PRD.md` before non-trivial infrastructure changes.
- Keep generated runtime state under `.playground/`.
- Do not hardcode secrets, SSH keys, passwords, or local-only credentials.
- Keep OpenTofu and Ansible changes idempotent.
- Leave unrelated dirty worktree changes untouched.

## Verification

Prefer the narrowest checks that prove the change:

```bash
PYTHONPATH=src uv run --no-project --with pytest --with pydantic --with ruamel.yaml --with jsonschema pytest tests/unit
cd tofu && tofu fmt -check && tofu validate
ansible-playbook -i ansible/inventory.ini ansible/site.yml --syntax-check
```

If a check cannot run because local tooling is missing, report that directly and
include the command attempted.
