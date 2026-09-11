"""Immutable and deterministic serialization for ADR-0001 resolution snapshots."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cv_agent.domain.contracts import AvailabilityState
from cv_agent.domain.errors import DomainValidationError, ValidationCode
from cv_agent.domain.identifiers import AgentId, CapabilityId, SkillId, ToolId
from cv_agent.domain.resolution import ResolvedToolRef


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise DomainValidationError(
            ValidationCode.INVALID_VALUE,
            field_name,
            "must be a non-empty string",
            value,
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise DomainValidationError(
            ValidationCode.INVALID_TIMESTAMP,
            "resolution_snapshot.created_at",
            "must be timezone-aware UTC",
            value,
        )
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ResolutionSnapshot:
    """Historical, serializable resolution fact; it is not mutable live registry state."""

    snapshot_id: str
    registry_version: str
    resolver_policy_version: str
    capability_id: CapabilityId
    capability_version: str
    skill_id: SkillId | None
    skill_version: str | None
    tools: tuple[ResolvedToolRef, ...]
    agent_id: AgentId | None
    target_reference: str | None
    compatibility_result: str
    availability_state: AvailabilityState
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "snapshot_id",
            "registry_version",
            "resolver_policy_version",
            "capability_version",
            "compatibility_result",
        ):
            _require_text(getattr(self, field_name), field_name)
        if (self.skill_id is None) != (self.skill_version is None):
            raise DomainValidationError(
                ValidationCode.INVALID_VALUE,
                "resolution_snapshot.skill",
                "skill_id and skill_version must both be set or both be absent",
            )
        if self.skill_version is not None:
            _require_text(self.skill_version, "skill_version")
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "created_at", _as_utc(self.created_at))

    def to_dict(self) -> dict[str, object]:
        """Return an ordinary JSON-compatible representation of this snapshot."""
        return {
            "snapshot_id": self.snapshot_id,
            "registry_version": self.registry_version,
            "resolver_policy_version": self.resolver_policy_version,
            "capability_id": str(self.capability_id),
            "capability_version": self.capability_version,
            "skill_id": str(self.skill_id) if self.skill_id is not None else None,
            "skill_version": self.skill_version,
            "tools": [
                {"tool_id": str(tool.tool_id), "tool_version": tool.tool_version}
                for tool in self.tools
            ],
            "agent_id": str(self.agent_id) if self.agent_id is not None else None,
            "target_reference": self.target_reference,
            "compatibility_result": self.compatibility_result,
            "availability_state": self.availability_state.value,
            "created_at": _format_utc(self.created_at),
        }

    def to_json(self) -> str:
        """Serialize with stable object-key ordering and no formatting ambiguity."""
        return json.dumps(
            self.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )

    @classmethod
    def from_json(cls, value: str) -> ResolutionSnapshot:
        """Deserialize one JSON snapshot while rejecting non-object payloads."""
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise DomainValidationError(
                ValidationCode.INVALID_SERIALIZATION,
                "resolution_snapshot",
                "must be valid JSON",
                value,
            ) from error
        if not isinstance(payload, dict):
            raise DomainValidationError(
                ValidationCode.INVALID_SERIALIZATION,
                "resolution_snapshot",
                "must decode to an object",
                payload,
            )
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ResolutionSnapshot:
        """Deserialize a structured snapshot with typed IDs and UTC timestamp validation."""
        try:
            tool_values = value["tools"]
            if not isinstance(tool_values, list):
                raise TypeError("tools must be a list")
            tools = tuple(
                ResolvedToolRef(
                    ToolId(_text(item, "tool_id")), _text(item, "tool_version")
                )
                for item in tool_values
                if isinstance(item, Mapping)
            )
            if len(tools) != len(tool_values):
                raise TypeError("every tool must be an object")
            skill_id = _optional_skill_id(value["skill_id"])
            agent_id = _optional_agent_id(value["agent_id"])
            created_at = datetime.fromisoformat(
                _text(value, "created_at").replace("Z", "+00:00")
            )
            return cls(
                snapshot_id=_text(value, "snapshot_id"),
                registry_version=_text(value, "registry_version"),
                resolver_policy_version=_text(value, "resolver_policy_version"),
                capability_id=CapabilityId(_text(value, "capability_id")),
                capability_version=_text(value, "capability_version"),
                skill_id=skill_id,
                skill_version=_optional_text(value["skill_version"]),
                tools=tools,
                agent_id=agent_id,
                target_reference=_optional_text(value["target_reference"]),
                compatibility_result=_text(value, "compatibility_result"),
                availability_state=AvailabilityState(
                    _text(value, "availability_state")
                ),
                created_at=created_at,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DomainValidationError(
                ValidationCode.INVALID_SERIALIZATION,
                "resolution_snapshot",
                "does not match the ResolutionSnapshot schema",
                value,
            ) from error


def _text(value: Mapping[str, object], field_name: str) -> str:
    item = value[field_name]
    if not isinstance(item, str):
        raise TypeError(f"{field_name} must be a string")
    return item


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("optional value must be a string or null")
    return value


def _optional_skill_id(value: object) -> SkillId | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("identifier must be a string or null")
    return SkillId(value)


def _optional_agent_id(value: object) -> AgentId | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("identifier must be a string or null")
    return AgentId(value)
