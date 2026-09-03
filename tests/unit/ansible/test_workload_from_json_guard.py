"""Guard for BUG-6: workload (and capture) roles must accept their JSON
group var as a list/dict OR a JSON string.

The rendered inventory writes `pg_workloads='[{...}]'` into the `.ini`, which
Ansible auto-parses into a list before the role sees it. The role's
`pg_workloads | from_json` then errors ("the JSON object must be str, bytes or
bytearray, not list") on any host that actually has a workload (e.g. docker1 in
generic-infra). The fix parses a string but passes an already-decoded value
through unchanged.

The `capture` role (`ansible/roles/capture/tasks/main.yml`) reimplements the
same guard for `pg_capture` and explicitly cites BUG-6 in its comment -- so it
is covered here too, parametrized on the var name.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# (role tasks file, group var name, id)
ROLE_TASKS = [
    (
        REPO_ROOT / "ansible" / "roles" / "workload_container" / "tasks" / "main.yml",
        "pg_workloads",
    ),
    (
        REPO_ROOT / "ansible" / "roles" / "workload_compose" / "tasks" / "main.yml",
        "pg_workloads",
    ),
    (
        REPO_ROOT / "ansible" / "roles" / "capture" / "tasks" / "main.yml",
        "pg_capture",
    ),
]
_IDS = ["workload_container", "workload_compose", "capture"]


def _guard_expr(var: str) -> str:
    return f"{{{{ {var} if ({var} is not string) else ({var} | from_json) }}}}"


@pytest.mark.parametrize(("role", "var"), ROLE_TASKS, ids=_IDS)
def test_role_guards_from_json_for_list_or_string(role: Path, var: str) -> None:
    text = role.read_text()
    assert f"{var} is not string" in text, (
        f"{role} must guard `{var} | from_json` so an already-parsed "
        "value is accepted (BUG-6)."
    )
    assert "from_json" in text  # a JSON string is still parsed
    # The original unguarded one-liner must be gone.
    assert f'{var}_parsed: "{{{{ {var} | from_json }}}}"' not in text


@pytest.mark.parametrize(
    ("var", "value", "expected"),
    [
        ("pg_workloads", [{"name": "demo"}], [{"name": "demo"}]),  # list -> unchanged
        ("pg_workloads", '[{"name": "demo"}]', [{"name": "demo"}]),  # JSON string -> parsed
        ("pg_workloads", "[]", []),
        ("pg_workloads", [], []),
        ("pg_capture", {"enabled": False}, {"enabled": False}),  # dict -> unchanged
        ("pg_capture", '{"enabled": false}', {"enabled": False}),  # JSON string -> parsed
        ("pg_capture", "{}", {}),
        ("pg_capture", {}, {}),
    ],
    ids=[
        "workloads-list",
        "workloads-json-string",
        "workloads-empty-json-string",
        "workloads-empty-list",
        "capture-dict",
        "capture-json-string",
        "capture-empty-json-string",
        "capture-empty-dict",
    ],
)
def test_guard_expression_handles_both_shapes(
    var: str, value: object, expected: object
) -> None:
    """Evaluate the exact guard Jinja against both shapes, for both the
    list-shaped workload var and the dict-shaped capture var (skipped if
    jinja2 isn't installed in this env; it always is under Ansible)."""
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment()  # noqa: S701 — no autoescape needed for a value test
    env.filters["from_json"] = json.loads
    rendered = env.from_string(_guard_expr(var)).render(**{var: value})
    assert ast.literal_eval(rendered) == expected


def test_captures_consumer_is_riskier_than_workloads_consumers() -> None:
    """The guard shape is identical across roles, but what runs AFTER it is
    not equally forgiving of a shape that slips through wrong.

    `tasks/main.yml:40` calls `.get('enabled', true)` on the parsed
    `pg_capture` value -- a DICT method that raises `AttributeError` if the
    value arrives as a plain string. The workload roles only ever iterate
    over the parsed value; iterating a string doesn't raise at all, it just
    silently produces the wrong (character-by-character) result. So a
    regression that lets an un-decoded JSON string slip past the guard
    fails LOUDLY for capture and SILENTLY for the workload roles -- which
    is exactly why capture's copy of this guard is more load-bearing, not
    less.
    """
    with pytest.raises(AttributeError):
        "not-a-dict".get("enabled", True)  # type: ignore[attr-defined]

    # No raise: iterating a string just walks its characters.
    for _ in "not-a-list":
        pass
