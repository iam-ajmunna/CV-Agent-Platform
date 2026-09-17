"""cv_agent.optimization.errors — Normalized error hierarchy for ADR-0012."""

from __future__ import annotations

from typing import Any


class OptimizationError(Exception):
    """Base exception for all Optimization & Deployment Subsystem errors."""


class OptimizationValidationError(OptimizationError):
    """Raised when an optimization plan, precision, shape profile, or binding violates invariants."""


class CalibrationLeakageError(OptimizationValidationError):
    """Raised when calibration dataset binding leaks test/evaluation split per ADR-0009 Invariant 13."""


class OptimizationExecutionError(OptimizationError):
    """Raised when compilation transport or physical execution fails via ADR-0005."""


class OptimizationVerificationError(OptimizationError):
    """Raised when empirical post-optimization evaluation verification fails (e.g. accuracy cliff)."""


class DeploymentValidationError(OptimizationError):
    """Raised when deployment plan, target profile, or health check specification is invalid."""


class DeploymentApprovalRequiredError(OptimizationError):
    """Raised when deployment activation lacks required ADR-0003 human approval credentials."""


class ReadinessProbeFailureError(OptimizationError):
    """Raised when candidate deployment fails startup timeout, latency budget, or memory headroom."""

    def __init__(self, message: str, failed_record: Any = None) -> None:
        super().__init__(message)
        self.failed_record = failed_record


class RollbackExecutionError(OptimizationError):
    """Raised when deployment rollback fails or cannot be performed (e.g. escalating to OPERATOR_REQUIRED)."""

    def __init__(self, message: str, record: Any = None) -> None:
        super().__init__(message)
        self.record = record


class HardwareMismatchError(OptimizationError):
    """Raised when target hardware architecture, compute capability, or memory budget mismatches."""
