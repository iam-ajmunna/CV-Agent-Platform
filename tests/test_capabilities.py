"""Tests for cv_agent.capabilities.registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from cv_agent.capabilities.registry import (
    Capability,
    CapabilityRegistry,
    RegistryItem,
)

# Use the committed registry file so tests reflect real data
_REGISTRY_PATH = Path(__file__).parent.parent / "spec" / "capability_registry.json"


@pytest.fixture(scope="module")
def registry() -> CapabilityRegistry:
    reg = CapabilityRegistry(_REGISTRY_PATH)
    reg.load()
    return reg


class TestRegistryLoading:
    def test_loads_without_error(self) -> None:
        reg = CapabilityRegistry(_REGISTRY_PATH)
        reg.load()  # must not raise

    def test_capability_count_is_nonzero(self, registry: CapabilityRegistry) -> None:
        assert registry.capability_count > 0

    def test_version_is_set(self, registry: CapabilityRegistry) -> None:
        assert registry.version != ""

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        reg = CapabilityRegistry(tmp_path / "nonexistent.json")
        with pytest.raises(FileNotFoundError):
            reg.load()

    def test_lazy_load_on_first_access(self, tmp_path: Path) -> None:
        """Registry should auto-load on first list() call."""
        reg = CapabilityRegistry(_REGISTRY_PATH)
        caps = reg.list()  # triggers load
        assert len(caps) > 0


class TestCapabilityList:
    def test_list_returns_capabilities(self, registry: CapabilityRegistry) -> None:
        caps = registry.list()
        assert all(isinstance(c, Capability) for c in caps)

    def test_list_is_sorted_by_id(self, registry: CapabilityRegistry) -> None:
        caps = registry.list()
        ids = [c.id for c in caps]
        assert ids == sorted(ids)

    def test_filter_by_category(self, registry: CapabilityRegistry) -> None:
        evaluation_caps = registry.list(category="evaluation")
        assert all(c.category == "evaluation" for c in evaluation_caps)

    def test_filter_by_status(self, registry: CapabilityRegistry) -> None:
        available = registry.list(status="available")
        assert all(c.status == "available" for c in available)

    def test_filter_by_both_category_and_status(
        self, registry: CapabilityRegistry
    ) -> None:
        caps = registry.list(category="deployment", status="available")
        assert all(c.category == "deployment" and c.status == "available" for c in caps)

    def test_known_capability_ids_present(self, registry: CapabilityRegistry) -> None:
        ids = {c.id for c in registry.list()}
        expected = {
            "cv.requirements.analysis",
            "cv.dataset.audit",
            "cv.model.selection",
            "cv.training.design",
            "cv.evaluation",
            "cv.benchmarking",
            "cv.model.inspection",
            "cv.deployment.optimization",
            "cv.research",
        }
        assert expected.issubset(ids)


class TestCapabilityDescribe:
    def test_describe_known_id(self, registry: CapabilityRegistry) -> None:
        cap = registry.describe("cv.evaluation")
        assert isinstance(cap, Capability)
        assert cap.id == "cv.evaluation"

    def test_describe_returns_full_metadata(
        self, registry: CapabilityRegistry
    ) -> None:
        cap = registry.describe("cv.deployment.optimization")
        assert cap.name
        assert cap.category == "deployment"
        assert cap.risk_level == "high"
        assert len(cap.relevant_skills) > 0
        assert len(cap.relevant_tools) > 0

    def test_describe_unknown_raises_key_error(
        self, registry: CapabilityRegistry
    ) -> None:
        with pytest.raises(KeyError, match="Unknown capability"):
            registry.describe("cv.does.not.exist")

    def test_inputs_and_outputs_are_typed(
        self, registry: CapabilityRegistry
    ) -> None:
        cap = registry.describe("cv.requirements.analysis")
        assert len(cap.required_inputs) > 0
        for inp in cap.required_inputs:
            assert inp.name
            assert inp.type
        for out in cap.outputs:
            assert out.name


class TestCapabilityCheck:
    def test_check_returns_dict(self, registry: CapabilityRegistry) -> None:
        result = registry.check("cv.evaluation")
        assert isinstance(result, dict)

    def test_check_keys(self, registry: CapabilityRegistry) -> None:
        result = registry.check("cv.evaluation")
        assert "capability_id" in result
        assert "declared" in result
        assert "executable" in result
        assert "status" in result
        assert "risk_level" in result
        assert "reason" in result
        assert "missing_prerequisites" in result

    def test_declared_capability_is_always_true(
        self, registry: CapabilityRegistry
    ) -> None:
        """Any capability check() can describe is declared, regardless of status."""
        result = registry.check("cv.evaluation")
        assert result["declared"] is True

    def test_planned_capability_is_not_executable(
        self, registry: CapabilityRegistry
    ) -> None:
        """DECLARED CAPABILITY != EXECUTABLE CAPABILITY: no skill/tool binding exists
        for cv.evaluation yet, so it must not be reported as executable merely
        because it is declared in the registry."""
        result = registry.check("cv.evaluation")
        assert result["status"] == "planned"
        assert result["executable"] is False

    def test_capability_id_matches(self, registry: CapabilityRegistry) -> None:
        result = registry.check("cv.research")
        assert result["capability_id"] == "cv.research"

    def test_missing_prereqs_is_list(self, registry: CapabilityRegistry) -> None:
        result = registry.check("cv.deployment.optimization")
        assert isinstance(result["missing_prerequisites"], list)

    def test_check_unknown_raises(self, registry: CapabilityRegistry) -> None:
        with pytest.raises(KeyError):
            registry.check("cv.nonexistent")


class TestCapabilitySelect:
    def test_select_returns_list(self, registry: CapabilityRegistry) -> None:
        caps = registry.select("planning")
        assert isinstance(caps, list)

    def test_select_all_are_executable(self, registry: CapabilityRegistry) -> None:
        caps = registry.select("model_training")
        assert all(c.is_available for c in caps)

    def test_select_excludes_planned_capabilities(
        self, registry: CapabilityRegistry
    ) -> None:
        """DECLARED CAPABILITY != EXECUTABLE CAPABILITY: select() must not return a
        capability just because it's declared for this task type — cv.training.design
        applies to model_training but has no executable binding, so it must not be
        selected even though registry.list() can describe it."""
        applicable = [
            c
            for c in registry.list(status="planned")
            if "model_training" in c.applicable_task_types
        ]
        assert len(applicable) > 0  # sanity: the fixture data isn't empty
        selected_ids = {c.id for c in registry.select("model_training")}
        assert selected_ids.isdisjoint({c.id for c in applicable})

    def test_select_matches_task_type(self, registry: CapabilityRegistry) -> None:
        caps = registry.select("deployment")
        assert all("deployment" in c.applicable_task_types for c in caps)

    def test_select_with_category_filter(self, registry: CapabilityRegistry) -> None:
        caps = registry.select("deployment", category="deployment")
        assert all(c.category == "deployment" for c in caps)

    def test_select_unknown_task_returns_empty(
        self, registry: CapabilityRegistry
    ) -> None:
        caps = registry.select("completely_unknown_task_xyz")
        assert caps == []


class TestRegistryItems:
    def test_list_items_returns_items(self, registry: CapabilityRegistry) -> None:
        items = registry.list_items()
        assert len(items) > 0
        assert all(isinstance(i, RegistryItem) for i in items)

    def test_list_skills(self, registry: CapabilityRegistry) -> None:
        skills = registry.list_items("skill")
        assert all(i.item_type == "skill" for i in skills)
        assert len(skills) > 0

    def test_list_tools(self, registry: CapabilityRegistry) -> None:
        tools = registry.list_items("tool")
        assert all(i.item_type == "tool" for i in tools)
        assert len(tools) > 0

    def test_list_agents(self, registry: CapabilityRegistry) -> None:
        agents = registry.list_items("agent")
        assert all(i.item_type == "agent" for i in agents)
        assert len(agents) > 0

    def test_list_knowledge_sources(self, registry: CapabilityRegistry) -> None:
        sources = registry.list_items("knowledge_source")
        assert all(i.item_type == "knowledge_source" for i in sources)
        assert len(sources) > 0

    def test_cross_type_ids_do_not_collide(self, registry: CapabilityRegistry) -> None:
        """The same ID may legitimately exist for different registry entity types."""
        skill = registry.describe_item("skill", "cuda-agent")
        agent = registry.describe_item("agent", "cuda-agent")
        triton_skill = registry.describe_item("skill", "triton-perf-analyzer")
        triton_tool = registry.describe_item("tool", "triton-perf-analyzer")

        assert skill.item_type == "skill"
        assert agent.item_type == "agent"
        assert triton_skill.item_type == "skill"
        assert triton_tool.item_type == "tool"

    def test_item_type_lists_are_not_overwritten(self, registry: CapabilityRegistry) -> None:
        skills = registry.list_items("skill")
        tools = registry.list_items("tool")
        agents = registry.list_items("agent")

        assert any(i.id == "cuda-agent" for i in skills)
        assert any(i.id == "cuda-agent" for i in agents)
        assert any(i.id == "triton-perf-analyzer" for i in skills)
        assert any(i.id == "triton-perf-analyzer" for i in tools)

    def test_known_skills_present(self, registry: CapabilityRegistry) -> None:
        skill_ids = {i.id for i in registry.list_items("skill")}
        assert "tensorrt" in skill_ids
        assert "deepstream" in skill_ids
        assert "jetson" in skill_ids

    def test_known_tools_present(self, registry: CapabilityRegistry) -> None:
        tool_ids = {i.id for i in registry.list_items("tool")}
        assert "trt-build" in tool_ids
        assert "deepstream-runtime" in tool_ids


class TestCrossTypeRegistryItems:
    """An ID may be reused only when its entity type differs."""

    def test_cross_type_ids_are_retained_and_described_by_type(
        self, registry: CapabilityRegistry
    ) -> None:
        assert len(registry.list_items("skill")) == 27
        assert len(registry.list_items("tool")) == 21
        assert len(registry.list_items("agent")) == 3

        cuda_skill = registry.describe_item("skill", "cuda-agent")
        cuda_agent = registry.describe_item("agent", "cuda-agent")
        triton_skill = registry.describe_item("skill", "triton-perf-analyzer")
        triton_tool = registry.describe_item("tool", "triton-perf-analyzer")

        assert (cuda_skill.id, cuda_skill.item_type) == ("cuda-agent", "skill")
        assert (cuda_agent.id, cuda_agent.item_type) == ("cuda-agent", "agent")
        assert (triton_skill.id, triton_skill.item_type) == (
            "triton-perf-analyzer",
            "skill",
        )
        assert (triton_tool.id, triton_tool.item_type) == (
            "triton-perf-analyzer",
            "tool",
        )

    def test_item_check_is_type_safe(self, registry: CapabilityRegistry) -> None:
        assert registry.check_item("skill", "cuda-agent") == {
            "item_id": "cuda-agent",
            "item_type": "skill",
            "declared": True,
            "executable": False,
            "reason": "no executable binding or discovery mechanism implemented",
        }

    def test_check_item_never_claims_executable(
        self, registry: CapabilityRegistry
    ) -> None:
        """DECLARED CAPABILITY != EXECUTABLE CAPABILITY, at the item level too: no
        skill/tool/agent/knowledge-source has a real invocation binding yet, so
        check_item() must report executable=False for every registered item."""
        for item in registry.list_items():
            result = registry.check_item(item.item_type, item.id)
            assert result["declared"] is True
            assert result["executable"] is False

    def test_capability_selection_returns_only_capabilities(
        self, registry: CapabilityRegistry
    ) -> None:
        selected = registry.select("benchmarking")

        # cv.benchmarking is declared for this task type but has no executable
        # binding (status="planned"), so it must not be selected.
        assert [cap.id for cap in selected] == []
        assert all(isinstance(cap, Capability) for cap in selected)
        assert "cv.benchmarking" in {
            c.id
            for c in registry.list()
            if "benchmarking" in c.applicable_task_types
        }
