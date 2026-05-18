# CODEX.md

This file provides guidance to Codex when working in this repository. The
official project instruction file for Codex is `AGENTS.md`; keep this file as a
human-readable companion.

## Operating Model

Work happens sequentially on `main`. Do not split work into parallel delivery
branches or maintain agent handoff documents. Use the normal Git history, the
issue or prompt at hand, and the repo files as the source of truth.

Claude Code and Codex are the only agent-specific workflows kept in this repo:

- Use `CODEX.md` for Codex execution guidance.
- Use `AGENTS.md` for Codex-loaded project instructions.
- Use `.codex/agents/` for Codex custom subagents.
- Use `CLAUDE.md` for Claude Code guidance and Claude subagents.

## Repository Priorities

- Preserve the OpenTofu -> Ansible -> Redroid pipeline described in `README.md`
  and `CLAUDE.md`.
- Treat `docs/product/requirements.md` as the highest-signal source of product
  intent, followed by `docs/product/user_stories.md` and
  `docs/product/mvp_scope.md`.
- Use `docs/workflow.md`, `docs/platform.md`, and `docs/roadmap.md` for durable
  process, design, and task sequencing.
- Re-read `PRD.md` before non-trivial infrastructure changes.
- Keep generated runtime state under `.playground/`.
- Do not hardcode secrets, SSH keys, passwords, or local-only credentials.
- Keep OpenTofu and Ansible changes idempotent.

## Verification

Prefer narrow verification tied to the change:

```bash
PYTHONPATH=src uv run --no-project --with pytest --with pydantic --with ruamel.yaml --with jsonschema pytest tests/unit
cd tofu && tofu fmt -check && tofu validate
ansible-playbook -i ansible/inventory.ini ansible/site.yml --syntax-check
```

Do not commit generated virtualenvs or transient lockfiles unless the project
intentionally adopts them.
