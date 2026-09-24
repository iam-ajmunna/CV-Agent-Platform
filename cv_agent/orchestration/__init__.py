"""cv_agent.orchestration — Authoritative ADR-0003 Orchestration Subsystem.

Exposes contracts, canonicalization, state machine, persistence store,
fencing, approvals, reconciliation, and graph runtime.
"""

from cv_agent.orchestration.approvals import (
    ApprovalManager,
    ApprovalSecurityError,
)
from cv_agent.orchestration.canonicalization import (
    CANONICALIZATION_VERSION,
    build_operation_envelope,
    canonicalize_payload,
    compute_payload_hash,
)
from cv_agent.orchestration.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ExecutionLease,
    LifecycleStatus,
    OperationId,
    OperationIntent,
    OperationPayloadEnvelope,
    OperationReconciler,
    OperationStatus,
    OrchestrationEvent,
    OrchestrationStore,
    OrchestratorConfig,
    SessionId,
    ThreadId,
)
from cv_agent.orchestration.discover_define import (
    DiscoverDefineGraphState,
    create_discover_define_graph,
)
from cv_agent.orchestration.fencing import LeaseManager
from cv_agent.orchestration.graph import (
    OrchestrationGraphState,
    SQLiteLangGraphCheckpointer,
    create_orchestration_graph,
)
from cv_agent.orchestration.reconciliation import (
    OperatorEscalationRequired,
    ReconciliationCoordinator,
    ReconciliationOutcome,
)
from cv_agent.orchestration.state_machine import (
    InvalidStateTransitionError,
    validate_lifecycle_transition,
    validate_operation_transition,
)
from cv_agent.orchestration.store import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointCorruptionError,
    SQLiteOrchestrationStore,
    StaleFencingLeaseError,
)

__all__ = [
    "CANONICALIZATION_VERSION",
    "CHECKPOINT_SCHEMA_VERSION",
    "ApprovalDecision",
    "ApprovalManager",
    "ApprovalRequest",
    "ApprovalSecurityError",
    "ApprovalStatus",
    "CheckpointCorruptionError",
    "DiscoverDefineGraphState",
    "ExecutionLease",
    "InvalidStateTransitionError",
    "LeaseManager",
    "LifecycleStatus",
    "OperationId",
    "OperationIntent",
    "OperationPayloadEnvelope",
    "OperationReconciler",
    "OperationStatus",
    "OperatorEscalationRequired",
    "OrchestrationEvent",
    "OrchestrationGraphState",
    "OrchestrationStore",
    "OrchestratorConfig",
    "ReconciliationCoordinator",
    "ReconciliationOutcome",
    "SQLiteLangGraphCheckpointer",
    "SQLiteOrchestrationStore",
    "SessionId",
    "StaleFencingLeaseError",
    "ThreadId",
    "build_operation_envelope",
    "canonicalize_payload",
    "compute_payload_hash",
    "create_discover_define_graph",
    "create_orchestration_graph",
    "validate_lifecycle_transition",
    "validate_operation_transition",
]
