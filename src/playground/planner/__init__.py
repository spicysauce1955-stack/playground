"""Planner: render a :class:`Plan` from a :class:`ResolvedLab`.

This module is the read-side of what `docs/system_design.md` calls the
planner. Today it produces a backend-neutral preview of what `apply`
**would** do — every action's verb is ``create`` because state
observation (comparing against ``.playground/state/observed/`` and what
the backend reports) is a separate slice. When that lands, the same
:class:`Plan` shape will carry ``update``/``delete``/``no_op`` verbs.

The planner does not execute backend commands and does not read state.
"""

from playground.planner.plan import (
    Plan,
    PlanAction,
    PlanBudget,
    render_plan,
)
from playground.planner.scheduling import (
    schedule_workloads,
    stage_workload_files,
    workload_to_ansible_payload,
)

__all__ = [
    "Plan",
    "PlanAction",
    "PlanBudget",
    "render_plan",
    "schedule_workloads",
    "stage_workload_files",
    "workload_to_ansible_payload",
]
