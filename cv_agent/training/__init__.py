"""cv_agent.training — Authoritative ADR-0010 Training Subsystem.

Provides structured immutable training plans, pre-execution deterministic cost estimation,
ADR-0003 approval verification, pure domain lifecycle state validation, online divergence
monitoring with explicit early stopping completion, checkpoint lineage manifests and retention
management, and transport delegation to ADR-0005.
"""

from __future__ import annotations

from cv_agent.training.canonicalization import (
    compute_training_payload_hash,
    extract_training_operation_payload,
)
from cv_agent.training.contracts import (
    AugmentationConfig,
    CheckpointManifest,
    ComputeRequirements,
    CostEstimate,
    LossComponentConfig,
    OptimizerConfig,
    RetentionDecision,
    RetentionPolicy,
    SchedulerConfig,
    TrainingEnvironmentRecord,
    TrainingJobRecord,
    TrainingPlan,
    TrainingTelemetryRecord,
    deep_freeze,
)
from cv_agent.training.errors import (
    CheckpointRetentionError,
    CostEstimationError,
    InvalidTrainingJobTransitionError,
    PlanValidationError,
    TrainingApprovalRequiredError,
    TrainingDivergenceError,
    TrainingError,
    TrainingExecutionError,
)
from cv_agent.training.estimator import (
    DeterministicTrainingEstimator,
    TrainingEstimator,
)
from cv_agent.training.launcher import (
    DefaultTrainingLauncher,
    TrainingLauncher,
)
from cv_agent.training.monitor import (
    OnlineDivergenceMonitor,
    TrainingMonitor,
)
from cv_agent.training.retention import CheckpointRetentionManager
from cv_agent.training.state_machine import validate_training_job_transition
from cv_agent.training.types import (
    ApprovalId,
    CheckpointId,
    CompletionReason,
    DistributedStrategy,
    FailureReason,
    PlanId,
    TrainingJobId,
    TrainingJobStatus,
    TrainingPrecision,
    validate_approval_id,
    validate_checkpoint_id,
    validate_plan_id,
    validate_training_job_id,
)

__all__ = [
    "ApprovalId",
    "AugmentationConfig",
    "CheckpointId",
    "CheckpointManifest",
    "CheckpointRetentionError",
    "CheckpointRetentionManager",
    "CompletionReason",
    "ComputeRequirements",
    "CostEstimate",
    "CostEstimationError",
    "DefaultTrainingLauncher",
    "DeterministicTrainingEstimator",
    "DistributedStrategy",
    "FailureReason",
    "InvalidTrainingJobTransitionError",
    "LossComponentConfig",
    "OnlineDivergenceMonitor",
    "OptimizerConfig",
    "PlanId",
    "PlanValidationError",
    "RetentionDecision",
    "RetentionPolicy",
    "SchedulerConfig",
    "TrainingApprovalRequiredError",
    "TrainingDivergenceError",
    "TrainingEnvironmentRecord",
    "TrainingError",
    "TrainingEstimator",
    "TrainingExecutionError",
    "TrainingJobId",
    "TrainingJobRecord",
    "TrainingJobStatus",
    "TrainingLauncher",
    "TrainingMonitor",
    "TrainingPlan",
    "TrainingPrecision",
    "TrainingTelemetryRecord",
    "compute_training_payload_hash",
    "deep_freeze",
    "extract_training_operation_payload",
    "validate_approval_id",
    "validate_checkpoint_id",
    "validate_plan_id",
    "validate_training_job_id",
    "validate_training_job_transition",
]
