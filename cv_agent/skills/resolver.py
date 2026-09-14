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


def _hardware_specificity(hw: HardwareClass) -> int:
    """Numerical hardware specificity ranking (higher = more specific/demanding)."""
    ranks = {
        HardwareClass.JETSON_ORIN: 5,
        HardwareClass.JETSON_XAVIER: 5,
        HardwareClass.JETSON: 4,
        HardwareClass.NVIDIA_GPU: 3,
        HardwareClass.CPU: 2,
        HardwareClass.ANY: 1,
    }
    return ranks.get(hw, 0)


def _candidate_target_key(
    cand: SkillDescriptor, target: ExecutionTargetDescriptor
) -> tuple[int, int, tuple[int, int], str, str]:
    """Deterministic total order key for (candidate, target) pairs (I5)."""
    cand_hw_rank = _hardware_specificity(cand.target_requirements.required_hardware)

    # Target hardware specificity
    target_hw_str = str(target.target_metadata.get("hardware", "")).lower()
    target_hw = None
    try:
        target_hw = HardwareClass(target_hw_str)
    except ValueError:
        pass
    target_hw_rank = _hardware_specificity(target_hw) if target_hw else 0

    target_compute = (0, 0)
    target_compute_val = target.target_metadata.get("cuda_compute")
    if target_compute_val:
        try:
            target_compute = parse_cuda_compute(str(target_compute_val))
        except ValueError:
            target_compute = (0, 0)

    return (
        -cand_hw_rank,
        -target_hw_rank,
        (-target_compute[0], -target_compute[1]),
        str(cand.skill_id),
        str(target.target_id),
    )


