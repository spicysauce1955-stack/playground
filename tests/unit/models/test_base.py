"""Tests for the base envelope and Metadata models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from playground.models.base import Metadata, ResourceEnvelope


def test_metadata_minimal() -> None:
    m = Metadata(name="x")
    assert m.name == "x"
    assert m.description is None
    assert m.tags == []


def test_metadata_strips_whitespace() -> None:
    m = Metadata(name="  generic-node  ")
    assert m.name == "generic-node"


def test_metadata_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        Metadata(name="")


def test_metadata_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Metadata(name="x", owner="bob")  # type: ignore[call-arg]


def test_envelope_requires_known_api_version() -> None:
    with pytest.raises(ValidationError):
        ResourceEnvelope(
            apiVersion="playground/v2",  # type: ignore[arg-type]
            kind="Lab",
            metadata=Metadata(name="x"),
        )


def test_envelope_accepts_known_api_version() -> None:
    env = ResourceEnvelope(
        apiVersion="playground/v1",
        kind="Lab",
        metadata=Metadata(name="x"),
    )
    assert env.kind == "Lab"


def test_envelope_is_frozen() -> None:
    env = ResourceEnvelope(
        apiVersion="playground/v1",
        kind="Lab",
        metadata=Metadata(name="x"),
    )
    with pytest.raises(ValidationError):
        env.kind = "VmRole"  # type: ignore[misc]
