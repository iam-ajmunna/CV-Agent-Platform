"""cv_agent.skills.resolver — Capability to Skill resolver delegating to ADR-0001 availability semantics.

Implements ADR-0007 §3.3, §3.5, §3.6:
- Resolves CapabilityId -> candidate SkillDescriptor -> ToolId(s) bound to ExecutionTargetDescriptor.
- Semantic evaluation of TargetRequirements (HardwareClass subsumption, numeric CUDA compute, allowed targets).
- Tool dependency verification against ADR-0005 ToolRegistry.
- Hardware-aware KNOWN_BUT_UNAVAILABLE enforcement on local macOS workstations (D-022).
- Zero silent CPU fallback guarantee per [P§15] and [P§29.4].
- Delegates availability lifecycle state directly to ADR-0001 (B2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from cv_agent.capabilities.registry import CapabilityRegistry
from cv_agent.domain.contracts import AvailabilityState
from cv_agent.domain.identifiers import CapabilityId
from cv_agent.skills.contracts import (
    HardwareClass,
    SkillDescriptor,
    SkillResolution,
    is_hardware_compatible,
    parse_cuda_compute,
)
from cv_agent.skills.errors import UnknownCapabilityError
from cv_agent.skills.registry import DiscoveryCache, SkillRegistry
from cv_agent.tools.contracts import ExecutionTargetDescriptor, TargetType, ToolRegistry


@runtime_checkable
class SkillResolver(Protocol):
    """Resolves capabilities to skills bound to concrete execution targets."""

    def resolve(
        self,
        capability_id: CapabilityId,
        available_targets: tuple[ExecutionTargetDescriptor, ...],
        *,
        tool_registry: ToolRegistry | None = None,
    ) -> SkillResolution:
        """Resolve a capability to candidate skills and evaluate availability."""
        ...


class DefaultSkillResolver:
    """Deterministic skill resolver delegating to ADR-0001 availability semantics."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        *,
        discovery_cache: DiscoveryCache | None = None,
        capability_registry: CapabilityRegistry | None = None,
    ) -> None:
        self._skill_registry = skill_registry
        self._discovery_cache = discovery_cache
        self._capability_registry = capability_registry

    def _is_capability_known(self, capability_id: CapabilityId) -> bool:
        """Check whether capability_id is registered under ADR-0001."""
        if self._capability_registry is None:
            return True
        return self._capability_registry.get_capability(capability_id) is not None

    def resolve(
        self,
        capability_id: CapabilityId,
        available_targets: tuple[ExecutionTargetDescriptor, ...],
        *,
        tool_registry: ToolRegistry | None = None,
    ) -> SkillResolution:
        """Resolve a capability to candidate skills and evaluate operational availability.

        Resolution chain (ADR-0007 §3.3, B2):
        CapabilityId -> Skill Candidates -> Target/Tool Compatibility -> ADR-0001 AvailabilityState.
        """
        # 1. Authority validation against ADR-0001 (B1)
        if not self._is_capability_known(capability_id):
            raise UnknownCapabilityError(
                f"Requested capability {str(capability_id)!r} is not registered in ADR-0001"
            )

        # 2. Gather candidates from authoritative registry and ephemeral discovery cache
        authoritative_candidates = self._skill_registry.list_skills_for_capability(
            capability_id
        )
        discovered_candidates: tuple[SkillDescriptor, ...] = ()
        if self._discovery_cache is not None:
            discovered_candidates = (
                self._discovery_cache.list_candidates_for_capability(capability_id)
            )

        all_candidates = authoritative_candidates + discovered_candidates

        # 3. Handle zero satisfying skills
        if not all_candidates:
            return SkillResolution(
                capability_id=capability_id,
                candidate_skills=(),
                selected_skill=None,
                availability_state=AvailabilityState.KNOWN,
                unavailability_reason="NO_SATISFYING_SKILL: Zero registered skills satisfy this capability",
            )

        # 4. If no compute targets configured at all
        if not available_targets:
            # Check if candidates require hardware constraints
            first_candidate = all_candidates[0]
            if (
                first_candidate.target_requirements.required_hardware
                != HardwareClass.ANY
            ):
                return SkillResolution(
                    capability_id=capability_id,
                    candidate_skills=all_candidates,
                    selected_skill=first_candidate,
                    availability_state=AvailabilityState.KNOWN_BUT_UNAVAILABLE,
                    unavailability_reason="No execution targets configured; hardware prerequisites cannot be satisfied",
                )
            return SkillResolution(
                capability_id=capability_id,
                candidate_skills=all_candidates,
                selected_skill=first_candidate,
                availability_state=AvailabilityState.KNOWN,
                unavailability_reason="Zero execution targets configured",
            )

        # 5. Evaluate candidates against targets
        # Check for host-local macOS constraint (D-022, Gate 5)
        # If candidate requires NVIDIA GPU / remote target, but only LOCAL (apple_silicon / cpu) targets exist:
        requires_nvidia = any(
            c.target_requirements.required_hardware
            in (
                HardwareClass.NVIDIA_GPU,
                HardwareClass.JETSON,
                HardwareClass.JETSON_ORIN,
                HardwareClass.JETSON_XAVIER,
            )
            or c.target_requirements.min_cuda_compute is not None
            or TargetType.LOCAL not in c.target_requirements.allowed_targets
            for c in all_candidates
        )

        has_remote_or_mcp = any(
            t.target_type in (TargetType.REMOTE, TargetType.MCP_ENDPOINT)
            for t in available_targets
        )

        # Test each candidate for a fully compatible target
        for candidate in all_candidates:
            compat_result = self._evaluate_candidate(
                candidate, available_targets, tool_registry
            )
            if compat_result is not None:
                target, missing_tools = compat_result
                if not missing_tools:
                    if target.is_available:
                        return SkillResolution(
                            capability_id=capability_id,
                            candidate_skills=all_candidates,
                            selected_skill=candidate,
                            availability_state=AvailabilityState.EXECUTABLE,
                            target_descriptor=target,
                        )
                    else:
                        return SkillResolution(
                            capability_id=capability_id,
                            candidate_skills=all_candidates,
                            selected_skill=candidate,
                            availability_state=AvailabilityState.UNAVAILABLE,
                            target_descriptor=target,
                            unavailability_reason=f"Target {str(target.target_id)!r} is offline or unavailable",
                        )
                else:
                    return SkillResolution(
                        capability_id=capability_id,
                        candidate_skills=all_candidates,
                        selected_skill=candidate,
                        availability_state=AvailabilityState.UNAVAILABLE,
                        target_descriptor=target,
                        missing_prerequisites=missing_tools,
                        unavailability_reason=f"Required tools missing on target {str(target.target_id)!r}: {missing_tools}",
                    )

        # If no compatible target found and NVIDIA capability requested on local host (D-022)
        if requires_nvidia and not has_remote_or_mcp:
            return SkillResolution(
                capability_id=capability_id,
                candidate_skills=all_candidates,
                selected_skill=all_candidates[0],
                availability_state=AvailabilityState.KNOWN_BUT_UNAVAILABLE,
                unavailability_reason=(
                    f"NVIDIA skill {str(all_candidates[0].skill_id)!r} requires remote GPU/Jetson target with "
                    f"CUDA compute, but only local macOS workstation is configured (D-022). Zero silent fallback permitted."
                ),
            )

        # Fallback if targets exist but none compatible
        return SkillResolution(
            capability_id=capability_id,
            candidate_skills=all_candidates,
            selected_skill=all_candidates[0],
            availability_state=AvailabilityState.UNAVAILABLE,
            unavailability_reason="No configured execution target satisfies skill target requirements",
        )

    def _evaluate_candidate(
        self,
        candidate: SkillDescriptor,
        available_targets: tuple[ExecutionTargetDescriptor, ...],
        tool_registry: ToolRegistry | None,
    ) -> tuple[ExecutionTargetDescriptor, tuple[str, ...]] | None:
        """Find the best compatible target for candidate, returning (target, missing_tools) or None."""
        reqs = candidate.target_requirements

        for target in available_targets:
            # Check target_type
            if target.target_type not in reqs.allowed_targets:
                continue

            # Check hardware
            target_hw = str(target.target_metadata.get("hardware", ""))
            if not is_hardware_compatible(reqs.required_hardware, target_hw):
                continue

            # Check numeric CUDA compute
            if reqs.min_cuda_compute is not None:
                target_compute_val = target.target_metadata.get("cuda_compute")
                if not target_compute_val:
                    continue
                try:
                    req_compute = parse_cuda_compute(reqs.min_cuda_compute)
                    target_compute = parse_cuda_compute(str(target_compute_val))
                    if target_compute < req_compute:
                        continue
                except ValueError:
                    continue

            # Check required env vars
            if reqs.required_env_vars:
                target_env = target.target_metadata.get("env_vars")
                if not isinstance(target_env, Mapping) or any(
                    v not in target_env for v in reqs.required_env_vars
                ):
                    continue

            # Check tools if tool_registry provided
            missing_tools: list[str] = []
            if tool_registry is not None:
                for tool_id in candidate.required_tools:
                    tool_desc = tool_registry.get(tool_id)
                    if tool_desc is None:
                        missing_tools.append(str(tool_id))
                    else:
                        avail = tool_registry.check_availability(
                            tool_id, target.target_id
                        )
                        if not avail.is_available:
                            missing_tools.append(str(tool_id))

            return target, tuple(missing_tools)

        return None
