"""Typed models for config loading, validation, and resolved lab state.

The public surface is:

- ``Diagnostic``
- ``ResolvedLab`` and its sub-models
- ``OperationRun``
- ``OperationEvent``
- ``ResourceStatus``
- ``Plan`` once provider adapters land
"""

from playground.models.base import ApiVersion, Metadata, ResourceEnvelope, StrictModel
from playground.models.diagnostic import Diagnostic, Severity, SourceLocation
from playground.models.kinds import (
    KNOWN_KINDS,
    AnyResource,
    ArtifactSources,
    Budget,
    CommandPreset,
    Defaults,
    Lab,
    LabSpec,
    LabVm,
    NetworkProfile,
    ProviderConfig,
    Resources,
    RetentionPolicy,
    SshConfig,
    TargetSelector,
    VmRole,
    VmRoleSpec,
    parse_resource,
)
from playground.models.resolved import (
    ResolvedArtifactImage,
    ResolvedArtifacts,
    ResolvedCommand,
    ResolvedDefaults,
    ResolvedLab,
    ResolvedNetwork,
    ResolvedVm,
    ResolvedWorkload,
)

__all__ = [
    "ApiVersion",
    "AnyResource",
    "ArtifactSources",
    "Budget",
    "CommandPreset",
    "Defaults",
    "Diagnostic",
    "KNOWN_KINDS",
    "Lab",
    "LabSpec",
    "LabVm",
    "Metadata",
    "NetworkProfile",
    "ProviderConfig",
    "ResolvedArtifactImage",
    "ResolvedArtifacts",
    "ResolvedCommand",
    "ResolvedDefaults",
    "ResolvedLab",
    "ResolvedNetwork",
    "ResolvedVm",
    "ResolvedWorkload",
    "ResourceEnvelope",
    "Resources",
    "RetentionPolicy",
    "Severity",
    "SourceLocation",
    "SshConfig",
    "StrictModel",
    "TargetSelector",
    "VmRole",
    "VmRoleSpec",
    "parse_resource",
]
