"""cv_agent.orchestration.graph — LangGraph workflow runtime over authoritative ADR-0003 contracts.

Implements PM Gate 1 and Gate D:
- SQLite/ADR-0003 is the sole authority; LangGraph is the workflow execution mechanism.
- State transitions strictly invoke validate_lifecycle_transition().
- Enforces Invariant 17: evaluate node terminates to COMPLETED/FAILED/PAUSED and never loops back to execute.
- Human-in-the-loop approval interrupt and resumption under active lease fencing.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from cv_agent.orchestration.approvals import ApprovalManager, ApprovalSecurityError
from cv_agent.orchestration.contracts import (
    ExecutionLease,
    LifecycleStatus,
    OperationIntent,
)
from cv_agent.orchestration.state_machine import validate_lifecycle_transition
from cv_agent.orchestration.store import SQLiteOrchestrationStore


class OrchestrationGraphState(TypedDict, total=False):
    """Runtime state passed across LangGraph nodes."""

    session_id: str
    thread_id: str
    lifecycle_status: str
    task: str | None
    plan: dict[str, Any] | None
    requires_approval: bool
    approval_request_id: str | None
    approval_policy_version: str | None
    approval_resource_scope: str | None
    operation_intent: dict[str, Any] | None
    execution_result: dict[str, Any] | None
    evaluation_result: dict[str, Any] | None
    error: str | None


def create_orchestration_graph(
    store: SQLiteOrchestrationStore,
    lease: ExecutionLease,
    approval_manager: ApprovalManager | None = None,
    checkpointer: Any = None,
) -> Any:
    """Build and compile the LangGraph workflow graph anchored to the authoritative store.

    Nodes execute transitions validated against ADR-0003 §3.9 state machine and
    persist transactional events in the SQLite store under the active lease.
    """
    appr_mgr = approval_manager or ApprovalManager(store)
    if checkpointer is None:
        checkpointer = MemorySaver()

    builder: StateGraph = StateGraph(OrchestrationGraphState)

    def node_initialize(state: OrchestrationGraphState) -> dict[str, Any]:
        curr = LifecycleStatus(
            state.get("lifecycle_status", LifecycleStatus.INITIALIZING)
        )
        validate_lifecycle_transition(curr, LifecycleStatus.READY)
        store.record_event(
            lease.session_id,
            "session_ready",
            {"status": LifecycleStatus.READY.value},
            lease,
        )
        return {"lifecycle_status": LifecycleStatus.READY.value}

    def node_plan(state: OrchestrationGraphState) -> dict[str, Any]:
        curr = LifecycleStatus(state.get("lifecycle_status", LifecycleStatus.READY))
        validate_lifecycle_transition(curr, LifecycleStatus.PLANNING)
        store.record_event(
            lease.session_id,
            "planning_started",
            {"task": state.get("task")},
            lease,
        )

        requires_approval = state.get("requires_approval", False)
        if requires_approval:
            target = LifecycleStatus.AWAITING_APPROVAL
        else:
            target = LifecycleStatus.EXECUTING

        validate_lifecycle_transition(LifecycleStatus.PLANNING, target)
        return {"lifecycle_status": target.value}

    def node_await_approval(state: OrchestrationGraphState) -> dict[str, Any]:
        request_id = state.get("approval_request_id")
        if not request_id:
            return {
                "lifecycle_status": LifecycleStatus.FAILED.value,
                "error": "Approval required but no approval_request_id provided.",
            }

        policy_ver = state.get("approval_policy_version") or "v1.0"
        scope = state.get("approval_resource_scope") or "default"
        raw_intent = state.get("operation_intent")

        if raw_intent is None:
            return {
                "lifecycle_status": LifecycleStatus.FAILED.value,
                "error": "No operation intent provided for approval verification.",
            }

        op_intent = OperationIntent.model_validate(raw_intent)
        try:
            appr_mgr.verify_authorization(
                request_id=request_id,
                current_operation=op_intent,
                policy_version=policy_ver,
                resource_scope=scope,
            )
            validate_lifecycle_transition(
                LifecycleStatus.AWAITING_APPROVAL, LifecycleStatus.EXECUTING
            )
            store.record_event(
                lease.session_id,
                "approval_verified",
                {
                    "request_id": request_id,
                    "operation_id": op_intent.operation_id.value,
                },
                lease,
            )
            return {"lifecycle_status": LifecycleStatus.EXECUTING.value, "error": None}
        except ApprovalSecurityError as exc:
            validate_lifecycle_transition(
                LifecycleStatus.AWAITING_APPROVAL, LifecycleStatus.FAILED
            )
            store.record_event(
                lease.session_id,
                "approval_denied",
                {"request_id": request_id, "error": exc.message},
                lease,
            )
            return {
                "lifecycle_status": LifecycleStatus.FAILED.value,
                "error": exc.message,
            }

    def node_execute(state: OrchestrationGraphState) -> dict[str, Any]:
        curr = LifecycleStatus(state.get("lifecycle_status", LifecycleStatus.EXECUTING))
        validate_lifecycle_transition(curr, LifecycleStatus.EVALUATING)
        store.record_event(
            lease.session_id,
            "execution_completed",
            {"execution_result": state.get("execution_result")},
            lease,
        )
        return {"lifecycle_status": LifecycleStatus.EVALUATING.value}

    def node_evaluate(state: OrchestrationGraphState) -> dict[str, Any]:
        """PM Invariant 17: Evaluates and transitions exclusively to terminal COMPLETED or FAILED."""
        curr = LifecycleStatus(
            state.get("lifecycle_status", LifecycleStatus.EVALUATING)
        )
        error = state.get("error")

        if error:
            target = LifecycleStatus.FAILED
        else:
            target = LifecycleStatus.COMPLETED

        # Invariant 17: Must not loop back to EXECUTING
        validate_lifecycle_transition(curr, target)
        store.record_event(
            lease.session_id,
            "evaluation_completed",
            {"final_status": target.value},
            lease,
        )
        return {"lifecycle_status": target.value}

    # Conditional routing after planning
    def route_after_planning(state: OrchestrationGraphState) -> str:
        status = state.get("lifecycle_status")
        if status == LifecycleStatus.AWAITING_APPROVAL.value:
            return "await_approval"
        return "execute"

    # Conditional routing after approval
    def route_after_approval(state: OrchestrationGraphState) -> str:
        status = state.get("lifecycle_status")
        if status == LifecycleStatus.EXECUTING.value:
            return "execute"
        return END

    # Build StateGraph
    builder.add_node("initialize", node_initialize)
    builder.add_node("plan", node_plan)
    builder.add_node("await_approval", node_await_approval)
    builder.add_node("execute", node_execute)
    builder.add_node("evaluate", node_evaluate)

    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "plan")
    builder.add_conditional_edges(
        "plan",
        route_after_planning,
        {"await_approval": "await_approval", "execute": "execute"},
    )
    builder.add_conditional_edges(
        "await_approval",
        route_after_approval,
        {"execute": "execute", END: END},
    )
    builder.add_edge("execute", "evaluate")
    builder.add_edge("evaluate", END)

    # Interrupt before await_approval for human-in-the-loop gating
    return builder.compile(
        checkpointer=checkpointer, interrupt_before=["await_approval"]
    )
