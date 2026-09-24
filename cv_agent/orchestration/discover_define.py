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
    operational_inquiry: dict[str, Any] | None
    characterization: dict[str, Any] | None
    decomposition: dict[str, Any] | None
    needs_elicitation: bool
    project_spec_committed: bool
    error: str | None


def create_discover_define_graph(
    store: SQLiteOrchestrationStore,
    lease: ExecutionLease,
    memory: GitProjectMemory,
    llm_gateway: LLMGateway,
    *,
    checkpointer: Any = None,
    tool_barrier: Phase4ExecutionBarrier | None = None,
) -> Any:
    """Build and compile the DISCOVER / DEFINE LangGraph graph anchored to SQLite store.

    Nodes execute transitions validated against ADR-0003 §3.9 state machine and
    persist transactional events in the SQLite store under the active lease.
    """
    if checkpointer is None:
        checkpointer = SQLiteLangGraphCheckpointer(str(store.db_path))

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
        human_answers = state.get("human_answers")

        # If human answers are already present, elicitation was completed
        if human_answers:
            return {"needs_elicitation": False}

        elicitation_node = ElicitationNode(llm_gateway)
        context = ReasoningContext.create(
            current_hop=0,
            max_hops=5,
            trace_id=f"discover-{lease.session_id}",
            parameters={"user_prompt": user_prompt},
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
            }

        return {"needs_elicitation": False}

    def route_after_discover(state: DiscoverDefineGraphState) -> str:
        if state.get("needs_elicitation", False) and not state.get("human_answers"):
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
            "define_phase_started",
            {"phase": "DEFINE"},
            lease,
        )
        return {"lifecycle_status": new_status.value}

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
        human_answers = state.get("human_answers") or {}

        # 1. Synthesize complete ProblemCharacterization via ProblemFramingNode
        framing_node = ProblemFramingNode(llm_gateway)
        context = ReasoningContext.create(
            current_hop=1,
            max_hops=5,
            trace_id=f"define-framing-{lease.session_id}",
            parameters={
                "user_prompt": user_prompt,
                "operational_answers": human_answers,
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
            max_hops=5,
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

        # 3. Assemble HardwareConstraints safely
        max_vram_val = characterization.constraints.get("max_vram_mb")
        max_vram = (
            int(max_vram_val)
            if isinstance(max_vram_val, (int, float, str))
            else (
                16384 if "jetson" in characterization.target_hardware.lower() else 8192
            )
        )
        max_ram_val = characterization.constraints.get("max_ram_mb")
        max_ram = (
            int(max_ram_val)
            if isinstance(max_ram_val, (int, float, str))
            else (
                32768 if "jetson" in characterization.target_hardware.lower() else 16384
            )
        )
        power_val = characterization.constraints.get("power_budget_watts")
        power_w = (
            float(power_val)
            if isinstance(power_val, (int, float, str))
            else (
                60.0 if "jetson" in characterization.target_hardware.lower() else None
            )
        )

        hw_constraints = HardwareConstraints(
            target_device=characterization.target_hardware,
            max_vram_mb=max_vram,
            max_ram_mb=max_ram,
            power_budget_watts=power_w,
            latency_ceiling_ms=characterization.latency_budget_ms,
            supported_precisions=("fp32", "fp16", "int8"),
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
    builder.add_edge("await_human_input", "define")
    builder.add_edge("define", END)

    return builder.compile(
        checkpointer=checkpointer, interrupt_before=["await_human_input"]
    )
