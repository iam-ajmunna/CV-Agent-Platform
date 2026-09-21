"""cv_agent.monitoring — Stream & Edge Monitoring Subsystem for ADR-0013."""

from __future__ import annotations

from typing import Any

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
    DeploymentBaselineBinding,
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


def _extract_authoritative_evidence_hash(
    evaluation_result: EvaluationResult,
    explicit_evidence_hash: ContentHash | str | None,
) -> ContentHash | None:
    """Discover authoritative distribution evidence hash from EvaluationResult metadata or explicit argument.

    Provenance Hierarchy:
    1. Explicit caller-provided authoritative evidence hash (e.g. from ADR-0011 ledger / signed artifact).
    2. Quality metric metadata (e.g. 'evidence_hash', 'distribution_evidence_hash', 'distribution_profile_hash').
    3. Evaluation plan requirements ('evaluation_requirements' or 'reproducibility_requirements').
    """
    if explicit_evidence_hash is not None:
        return ContentHash(str(explicit_evidence_hash))

    # Inspect MetricResult metadata
    for qm in evaluation_result.quality_metrics:
        for key in (
            "evidence_hash",
            "distribution_evidence_hash",
            "distribution_profile_hash",
            "distribution_hash",
        ):
            val = qm.metadata.get(key)
            if val:
                return ContentHash(str(val))

    # Inspect EvaluationPlan requirements
    for key in (
        "evidence_hash",
        "distribution_evidence_hash",
        "distribution_profile_hash",
        "distribution_hash",
    ):
        val = evaluation_result.plan.evaluation_requirements.get(
            key
        ) or evaluation_result.plan.reproducibility_requirements.get(key)
        if val:
            return ContentHash(str(val))

    return None


