"""Base envelope shared by every on-disk YAML kind."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ApiVersion = Literal["playground/v1"]


class StrictModel(BaseModel):
    """Common base: forbid unknown fields, immutable, normalize whitespace."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Metadata(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class ResourceEnvelope(StrictModel):
    """Every YAML file shares this envelope around its kind-specific ``spec``."""

    apiVersion: ApiVersion
    kind: str
    metadata: Metadata


__all__ = ["ApiVersion", "Metadata", "ResourceEnvelope", "StrictModel"]
