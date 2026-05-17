"""Typed contract models shared across teams.

See ``ai/architecture/shared_contracts.md`` for the field-level
contract these models must implement. The public surface is:

- ``Diagnostic``
- ``ResolvedLab`` and its sub-models
- ``OperationRun``
- ``OperationEvent``
- ``ResourceStatus``
- ``Plan`` (input/output type for :class:`ProviderAdapter`)

Owners: Team A. Other teams import from here and must not redefine
these types locally.
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
