"""cv_agent.experiments — Public exports for ADR-0004 Experiment Ledger."""

from __future__ import annotations

from cv_agent.experiments.interfaces import (
    AccuracyMetrics,
    ArtifactManifest,
    ExecutionTarget,
    ExperimentLedger,
    ExperimentRecord,
    ExperimentSpec,
    ExperimentStatus,
    SystemMetrics,
    WorkloadType,
)
from cv_agent.experiments.ledger import (
    ApprovalReferenceFormatError,
    LedgerError,
    LedgerWorkspaceContainmentError,
    RelationalIntegrityError,
    ReproducibilityMetadataError,
    SQLiteExperimentLedger,
)
from cv_agent.experiments.metrics_validator import (
    IncompleteMetricsError,
    validate_metric_completeness,
)
from cv_agent.experiments.state_machine import (
    ExperimentImmutabilityError,
    ExperimentStateError,
    InvalidExperimentTransitionError,
    validate_experiment_transition,
)

__all__ = [
    "AccuracyMetrics",
    "ApprovalReferenceFormatError",
    "ArtifactManifest",
    "ExecutionTarget",
    "ExperimentImmutabilityError",
    "ExperimentLedger",
    "ExperimentRecord",
    "ExperimentSpec",
    "ExperimentStateError",
    "ExperimentStatus",
    "IncompleteMetricsError",
    "InvalidExperimentTransitionError",
    "LedgerError",
    "LedgerWorkspaceContainmentError",
    "RelationalIntegrityError",
    "ReproducibilityMetadataError",
    "SQLiteExperimentLedger",
    "SystemMetrics",
    "WorkloadType",
    "validate_experiment_transition",
    "validate_metric_completeness",
]
