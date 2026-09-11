"""Deterministic, execution-free ADR-0001 capability resolution contracts."""

from __future__ import annotations

from dataclasses import dataclass

from cv_agent.domain.contracts import (
    AvailabilityState,
    CapabilityDefinition,
    SkillBinding,
)
from cv_agent.domain.errors import DomainValidationError, ValidationCode
from cv_agent.domain.identifiers import AgentId, SkillId, ToolId
from cv_agent.domain.serialization import canonical_json, to_json_value


@dataclass(frozen=True)
class ResolvedToolRef:
    """Tool ID and version retained as one pair for reproducible resolution."""

    tool_id: ToolId
    tool_version: str

    def __post_init__(self) -> None:
        if not self.tool_version:
            raise DomainValidationError(
                ValidationCode.INVALID_VALUE,
                "resolved_tool_ref.tool_version",
                "must be a non-empty string",
                self.tool_version,
            )


@dataclass(frozen=True)
class ResolutionCandidate:
    """Externally evaluated candidate facts consumed by the capability resolver."""

    binding: SkillBinding
    skill_version: str
    tools: tuple[ResolvedToolRef, ...]
    agent_id: AgentId | None
    target_reference: str | None
    compatibility_result: str
    availability_state: AvailabilityState
    compatible: bool
    policy_eligible: bool
    authorized: bool
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.skill_version:
            raise DomainValidationError(
                ValidationCode.INVALID_VALUE,
                "resolution_candidate.skill_version",
                "must be a non-empty string",
                self.skill_version,
            )
        object.__setattr__(self, "tools", tuple(self.tools))
        if not self.compatibility_result:
            raise DomainValidationError(
                ValidationCode.INVALID_VALUE,
                "resolution_candidate.compatibility_result",
                "must be a non-empty string",
                self.compatibility_result,
            )


@dataclass(frozen=True)
class RejectedCandidate:
    """Machine-readable explanation for one unselected resolution candidate."""

    skill_id: SkillId
    code: str
    reason: str


@dataclass(frozen=True)
class Resolution:
    """Immutable capability resolution result, without execution authority."""

    capability: CapabilityDefinition
    skill_id: SkillId | None
    skill_version: str | None
    tools: tuple[ResolvedToolRef, ...]
    agent_id: AgentId | None
    target_reference: str | None
    availability_state: AvailabilityState
    compatible: bool
    authorized: bool
    compatibility_result: str
    unavailable_reason: str | None
    rejected_candidates: tuple[RejectedCandidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "rejected_candidates", tuple(self.rejected_candidates))


def canonical_candidate_facts(candidate: ResolutionCandidate) -> dict[str, object]:
    """Extract canonicalized dictionary of candidate facts for deterministic comparison."""
    return {
        "agent_id": str(candidate.agent_id) if candidate.agent_id is not None else None,
        "authorized": candidate.authorized,
        "availability_state": candidate.availability_state.value,
        "capability_id": str(candidate.binding.capability_id),
        "compatibility_requirements": to_json_value(
            candidate.binding.compatibility.requirements
        ),
        "compatibility_result": candidate.compatibility_result,
        "compatible": candidate.compatible,
        "policy_eligible": candidate.policy_eligible,
        "priority": candidate.binding.priority,
        "provenance": {
            "kind": candidate.binding.provenance.kind.value,
            "metadata": to_json_value(candidate.binding.provenance.metadata),
            "publisher": candidate.binding.provenance.publisher,
            "source": candidate.binding.provenance.source,
            "specification_version": candidate.binding.provenance.specification_version,
            "verified": candidate.binding.provenance.verified,
        },
        "skill_id": str(candidate.binding.skill_id),
        "skill_version": candidate.skill_version,
        "target_reference": candidate.target_reference,
        "tools": [
            {
                "tool_id": str(tool.tool_id),
                "tool_version": tool.tool_version,
            }
            for tool in sorted(
                candidate.tools,
                key=lambda tool: (str(tool.tool_id), tool.tool_version),
            )
        ],
        "unavailable_reason": candidate.unavailable_reason,
    }


def candidate_tie_breaker(candidate: ResolutionCandidate) -> str:
    """Deterministic canonical string representation of candidate facts for tie-breaking."""
    return canonical_json(canonical_candidate_facts(candidate))


