"""cv_agent.monitoring — Stream & Edge Monitoring Subsystem for ADR-0013."""

from __future__ import annotations

from cv_agent.dataset import ContentHash
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
    compute_distribution_profile_hash,
    deep_freeze,
)
from cv_agent.monitoring.drift import (
    DefaultStatisticalDriftDetector,
    StatisticalDriftDetector,
    parse_numeric_histogram,
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
    distribution_profile: BaselineDistributionProfile,
    *,
    baseline_fps_p05: float,
    baseline_fps_p50: float | None = None,
    evidence_hash: ContentHash | None = None,
) -> BaselineReferenceBinding:
    """Factory helper to construct an authoritative BaselineReferenceBinding from an ADR-0011 EvaluationResult.

    Enforces:
    - B19: Zero synthetic fallback. Fails closed if system benchmarks or confidence metrics are missing.
      fps_p05 must be authoritative and explicitly provided; never inferred from throughput [P§29.2].
    - B21: Disentangled evidence authenticity boundary:
      * evaluation provenance: EvaluationResult is the authoritative source [ADR-0011].
      * distribution_ref: Identity binding referencing evaluation_id.
      * evidence_hash: Cryptographic SHA-256 content integrity check.
      * Cohort consistency: Sample count and class distribution vocabulary alignment.
    """
    if not isinstance(evaluation_result, EvaluationResult):
        raise TypeError(
            f"Expected ADR-0011 EvaluationResult, got {type(evaluation_result)}"
        )
    if not isinstance(distribution_profile, BaselineDistributionProfile):
        raise TypeError(
            f"Expected BaselineDistributionProfile, got {type(distribution_profile)}"
        )

    # 1. B19: Authoritative Systems Performance Evidence (Fail-Closed)
    if not evaluation_result.system_benchmarks:
        raise BaselineMismatchError(
            "ADR-0011 EvaluationResult contains no system_benchmarks evidence. "
            "Baseline Primacy [P§29.2, ADR-0013] requires measured systems latency and throughput. "
            "Synthetic fallbacks are strictly prohibited."
        )

    bm = evaluation_result.system_benchmarks[0]
    fps_p50 = baseline_fps_p50 if baseline_fps_p50 is not None else bm.throughput_fps

    if baseline_fps_p05 is None or baseline_fps_p05 <= 0.0:
        raise BaselineMismatchError(
            "Authoritative baseline_fps_p05 must be explicitly provided; "
            "inferring from throughput is strictly prohibited [ADR-0013, P§29.2]."
        )
    if baseline_fps_p05 > fps_p50:
        raise BaselineMismatchError(
            f"baseline_fps_p05 ({baseline_fps_p05}) cannot exceed baseline_fps_p50 ({fps_p50})"
        )

    lat_p95 = bm.latency_p95_ms
    lat_p99 = bm.latency_p99_ms

    # 2. B19: Authoritative Quality Confidence Evidence (Fail-Closed)
    baseline_conf_mean: float | None = None
    for qm in evaluation_result.quality_metrics:
        metric_name = qm.metric.name.lower()
        if "confidence" in metric_name or "score" in metric_name:
            baseline_conf_mean = qm.primary_value
            break

    if baseline_conf_mean is None:
        raise BaselineMismatchError(
            "ADR-0011 EvaluationResult contains no authoritative confidence metric "
            "in quality_metrics. Synthetic fallback defaults are strictly prohibited [ADR-0013, P§29.2]."
        )

    # 3. B21: Distribution Reference Identity Binding
    eval_id_str = str(evaluation_result.evaluation_id)
    if eval_id_str not in distribution_profile.distribution_ref:
        raise BaselineMismatchError(
            f"Identity check failed: distribution_profile.distribution_ref "
            f"({distribution_profile.distribution_ref!r}) does not reference "
            f"evaluation_id ({eval_id_str!r}) [ADR-0013, B21]."
        )

    # 4. B21: Sample Count Cohort Alignment
    if (
        evaluation_result.error_analysis is not None
        and evaluation_result.error_analysis.total_samples > 0
    ):
        if (
            evaluation_result.error_analysis.total_samples
            != distribution_profile.sample_count
        ):
            raise BaselineMismatchError(
                f"Sample count mismatch: distribution_profile.sample_count ({distribution_profile.sample_count}) "
                f"does not match EvaluationResult error_analysis.total_samples ({evaluation_result.error_analysis.total_samples})."
            )
    else:
        for qm in evaluation_result.quality_metrics:
            if (
                qm.sample_count > 0
                and qm.sample_count != distribution_profile.sample_count
            ):
                raise BaselineMismatchError(
                    f"Sample count mismatch: distribution_profile.sample_count ({distribution_profile.sample_count}) "
                    f"does not match MetricResult ({qm.metric.name}) sample_count ({qm.sample_count})."
                )

    # 5. B21: Class Vocabulary Alignment
    if (
        evaluation_result.error_analysis is not None
        and evaluation_result.error_analysis.class_labels
    ):
        eval_classes = set(evaluation_result.error_analysis.class_labels)
        profile_classes = set(distribution_profile.class_distribution.keys())
        if eval_classes != profile_classes:
            raise BaselineMismatchError(
                f"Class distribution vocabulary mismatch: profile classes {sorted(profile_classes)} "
                f"do not match EvaluationResult class_labels {sorted(eval_classes)}."
            )
    else:
        for qm in evaluation_result.quality_metrics:
            if qm.per_class_values:
                eval_classes = set(qm.per_class_values.keys())
                profile_classes = set(distribution_profile.class_distribution.keys())
                if eval_classes != profile_classes:
                    raise BaselineMismatchError(
                        f"Class distribution vocabulary mismatch: profile classes {sorted(profile_classes)} "
                        f"do not match MetricResult ({qm.metric.name}) per_class_values keys {sorted(eval_classes)}."
                    )

    # 6. B21: Content Hash Integrity Verification
    computed_hash = compute_distribution_profile_hash(distribution_profile)
    effective_evidence_hash = evidence_hash or distribution_profile.evidence_hash
    if effective_evidence_hash is not None and str(computed_hash) != str(
        effective_evidence_hash
    ):
        raise BaselineMismatchError(
            f"Cryptographic evidence hash verification failed: computed profile digest {computed_hash} "
            f"does not match authoritative evidence_hash {effective_evidence_hash} [ADR-0013, B21]."
        )

    eval_plan = evaluation_result.plan
    return BaselineReferenceBinding(
        evaluation_id=evaluation_result.evaluation_id,
        dataset_manifest_hash=eval_plan.dataset_binding.dataset_manifest_hash,
        model_checkpoint_hash=eval_plan.model_artifact.weights_hash,
        baseline_distribution_ref=distribution_profile.distribution_ref,
        distribution_profile=distribution_profile,
        evidence_hash=effective_evidence_hash or computed_hash,
        baseline_fps_p50=fps_p50,
        baseline_fps_p05=baseline_fps_p05,
        baseline_latency_p95_ms=lat_p95,
        baseline_latency_p99_ms=lat_p99,
        baseline_confidence_mean=baseline_conf_mean,
        baseline_class_distribution=distribution_profile.class_distribution,
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
    "compute_distribution_profile_hash",
    "deep_freeze",
    "extract_baseline_reference_binding",
    "parse_numeric_histogram",
    "validate_alert_id",
    "validate_candidate_batch_id",
    "validate_candidate_sample_id",
    "validate_drift_analysis_id",
    "validate_snapshot_id",
    "validate_trigger_proposal_id",
]
