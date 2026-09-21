"""
cv_agent.runtime.agent — Top-level CVAgent orchestrator.

CVAgent wires together:
    - Configuration (AgentConfig)
    - LLM provider (via provider registry)
    - Capability registry
    - LangGraph compiled graph

It is the primary interface for:
    - Running agent tasks (run())
    - Health checks (health_check())

Graph logic, LLM prompting, and capability invocation are intentionally
kept out of this class and belong in their respective modules.
"""

from __future__ import annotations

import uuid
from typing import Any

from cv_agent.capabilities.registry import CapabilityRegistry
from cv_agent.config.settings import AgentConfig, load_config
from cv_agent.graph.builder import build_graph
from cv_agent.graph.state import AgentState
from cv_agent.llm.base import LLMProvider
from cv_agent.llm.registry import get_provider
from cv_agent.skills import (
    CapabilityAvailabilitySummary,
    DefaultSkillResolver,
    SkillAvailabilitySummary,
    SkillRegistry,
    SkillResolver,
    load_default_skill_registry,
)
from cv_agent.tools import (
    ExecutionTargetDescriptor,
    ToolRegistry,
    detect_host_execution_target,
)


class CVAgent:
    """CV Engineering Agent — top-level orchestrator."""

    def __init__(self, config: AgentConfig | None = None) -> None:
        self._config: AgentConfig = config or load_config()
        self._llm: LLMProvider = get_provider(
            self._config.llm.provider,
            self._config.llm.model,
        )
        self._registry: CapabilityRegistry = CapabilityRegistry(
            self._config.registry_path
        )
        self._graph: Any = build_graph()
        self._skill_registry: SkillRegistry = load_default_skill_registry(
            capability_registry=self._registry
        )
        self._skill_resolver: SkillResolver = DefaultSkillResolver(
            self._skill_registry,
            capability_registry=self._registry,
        )

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def llm(self) -> LLMProvider:
        return self._llm

    @property
    def registry(self) -> CapabilityRegistry:
        return self._registry

    @property
    def skill_registry(self) -> SkillRegistry:
        return self._skill_registry

    @property
    def skill_resolver(self) -> SkillResolver:
        return self._skill_resolver

    def enumerate_capabilities(
        self,
        targets: tuple[ExecutionTargetDescriptor, ...] | None = None,
        *,
        tool_registry: ToolRegistry | None = None,
    ) -> tuple[CapabilityAvailabilitySummary, ...]:
        """Enumerate all registered capabilities and their operational availability across targets.

        Guarantees (Issue #68, ADR-0007):
        - Inspection only: 0 tool executions, 0 registry mutations, 0 subprocesses, 0 MCP calls.
        - Defaults to canonical host execution target from ADR-0005 boundary (D-022).
        """
        effective_targets = (
            targets if targets is not None else (detect_host_execution_target(),)
        )
        return self._skill_resolver.enumerate_capabilities(
            effective_targets, tool_registry=tool_registry
        )

    def enumerate_skills(
        self,
        targets: tuple[ExecutionTargetDescriptor, ...] | None = None,
        *,
        tool_registry: ToolRegistry | None = None,
    ) -> tuple[SkillAvailabilitySummary, ...]:
        """Enumerate all registered skills and their operational availability across targets.

        Guarantees (Issue #68, ADR-0007):
        - Inspection only: 0 tool executions, 0 registry mutations, 0 subprocesses, 0 MCP calls.
        - Defaults to canonical host execution target from ADR-0005 boundary (D-022).
        """
        effective_targets = (
            targets if targets is not None else (detect_host_execution_target(),)
        )
        return self._skill_resolver.enumerate_skills(
            effective_targets, tool_registry=tool_registry
        )

    def run(
        self,
        task: str | None = None,
        *,
        session_id: str | None = None,
        task_type: str | None = None,
    ) -> AgentState:
        """Execute a single agent session through the graph."""
        sid = session_id or str(uuid.uuid4())
        initial_state: AgentState = {
            "session_id": sid,
            "status": "initializing",
            "task": task,
            "task_type": task_type,
            "provider": self._llm.provider_name,
            "model": self._llm.model_name,
            "steps": [],
            "selected_capabilities": [],
            "error": None,
            "pending_human_input": None,
            "human_feedback": None,
        }
        graph_config = {"configurable": {"thread_id": sid}}
        result: AgentState = self._graph.invoke(initial_state, config=graph_config)
        return result

    def health_check(self) -> dict[str, Any]:
        """Return a structured health summary."""
        try:
            from importlib.metadata import version

            lg_version: str = version("langgraph")
            lg_ok = True
        except Exception as exc:  # noqa: BLE001
            lg_version = str(exc)
            lg_ok = False

        try:
            if not getattr(self._registry, "is_loaded", False):
                self._registry.load()
            cap_count = self._registry.capability_count
            registry_ok = True
        except Exception:  # noqa: BLE001
            cap_count = 0
            registry_ok = False

        overall = "ok" if (lg_ok and registry_ok) else "degraded"

        return {
            "status": overall,
            "provider": self._llm.provider_name,
            "model": self._llm.model_name,
            "capability_count": cap_count,
            "langgraph_available": lg_ok,
            "langgraph_version": lg_version,
            "registry_ok": registry_ok,
        }
