"""Pydantic models for each on-disk YAML kind.

Spec models use ``StrictModel`` so any YAML typo surfaces as a
``ValidationError`` that the loader can translate into a ``Diagnostic``.

Lab specs intentionally tolerate unknown keys under ``provider`` /
``provider_overrides`` blocks because backend adapters version their own
config.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from playground.models.base import ResourceEnvelope, StrictModel

# ---------------------------------------------------------------------------
# Common value objects
# ---------------------------------------------------------------------------


class Resources(StrictModel):
    vcpu: int = Field(ge=1)
    memory_mb: int = Field(ge=128)
    disk_gb: int = Field(ge=1)


class Budget(StrictModel):
    mode: Literal["strict", "permissive"]
    max_vcpu: int = Field(ge=1)
    max_memory_mb: int = Field(ge=128)
    max_disk_gb: int = Field(ge=1)
    max_vms: int = Field(ge=1)
    max_containers: int = Field(ge=0)


class SshConfig(StrictModel):
    user: str = Field(min_length=1)
    public_key_path: str | None = None


class TargetSelector(StrictModel):
    role: str | None = None
    vm: str | None = None
    tag: str | None = None
    any: bool | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> TargetSelector:
        set_keys = [k for k, v in self.model_dump().items() if v not in (None, False)]
        if len(set_keys) != 1:
            raise ValueError(
                f"TargetSelector must set exactly one of role/vm/tag/any; got {set_keys}"
            )
        if self.any is not None and self.any is not True:
            raise ValueError("TargetSelector.any must be true when set")
        return self


# ---------------------------------------------------------------------------
# Retention (also used by Defaults.spec.retention)
# ---------------------------------------------------------------------------


class RetentionRuns(StrictModel):
    keep_last: int = Field(ge=0)
    max_age_days: int = Field(ge=0)


class RetentionLogs(StrictModel):
    keep_per_run: bool
    compress_after_days: int = Field(ge=0)


class RetentionPolicy(StrictModel):
    runs: RetentionRuns
    logs: RetentionLogs


# ---------------------------------------------------------------------------
# Capture (also used by Defaults.spec.capture)
# ---------------------------------------------------------------------------


class CaptureOptions(StrictModel):
    """Bounds on packet capture for one device, per session.

    ``max_file_mb * max_files`` is a hard ceiling on disk: 1 GB at these
    defaults. It is not optional. pcaps grow without bound, `redroid-host`
    asks for a 60 GB disk, and filling a guest's root filesystem takes
    sshd down and therefore every other verb with it.

    Two properties of that ceiling are deliberate and must not be
    "corrected":

    * ``max_file_mb`` maps to ``tcpdump -C``, which counts units of
      1,000,000 bytes -- not MiB. The field name says ``mb`` and means
      exactly what tcpdump does.
    * ``-C`` with ``-W`` is a ring, so the OLDEST file is overwritten once
      the ceiling is reached. A session that outlives its budget keeps the
      most recent 1 GB and silently discards the beginning. That is better
      than filling the disk, and `capture status` reports the file count so
      a wrapping session is visible.

    ``enabled: false`` is a PROVISIONING switch, not just a CLI gate: the
    `capture` role's tasks end the host (``meta: end_host``) at its very
    first tasks, before the tcpdump package install, so no package is
    installed and no unit exists. The host still lands in
    `needs_capture` -- it opts out at the role's own guard, not by being
    excluded from the play. It is the air-gap / lean-guest opt-out.
    """

    enabled: bool = True
    max_file_mb: int = Field(default=100, ge=1)
    max_files: int = Field(default=10, ge=1)
    snaplen: int = Field(default=0, ge=0)
    """``tcpdump -s``. 0 means the whole packet."""


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class DefaultsVm(StrictModel):
    image: str = Field(min_length=1)
    resources: Resources
    ssh: SshConfig


class DefaultsNetwork(StrictModel):
    profile: str = Field(min_length=1)


class DefaultsSpec(StrictModel):
    backend: str = Field(min_length=1)
    offline: bool = False
    budget: Budget
    vm: DefaultsVm
    network: DefaultsNetwork
    retention: RetentionPolicy
    capture: CaptureOptions = Field(default_factory=CaptureOptions)


class Defaults(ResourceEnvelope):
    kind: Literal["Defaults"]
    spec: DefaultsSpec


# ---------------------------------------------------------------------------
# ProviderConfig — open-keyed spec; adapters own their schema
# ---------------------------------------------------------------------------


class ProviderConfigSpec(BaseModel):
    """Open spec — backend adapters validate their own keys.

    The only platform-level invariant is that ``driver`` matches the
    enclosing ``metadata.name``; the loader enforces that.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    driver: str = Field(min_length=1)


