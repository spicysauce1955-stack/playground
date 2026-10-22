"""Operation runs — lifecycle records for mutating CLI commands.

See :mod:`playground.runs.operation` for the public surface.
"""

from playground.runs.operation import (
    Operation,
    OperationRun,
    RunStatus,
    StepResult,
    allocate_run_id,
    finish_run,
    start_run,
)

__all__ = [
    "Operation",
    "OperationRun",
    "RunStatus",
    "StepResult",
    "allocate_run_id",
    "finish_run",
    "start_run",
]
