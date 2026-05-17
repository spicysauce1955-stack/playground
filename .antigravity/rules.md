# Antigravity Agent Rules

Before acting:
1. Read ai/global_context.md
2. Read ai/agents_state.md
3. Read ai/engineering/implementation_plan.md
4. Confirm the current implementation slice

Allowed:
- edit project files
- run tests
- run local app
- use browser verification
- create screenshots or verification artifacts
- update ai/agents_state.md

Not allowed without explicit approval:
- delete large directories
- run destructive shell commands
- modify secrets
- change deployment infrastructure
- force push
- rewrite git history
- change architecture without ADR

## Multi-Agent Delegation Policy

Do not wait on external agents during the critical implementation path unless explicitly asked for a second-agent review. Use yourself as the primary executor and internally adopt the roles of Architect, Implementer, and Reviewer.

Use external agents only when they can operate independently:
* **Claude Code** as an independent review/debug pass after implementation is complete.
* **Codex** for an alternative patch or isolated implementation attempt.
* **OpenCode/GLM** for deep architecture or planning before implementation.