class DeterministicCapabilityResolver:
    """Select an already-evaluated binding without discovery, policy, or execution."""

    def __init__(self, *, policy_version: str) -> None:
        if not policy_version:
            raise DomainValidationError(
                ValidationCode.INVALID_VALUE,
                "resolver.policy_version",
                "must be a non-empty string",
                policy_version,
            )
        self._policy_version = policy_version

    @property
    def policy_version(self) -> str:
        """Version identifying the declared deterministic suitability ordering."""
        return self._policy_version

    @staticmethod
    def _candidate_tie_breaker(candidate: ResolutionCandidate) -> str:
        return candidate_tie_breaker(candidate)

    def resolve(
        self,
        capability: CapabilityDefinition,
        candidates: tuple[ResolutionCandidate, ...],
    ) -> Resolution:
        """Resolve by priority then stable identity, preserving rejection evidence."""
        for candidate in candidates:
            if candidate.binding.capability_id != capability.capability_id:
                raise DomainValidationError(
                    ValidationCode.CAPABILITY_MISMATCH,
                    "resolution_candidate.binding.capability_id",
                    "must match the requested capability",
                    candidate.binding.capability_id,
                )

        ordered = tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    -candidate.binding.priority,
                    str(candidate.binding.skill_id),
                    candidate.skill_version,
                    self._candidate_tie_breaker(candidate),
                ),
            )
        )
        rejected: list[RejectedCandidate] = []
        for candidate in ordered:
            rejection = self._rejection_for(candidate)
            if rejection is not None:
                rejected.append(rejection)
                continue
            return Resolution(
                capability=capability,
                skill_id=candidate.binding.skill_id,
                skill_version=candidate.skill_version,
                tools=candidate.tools,
                agent_id=candidate.agent_id,
                target_reference=candidate.target_reference,
                availability_state=AvailabilityState.EXECUTABLE,
                compatible=True,
                authorized=candidate.authorized,
                compatibility_result=candidate.compatibility_result,
                unavailable_reason=None,
                rejected_candidates=tuple(rejected),
            )

        state, reason = self._unresolved_state(ordered)
        return Resolution(
            capability=capability,
            skill_id=None,
            skill_version=None,
            tools=(),
            agent_id=None,
            target_reference=None,
            availability_state=state,
            compatible=False,
            authorized=False,
            compatibility_result="unresolved",
            unavailable_reason=reason,
            rejected_candidates=tuple(rejected),
        )

    @staticmethod
    def _rejection_for(candidate: ResolutionCandidate) -> RejectedCandidate | None:
        if not candidate.policy_eligible:
            return RejectedCandidate(
                candidate.binding.skill_id,
                "POLICY_INELIGIBLE",
                "candidate is not eligible under the supplied policy context",
            )
        if candidate.availability_state is AvailabilityState.KNOWN_BUT_UNAVAILABLE:
            return RejectedCandidate(
                candidate.binding.skill_id,
                "KNOWN_BUT_UNAVAILABLE",
                candidate.unavailable_reason or candidate.compatibility_result,
            )
        if not candidate.compatible:
            return RejectedCandidate(
                candidate.binding.skill_id,
                "INCOMPATIBLE",
                candidate.unavailable_reason or candidate.compatibility_result,
            )
        if candidate.availability_state is not AvailabilityState.EXECUTABLE:
            return RejectedCandidate(
                candidate.binding.skill_id,
                "NOT_EXECUTABLE",
                candidate.availability_state.value,
            )
        return None

    @staticmethod
    def _unresolved_state(
        candidates: tuple[ResolutionCandidate, ...],
    ) -> tuple[AvailabilityState, str]:
        for candidate in candidates:
            if candidate.availability_state is AvailabilityState.KNOWN_BUT_UNAVAILABLE:
                return (
                    AvailabilityState.KNOWN_BUT_UNAVAILABLE,
                    candidate.unavailable_reason or candidate.compatibility_result,
                )
        if candidates:
            return AvailabilityState.UNAVAILABLE, "NO_EXECUTABLE_CANDIDATE"
        return AvailabilityState.KNOWN, "NO_SATISFYING_SKILL"
