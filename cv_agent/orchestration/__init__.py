"""cv_agent.orchestration — Authoritative ADR-0003 Orchestration Subsystem.

Exposes contracts, canonicalization, state machine, persistence store,
fencing, approvals, reconciliation, and graph runtime.
"""

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from cv_agent.orchestration.data_baseline import (
        BaselineEstablishmentRequest,
        BaselineEstablishmentResult,
        DatasetBaselineWorkflow,
        build_baseline_evaluation_plan,
        establish_dataset_baseline,
    )
    from cv_agent.orchestration.train_evaluate_diagnose import (
        AttestedTrainingSystemMetrics,
        CompositeEvaluationResult,
        FailureCategoryAnalysis,
        OperatingPoint,
        Phase6ApprovalError,
        Phase6DiagnosisError,
        Phase6EvaluationError,
        Phase6ExecutionError,
        Phase6ValidationError,
        ProposedNextExperiment,
        RankedFailureCategory,
        TargetConstraint,
        TargetConstraintVerdict,
        TrainEvaluateDiagnoseError,
        TrainEvaluateDiagnoseRequest,
        TrainEvaluateDiagnoseResult,
        TrainEvaluateDiagnoseWorkflow,
        build_training_evaluation_plan,
        run_train_evaluate_diagnose,
    )
from cv_agent.orchestration.discover_define import (
    CANONICAL_OPERATIONAL_DIMENSIONS,
    DiscoverDefineGraphState,
    IncompleteCharacterizationError,
    create_discover_define_graph,
    find_missing_operational_dimensions,
    route_after_discover,
    validate_define_preconditions,
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
    "CANONICAL_OPERATIONAL_DIMENSIONS",
    "CHECKPOINT_SCHEMA_VERSION",
    "ApprovalDecision",
    "ApprovalManager",
    "ApprovalRequest",
    "ApprovalSecurityError",
    "ApprovalStatus",
    "AttestedTrainingSystemMetrics",
    "BaselineEstablishmentRequest",
    "BaselineEstablishmentResult",
    "CheckpointCorruptionError",
    "CompositeEvaluationResult",
    "DatasetBaselineWorkflow",
    "DiscoverDefineGraphState",
    "ExecutionLease",
    "FailureCategoryAnalysis",
    "IncompleteCharacterizationError",
    "InvalidStateTransitionError",
    "LeaseManager",
    "LifecycleStatus",
    "OperatingPoint",
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
    "Phase6ApprovalError",
    "Phase6DiagnosisError",
    "Phase6EvaluationError",
    "Phase6ExecutionError",
    "Phase6ValidationError",
    "ProposedNextExperiment",
    "RankedFailureCategory",
    "ReconciliationCoordinator",
    "ReconciliationOutcome",
    "SQLiteLangGraphCheckpointer",
    "SQLiteOrchestrationStore",
    "SessionId",
    "StaleFencingLeaseError",
    "TargetConstraint",
    "TargetConstraintVerdict",
    "ThreadId",
    "TrainEvaluateDiagnoseError",
    "TrainEvaluateDiagnoseRequest",
    "TrainEvaluateDiagnoseResult",
    "TrainEvaluateDiagnoseWorkflow",
    "build_baseline_evaluation_plan",
    "build_operation_envelope",
    "build_training_evaluation_plan",
    "canonicalize_payload",
    "compute_payload_hash",
    "create_discover_define_graph",
    "create_orchestration_graph",
    "establish_dataset_baseline",
    "find_missing_operational_dimensions",
    "route_after_discover",
    "run_train_evaluate_diagnose",
    "validate_define_preconditions",
    "validate_lifecycle_transition",
    "validate_operation_transition",
]


def __getattr__(name: str) -> Any:
    if name in (
        "BaselineEstablishmentRequest",
        "BaselineEstablishmentResult",
        "DatasetBaselineWorkflow",
        "build_baseline_evaluation_plan",
        "establish_dataset_baseline",
    ):
        import cv_agent.orchestration.data_baseline as _data_baseline

        return getattr(_data_baseline, name)
    if name in (
        "AttestedTrainingSystemMetrics",
        "CompositeEvaluationResult",
        "FailureCategoryAnalysis",
        "OperatingPoint",
        "Phase6ApprovalError",
        "Phase6DiagnosisError",
        "Phase6EvaluationError",
        "Phase6ExecutionError",
        "Phase6ValidationError",
        "ProposedNextExperiment",
        "RankedFailureCategory",
        "TargetConstraint",
        "TargetConstraintVerdict",
        "TrainEvaluateDiagnoseError",
        "TrainEvaluateDiagnoseRequest",
        "TrainEvaluateDiagnoseResult",
        "TrainEvaluateDiagnoseWorkflow",
        "build_training_evaluation_plan",
        "run_train_evaluate_diagnose",
    ):
        import cv_agent.orchestration.train_evaluate_diagnose as _ted

        return getattr(_ted, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
