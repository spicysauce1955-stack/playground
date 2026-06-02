"""Guard for BUG-6: workload roles must accept `pg_workloads` as a list OR a
JSON string.

The rendered inventory writes `pg_workloads='[{...}]'` into the `.ini`, which
Ansible auto-parses into a list before the role sees it. The role's
`pg_workloads | from_json` then errors ("the JSON object must be str, bytes or
bytearray, not list") on any host that actually has a workload (e.g. docker1 in
generic-infra). The fix parses a string but passes a list through unchanged.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ROLE_TASKS = [
    REPO_ROOT / "ansible" / "roles" / "workload_container" / "tasks" / "main.yml",
    REPO_ROOT / "ansible" / "roles" / "workload_compose" / "tasks" / "main.yml",
]

GUARD_EXPR = (
    "{{ pg_workloads if (pg_workloads is not string) "
    "else (pg_workloads | from_json) }}"
)


@pytest.mark.parametrize("role", ROLE_TASKS, ids=lambda p: p.parent.parent.name)
def test_role_guards_from_json_for_list_or_string(role: Path) -> None:
    text = role.read_text()
    assert "pg_workloads is not string" in text, (
        f"{role} must guard `pg_workloads | from_json` so an already-parsed "
        "list is accepted (BUG-6)."
    )
    assert "from_json" in text  # a JSON string is still parsed
    # The original unguarded one-liner must be gone.
    assert 'pg_workloads_parsed: "{{ pg_workloads | from_json }}"' not in text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([{"name": "demo"}], [{"name": "demo"}]),   # already a list -> unchanged
        ('[{"name": "demo"}]', [{"name": "demo"}]),  # JSON string -> parsed
        ("[]", []),
        ([], []),
    ],
)
def test_guard_expression_handles_both_shapes(value: object, expected: object) -> None:
    """Evaluate the exact guard Jinja against both shapes (skipped if jinja2
    isn't installed in this env; it always is under Ansible)."""
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment()  # noqa: S701 — no autoescape needed for a value test
    env.filters["from_json"] = json.loads
    rendered = env.from_string(GUARD_EXPR).render(pg_workloads=value)
    assert ast.literal_eval(rendered) == expected