def extract_baseline_reference_binding(
    evaluation_result: EvaluationResult,
    distribution_profile: BaselineDistributionProfile,
    *,
    baseline_fps_p05: float | None = None,
    baseline_fps_p50: float | None = None,
    evidence_hash: ContentHash | str | None = None,
    deployed_artifact_id: str | None = None,
    source_checkpoint_id: str | None = None,
    deployment_record: Any | None = None,
) -> BaselineReferenceBinding:
    """Factory helper to construct an authoritative BaselineReferenceBinding from an ADR-0011 EvaluationResult.

    Enforces:
    - B19: Zero synthetic fallback. Fails closed if system benchmarks or confidence metrics are missing.
      fps_p05 must be authoritative and explicitly provided; never inferred from throughput [P§29.2].
      Omitting baseline_fps_p05 raises typed BaselineMismatchError fail-closed.
    - B21: Disentangled evidence authenticity boundary:
      * evaluation provenance: EvaluationResult is the authoritative source [ADR-0011].
      * distribution_ref: Identity binding referencing evaluation_id.
      * evidence_hash: Cryptographic SHA-256 content integrity check against authoritative ADR-0011 evidence.
        Self-generating an evidence hash from caller-supplied profile data is strictly prohibited.
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

    # 6. B21: Authoritative Evidence Provenance & Cryptographic Verification (Fail-Closed)
    authoritative_evidence_hash = _extract_authoritative_evidence_hash(
        evaluation_result, evidence_hash
    )
    if authoritative_evidence_hash is None:
        raise BaselineMismatchError(
            "ADR-0011 EvaluationResult does not provide an authoritative distribution evidence hash "
            "(and no external authoritative evidence_hash was provided). "
            "Self-generating an evidence hash from caller-supplied profile data to manufacture "
            "an authoritative baseline is strictly prohibited [ADR-0013, B21, P§29.2]."
        )

    eval_metadata_hash = _extract_authoritative_evidence_hash(evaluation_result, None)
    if (
        evidence_hash is not None
        and eval_metadata_hash is not None
        and str(evidence_hash) != str(eval_metadata_hash)
    ):
        raise BaselineMismatchError(
            f"Explicit evidence_hash ({evidence_hash}) does not match "
            f"authoritative evidence_hash in EvaluationResult metadata ({eval_metadata_hash}) [B21]."
        )

    computed_hash = compute_distribution_profile_hash(distribution_profile)
    if str(computed_hash) != str(authoritative_evidence_hash):
        raise BaselineMismatchError(
            f"Cryptographic evidence hash verification failed: computed profile digest {computed_hash} "
            f"does not match authoritative evidence_hash {authoritative_evidence_hash} [ADR-0013, B21]."
        )

    if distribution_profile.evidence_hash is not None and str(
        distribution_profile.evidence_hash
    ) != str(authoritative_evidence_hash):
        raise BaselineMismatchError(
            f"Profile evidence_hash ({distribution_profile.evidence_hash}) does not match "
            f"authoritative evidence_hash ({authoritative_evidence_hash}) [ADR-0013, B21]."
        )

    if deployment_record is not None:
        rec_art_id = str(deployment_record.plan.artifact_id)
        if deployed_artifact_id is not None and deployed_artifact_id != rec_art_id:
            raise BaselineMismatchError(
                f"deployed_artifact_id ({deployed_artifact_id!r}) conflicts with "
                f"DeploymentRecord artifact_id ({rec_art_id!r}) [B24]."
            )
        deployed_artifact_id = rec_art_id

    eval_plan = evaluation_result.plan
    return BaselineReferenceBinding(
        evaluation_id=evaluation_result.evaluation_id,
        dataset_manifest_hash=eval_plan.dataset_binding.dataset_manifest_hash,
        model_checkpoint_hash=eval_plan.model_artifact.weights_hash,
        baseline_distribution_ref=distribution_profile.distribution_ref,
        distribution_profile=distribution_profile,
        evidence_hash=authoritative_evidence_hash,
        baseline_fps_p50=fps_p50,
        baseline_fps_p05=baseline_fps_p05,
        baseline_latency_p95_ms=lat_p95,
        baseline_latency_p99_ms=lat_p99,
        baseline_confidence_mean=baseline_conf_mean,
        baseline_class_distribution=distribution_profile.class_distribution,
        deployed_artifact_id=deployed_artifact_id,
        source_checkpoint_id=source_checkpoint_id,
    )


def bind_deployment_to_monitoring_baseline(
    deployment_record: Any,
    baseline_binding: BaselineReferenceBinding,
    *,
    candidate_artifact_hash: ContentHash | str | None = None,
    optimization_result: Any | None = None,
) -> DeploymentBaselineBinding:
    """Explicitly binds an active DeploymentRecord to an authoritative BaselineReferenceBinding.

    Enforces B24 [P§29.2, ADR-0012, ADR-0013]:
    1. Deployed artifact ID in deployment_record matches baseline_binding.deployed_artifact_id (if set).
    2. Deployed artifact hash matches baseline_binding.model_checkpoint_hash.
    3. Evaluation evidence hash is authoritative and present.
    4. If optimization_result is provided, cross-validates candidate artifact binding and evaluation_id.
    5. Fails closed on any lineage, hash, identity, or evidence tampering.
    """
    from cv_agent.optimization.types import DeploymentStatus

    if deployment_record.status not in (
        DeploymentStatus.ACTIVE,
        DeploymentStatus.VERIFYING,
    ):
        raise BaselineMismatchError(
            f"DeploymentRecord {deployment_record.deployment_id} is not in ACTIVE or VERIFYING status "
            f"(status={deployment_record.status.value})"
        )

    deployed_art_id = str(deployment_record.plan.artifact_id)
    if (
        baseline_binding.deployed_artifact_id is not None
        and str(baseline_binding.deployed_artifact_id) != deployed_art_id
    ):
        raise BaselineMismatchError(
            f"Deployed artifact ID mismatch: DeploymentRecord has {deployed_art_id!r} "
            f"but BaselineReferenceBinding expects {baseline_binding.deployed_artifact_id!r} [B24]."
        )

    expected_hash = candidate_artifact_hash
    if optimization_result is not None:
        opt_art = optimization_result.artifact
        if str(opt_art.artifact_id) != deployed_art_id:
            raise BaselineMismatchError(
                f"OptimizationResult artifact_id {opt_art.artifact_id!r} does not match "
                f"deployed artifact {deployed_art_id!r} [B24]."
            )
        if expected_hash is None:
            expected_hash = opt_art.artifact_hash
        elif str(expected_hash) != str(opt_art.artifact_hash):
            raise BaselineMismatchError(
                f"Candidate artifact hash {expected_hash!r} conflicts with "
                f"OptimizationResult artifact hash {opt_art.artifact_hash!r} [B24]."
            )
        if (
            optimization_result.candidate_artifact_binding.evaluation_id is not None
            and str(baseline_binding.evaluation_id)
            != str(optimization_result.candidate_artifact_binding.evaluation_id)
        ):
            raise BaselineMismatchError(
                f"EvaluationId mismatch: BaselineReferenceBinding has {baseline_binding.evaluation_id!r} "
                f"but OptimizationResult candidate binding has {optimization_result.candidate_artifact_binding.evaluation_id!r} [B24]."
            )

    if expected_hash is not None and str(baseline_binding.model_checkpoint_hash) != str(
        expected_hash
    ):
        raise BaselineMismatchError(
            f"Deployed artifact hash mismatch: BaselineReferenceBinding model_checkpoint_hash "
            f"({baseline_binding.model_checkpoint_hash!r}) does not match deployed artifact hash ({expected_hash!r}) [B24]."
        )

    if baseline_binding.evidence_hash is None:
        raise BaselineMismatchError(
            "BaselineReferenceBinding lacks authoritative evidence_hash; cannot bind deployment [B24, B21]."
        )

    source_ckpt = baseline_binding.source_checkpoint_id
    if source_ckpt is None and optimization_result is not None:
        source_ckpt = str(optimization_result.plan.source_checkpoint_id)

    actual_art_hash = (
        expected_hash
        if expected_hash is not None
        else baseline_binding.model_checkpoint_hash
    )

    return DeploymentBaselineBinding(
        deployment_id=deployment_record.deployment_id,
        deployed_artifact_id=deployed_art_id,
        deployed_artifact_hash=ContentHash(str(actual_art_hash)),
        evaluation_id=baseline_binding.evaluation_id,
        evaluation_evidence_hash=baseline_binding.evidence_hash,
        baseline_binding=baseline_binding,
        source_checkpoint_id=source_ckpt,
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
    "DeploymentBaselineBinding",
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
    "bind_deployment_to_monitoring_baseline",
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
