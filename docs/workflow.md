# Workflow

Work happens sequentially on `main` unless the user explicitly asks for a short
task branch.

## Pipeline

```text
plan -> design -> implement -> test -> review -> integrate
```

Subagents are quality gates and specialists, not parallel owners.

Use the full pipeline for infrastructure changes, cross-module Python changes,
config/model/validation changes, and anything that alters the documented deploy
pipeline.

Use a lightweight path for small work:

- docs/comment-only: implement -> review
- test-only: implement -> test -> review
- obvious one-file fix: implement -> test -> review -> integrate

Each stage must have a concrete output:

- Plan: scope, non-goals, acceptance criteria, risks, next step.
- Design: boundaries, data flow, chosen approach, rejected alternatives, tests.
- Implement: focused patch, no unrelated refactor.
- Test: commands run, pass/fail, uncovered risks.
- Review: findings first, severity, file/line evidence.
- Integrate: status, cleanup, final summary, commit/merge readiness.

## Canonical Checks

Run the narrowest checks that prove the change. Common checks:

```bash
PYTHONPATH=src uv run --no-project --with pytest --with pydantic --with ruamel.yaml --with jsonschema pytest tests/unit
cd tofu && tofu fmt -check && tofu validate
ansible-playbook -i ansible/inventory.ini ansible/site.yml --syntax-check
```

If a tool is missing, report the attempted command and the missing executable.

## Agent Mapping

| Stage | Codex | Claude |
| --- | --- | --- |
| Plan | `.codex/agents/planner.toml` | `.claude/agents/planner.md` |
| Design | `.codex/agents/architect.toml` | `.claude/agents/architect.md` |
| Implement | `.codex/agents/iac-engineer.toml` or built-in `worker` | `.claude/agents/iac-implementer.md` |
| Test | `.codex/agents/tester.toml` | `.claude/agents/qa-engineer.md` |
| Review | `.codex/agents/reviewer.toml` | `.claude/agents/code-reviewer.md` |
| Integrate | `.codex/agents/integrator.toml` | `.claude/agents/integrator.md` |