class ProviderConfig(ResourceEnvelope):
    kind: Literal["ProviderConfig"]
    spec: ProviderConfigSpec

    @model_validator(mode="after")
    def driver_matches_name(self) -> ProviderConfig:
        if self.spec.driver != self.metadata.name:
            raise ValueError(
                f"ProviderConfig.spec.driver ({self.spec.driver!r}) "
                f"must equal metadata.name ({self.metadata.name!r})"
            )
        return self


# ---------------------------------------------------------------------------
# ArtifactSources
# ---------------------------------------------------------------------------


class VmImageSource(StrictModel):
    type: Literal["qcow2", "iso", "raw"]
    version: str
    default_source: str
    local_path: str | None = None
    checksum: str | None = None


class TofuProviderSource(StrictModel):
    version: str
    default_source: str
    local_path: str | None = None


class AnsibleCollectionSource(StrictModel):
    version: str
    default_source: str
    local_path: str | None = None


class DockerImageSource(StrictModel):
    image: str
    registry: str | None = None
    default_source: str | None = None
    local_archive: str | None = None
    checksum: str | None = None


class ArtifactDefaults(StrictModel):
    offline: bool = False


class ArtifactSourcesSpec(StrictModel):
    defaults: ArtifactDefaults = Field(default_factory=ArtifactDefaults)
    vm_images: dict[str, VmImageSource] = Field(default_factory=dict)
    tofu_providers: dict[str, TofuProviderSource] = Field(default_factory=dict)
    ansible_collections: dict[str, AnsibleCollectionSource] = Field(default_factory=dict)
    docker_images: dict[str, DockerImageSource] = Field(default_factory=dict)


class ArtifactSources(ResourceEnvelope):
    kind: Literal["ArtifactSources"]
    spec: ArtifactSourcesSpec


# ---------------------------------------------------------------------------
# NetworkProfile
# ---------------------------------------------------------------------------


class NetworkDns(StrictModel):
    enabled: bool


class NetworkProfileSpec(StrictModel):
    intent: Literal["nat", "isolated", "routed"]
    internet_access: bool | Literal["configurable"]
    dns: NetworkDns


class NetworkProfile(ResourceEnvelope):
    kind: Literal["NetworkProfile"]
    spec: NetworkProfileSpec


# ---------------------------------------------------------------------------
# VmRole — extends/capabilities/routing
# ---------------------------------------------------------------------------


class VmProvisioner(StrictModel):
    ansible_role: str = Field(min_length=1)


class VmRouting(StrictModel):
    mode: Literal["automatic", "manual"]
    allow_overrides: bool = False


