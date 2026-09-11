"""Contract tests for the Phase 2A ADR-0001 domain substrate."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from cv_agent.domain.contracts import (
    AgentBinding,
    AvailabilityState,
    CapabilityDefinition,
    CompatibilityConstraint,
    Provenance,
    ProvenanceKind,
    SkillBinding,
    Stage,
    ToolBinding,
)
from cv_agent.domain.errors import DomainValidationError, ValidationCode
from cv_agent.domain.identifiers import AgentId, CapabilityId, SkillId, ToolId
from cv_agent.domain.resolution import (
    DeterministicCapabilityResolver,
    ResolutionCandidate,
    ResolvedToolRef,
)
from cv_agent.domain.snapshots import ResolutionSnapshot


def _capability() -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=CapabilityId("model.optimize.tensorrt"),
        version="1.0.0",
        stage=Stage.OPTIMIZE,
        description="Compile a model for TensorRT.",
        risk_level="high",
        requires_approval=True,
    )


def _binding(*, priority: int = 10) -> SkillBinding:
    return SkillBinding(
        capability_id=_capability().capability_id,
        skill_id=SkillId("nvidia.tensorrt.inference_optimization"),
        compatibility=CompatibilityConstraint(
            requirements={"target_class": "REMOTE", "accelerator": "nvidia_gpu"}
        ),
        priority=priority,
        provenance=Provenance(kind=ProvenanceKind.OFFICIAL_VENDOR, publisher="NVIDIA"),
    )


@pytest.mark.parametrize(
    "identifier",
    ["", "UPPERCASE", "leading/slash/", "leading//slash", "has space", "é"],
)
def test_identifiers_reject_noncanonical_syntax(identifier: str) -> None:
    with pytest.raises(DomainValidationError) as error:
        CapabilityId(identifier)

    assert error.value.code is ValidationCode.INVALID_IDENTIFIER


def test_identifier_types_are_semantically_distinct() -> None:
    capability_id = CapabilityId("model.optimize.tensorrt")
    skill_id = SkillId("nvidia.tensorrt.inference_optimization")
    tool_id = ToolId("tool.nvidia.trtexec")
    agent_id = AgentId("agent.remote.gpu")

    assert type(capability_id) is CapabilityId
    assert type(skill_id) is SkillId
    assert type(tool_id) is ToolId
    assert type(agent_id) is AgentId
    assert (
        len({type(capability_id), type(skill_id), type(tool_id), type(agent_id)}) == 4
    )


def test_typed_bindings_are_immutable_and_preserve_identity_boundaries() -> None:
    binding = _binding()
    tool_binding = ToolBinding(
        skill_id=binding.skill_id,
        tool_id=ToolId("tool.nvidia.trtexec"),
        compatibility=CompatibilityConstraint(requirements={"os": "linux"}),
    )
    agent_binding = AgentBinding(
        tool_id=tool_binding.tool_id,
        agent_id=AgentId("agent.remote.gpu"),
    )

    with pytest.raises(FrozenInstanceError):
        binding.priority = 20  # type: ignore[misc]

    assert agent_binding.tool_id == tool_binding.tool_id
    assert binding.capability_id == _capability().capability_id


def test_resolution_is_deterministic_when_registration_order_differs() -> None:
    capability = _capability()
    selected = ResolutionCandidate(
        binding=_binding(priority=20),
        skill_version="2.0.0",
        tools=(ResolvedToolRef(ToolId("tool.nvidia.trtexec"), "10.0.0"),),
        agent_id=AgentId("agent.remote.gpu"),
        target_reference="target/gpu-a",
        compatibility_result="compatible",
        availability_state=AvailabilityState.EXECUTABLE,
        compatible=True,
        policy_eligible=True,
        authorized=False,
    )
    lower_priority = ResolutionCandidate(
        binding=_binding(priority=1),
        skill_version="1.0.0",
        tools=(),
        agent_id=None,
        target_reference="target/gpu-b",
        compatibility_result="compatible",
        availability_state=AvailabilityState.EXECUTABLE,
        compatible=True,
        policy_eligible=True,
        authorized=True,
    )
    resolver = DeterministicCapabilityResolver(policy_version="1")

    first = resolver.resolve(capability, (lower_priority, selected))
    second = resolver.resolve(capability, (selected, lower_priority))

    assert first == second
    assert first.skill_id == selected.binding.skill_id
    assert first.authorized is False


def test_known_nvidia_capability_without_target_is_not_silently_replaced() -> None:
    capability = _capability()
    unavailable = ResolutionCandidate(
        binding=_binding(),
        skill_version="2.0.0",
        tools=(),
        agent_id=None,
        target_reference=None,
        compatibility_result="no compatible execution target",
        availability_state=AvailabilityState.KNOWN_BUT_UNAVAILABLE,
        compatible=False,
        policy_eligible=True,
        authorized=False,
        unavailable_reason="NO_COMPATIBLE_TARGET",
    )

    resolution = DeterministicCapabilityResolver(policy_version="1").resolve(
        capability, (unavailable,)
    )

    assert resolution.skill_id is None
    assert resolution.availability_state is AvailabilityState.KNOWN_BUT_UNAVAILABLE
    assert resolution.unavailable_reason == "NO_COMPATIBLE_TARGET"
    assert resolution.rejected_candidates[0].code == "KNOWN_BUT_UNAVAILABLE"


def test_resolution_snapshot_is_immutable_and_round_trips() -> None:
    mutable_tools = [ResolvedToolRef(ToolId("tool.nvidia.trtexec"), "10.0.0")]
    snapshot = ResolutionSnapshot(
        snapshot_id="resolution/20260911-0001",
        registry_version="1.0.0",
        resolver_policy_version="1",
        capability_id=CapabilityId("model.optimize.tensorrt"),
        capability_version="1.0.0",
        skill_id=SkillId("nvidia.tensorrt.inference_optimization"),
        skill_version="2.0.0",
        tools=mutable_tools,  # type: ignore[arg-type]
        agent_id=AgentId("agent.remote.gpu"),
        target_reference="target/gpu-a",
        compatibility_result="compatible",
        availability_state=AvailabilityState.EXECUTABLE,
        created_at=datetime(2026, 9, 11, 10, 15, tzinfo=UTC),
    )

    with pytest.raises(FrozenInstanceError):
        snapshot.registry_version = "2.0.0"  # type: ignore[misc]

    mutable_tools.clear()
    assert snapshot.tools == (ResolvedToolRef(ToolId("tool.nvidia.trtexec"), "10.0.0"),)
    assert ResolutionSnapshot.from_json(snapshot.to_json()) == snapshot
    assert ResolutionSnapshot.from_dict(snapshot.to_dict()) == snapshot
