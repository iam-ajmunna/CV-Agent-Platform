"""cv_agent.evaluation — Evaluation Subsystem for ADR-0011."""

from __future__ import annotations

from cv_agent.evaluation.benchmarks import BenchmarkRunner, DefaultBenchmarkRunner
from cv_agent.evaluation.canonicalization import (
    compute_evaluation_payload_hash,
    extract_evaluation_operation_payload,
)
from cv_agent.evaluation.comparison import BaselineComparator, DefaultBaselineComparator
from cv_agent.evaluation.contracts import (
    BaselineComparisonResult,
    BenchmarkConfiguration,
    BenchmarkResult,
    ErrorAnalysisRecord,
    EvaluationDatasetBinding,
    EvaluationEnvironmentRecord,
    EvaluationPlan,
    EvaluationReport,
    EvaluationResult,
    EvaluationTargetRequirements,
    MetricDefinition,
    MetricResult,
    ModelArtifactReference,
    deep_freeze,
)
from cv_agent.evaluation.diagnostics import DefaultErrorAnalyzer, ErrorAnalyzer
from cv_agent.evaluation.errors import (
    BaselineComparisonError,
    BenchmarkExecutionError,
    EvaluationApprovalRequiredError,
    EvaluationError,
    EvaluationSecurityError,
    EvaluationValidationError,
    MetricCalculationError,
)
from cv_agent.evaluation.metrics import MetricCalculator, StandardMetricCalculator
from cv_agent.evaluation.runner import (
    DefaultEvaluationRunner,
    EvaluationRunner,
    parse_evaluation_result,
)
from cv_agent.evaluation.types import (
    BenchmarkId,
    EvaluationId,
    EvaluationVerdict,
    ExperimentId,
    HardwareClass,
    MetricId,
    StatisticalTestType,
    TaskType,
    validate_benchmark_id,
    validate_evaluation_id,
    validate_experiment_id,
    validate_metric_id,
)

__all__ = [
    "BaselineComparator",
    "BaselineComparisonError",
    "BaselineComparisonResult",
    "BenchmarkConfiguration",
    "BenchmarkExecutionError",
    "BenchmarkId",
    "BenchmarkResult",
    "BenchmarkRunner",
    "DefaultBaselineComparator",
    "DefaultBenchmarkRunner",
    "DefaultErrorAnalyzer",
    "DefaultEvaluationRunner",
    "ErrorAnalysisRecord",
    "ErrorAnalyzer",
    "EvaluationApprovalRequiredError",
    "EvaluationDatasetBinding",
    "EvaluationEnvironmentRecord",
    "EvaluationError",
    "EvaluationId",
    "EvaluationPlan",
    "EvaluationReport",
    "EvaluationResult",
    "EvaluationRunner",
    "EvaluationSecurityError",
    "EvaluationTargetRequirements",
    "EvaluationValidationError",
    "EvaluationVerdict",
    "ExperimentId",
    "HardwareClass",
    "MetricCalculationError",
    "MetricCalculator",
    "MetricDefinition",
    "MetricId",
    "MetricResult",
    "ModelArtifactReference",
    "StandardMetricCalculator",
    "StatisticalTestType",
    "TaskType",
    "compute_evaluation_payload_hash",
    "deep_freeze",
    "extract_evaluation_operation_payload",
    "parse_evaluation_result",
    "validate_benchmark_id",
    "validate_evaluation_id",
    "validate_experiment_id",
    "validate_metric_id",
]