class VmRoleSpec(BaseModel):
    """Spec for a VmRole.

    ``extends`` is single-chain inheritance; the resolver flattens it
    into a single VmRoleSpec before the model leaves the validation layer.

    ``capabilities`` is an open map (``extra="allow"`` is not used —
    instead it's a typed dict of known capability names to bool/value).
    Backend adapters interpret these; the platform treats them as
    opaque metadata.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    extends: str | None = None
    image: str | None = None
    resources: Resources | None = None
    ssh: SshConfig | None = None
    provisioners: list[VmProvisioner] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    routing: VmRouting | None = None


class VmRole(ResourceEnvelope):
    kind: Literal["VmRole"]
    spec: VmRoleSpec


# ---------------------------------------------------------------------------
# CommandPreset
# ---------------------------------------------------------------------------


class CommandBody(StrictModel):
    shell: str = Field(min_length=1)


class CommandEscalation(StrictModel):
    become: bool = False


class CommandPresetSpec(StrictModel):
    target: TargetSelector
    command: CommandBody
    working_directory: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(gt=0, le=86400)
    escalation: CommandEscalation = Field(default_factory=CommandEscalation)


class CommandPreset(ResourceEnvelope):
    kind: Literal["CommandPreset"]
    spec: CommandPresetSpec


# ---------------------------------------------------------------------------
# Lab
# ---------------------------------------------------------------------------


class LabNetwork(StrictModel):
    name: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    cidr: str = Field(min_length=7)  # x.x.x.x/n minimum length


class WorkloadPlacement(BaseModel):
    """Exactly one of target_role / target_vm / target_tag, or auto=True."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_role: str | None = None
    target_vm: str | None = None
    target_tag: str | None = None
    auto: bool | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> WorkloadPlacement:
        set_keys = [
            k for k, v in self.model_dump().items() if v not in (None, False)
        ]
        if len(set_keys) != 1:
            raise ValueError(
                "WorkloadPlacement must set exactly one of "
                f"target_role/target_vm/target_tag/auto; got {set_keys}"
            )
        return self


class AndroidAppOptions(StrictModel):
    """Android-specific fields for a ``type: android_app`` workload.

    Kept in a sub-model so Android concerns do not widen the schema every
    workload type shares. ``package`` is required and cannot be inferred:
    it drives the install idempotency check, the launch, and the
    verify-lab assertion.

    ``activity`` is optional because the launcher activity is
    discoverable at runtime with
    ``cmd package resolve-activity --brief``; when unset the role
    launches via ``monkey``, which needs no activity name.
    """

    package: str = Field(min_length=1)
    launch: bool = False
    activity: str | None = None
    permissions: list[str] = Field(default_factory=list)
    reinstall: bool = False


class LabWorkload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    type: Literal["container", "compose", "swarm", "android_app"]
    source: str = Field(min_length=1)
    placement: WorkloadPlacement
    networks: list[str] = Field(default_factory=list)
    ports: list[str] = Field(default_factory=list)
    volumes: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    resources: Resources | None = None
    tags: list[str] = Field(default_factory=list)
    android: AndroidAppOptions | None = None


class LabVmNetwork(StrictModel):
    """One VM-to-network attachment, optionally with a pinned IP.

    Labs can declare a VM joins ``deploy-net`` with ``ip: 10.20.40.20``;
    the tofu adapter turns that into a DHCP reservation (or a domain-
    side IP pin, depending on the provider's capabilities). Today's
    labs that use the legacy ``networks: [edge, lab-private]`` shape
    are normalized into this model by :func:`LabVm.coerce_network_strings`.
    """

    name: str = Field(min_length=1)
    ip: str | None = None
    """Optional IPv4 dotted-quad. Validated against the lab's
    ``LabNetwork.cidr`` by :func:`validation.validator`."""


