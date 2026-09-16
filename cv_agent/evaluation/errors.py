"""cv_agent.evaluation.errors — Normalized error hierarchy for ADR-0011."""

from __future__ import annotations


class EvaluationError(Exception):
    """Base exception for all Evaluation Subsystem errors."""


class EvaluationValidationError(EvaluationError):
    """Raised when an evaluation plan, binding, metric, or benchmark configuration violates invariants."""


class MetricCalculationError(EvaluationError):
    """Raised when metric computation fails or receives malformed predictions/annotations."""


class BenchmarkExecutionError(EvaluationError):
    """Raised when systems benchmarking encounters execution anomalies or timeouts."""


class BaselineComparisonError(EvaluationError):
    """Raised when baseline comparison cannot be evaluated or references incompatible experiments."""


class EvaluationApprovalRequiredError(EvaluationError):
    """Raised when an evaluation plan lacks required ADR-0003 approval or credentials mismatch."""


class EvaluationSecurityError(EvaluationError):
    """Raised when an evaluation invocation violates security invariants such as path containment."""
