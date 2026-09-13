"""cv_agent.orchestration.graph — LangGraph workflow runtime over authoritative ADR-0003 contracts.

Implements PM Gate 1 and Gate D:
- SQLite/ADR-0003 is the sole authority; LangGraph is the workflow execution mechanism.
- State transitions strictly invoke store.transition_lifecycle() with atomic CAS fencing.
- Enforces Invariant 17: evaluate node terminates to COMPLETED/FAILED/PAUSED and never loops back to execute.
- Human-in-the-loop approval interrupt and resumption under active lease fencing.
- SQLiteLangGraphCheckpointer persists graph state directly to SQLite by default.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_metadata,
)
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from cv_agent.orchestration.approvals import ApprovalManager, ApprovalSecurityError
from cv_agent.orchestration.contracts import (
    ExecutionLease,
    LifecycleStatus,
    OperationIntent,
)
from cv_agent.orchestration.store import SQLiteOrchestrationStore


class SQLiteLangGraphCheckpointer(BaseCheckpointSaver):
    """Durable SQLite-backed LangGraph checkpointer adapter (ADR-0003 §3.1)."""

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self.db_path = str(db_path)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS lg_checkpoints (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    parent_id TEXT,
                    checkpoint_type TEXT NOT NULL,
                    checkpoint_data BLOB NOT NULL,
                    metadata_type TEXT NOT NULL,
                    metadata_data BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                );
                CREATE TABLE IF NOT EXISTS lg_blobs (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    version TEXT NOT NULL,
                    type TEXT NOT NULL,
                    data BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
                );
                CREATE TABLE IF NOT EXISTS lg_writes (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    type TEXT NOT NULL,
                    data BLOB NOT NULL,
                    task_path TEXT NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                );
                """
            )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"].get("checkpoint_id")
        with self._get_conn() as conn:
            cur = conn.cursor()
            if checkpoint_id:
                cur.execute(
                    """
                    SELECT checkpoint_id, parent_id, checkpoint_type, checkpoint_data, metadata_type, metadata_data
                    FROM lg_checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                    """,
                    (thread_id, checkpoint_ns, checkpoint_id),
                )
            else:
                cur.execute(
                    """
                    SELECT checkpoint_id, parent_id, checkpoint_type, checkpoint_data, metadata_type, metadata_data
                    FROM lg_checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ?
                    ORDER BY rowid DESC LIMIT 1
                    """,
                    (thread_id, checkpoint_ns),
                )
            row = cur.fetchone()
            if not row:
                return None

            cid, pid, ct, cd, mt, md = row
            checkpoint = self.serde.loads_typed((ct, cd))
            metadata = self.serde.loads_typed((mt, md))

            channel_values: dict[str, Any] = {}
            for k, v in checkpoint.get("channel_versions", {}).items():
                cur.execute(
                    """
                    SELECT type, data FROM lg_blobs
                    WHERE thread_id = ? AND checkpoint_ns = ? AND channel = ? AND version = ?
                    """,
                    (thread_id, checkpoint_ns, k, str(v)),
                )
                brow = cur.fetchone()
                if brow and (brow[0] != "empty" or brow[1] != b""):
                    channel_values[k] = self.serde.loads_typed((brow[0], brow[1]))

            checkpoint["channel_values"] = channel_values

            cur.execute(
                """
                SELECT task_id, channel, type, data
                FROM lg_writes
                WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                """,
                (thread_id, checkpoint_ns, cid),
            )
            writes = [
                (r[0], r[1], self.serde.loads_typed((r[2], r[3])))
                for r in cur.fetchall()
            ]

            parent_cfg: RunnableConfig | None = (
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": pid,
                    }
                }
                if pid
                else None
            )

            return CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": cid,
                    }
                },
                checkpoint=checkpoint,
                metadata=metadata,
                parent_config=parent_cfg,
                pending_writes=writes,
            )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        return iter([])

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        c = checkpoint.copy()
        values: dict[str, Any] = c.pop("channel_values")  # type: ignore[misc]

        with self._get_conn() as conn:
            for k, v in new_versions.items():
                t, d = (
                    self.serde.dumps_typed(values[k])
                    if k in values
                    else ("empty", b"")
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO lg_blobs (thread_id, checkpoint_ns, channel, version, type, data)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (thread_id, checkpoint_ns, k, str(v), t, d),
                )

            ct, cd = self.serde.dumps_typed(c)
            mt, md = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
            parent_id = config["configurable"].get("checkpoint_id")

            conn.execute(
                """
                INSERT OR REPLACE INTO lg_checkpoints (
                    thread_id, checkpoint_ns, checkpoint_id, parent_id,
                    checkpoint_type, checkpoint_data, metadata_type, metadata_data
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint["id"],
                    parent_id,
                    ct,
                    cd,
                    mt,
                    md,
                ),
            )
            conn.commit()

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]

        with self._get_conn() as conn:
            for idx, (c, v) in enumerate(writes):
                t, d = self.serde.dumps_typed(v)
                inner_idx = WRITES_IDX_MAP.get(c, idx)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO lg_writes (
                        thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, data, task_path
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        checkpoint_ns,
                        checkpoint_id,
                        task_id,
                        inner_idx,
                        c,
                        t,
                        d,
                        task_path,
                    ),
                )
            conn.commit()


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
    """Build and compile the LangGraph workflow graph anchored to the authoritative SQLite store.

    Nodes execute transitions validated against ADR-0003 §3.9 state machine and
    persist transactional events in the SQLite store under the active lease.
    """
    appr_mgr = approval_manager or ApprovalManager(store)
    if checkpointer is None:
        checkpointer = SQLiteLangGraphCheckpointer(str(store.db_path))

    builder: StateGraph = StateGraph(OrchestrationGraphState)

    def node_initialize(state: OrchestrationGraphState) -> dict[str, Any]:
        new_status, _ = store.transition_lifecycle(
            lease.session_id,
            LifecycleStatus.READY,
            "session_ready",
            {"status": LifecycleStatus.READY.value},
            lease,
        )
        return {"lifecycle_status": new_status.value}

    def node_plan(state: OrchestrationGraphState) -> dict[str, Any]:
        requires_approval = state.get("requires_approval", False)
        target = (
            LifecycleStatus.AWAITING_APPROVAL
            if requires_approval
            else LifecycleStatus.EXECUTING
        )

        store.transition_lifecycle(
            lease.session_id,
            LifecycleStatus.PLANNING,
            "planning_started",
            {"task": state.get("task")},
            lease,
        )

        new_status, _ = store.transition_lifecycle(
            lease.session_id,
            target,
            "planning_completed",
            {"target": target.value},
            lease,
        )
        return {"lifecycle_status": new_status.value}

    def node_await_approval(state: OrchestrationGraphState) -> dict[str, Any]:
        request_id = state.get("approval_request_id")
        if not request_id:
            new_status, _ = store.transition_lifecycle(
                lease.session_id,
                LifecycleStatus.FAILED,
                "approval_failed",
                {"error": "Approval required but no approval_request_id provided."},
                lease,
            )
            return {
                "lifecycle_status": new_status.value,
                "error": "Approval required but no approval_request_id provided.",
            }

        policy_ver = state.get("approval_policy_version") or "v1.0"
        scope = state.get("approval_resource_scope") or "default"
        raw_intent = state.get("operation_intent")

        if raw_intent is None:
            new_status, _ = store.transition_lifecycle(
                lease.session_id,
                LifecycleStatus.FAILED,
                "approval_failed",
                {"error": "No operation intent provided for approval verification."},
                lease,
            )
            return {
                "lifecycle_status": new_status.value,
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
            new_status, _ = store.transition_lifecycle(
                lease.session_id,
                LifecycleStatus.EXECUTING,
                "approval_verified",
                {
                    "request_id": request_id,
                    "operation_id": op_intent.operation_id.value,
                },
                lease,
            )
            return {"lifecycle_status": new_status.value, "error": None}
        except ApprovalSecurityError as exc:
            new_status, _ = store.transition_lifecycle(
                lease.session_id,
                LifecycleStatus.FAILED,
                "approval_denied",
                {"request_id": request_id, "error": exc.message},
                lease,
            )
            return {
                "lifecycle_status": new_status.value,
                "error": exc.message,
            }

    def node_execute(state: OrchestrationGraphState) -> dict[str, Any]:
        new_status, _ = store.transition_lifecycle(
            lease.session_id,
            LifecycleStatus.EVALUATING,
            "execution_completed",
            {"execution_result": state.get("execution_result")},
            lease,
        )
        return {"lifecycle_status": new_status.value}

    def node_evaluate(state: OrchestrationGraphState) -> dict[str, Any]:
        """PM Invariant 17: Evaluates and transitions exclusively to terminal COMPLETED or FAILED."""
        error = state.get("error")
        target = LifecycleStatus.FAILED if error else LifecycleStatus.COMPLETED

        new_status, _ = store.transition_lifecycle(
            lease.session_id,
            target,
            "evaluation_completed",
            {"final_status": target.value},
            lease,
        )
        return {"lifecycle_status": new_status.value}

    def route_after_planning(state: OrchestrationGraphState) -> str:
        status = state.get("lifecycle_status")
        if status == LifecycleStatus.AWAITING_APPROVAL.value:
            return "await_approval"
        return "execute"

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
