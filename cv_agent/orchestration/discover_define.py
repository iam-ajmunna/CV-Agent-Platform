"""cv_agent.orchestration.discover_define — DISCOVER / DEFINE workflow graph.

Implements Phase 4 DISCOVER / DEFINE Workflow per Issue #70:
- ADR-0003 owns all lifecycle transitions, thread checkpointing, and resumption.
- ADR-0008 owns cognitive elicitation and domain decomposition without tool execution.
- Operational characterization before model selection invariant: zero concrete models
  prior to complete characterization across all 8 canonical dimensions.
- Atomic commit to GitProjectMemory at DEFINE exit.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from cv_agent.domain.errors import DomainValidationError, ValidationCode
from cv_agent.llm.gateway import LLMGateway
from cv_agent.memory.interfaces import (
    ArchitecturalDecision,
    HardwareConstraints,
    ProjectSpecification,
)
from cv_agent.memory.service import GitProjectMemory
from cv_agent.orchestration.contracts import ExecutionLease, LifecycleStatus
from cv_agent.orchestration.graph import SQLiteLangGraphCheckpointer
from cv_agent.orchestration.store import SQLiteOrchestrationStore
from cv_agent.reasoning.contracts import (
    CapabilityIntent,
    CVTaskDecompositionDAG,
    OperationalInquiry,
    ProblemCharacterization,
    ReasoningContext,
    assert_zero_prohibited_models,
)
from cv_agent.reasoning.errors import (
    IncompleteCharacterizationError,
)
from cv_agent.reasoning.nodes import (
    ElicitationNode,
    ProblemFramingNode,
    TaskDecompositionNode,
)
from cv_agent.tools.barrier import Phase4ExecutionBarrier


class DiscoverDefineGraphState(TypedDict, total=False):
    """Runtime state for the DISCOVER / DEFINE workflow graph."""

    session_id: str
    thread_id: str
    lifecycle_status: str
    user_prompt: str
    human_answers: dict[str, str] | None
    accumulated_answers: dict[str, str]
    operational_inquiry: dict[str, Any] | None
    characterization: dict[str, Any] | None
    decomposition: dict[str, Any] | None
    capability_intents: list[dict[str, Any]] | None
    needs_elicitation: bool
    project_spec_committed: bool
    error: str | None


def create_discover_define_graph(
    store: SQLiteOrchestrationStore,
    lease: ExecutionLease,
    memory: GitProjectMemory,
    llm_gateway: LLMGateway,
    *,
    capability_registry: Any = None,
    checkpointer: Any = None,
    tool_barrier: Phase4ExecutionBarrier | None = None,
) -> Any:
    """Build and compile the DISCOVER / DEFINE LangGraph graph anchored to SQLite store.

    Nodes execute transitions validated against ADR-0003 §3.9 state machine and
    persist transactional events in the SQLite store under the active lease.
    """
    if checkpointer is None:
        checkpointer = SQLiteLangGraphCheckpointer(str(store.db_path))

    if capability_registry is None:
        from cv_agent.capabilities.registry import load_default_capability_registry

        capability_registry = load_default_capability_registry()

    builder: StateGraph = StateGraph(DiscoverDefineGraphState)

    def node_initialize(state: DiscoverDefineGraphState) -> dict[str, Any]:
        curr = store.get_lifecycle_status(lease.session_id)
        if curr is None or curr == LifecycleStatus.INITIALIZING:
            new_status, _ = store.transition_lifecycle(
                lease.session_id,
                LifecycleStatus.READY,
                "discover_define_initialized",
                {"status": LifecycleStatus.READY.value},
                lease,
            )
            return {"lifecycle_status": new_status.value}
        return {"lifecycle_status": curr.value}

    def node_discover(state: DiscoverDefineGraphState) -> dict[str, Any]:
        user_prompt = state.get("user_prompt", "")
        accumulated = dict(state.get("accumulated_answers") or {})
        new_answers = state.get("human_answers")
        if new_answers:
            accumulated.update(new_answers)

        elicitation_node = ElicitationNode(llm_gateway)
        context = ReasoningContext.create(
            current_hop=0,
            max_hops=10,
            trace_id=f"discover-{lease.session_id}",
            parameters={
                "user_prompt": user_prompt,
                "operational_answers": accumulated,
            },
        )
        reasoning_output = elicitation_node.execute(context)
        inquiry: OperationalInquiry = reasoning_output.payload  # type: ignore[assignment]

        # Anti-YOLO schema invariant check (PM Correction 1)
        assert_zero_prohibited_models(
            inquiry.context_summary, "OperationalInquiry.context_summary"
        )
        for q in inquiry.targeted_questions:
            assert_zero_prohibited_models(q, "OperationalInquiry.targeted_questions")

        # If characterization is incomplete, ask operational questions & PAUSE
        if inquiry.targeted_questions or inquiry.unresolved_dimensions:
            new_status, _ = store.transition_lifecycle(
                lease.session_id,
                LifecycleStatus.PAUSED,
                "awaiting_human_operational_input",
                {
                    "inquiry_id": inquiry.inquiry_id,
                    "unresolved_dimensions": list(inquiry.unresolved_dimensions),
                    "question_count": len(inquiry.targeted_questions),
                },
                lease,
            )
            inquiry_dict = {
                "inquiry_id": inquiry.inquiry_id,
                "unresolved_dimensions": list(inquiry.unresolved_dimensions),
                "targeted_questions": list(inquiry.targeted_questions),
                "context_summary": inquiry.context_summary,
                "assumptions_and_risks": list(inquiry.assumptions_and_risks),
            }
            return {
                "lifecycle_status": new_status.value,
                "operational_inquiry": inquiry_dict,
                "needs_elicitation": True,
                "accumulated_answers": accumulated,
                "human_answers": None,
            }

        return {
            "needs_elicitation": False,
            "accumulated_answers": accumulated,
            "human_answers": None,
            "operational_inquiry": None,
        }

    def route_after_discover(state: DiscoverDefineGraphState) -> str:
        if state.get("needs_elicitation", False):
            return "await_human_input"
        return "define"

    def node_await_human_input(state: DiscoverDefineGraphState) -> dict[str, Any]:
        """Resumption point after user responds to operational questions."""
        curr = store.get_lifecycle_status(lease.session_id)
        if curr == LifecycleStatus.PAUSED:
            store.transition_lifecycle(
                lease.session_id,
                LifecycleStatus.READY,
                "human_input_received",
                {"answers_provided": list((state.get("human_answers") or {}).keys())},
                lease,
            )
        new_status, _ = store.transition_lifecycle(
            lease.session_id,
            LifecycleStatus.PLANNING,
            "evaluating_accumulated_operational_input",
            {"phase": "DISCOVER"},
            lease,
        )
        accumulated = dict(state.get("accumulated_answers") or {})
        new_answers = state.get("human_answers")
        if new_answers:
            accumulated.update(new_answers)
        return {
            "lifecycle_status": new_status.value,
            "accumulated_answers": accumulated,
            "human_answers": None,
        }

    def node_define(state: DiscoverDefineGraphState) -> dict[str, Any]:
        curr = store.get_lifecycle_status(lease.session_id)
        if curr != LifecycleStatus.PLANNING:
            store.transition_lifecycle(
                lease.session_id,
                LifecycleStatus.PLANNING,
                "define_phase_started",
                {"phase": "DEFINE"},
                lease,
            )

        user_prompt = state.get("user_prompt", "")
        accumulated = dict(state.get("accumulated_answers") or {})
        new_answers = state.get("human_answers")
        if new_answers:
            accumulated.update(new_answers)

        # 1. Synthesize complete ProblemCharacterization via ProblemFramingNode
        framing_node = ProblemFramingNode(llm_gateway)
        context = ReasoningContext.create(
            current_hop=1,
            max_hops=10,
            trace_id=f"define-framing-{lease.session_id}",
            parameters={
                "user_prompt": user_prompt,
                "operational_answers": accumulated,
            },
        )
        framing_output = framing_node.execute(context)
        characterization: ProblemCharacterization = framing_output.payload  # type: ignore[assignment]

        if not characterization.is_well_formed:
            raise IncompleteCharacterizationError(
                f"ProblemCharacterization at DEFINE is incomplete. All 8 canonical dimensions required: {characterization}"
            )

        # 2. Decompose into CV domain task DAG via TaskDecompositionNode
        decomp_node = TaskDecompositionNode(llm_gateway)
        decomp_context = ReasoningContext.create(
            current_hop=2,
            max_hops=10,
            trace_id=f"define-decomp-{lease.session_id}",
            parameters={
                "task_type": characterization.task_type,
                "operational_definition": characterization.operational_definition,
                "physical_environment": characterization.physical_environment,
                "camera_infrastructure": characterization.camera_infrastructure,
                "target_hardware": characterization.target_hardware,
                "error_tolerance": characterization.error_tolerance,
                "data_availability": characterization.data_availability,
                "intervention_modality": characterization.intervention_modality,
            },
        )
        decomp_output = decomp_node.execute(decomp_context)
        dag: CVTaskDecompositionDAG = decomp_output.payload  # type: ignore[assignment]
        intents: tuple[CapabilityIntent, ...] = decomp_output.capability_intents

        # B27: Validate every capability_id against authoritative CapabilityRegistry
        for ci in intents:
            cap_def = capability_registry.get_capability(ci.capability_id)
            if cap_def is None:
                raise DomainValidationError(
                    ValidationCode.INVALID_VALUE,
                    "capability_intent.capability_id",
                    f"Task decomposition references unregistered/synthetic capability: {ci.capability_id!r}",
                    ci.capability_id,
                )

        # 3. Assemble HardwareConstraints without fabricated defaults (B28)
        # Only take values explicitly established by characterization / user constraints.
        # Do NOT infer specifications from product name strings (e.g. "jetson").
        raw_vram = characterization.constraints.get("max_vram_mb")
        max_vram = (
            int(str(raw_vram))
            if raw_vram is not None and str(raw_vram).isdigit()
            else None
        )

        raw_ram = characterization.constraints.get("max_ram_mb")
        max_ram = (
            int(str(raw_ram))
            if raw_ram is not None and str(raw_ram).isdigit()
            else None
        )

        raw_power = characterization.constraints.get("power_budget_watts")
        try:
            power_w = float(str(raw_power)) if raw_power is not None else None
        except (ValueError, TypeError):
            power_w = None

        raw_precisions = characterization.constraints.get("supported_precisions")
        if isinstance(raw_precisions, (list, tuple)):
            supported_precisions = tuple(
                str(p).strip() for p in raw_precisions if str(p).strip()
            )
        else:
            supported_precisions = ()

        hw_constraints = HardwareConstraints(
            target_device=characterization.target_hardware,
            max_vram_mb=max_vram,
            max_ram_mb=max_ram,
            power_budget_watts=power_w,
            latency_ceiling_ms=characterization.latency_budget_ms,
            supported_precisions=supported_precisions,
        )

        # 4. Formulate architectural choices
        arch_decisions = (
            ArchitecturalDecision(
                decision_id="AD-001",
                timestamp="2026-09-24T00:00:00Z",
                title="Modular domain DAG decomposition for verifiable pipeline stages",
                rationale=(
                    f"Decomposed {characterization.task_type} into independent verifiable "
                    f"stages ({', '.join(t.task_type for t in dag.tasks)}) to isolate detection from tracking "
                    f"and spatial geometry reasoning under {characterization.physical_environment} constraints."
                ),
                linked_experiment_ids=(),
            ),
        )

        # 5. Atomically commit ProjectSpecification to persistent memory (ADR-0004)
        spec = ProjectSpecification(
            characterization=characterization,
            decomposition=dag,
            hardware_constraints=hw_constraints,
            capability_intents=intents,
            architectural_choices=arch_decisions,
            dataset_lineage_notes=f"Data availability noted: {characterization.data_availability}",
        )
        memory.commit_project_specification(spec)

        # 6. Transition lifecycle PLANNING -> EXECUTING -> EVALUATING -> COMPLETED (ADR-0003 §3.9)
        store.transition_lifecycle(
            lease.session_id,
            LifecycleStatus.EXECUTING,
            "memory_persisted",
            {"project_spec_committed": True},
            lease,
        )
        store.transition_lifecycle(
            lease.session_id,
            LifecycleStatus.EVALUATING,
            "specification_evaluated",
            {"is_well_formed": characterization.is_well_formed},
            lease,
        )
        new_status, _ = store.transition_lifecycle(
            lease.session_id,
            LifecycleStatus.COMPLETED,
            "define_phase_completed",
            {"project_spec_committed": True},
            lease,
        )

        return {
            "lifecycle_status": new_status.value,
            "characterization": {
                "task_type": characterization.task_type,
                "operational_definition": characterization.operational_definition,
                "physical_environment": characterization.physical_environment,
                "camera_infrastructure": characterization.camera_infrastructure,
                "target_hardware": characterization.target_hardware,
                "latency_budget_ms": characterization.latency_budget_ms,
                "throughput_fps": characterization.throughput_fps,
                "error_tolerance": characterization.error_tolerance,
                "data_availability": characterization.data_availability,
                "intervention_modality": characterization.intervention_modality,
                "is_well_formed": characterization.is_well_formed,
            },
            "decomposition": {
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "task_type": t.task_type,
                        "description": t.description,
                        "dependencies": list(t.dependencies),
                        "inputs": list(t.inputs),
                        "outputs": list(t.outputs),
                    }
                    for t in dag.tasks
                ]
            },
            "capability_intents": [
                {
                    "capability_id": str(ci.capability_id),
                    "intent_description": ci.intent_description,
                    "parameter_intent": dict(ci.parameter_intent),
                    "target_preference": ci.target_preference,
                }
                for ci in intents
            ],
            "project_spec_committed": True,
        }

    builder.add_node("initialize", node_initialize)
    builder.add_node("discover", node_discover)
    builder.add_node("await_human_input", node_await_human_input)
    builder.add_node("define", node_define)

    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "discover")
    builder.add_conditional_edges(
        "discover",
        route_after_discover,
        {"await_human_input": "await_human_input", "define": "define"},
    )
    builder.add_edge("await_human_input", "discover")
    builder.add_edge("define", END)

    return builder.compile(
        checkpointer=checkpointer, interrupt_before=["await_human_input"]
    )
