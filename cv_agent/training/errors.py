"""cv_agent.training.errors — Domain exception hierarchy for ADR-0010."""

from __future__ import annotations


class TrainingError(Exception):
    """Base exception for all training subsystem errors."""


class PlanValidationError(TrainingError):
    """Raised when a TrainingPlan violates structural or architectural invariants."""


class CostEstimationError(TrainingError):
    """Raised when cost estimation inputs are invalid or calculation fails closed."""


class TrainingApprovalRequiredError(TrainingError):
    """Raised when training dispatch lacks valid, verified ADR-0003 approval evidence."""


class InvalidTrainingJobTransitionError(TrainingError):
    """Raised when an illegal lifecycle state transition is attempted on a training job."""


class CheckpointRetentionError(TrainingError):
    """Raised when checkpoint manifest evaluation or retention policy fails."""


class TrainingExecutionError(TrainingError):
    """Raised when tool delegation through ADR-0005 fails or encounters errors."""


class TrainingDivergenceError(TrainingError):
    """Raised when an online training monitor detects divergence, NaN/Inf, or failure."""
