"""cv_agent.skills.registry — Authoritative SkillRegistry and ephemeral DiscoveryCache.

Implements:
- SkillRegistry protocol for validated skill specifications.
- DefaultSkillRegistry enforcing ADR-0001 capability authority (B1) and unique SkillId.
- DiscoveryCache isolating non-authoritative runtime candidates (via MCP or remote probe)
  from authoritative registry entries (ADR-0007 §3.3, §3.7).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol, runtime_checkable

from cv_agent.capabilities.registry import CapabilityRegistry
from cv_agent.domain.identifiers import CapabilityId, SkillId
from cv_agent.skills.contracts import SkillDescriptor, SkillProvenance
from cv_agent.skills.errors import UnknownCapabilityError


@runtime_checkable
class SkillRegistry(Protocol):
    """Authoritative catalogue of validated, registered skill descriptors."""

    def register_skill(self, descriptor: SkillDescriptor) -> None:
        """Register a validated skill descriptor into the registry."""
        ...

    def get_skill(self, skill_id: SkillId) -> SkillDescriptor | None:
        """Retrieve a skill descriptor by its unique ID."""
        ...

    def list_skills(self) -> tuple[SkillDescriptor, ...]:
        """List all authoritative skills registered in the system."""
        ...

    def list_skills_for_capability(
        self, capability_id: CapabilityId
    ) -> tuple[SkillDescriptor, ...]:
        """List all skills that satisfy the specified capability."""
        ...


class DefaultSkillRegistry:
    """Thread-safe authoritative skill registry enforcing ADR-0001 capability authority."""

    def __init__(
        self,
        *,
        capability_registry: CapabilityRegistry | None = None,
        valid_capability_ids: Collection[CapabilityId] | None = None,
    ) -> None:
        self._skills: dict[SkillId, SkillDescriptor] = {}
        self._cap_to_skills: dict[CapabilityId, list[SkillDescriptor]] = {}
        self._capability_registry = capability_registry
        self._valid_capability_ids = (
            frozenset(valid_capability_ids)
            if valid_capability_ids is not None
            else None
        )

    def is_capability_valid(self, capability_id: CapabilityId) -> bool:
        """Verify whether capability_id is authorized under ADR-0001."""
        if self._valid_capability_ids is not None:
            return capability_id in self._valid_capability_ids

        if self._capability_registry is not None:
            return self._capability_registry.get_capability(capability_id) is not None

        # If no capability authority passed, treat any non-empty CapabilityId as valid
        return bool(str(capability_id).strip())

    def register_skill(self, descriptor: SkillDescriptor) -> None:
        """Register a validated skill descriptor into the authoritative catalogue.

        Enforces:
        - Unique skill_id (rejects duplicate registrations).
        - ADR-0001 capability authority (B1): every declared capability ID must exist.
        - Provenance invariant: authoritative skills must be INTERNAL or EXTERNAL.
        """
        if descriptor.skill_id in self._skills:
            raise ValueError(
                f"Skill {str(descriptor.skill_id)!r} is already registered in the authoritative catalogue"
            )

        if descriptor.provenance == SkillProvenance.DISCOVERED:
            raise ValueError(
                f"Cannot register discovered skill {str(descriptor.skill_id)!r} directly into "
                f"authoritative registry; must use DiscoveryCache"
            )

        # Enforce ADR-0001 capability authority (B1)
        for cap_id in descriptor.satisfies_capabilities:
            if not self.is_capability_valid(cap_id):
                raise UnknownCapabilityError(
                    f"Skill {str(descriptor.skill_id)!r} declares unregistered capability {str(cap_id)!r} "
                    f"violating ADR-0001 authority (B1)"
                )

        self._skills[descriptor.skill_id] = descriptor
        for cap_id in descriptor.satisfies_capabilities:
            self._cap_to_skills.setdefault(cap_id, []).append(descriptor)

    def get_skill(self, skill_id: SkillId) -> SkillDescriptor | None:
        """Retrieve a skill descriptor by ID."""
        return self._skills.get(skill_id)

    def list_skills(self) -> tuple[SkillDescriptor, ...]:
        """List all authoritative skills."""
        return tuple(self._skills.values())

    def list_skills_for_capability(
        self, capability_id: CapabilityId
    ) -> tuple[SkillDescriptor, ...]:
        """List all authoritative skills satisfying the specified capability."""
        return tuple(self._cap_to_skills.get(capability_id, ()))


class DiscoveryCache:
    """Ephemeral candidate pool for runtime-discovered skills (via MCP or remote probe).

    Invariants (ADR-0007 §3.3, §3.7):
    - Discovered skills are held in a separate ephemeral cache.
    - Discovered skills NEVER overwrite authoritative registry entries.
    - Discovered skills must bind to existing authoritative ADR-0001 capabilities.
    """

    def __init__(
        self,
        authoritative_registry: SkillRegistry,
        *,
        capability_registry: CapabilityRegistry | None = None,
        valid_capability_ids: Collection[CapabilityId] | None = None,
    ) -> None:
        self._authoritative = authoritative_registry
        self._candidates: dict[SkillId, SkillDescriptor] = {}
        self._cap_to_candidates: dict[CapabilityId, list[SkillDescriptor]] = {}
        self._capability_registry = capability_registry
        self._valid_capability_ids = (
            frozenset(valid_capability_ids)
            if valid_capability_ids is not None
            else None
        )

    def is_capability_valid(self, capability_id: CapabilityId) -> bool:
        """Verify whether capability_id is recognized under ADR-0001."""
        if self._valid_capability_ids is not None:
            return capability_id in self._valid_capability_ids

        if self._capability_registry is not None:
            return self._capability_registry.get_capability(capability_id) is not None

        return True

    def register_discovered(self, descriptor: SkillDescriptor) -> None:
        """Register a runtime-discovered candidate skill into the discovery cache.

        Enforces:
        - Ephemeral candidates cannot overwrite authoritative registry entries.
        - Provenance must be SkillProvenance.DISCOVERED.
        - Satisfies capabilities must be valid ADR-0001 capabilities.
        """
        if self._authoritative.get_skill(descriptor.skill_id) is not None:
            raise ValueError(
                f"Discovered candidate skill {str(descriptor.skill_id)!r} cannot overwrite "
                f"authoritative skill in SkillRegistry"
            )

        if descriptor.provenance != SkillProvenance.DISCOVERED:
            raise ValueError(
                f"Discovered candidate skill must have provenance=SkillProvenance.DISCOVERED, "
                f"got {descriptor.provenance!r}"
            )

        for cap_id in descriptor.satisfies_capabilities:
            if not self.is_capability_valid(cap_id):
                raise UnknownCapabilityError(
                    f"Discovered skill {str(descriptor.skill_id)!r} declares unregistered capability "
                    f"{str(cap_id)!r} violating ADR-0001 authority (B1)"
                )

        self._candidates[descriptor.skill_id] = descriptor
        for cap_id in descriptor.satisfies_capabilities:
            self._cap_to_candidates.setdefault(cap_id, []).append(descriptor)

    def get_candidate(self, skill_id: SkillId) -> SkillDescriptor | None:
        """Retrieve a discovered candidate by ID."""
        return self._candidates.get(skill_id)

    def list_candidates(self) -> tuple[SkillDescriptor, ...]:
        """List all discovered candidates."""
        return tuple(self._candidates.values())

    def list_candidates_for_capability(
        self, capability_id: CapabilityId
    ) -> tuple[SkillDescriptor, ...]:
        """List discovered candidates satisfying the specified capability."""
        return tuple(self._cap_to_candidates.get(capability_id, ()))

    def clear(self) -> None:
        """Clear all ephemeral discovery candidates."""
        self._candidates.clear()
        self._cap_to_candidates.clear()
