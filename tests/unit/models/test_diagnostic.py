"""Tests for the Diagnostic model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from playground.models.diagnostic import Diagnostic, SourceLocation


def test_diagnostic_minimal() -> None:
    d = Diagnostic(id="config.schema.missing_field", severity="error", message="x")
    assert d.severity == "error"
    assert d.source is None
    assert d.tags == []


def test_diagnostic_full() -> None:
    d = Diagnostic(
        id="config.reference.unknown_role",
        severity="error",
        message="role 'router' is not defined",
        source=SourceLocation(path="config/labs/generic-infra.yaml", line=42, column=11),
        key_path="spec.vms[2].role",
        suggestion="add a VmRole named 'router' under config/roles/",
        tags=["resolver", "reference"],
    )
    assert d.source is not None
    assert d.source.line == 42
    assert d.key_path == "spec.vms[2].role"


def test_diagnostic_rejects_unknown_severity() -> None:
    with pytest.raises(ValidationError):
        Diagnostic(
            id="x",
            severity="critical",  # type: ignore[arg-type]
            message="x",
        )


def test_diagnostic_rejects_empty_id() -> None:
    with pytest.raises(ValidationError):
        Diagnostic(id="", severity="error", message="x")


def test_source_rejects_zero_line() -> None:
    with pytest.raises(ValidationError):
        SourceLocation(path="x.yaml", line=0)