class LabVm(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    networks: list[LabVmNetwork] = Field(min_length=1)
    """Networks this VM attaches to. Accepts both the legacy
    ``list[str]`` shape (back-compat for labs predating per-VM IPs)
    and the rich ``list[{name, ip?}]`` shape via
    :func:`coerce_network_strings`."""
    resources: Resources | None = None
    tags: list[str] = Field(default_factory=list)
    extra_hosts: list[str] = Field(default_factory=list)
    """Lines to append to ``/etc/hosts`` on this VM at provision time.

    Workaround for the missing lab-scoped DNS (tracked in the
    roadmap backlog). Each entry is a literal ``/etc/hosts`` line
    such as ``"10.20.40.21 target"``.
    """
    provider_overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("networks", mode="before")
    @classmethod
    def coerce_network_strings(cls, value: Any) -> Any:
        """Accept the legacy ``list[str]`` shape alongside ``list[{name, ip?}]``.

        Existing labs that wrote ``networks: [edge, lab-private]``
        continue to parse — each string becomes ``{name: <string>}``
        with no IP pinned. New labs use the object form to pin
        per-network IPs.
        """
        if isinstance(value, list):
            return [
                {"name": item} if isinstance(item, str) else item
                for item in value
            ]
        return value


class LabCommands(StrictModel):
    enabled: list[str] = Field(default_factory=list)


class LabProviders(BaseModel):
    """Per-lab overlays on top of ProviderConfig.spec; open keys."""

    model_config = ConfigDict(extra="allow", frozen=True)


class LabSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str = Field(min_length=1)
    offline: bool = False
    budget: Budget | None = None
    dns_domain: str | None = None
    """Per-lab DNS domain. When unset, the resolver derives
    ``<lab.metadata.name>.lab``. Used by the local-libvirt adapter to
    populate ``libvirt_network.domain`` and per-VM
    ``dns { hosts { hostname, ip } }`` so cross-VM hostname resolution
    works without ``extra_hosts``."""
    networks: list[LabNetwork] = Field(default_factory=list)
    vms: list[LabVm] = Field(default_factory=list)
    workloads: list[LabWorkload] = Field(default_factory=list)
    commands: LabCommands = Field(default_factory=LabCommands)
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    capture: CaptureOptions | None = None
    """Per-lab capture bounds. When unset the resolver falls back to
    ``Defaults.spec.capture``."""

    @field_validator("networks", "vms", "workloads")
    @classmethod
    def names_unique(cls, items: list[Any]) -> list[Any]:
        names = [item.name for item in items]
        if len(names) != len(set(names)):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate names: {duplicates}")
        return items


class Lab(ResourceEnvelope):
    kind: Literal["Lab"]
    spec: LabSpec


# ---------------------------------------------------------------------------
# Discriminated union over every kind
# ---------------------------------------------------------------------------

AnyResource = (
    Defaults
    | ProviderConfig
    | ArtifactSources
    | NetworkProfile
    | VmRole
    | CommandPreset
    | Lab
)
"""Discriminated union by ``kind`` field. Use ``parse_resource`` to dispatch."""


_KIND_MODELS: dict[str, type[ResourceEnvelope]] = {
    "Defaults": Defaults,
    "ProviderConfig": ProviderConfig,
    "ArtifactSources": ArtifactSources,
    "NetworkProfile": NetworkProfile,
    "VmRole": VmRole,
    "CommandPreset": CommandPreset,
    "Lab": Lab,
}


def parse_resource(raw: dict[str, Any]) -> ResourceEnvelope:
    """Dispatch a parsed YAML dict to the right kind model.

    Raises ``ValueError`` if ``kind:`` is missing or unknown; otherwise
    delegates to Pydantic which raises ``ValidationError``.
    """
    if "kind" not in raw:
        raise ValueError("missing top-level 'kind' field")
    kind = raw["kind"]
    cls = _KIND_MODELS.get(kind)
    if cls is None:
        raise ValueError(
            f"unknown kind {kind!r}; expected one of {sorted(_KIND_MODELS)}"
        )
    return cls.model_validate(raw)


KNOWN_KINDS = frozenset(_KIND_MODELS)


__all__ = [
    "KNOWN_KINDS",
    "AnyResource",
    "ArtifactDefaults",
    "ArtifactSources",
    "ArtifactSourcesSpec",
    "AnsibleCollectionSource",
    "Budget",
    "CaptureOptions",
    "CommandBody",
    "CommandEscalation",
    "CommandPreset",
    "CommandPresetSpec",
    "Defaults",
    "DefaultsNetwork",
    "DefaultsSpec",
    "DefaultsVm",
    "DockerImageSource",
    "Lab",
    "LabCommands",
    "LabNetwork",
    "LabSpec",
    "LabVm",
    "LabVmNetwork",
    "LabWorkload",
    "NetworkDns",
    "NetworkProfile",
    "NetworkProfileSpec",
    "ProviderConfig",
    "ProviderConfigSpec",
    "Resources",
    "RetentionLogs",
    "RetentionPolicy",
    "RetentionRuns",
    "SshConfig",
    "TargetSelector",
    "TofuProviderSource",
    "VmImageSource",
    "VmProvisioner",
    "VmRole",
    "VmRoleSpec",
    "VmRouting",
    "WorkloadPlacement",
    "parse_resource",
]
