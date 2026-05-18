"""Resolved backend-neutral lab model produced by the config resolver."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from playground.models.base import StrictModel
from playground.models.kinds import (
    Budget,
    NetworkProfileSpec,
    RetentionPolicy,
    SshConfig,
    TargetSelector,
    WorkloadPlacement,
)


class ResolvedNetwork(StrictModel):
    name: str
    intent: Literal["nat", "isolated", "routed"]
    cidr: str
    internet_access: bool | Literal["configurable"]
    dns_enabled: bool
    routes: list[Any] = Field(default_factory=list)
    provider_overrides: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class ResolvedVm(StrictModel):
    name: str
    role: str
    image: str
    vcpu: int = Field(ge=1)
    memory_mb: int = Field(ge=128)
    disk_gb: int = Field(ge=1)
    networks: list[str]
    ssh: SshConfig
    provisioners: list[dict[str, str]] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    provider_overrides: dict[str, Any] = Field(default_factory=dict)


class ResolvedWorkload(StrictModel):
    name: str
    type: Literal["container", "compose", "swarm"]
    source: str
    placement: WorkloadPlacement
    networks: list[str] = Field(default_factory=list)
    ports: list[str] = Field(default_factory=list)
    volumes: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    resources: dict[str, int] | None = None
    tags: list[str] = Field(default_factory=list)


class ResolvedCommand(StrictModel):
    name: str
    description: str | None
    target: TargetSelector
    shell: str
    working_directory: str | None
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int
    escalate: bool


class ResolvedDefaults(StrictModel):
    backend: str
    offline: bool
    budget: Budget
    retention: RetentionPolicy


class ResolvedArtifactImage(StrictModel):
    type: str
    version: str
    source: str
    local_path: str | None = None
    available_locally: bool = False
    available_remote: bool = True


class ResolvedArtifacts(StrictModel):
    vm_images: dict[str, ResolvedArtifactImage] = Field(default_factory=dict)
    tofu_providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    ansible_collections: dict[str, dict[str, Any]] = Field(default_factory=dict)
    docker_images: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ResolvedLab(StrictModel):
    """The fully resolved, backend-neutral lab.

    Pydantic config keeps the model frozen; once handed to a backend adapter it
    should be treated as immutable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: Literal["playground/v1"] = "playground/v1"
    lab_name: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    backend: str
    offline: bool
    budget: Budget
    defaults: ResolvedDefaults
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    networks: list[ResolvedNetwork]
    vms: list[ResolvedVm]
    workloads: list[ResolvedWorkload] = Field(default_factory=list)
    commands: list[ResolvedCommand] = Field(default_factory=list)
    artifacts: ResolvedArtifacts
    network_profiles: dict[str, NetworkProfileSpec] = Field(default_factory=dict)
    runtime_overrides: list[Any] = Field(default_factory=list)
    source_map: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "ResolvedArtifactImage",
    "ResolvedArtifacts",
    "ResolvedCommand",
    "ResolvedDefaults",
    "ResolvedLab",
    "ResolvedNetwork",
    "ResolvedVm",
    "ResolvedWorkload",
]
