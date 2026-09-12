"""Immutable ADR-0001 capability-model value objects and bindings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from cv_agent.domain.errors import DomainValidationError, ValidationCode
from cv_agent.domain.identifiers import AgentId, CapabilityId, SkillId, ToolId
from cv_agent.domain.serialization import FrozenJsonValue, freeze_json_mapping


class Stage(str, Enum):
    """Canonical CV engineering stages owned by the capability model."""

    DISCOVER = "DISCOVER"
    DEFINE = "DEFINE"
    RESEARCH = "RESEARCH"
    DESIGN = "DESIGN"
    DATA = "DATA"
    BASELINE = "BASELINE"
    TRAIN = "TRAIN"
    EVALUATE = "EVALUATE"
    DIAGNOSE = "DIAGNOSE"
    OPTIMIZE = "OPTIMIZE"
    BENCHMARK = "BENCHMARK"
    DEPLOY = "DEPLOY"
    MONITOR = "MONITOR"


class AvailabilityState(str, Enum):
    """Operational readiness, deliberately separate from authorization."""

    KNOWN = "KNOWN"
    DISCOVERED = "DISCOVERED"
    CONFIGURED = "CONFIGURED"
    AVAILABLE = "AVAILABLE"
    EXECUTABLE = "EXECUTABLE"
    UNAVAILABLE = "UNAVAILABLE"
    KNOWN_BUT_UNAVAILABLE = "KNOWN_BUT_UNAVAILABLE"


class ProvenanceKind(str, Enum):
    """Extensible provenance classes required for trustworthy resolution records."""

    INTERNAL = "internal"
    OFFICIAL_VENDOR = "official_vendor"
    OFFICIAL_REPOSITORY = "official_repository"
    COMMUNITY = "community"
    REMOTE_SERVICE = "remote_service"
    GENERATED = "generated"
    DISCOVERED = "discovered"
    USER_REGISTERED = "user_registered"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise DomainValidationError(
            ValidationCode.INVALID_VALUE,
            field_name,
            "must be a non-empty string",
            value,
        )


@dataclass(frozen=True)
class CompatibilityConstraint:
    """Declarative requirements evaluated externally against an actual environment."""

    requirements: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "requirements",
            freeze_json_mapping(self.requirements, field="compatibility.requirements"),
        )


@dataclass(frozen=True)
class Provenance:
    """Source and verification context without embedding discovery policy."""

    kind: ProvenanceKind
    publisher: str | None = None
    source: str | None = None
    specification_version: str | None = None
    verified: bool | None = None
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "metadata",
            freeze_json_mapping(self.metadata, field="provenance.metadata"),
        )


@dataclass(frozen=True)
class CapabilityDefinition:
    """Declarative capability metadata; it neither authorizes nor executes work."""

    capability_id: CapabilityId
    version: str
    stage: Stage
    description: str
    risk_level: str
    requires_approval: bool

    def __post_init__(self) -> None:
        _require_text(self.version, "capability.version")
        _require_text(self.description, "capability.description")
        _require_text(self.risk_level, "capability.risk_level")


@dataclass(frozen=True)
class SkillBinding:
    """Typed statement that a skill can satisfy a capability under constraints."""

    capability_id: CapabilityId
    skill_id: SkillId
    compatibility: CompatibilityConstraint
    priority: int
    provenance: Provenance

    def __post_init__(self) -> None:
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise DomainValidationError(
                ValidationCode.INVALID_VALUE,
                "skill_binding.priority",
                "must be an integer",
                self.priority,
            )


@dataclass(frozen=True)
class ToolBinding:
    """Typed statement that a skill depends on a logical tool identity."""

    skill_id: SkillId
    tool_id: ToolId
    compatibility: CompatibilityConstraint


@dataclass(frozen=True)
class AgentBinding:
    """Typed statement linking a logical tool to an execution-capable actor."""

    tool_id: ToolId
    agent_id: AgentId