class DefaultSkillResolver:
    """Deterministic skill resolver delegating to ADR-0001 availability semantics."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        *,
        discovery_cache: DiscoveryCache | None = None,
        capability_registry: CapabilityRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._skill_registry = skill_registry
        self._discovery_cache = discovery_cache
        if capability_registry is None:
            from cv_agent.capabilities.registry import load_default_capability_registry

            capability_registry = load_default_capability_registry()
        self._capability_registry = capability_registry
        self._tool_registry = tool_registry

    def _is_capability_known(self, capability_id: CapabilityId) -> bool:
        """Check whether capability_id is registered under ADR-0001."""
        if self._capability_registry is not None:
            return self._capability_registry.get_capability(capability_id) is not None
        return False

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
        # 1. Authority validation against ADR-0001 (B1, B3)
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

        all_candidates = tuple(
            sorted(
                authoritative_candidates + discovered_candidates,
                key=lambda c: (
                    -_hardware_specificity(c.target_requirements.required_hardware),
                    str(c.skill_id),
                ),
            )
        )

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

        # 5. Target Compatibility Evaluation
        requires_nvidia = any(
            c.target_requirements.required_hardware
            in (
                HardwareClass.NVIDIA_GPU,
                HardwareClass.JETSON,
                HardwareClass.JETSON_ORIN,
                HardwareClass.JETSON_XAVIER,
            )
            or c.target_requirements.min_cuda_compute is not None
            for c in all_candidates
        )

        has_remote_or_mcp = any(
            t.target_type in (TargetType.REMOTE, TargetType.MCP_ENDPOINT)
            for t in available_targets
        )

        effective_tool_registry = tool_registry or self._tool_registry

        # Buckets for order-independent selection (I5):
        # Cat 1: target available + all tools & prerequisites satisfied (EXECUTABLE)
        # Cat 2: target available + missing tools or prerequisites (UNAVAILABLE)
        # Cat 3: target offline, but hardware matches (UNAVAILABLE)
        cat1: list[tuple[SkillDescriptor, ExecutionTargetDescriptor]] = []
        cat2: list[
            tuple[SkillDescriptor, ExecutionTargetDescriptor, tuple[str, ...]]
        ] = []
        cat3: list[
            tuple[SkillDescriptor, ExecutionTargetDescriptor, tuple[str, ...]]
        ] = []

        for candidate in all_candidates:
            for target in available_targets:
                eval_res = self._evaluate_candidate_target(
                    candidate, target, effective_tool_registry
                )
                if eval_res is None:
                    continue  # Incompatible target (hardware/compute/target_type mismatch)

                missing = eval_res
                if target.is_available:
                    if not missing:
                        cat1.append((candidate, target))
                    else:
                        cat2.append((candidate, target, missing))
                else:
                    cat3.append((candidate, target, missing))

        # Order-independent priority selection (I5 - fully deterministically sorted buckets):
        if cat1:
            cat1.sort(key=lambda item: _candidate_target_key(item[0], item[1]))
            best_cand, best_target = cat1[0]
            return SkillResolution(
                capability_id=capability_id,
                candidate_skills=all_candidates,
                selected_skill=best_cand,
                availability_state=AvailabilityState.EXECUTABLE,
                target_descriptor=best_target,
            )

        if cat2:
            # Sort by fewest missing items, then deterministic candidate/target tie-break
            cat2.sort(
                key=lambda item: (
                    len(item[2]),
                    _candidate_target_key(item[0], item[1]),
                )
            )
            best_cand, best_target, missing = cat2[0]
            return SkillResolution(
                capability_id=capability_id,
                candidate_skills=all_candidates,
                selected_skill=best_cand,
                availability_state=AvailabilityState.UNAVAILABLE,
                target_descriptor=best_target,
                missing_prerequisites=missing,
                unavailability_reason=(
                    f"Required tools or prerequisites missing on target {str(best_target.target_id)!r}: "
                    f"{list(missing)}"
                ),
            )

        if cat3:
            # Sort by fewest missing items, then deterministic candidate/target tie-break
            cat3.sort(
                key=lambda item: (
                    len(item[2]),
                    _candidate_target_key(item[0], item[1]),
                )
            )
            best_cand, best_target, missing = cat3[0]
            return SkillResolution(
                capability_id=capability_id,
                candidate_skills=all_candidates,
                selected_skill=best_cand,
                availability_state=AvailabilityState.UNAVAILABLE,
                target_descriptor=best_target,
                missing_prerequisites=missing,
                unavailability_reason=f"Target {str(best_target.target_id)!r} is offline or unavailable",
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

    def _evaluate_candidate_target(
        self,
        candidate: SkillDescriptor,
        target: ExecutionTargetDescriptor,
        tool_registry: ToolRegistry | None,
    ) -> tuple[str, ...] | None:
        """Evaluate a candidate skill against a single target.

        Returns tuple of missing tool IDs and prerequisites, or None if hardware/type incompatible.
        """
        reqs = candidate.target_requirements

        # 1. Target type compatibility
        if target.target_type not in reqs.allowed_targets:
            return None

        # 2. Hardware class compatibility
        target_hw = str(target.target_metadata.get("hardware", ""))
        if not is_hardware_compatible(reqs.required_hardware, target_hw):
            return None

        # 3. Numeric CUDA compute check
        if reqs.min_cuda_compute is not None:
            target_compute_val = target.target_metadata.get("cuda_compute")
            if not target_compute_val:
                return None
            try:
                req_compute = parse_cuda_compute(reqs.min_cuda_compute)
                target_compute = parse_cuda_compute(str(target_compute_val))
                if target_compute < req_compute:
                    return None
            except ValueError:
                return None

        # 4. Required environment variables
        if reqs.required_env_vars:
            target_env = target.target_metadata.get("env_vars")
            if not isinstance(target_env, Mapping) or any(
                v not in target_env for v in reqs.required_env_vars
            ):
                return None

        # 5. Tool dependency verification (Blocker B4)
        missing_tools: list[str] = []
        if candidate.required_tools:
            if tool_registry is None:
                # B4: If tool_registry is None, required tools cannot be verified.
                # Must fail verification to prevent false-positive EXECUTABLE states.
                missing_tools = [
                    f"unverified_tool:{tid}" for tid in candidate.required_tools
                ]
            else:
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

        # 6. Declarative prerequisites evaluation (Important I3 - Fail Closed)
        missing_prereqs: list[str] = []
        if candidate.prerequisites:
            target_prereqs = target.target_metadata.get("prerequisites")
            target_features = target.target_metadata.get("features")
            provided: set[str] = set()
            if isinstance(target_prereqs, (list, tuple, set, frozenset)):
                provided.update(str(p).lower() for p in target_prereqs)
            if isinstance(target_features, (list, tuple, set, frozenset)):
                provided.update(str(f).lower() for f in target_features)

            # Fail closed: if prerequisite metadata is absent or missing required items,
            # each unsatisfied prerequisite is unconditionally added to missing_prereqs.
            for req_p in candidate.prerequisites:
                if req_p.lower() not in provided:
                    missing_prereqs.append(req_p)

        return tuple(missing_tools) + tuple(missing_prereqs)
