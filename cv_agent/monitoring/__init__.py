"""cv_agent.monitoring — Stream & Edge Monitoring Subsystem for ADR-0013."""

from __future__ import annotations

from collections.abc import Mapping

from cv_agent.evaluation.contracts import EvaluationResult
from cv_agent.monitoring.active_learning import (
    ActiveLearningSelector,
    DefaultActiveLearningSelector,
)
from cv_agent.monitoring.contracts import (
    ActiveLearningCandidateBatch,
    BaselineDistributionProfile,
    BaselineReferenceBinding,
    CandidateSample,
    DataTelemetryRecord,
    DriftAnalysisRecord,
    DriftMetricResult,
    ModelTelemetryRecord,
    MonitoringAlertRecord,
    RetrainingTriggerProposal,
    SystemTelemetryRecord,
    UnifiedTelemetrySnapshot,
    deep_freeze,
)
from cv_agent.monitoring.drift import (
    DefaultStatisticalDriftDetector,
    StatisticalDriftDetector,
)
from cv_agent.monitoring.errors import (
    ActiveLearningBufferError,
    BaselineMismatchError,
    DriftEvaluationError,
    ExpiredCandidatePayloadError,
    MonitoringError,
    TelemetryValidationError,
    UnauthorizedRetrainingError,
)
from cv_agent.monitoring.pipeline import (
    DefaultTelemetryPipeline,
    MonitoringCoordinator,
    TelemetryIngestionSink,
)
from cv_agent.monitoring.types import (
    ActiveLearningStrategy,
    AlertId,
    CandidateBatchId,
    CandidateSampleId,
    DriftAnalysisId,
    DriftMetricType,
    DriftSeverity,
    SnapshotId,
    TelemetryDimension,
    TriggerProposalId,
    TriggerReason,
    validate_alert_id,
    validate_candidate_batch_id,
    validate_candidate_sample_id,
    validate_drift_analysis_id,
    validate_snapshot_id,
    validate_trigger_proposal_id,
)


def extract_baseline_reference_binding(
    evaluation_result: EvaluationResult,
    baseline_distribution_ref: str,
    confidence_histogram: Mapping[str, int],
    confidence_quantiles: tuple[float, ...],
    class_distribution: Mapping[str, float],
    prediction_entropy_mean: float,
    sample_count: int,
    feature_embedding_centroid: tuple[float, ...] | None = None,
) -> BaselineReferenceBinding:
    """Factory helper to construct an authoritative BaselineReferenceBinding from an ADR-0011 EvaluationResult."""
    if not isinstance(evaluation_result, EvaluationResult):
        raise TypeError(
            f"Expected ADR-0011 EvaluationResult, got {type(evaluation_result)}"
        )

    eval_plan = evaluation_result.plan
    dataset_hash = eval_plan.dataset_binding.dataset_manifest_hash
    model_hash = eval_plan.model_artifact.weights_hash

    # Extract systems throughput and latency baselines from benchmarks
    baseline_fps_p50 = 30.0
    baseline_fps_p05 = 20.0
    baseline_latency_p95_ms = 35.0
    baseline_latency_p99_ms = 50.0

    if evaluation_result.system_benchmarks:
        bm = evaluation_result.system_benchmarks[0]
        baseline_fps_p50 = bm.throughput_fps
        baseline_fps_p05 = bm.throughput_fps * 0.70
        baseline_latency_p95_ms = bm.latency_p95_ms
        baseline_latency_p99_ms = bm.latency_p99_ms

    # Extract confidence mean from quality metrics
    baseline_conf_mean = 0.85
    for qm in evaluation_result.quality_metrics:
        if "confidence" in qm.metric.name.lower() or "score" in qm.metric.name.lower():
            baseline_conf_mean = qm.primary_value
            break

    profile = BaselineDistributionProfile(
        distribution_ref=baseline_distribution_ref,
        confidence_histogram=confidence_histogram,
        confidence_quantiles=confidence_quantiles,
        class_distribution=class_distribution,
        prediction_entropy_mean=prediction_entropy_mean,
        sample_count=sample_count,
        feature_embedding_centroid=feature_embedding_centroid,
    )

    return BaselineReferenceBinding(
        evaluation_id=evaluation_result.evaluation_id,
        dataset_manifest_hash=dataset_hash,
        model_checkpoint_hash=model_hash,
        baseline_distribution_ref=baseline_distribution_ref,
        distribution_profile=profile,
        baseline_fps_p50=baseline_fps_p50,
        baseline_fps_p05=baseline_fps_p05,
        baseline_latency_p95_ms=baseline_latency_p95_ms,
        baseline_latency_p99_ms=baseline_latency_p99_ms,
        baseline_confidence_mean=baseline_conf_mean,
        baseline_class_distribution=class_distribution,
    )


__all__ = [
    "ActiveLearningBufferError",
    "ActiveLearningCandidateBatch",
    "ActiveLearningSelector",
    "ActiveLearningStrategy",
    "AlertId",
    "BaselineDistributionProfile",
    "BaselineMismatchError",
    "BaselineReferenceBinding",
    "CandidateBatchId",
    "CandidateSample",
    "CandidateSampleId",
    "DataTelemetryRecord",
    "DefaultActiveLearningSelector",
    "DefaultStatisticalDriftDetector",
    "DefaultTelemetryPipeline",
    "DriftAnalysisId",
    "DriftAnalysisRecord",
    "DriftEvaluationError",
    "DriftMetricResult",
    "DriftMetricType",
    "DriftSeverity",
    "ExpiredCandidatePayloadError",
    "ModelTelemetryRecord",
    "MonitoringAlertRecord",
    "MonitoringCoordinator",
    "MonitoringError",
    "RetrainingTriggerProposal",
    "SnapshotId",
    "StatisticalDriftDetector",
    "SystemTelemetryRecord",
    "TelemetryDimension",
    "TelemetryIngestionSink",
    "TelemetryValidationError",
    "TriggerProposalId",
    "TriggerReason",
    "UnauthorizedRetrainingError",
    "UnifiedTelemetrySnapshot",
    "deep_freeze",
    "extract_baseline_reference_binding",
    "validate_alert_id",
    "validate_candidate_batch_id",
    "validate_candidate_sample_id",
    "validate_drift_analysis_id",
    "validate_snapshot_id",
    "validate_trigger_proposal_id",
]
