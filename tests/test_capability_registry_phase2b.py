"""Phase 2B contract tests for authoritative ADR-0001 CapabilityRegistry subsystem."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from cv_agent.capabilities.registry import CapabilityRegistry
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
    ResolutionCandidate,
    ResolvedToolRef,
)
from cv_agent.domain.snapshots import ResolutionSnapshot

_REGISTRY_PATH = Path(__file__).parent.parent / "spec" / "capability_registry.json"


@pytest.fixture(scope="module")
def registry() -> CapabilityRegistry:
    reg = CapabilityRegistry(_REGISTRY_PATH)
    reg.load()
    return reg


class TestAuthoritativeDefinitions:
    def test_loaded_capabilities_are_authoritative_definitions(
        self, registry: CapabilityRegistry
    ) -> None:
        cap = registry.get_capability(CapabilityId("cv.requirements.analysis"))
        assert cap is not None
        assert isinstance(cap, CapabilityDefinition)
        assert cap.capability_id == CapabilityId("cv.requirements.analysis")
        assert cap.version != ""
        assert cap.stage in (Stage.DEFINE, Stage.DISCOVER)
        assert cap.description != ""
        assert cap.risk_level in ("low", "medium", "high")

    def test_string_coercion_for_capability_lookup(
        self, registry: CapabilityRegistry
    ) -> None:
        cap = registry.get_capability("cv.requirements.analysis")
        assert cap is not None
        assert cap.capability_id == CapabilityId("cv.requirements.analysis")

    def test_unknown_capability_returns_none(
        self, registry: CapabilityRegistry
    ) -> None:
        assert registry.get_capability(CapabilityId("cv.does.not.exist")) is None
        assert registry.get_capability("cv.does.not.exist") is None

    def test_list_capabilities_is_deterministically_sorted(
        self, registry: CapabilityRegistry
    ) -> None:
        caps = registry.list_capabilities()
        assert len(caps) > 0
        ids = [c.capability_id for c in caps]
        assert ids == sorted(ids)

    def test_list_capabilities_filtered_by_stage(
        self, registry: CapabilityRegistry
    ) -> None:
        eval_caps = registry.list_capabilities(stage=Stage.EVALUATE)
        assert len(eval_caps) > 0
        assert all(c.stage is Stage.EVALUATE for c in eval_caps)

    def test_authoritative_capability_definition_is_frozen(
        self, registry: CapabilityRegistry
    ) -> None:
        cap = registry.get_capability("cv.requirements.analysis")
        assert cap is not None
        with pytest.raises(FrozenInstanceError):
            cap.description = "modified"  # type: ignore[misc]


class TestTypedBindings:
    def test_skill_bindings_loaded_with_typed_identities(
        self, registry: CapabilityRegistry
    ) -> None:
        bindings = registry.get_skill_bindings("cv.requirements.analysis")
        assert len(bindings) > 0
        assert all(isinstance(b, SkillBinding) for b in bindings)
        assert all(
            b.capability_id == CapabilityId("cv.requirements.analysis")
            for b in bindings
        )
        skill_ids = {b.skill_id for b in bindings}
        assert SkillId("problem-decomposition") in skill_ids

    def test_skill_bindings_are_sorted_by_priority(
        self, registry: CapabilityRegistry
    ) -> None:
        bindings = registry.get_skill_bindings("cv.requirements.analysis")
        priorities = [b.priority for b in bindings]
        assert priorities == sorted(priorities, reverse=True)


class TestRegistryImmutability:
    def test_published_internal_mappings_are_immutable(
        self, registry: CapabilityRegistry
    ) -> None:
        assert isinstance(registry._capabilities, MappingProxyType)
        with pytest.raises(TypeError):
            registry._capabilities[CapabilityId("cv.new")] = None  # type: ignore[index]

    def test_published_binding_mappings_are_immutable(
        self, registry: CapabilityRegistry
    ) -> None:
        assert isinstance(registry._skill_bindings, MappingProxyType)
        with pytest.raises(TypeError):
            registry._skill_bindings[CapabilityId("cv.new")] = ()  # type: ignore[index]

    def test_list_capabilities_returns_immutable_tuple(
        self, registry: CapabilityRegistry
    ) -> None:
        caps = registry.list_capabilities()
        assert isinstance(caps, tuple)


class TestDeterministicResolutionAndSnapshots:
    def test_resolve_produces_immutable_resolution_snapshot(
        self, registry: CapabilityRegistry
    ) -> None:
        cap = registry.get_capability("cv.evaluation")
        assert cap is not None

        candidate = ResolutionCandidate(
            binding=SkillBinding(
                capability_id=cap.capability_id,
                skill_id=SkillId("metric-computation"),
                compatibility=CompatibilityConstraint(),
                priority=100,
                provenance=Provenance(kind=ProvenanceKind.INTERNAL),
            ),
            skill_version="1.0.0",
            tools=(ResolvedToolRef(ToolId("pytorch-profiler"), "2.0.0"),),
            agent_id=AgentId("agent.local.worker"),
            target_reference="target/local-cpu",
            compatibility_result="compatible",
            availability_state=AvailabilityState.EXECUTABLE,
            compatible=True,
            policy_eligible=True,
            authorized=False,
        )

        snapshot = registry.resolve(
            cap,
            (candidate,),
            resolver_policy_version="1",
            snapshot_id="res_test_001",
        )

        assert isinstance(snapshot, ResolutionSnapshot)
        assert snapshot.snapshot_id == "res_test_001"
        assert snapshot.capability_id == cap.capability_id
        assert snapshot.skill_id == SkillId("metric-computation")
        assert snapshot.skill_version == "1.0.0"
        assert snapshot.availability_state is AvailabilityState.EXECUTABLE
        assert snapshot.tools == (ResolvedToolRef(ToolId("pytorch-profiler"), "2.0.0"),)

        # Immutability
        with pytest.raises(FrozenInstanceError):
            snapshot.skill_version = "2.0.0"  # type: ignore[misc]

        # Round-trip serialization
        assert ResolutionSnapshot.from_dict(snapshot.to_dict()) == snapshot
        assert ResolutionSnapshot.from_json(snapshot.to_json()) == snapshot

    def test_resolve_with_unknown_capability_raises_validation_error(
        self, registry: CapabilityRegistry
    ) -> None:
        with pytest.raises(DomainValidationError) as err:
            registry.resolve("cv.completely.unknown", ())
        assert err.value.code == ValidationCode.INVALID_VALUE

    def test_resolve_breaks_ties_deterministically_via_registry(
        self, registry: CapabilityRegistry
    ) -> None:
        cap = registry.get_capability("cv.evaluation")
        assert cap is not None

        binding_a = SkillBinding(
            capability_id=cap.capability_id,
            skill_id=SkillId("metric-computation"),
            compatibility=CompatibilityConstraint(requirements={"variant": "a"}),
            priority=10,
            provenance=Provenance(kind=ProvenanceKind.INTERNAL),
        )
        binding_b = SkillBinding(
            capability_id=cap.capability_id,
            skill_id=SkillId("metric-computation"),
            compatibility=CompatibilityConstraint(requirements={"variant": "b"}),
            priority=10,
            provenance=Provenance(kind=ProvenanceKind.INTERNAL),
        )

        candidate_a = ResolutionCandidate(
            binding=binding_a,
            skill_version="1.0.0",
            tools=(ResolvedToolRef(ToolId("pytorch-profiler"), "1.0.0"),),
            agent_id=AgentId("agent.worker-a"),
            target_reference="target/a",
            compatibility_result="compatible",
            availability_state=AvailabilityState.EXECUTABLE,
            compatible=True,
            policy_eligible=True,
            authorized=False,
        )
        candidate_b = ResolutionCandidate(
            binding=binding_b,
            skill_version="1.0.0",
            tools=(ResolvedToolRef(ToolId("pytorch-profiler"), "2.0.0"),),
            agent_id=AgentId("agent.worker-b"),
            target_reference="target/b",
            compatibility_result="compatible",
            availability_state=AvailabilityState.EXECUTABLE,
            compatible=True,
            policy_eligible=True,
            authorized=False,
        )

        fixed_time = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        first = registry.resolve(
            cap,
            (candidate_a, candidate_b),
            snapshot_id="fixed_id",
            created_at=fixed_time,
        )
        second = registry.resolve(
            cap,
            (candidate_b, candidate_a),
            snapshot_id="fixed_id",
            created_at=fixed_time,
        )

        assert first == second


class TestKnownButUnavailableNvidiaSemantics:
    def test_nvidia_capability_without_target_resolves_known_but_unavailable(
        self, registry: CapabilityRegistry
    ) -> None:
        """When an NVIDIA capability has no compatible target, it must resolve to
        KNOWN_BUT_UNAVAILABLE without silently substituting a mock or CPU fallback."""
        cap = registry.get_capability("cv.deployment.optimization")
        assert cap is not None

        unavailable_candidate = ResolutionCandidate(
            binding=SkillBinding(
                capability_id=cap.capability_id,
                skill_id=SkillId("tensorrt"),
                compatibility=CompatibilityConstraint(
                    requirements={"accelerator": "nvidia_gpu"}
                ),
                priority=100,
                provenance=Provenance(
                    kind=ProvenanceKind.OFFICIAL_VENDOR, publisher="NVIDIA"
                ),
            ),
            skill_version="10.0.0",
            tools=(ResolvedToolRef(ToolId("trt-build"), "10.0.0"),),
            agent_id=None,
            target_reference=None,
            compatibility_result="no compatible nvidia target detected",
            availability_state=AvailabilityState.KNOWN_BUT_UNAVAILABLE,
            compatible=False,
            policy_eligible=True,
            authorized=False,
            unavailable_reason="NO_COMPATIBLE_NVIDIA_TARGET",
        )

        snapshot = registry.resolve(
            cap,
            (unavailable_candidate,),
            snapshot_id="res_nvidia_unavailable",
        )

        assert snapshot.availability_state is AvailabilityState.KNOWN_BUT_UNAVAILABLE
        assert snapshot.skill_id is None
        assert snapshot.skill_version is None
        assert snapshot.tools == ()
        assert snapshot.target_reference is None


class TestMemoryConstruction:
    def test_from_definitions_constructs_valid_registry(self) -> None:
        cap = CapabilityDefinition(
            capability_id=CapabilityId("test.cap"),
            version="1.0.0",
            stage=Stage.TRAIN,
            description="A test capability",
            risk_level="low",
            requires_approval=False,
        )
        binding = SkillBinding(
            capability_id=cap.capability_id,
            skill_id=SkillId("test.skill"),
            compatibility=CompatibilityConstraint(),
            priority=50,
            provenance=Provenance(kind=ProvenanceKind.INTERNAL),
        )
        tool_binding = ToolBinding(
            skill_id=binding.skill_id,
            tool_id=ToolId("test.tool"),
            compatibility=CompatibilityConstraint(),
        )
        agent_binding = AgentBinding(
            tool_id=tool_binding.tool_id,
            agent_id=AgentId("test.agent"),
        )

        reg = CapabilityRegistry.from_definitions(
            capabilities=(cap,),
            skill_bindings=(binding,),
            tool_bindings=(tool_binding,),
            agent_bindings=(agent_binding,),
        )

        assert reg.capability_count == 1
        assert reg.get_capability("test.cap") == cap
        assert reg.get_skill_bindings("test.cap") == (binding,)
        assert reg.get_tool_bindings("test.skill") == (tool_binding,)
        assert reg.get_agent_bindings("test.tool") == (agent_binding,)
