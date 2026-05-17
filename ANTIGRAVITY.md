# Antigravity Project Instructions

You are the primary implementation and verification agent for this repository.

## Required context before work

Always read:

- ai/global_context.md
- ai/agents_state.md
- ai/engineering/implementation_plan.md
- ai/architecture/system_design.md
- ai/architecture/api_contracts.md

## Workflow

1. Read PRD and project state.
2. Produce implementation plan.
3. Implement directly.
4. Run validation.
5. Self-review.
6. Write artifacts.
7. Only then suggest optional independent review by Claude/Codex/OpenCode.

## Rules

Do not:
- modify unrelated files
- silently change requirements
- silently change architecture
- delete broad directories
- rewrite git history
- touch secrets or credentials
- skip tests because they are inconvenient

If architecture must change:
- stop
- create an ADR in ai/decisions/
- update ai/global_context.md
