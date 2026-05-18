"""Diagnostic model — see ``ai/architecture/shared_contracts.md §2``."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from playground.models.base import StrictModel

Severity = Literal["error", "warning", "info"]


class SourceLocation(StrictModel):
    path: str
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)


class Diagnostic(StrictModel):
    id: str = Field(min_length=1)
    severity: Severity
    message: str = Field(min_length=1)
    source: SourceLocation | None = None
    key_path: str | None = None
    suggestion: str | None = None
    tags: list[str] = Field(default_factory=list)


__all__ = ["Diagnostic", "Severity", "SourceLocation"]
