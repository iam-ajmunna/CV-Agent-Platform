"""tests/test_monitoring_boundary.py — Adversarial boundary test suite for ADR-0013.

Exercises all 12 architectural invariants and cross-ADR boundary seams:
1. Baseline Primacy Invariant ([P§29.2]) — fail-closed without validated ADR-0011 baseline
2. Tri-Dimensional Completeness Invariant ([P§28]) — synchronous model, system, and data metrics
3. Zero Autonomous Retraining Invariant ([P§24], [P§31]) — strictly advisory proposals
4. Mandatory Human Approval Invariant (docs/APPROVALS.md) — requires_human_approval = True
5. Cryptographic Frame Lineage Invariant ([P§25], [P§26]) — SHA-256 frame payload hashing
6. Dataset Authority Delegation Invariant ([P§26], ADR-0009) — candidate handoff without manifest tampering
7. Layer Decoupling Invariant ([P§19]–[P§22]) — zero training runtime or execution leaks
8. Statistically Grounded Drift Invariant ([P§29.3]) — KS, Wasserstein, PSI, Chi2, JS divergence
9. Bounded Storage Buffer Invariant ([P§28]) — capacity limits, deterministic eviction, temporal TTL
10. Hardware-Aware Edge Viability Invariant ([P§13], [P§29.4]) — lightweight pure-Python execution
11. Immutability of Audit Records Invariant ([P§25]) — frozen Pydantic contracts and MappingProxyType
12. Transport Agnosticism Invariant (ADR-0005) — decoupled from transport protocols
AST Audits: Zero subprocess, zero os.system, zero sqlite3, zero PyTorch/TensorFlow imports
"""

from __future__ import annotations

import ast
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from cv_agent.dataset import ContentHash, DatasetId, DatasetVersion
from cv_agent.domain import CapabilityId
from cv_agent.evaluation.contracts import (
    BenchmarkId,
    BenchmarkResult,
    EvaluationDatasetBinding,
    EvaluationEnvironmentRecord,
    EvaluationPlan,
    EvaluationResult,
    MetricDefinition,
    MetricId,
    MetricResult,
    ModelArtifactReference,
    TaskType,
    TrainingPrecision,
)
from cv_agent.evaluation.types import EvaluationId, EvaluationVerdict, ExperimentId
from cv_agent.monitoring import (
    ActiveLearningBufferError,
    ActiveLearningStrategy,
    BaselineDistributionProfile,
    BaselineMismatchError,
    BaselineReferenceBinding,
    CandidateSample,
    CandidateSampleId,
    DataTelemetryRecord,
    DefaultActiveLearningSelector,
    DefaultStatisticalDriftDetector,
    DefaultTelemetryPipeline,
    DriftEvaluationError,
    DriftMetricType,
    DriftSeverity,
    ModelTelemetryRecord,
    MonitoringAlertRecord,
    RetrainingTriggerProposal,
    SnapshotId,
    SystemTelemetryRecord,
    TriggerProposalId,
    TriggerReason,
    UnifiedTelemetrySnapshot,
    compute_distribution_profile_hash,
    extract_baseline_reference_binding,
    parse_numeric_histogram,
)
from cv_agent.optimization.types import DeploymentId
from cv_agent.tools import TargetId
from cv_agent.training.types import ApprovalId, CheckpointId

# =============================================================================
# Test Fixtures & Helpers
# =============================================================================


def _make_valid_baseline_profile() -> BaselineDistributionProfile:
    """Construct an authoritative ADR-0011 baseline distribution profile."""
    return BaselineDistributionProfile(
        distribution_ref="eval-evidence://eval-20260920-v1/distributions",
        confidence_histogram={
            "0.0-0.1": 5,
            "0.1-0.2": 10,
            "0.2-0.3": 15,
            "0.3-0.4": 20,
            "0.4-0.5": 50,
            "0.5-0.6": 100,
            "0.6-0.7": 200,
            "0.7-0.8": 500,
            "0.8-0.9": 1500,
            "0.9-1.0": 2600,
        },
        confidence_quantiles=(0.1, 0.3, 0.5, 0.7, 0.8, 0.85, 0.9, 0.93, 0.96, 0.99),
        class_distribution={"person": 0.60, "vehicle": 0.30, "bicycle": 0.10},
        prediction_entropy_mean=0.35,
        sample_count=5000,
        feature_embedding_centroid=(0.1, 0.2, 0.3, 0.4),
    )


def _make_valid_baseline_binding() -> BaselineReferenceBinding:
    """Construct an authoritative BaselineReferenceBinding."""
    profile = _make_valid_baseline_profile()
    return BaselineReferenceBinding(
        evaluation_id=EvaluationId("eval-20260920-v1"),
        dataset_manifest_hash=ContentHash("d4e5f6a1" * 8),
        model_checkpoint_hash=ContentHash("c1c2c3c4" * 8),
        baseline_distribution_ref=profile.distribution_ref,
        distribution_profile=profile,
        baseline_fps_p50=60.0,
        baseline_fps_p05=45.0,
        baseline_latency_p95_ms=25.0,
        baseline_latency_p99_ms=35.0,
        baseline_confidence_mean=0.88,
        baseline_class_distribution=profile.class_distribution,
    )


def _make_valid_telemetry_snapshot(
    deployment_id: str = "dept/yolo-v8/edge-01",
    stream_id: str = "stream-cam-01",
    confidence_histogram: dict[str, int] | None = None,
    class_counts: dict[str, int] | None = None,
    class_proportions: dict[str, float] | None = None,
    throughput_fps: float = 58.0,
    latency_p95_ms: float = 22.0,
    blur_variance: float = 120.0,
    occlusion: bool = False,
    entropy_mean: float = 0.36,
) -> UnifiedTelemetrySnapshot:
    """Construct a synchronized multi-dimensional telemetry snapshot."""
    now = datetime.now(timezone.utc)
    dep_id = DeploymentId(deployment_id)

    hist = confidence_histogram or {
        "0.0-0.1": 6,
        "0.1-0.2": 11,
        "0.2-0.3": 14,
        "0.3-0.4": 22,
        "0.4-0.5": 52,
        "0.5-0.6": 98,
        "0.6-0.7": 195,
        "0.7-0.8": 505,
        "0.8-0.9": 1490,
        "0.9-1.0": 2607,
    }

    counts = class_counts or {"person": 600, "vehicle": 300, "bicycle": 100}
    props = class_proportions or {"person": 0.60, "vehicle": 0.30, "bicycle": 0.10}

    model_t = ModelTelemetryRecord(
        deployment_id=dep_id,
        window_start=now - timedelta(seconds=60),
        window_end=now,
        window_seconds=60.0,
        total_inferences=sum(hist.values()),
        confidence_histogram=hist,
        confidence_mean=0.87,
        confidence_p10=0.55,
        confidence_p50=0.88,
        confidence_p90=0.97,
        class_counts=counts,
        class_proportions=props,
        mean_detections_per_frame=1.8,
        zero_detection_frame_ratio=0.15,
        prediction_entropy_mean=entropy_mean,
        out_of_distribution_score_mean=0.05,
    )

    system_t = SystemTelemetryRecord(
        deployment_id=dep_id,
        timestamp=now,
        hardware_target="jetson_orin_nano",
        throughput_fps=throughput_fps,
        latency_p50_ms=15.0,
        latency_p95_ms=latency_p95_ms,
        latency_p99_ms=latency_p95_ms + 5.0,
        dropped_frames_total=2,
        dropped_frame_ratio=0.001,
        gpu_utilization_pct=72.0,
        cpu_utilization_pct=45.0,
        vram_used_mb=2100.0,
        vram_total_mb=8192.0,
        ram_used_mb=3200.0,
        gpu_temperature_celsius=52.0,
        power_draw_watts=12.5,
        thermal_throttling_active=False,
    )

    data_t = DataTelemetryRecord(
        stream_id=stream_id,
        camera_id="cam-front-gate",
        timestamp=now,
        mean_luminance=128.0,
        contrast_std=45.0,
        blur_laplacian_variance=blur_variance,
        occlusion_detected=occlusion,
        overexposure_ratio=0.02,
        underexposure_ratio=0.03,
        resolution_width=1920,
        resolution_height=1080,
        stream_bitrate_kbps=4000.0,
        packet_loss_ratio=0.005,
        scene_change_detected=False,
    )

    return UnifiedTelemetrySnapshot(
        snapshot_id=SnapshotId(
            f"snap-{dep_id.replace('/', '-')}-{stream_id}-{int(now.timestamp())}"
        ),
        deployment_id=dep_id,
        stream_id=stream_id,
        timestamp=now,
        model_telemetry=model_t,
        system_telemetry=system_t,
        data_telemetry=data_t,
    )


# =============================================================================
# Invariant 1: Baseline Primacy Invariant ([P§29.2])
# =============================================================================


def test_baseline_primacy_fails_closed_without_baseline() -> None:
    """[Invariant 1] Detector must raise BaselineMismatchError if baseline is None or invalid."""
    detector = DefaultStatisticalDriftDetector()
    snap = _make_valid_telemetry_snapshot()

    with pytest.raises(
        BaselineMismatchError,
        match="Authoritative BaselineReferenceBinding is mandatory",
    ):
        detector.evaluate_drift(snap, None)  # type: ignore[arg-type]


def test_baseline_primacy_fails_closed_on_ref_mismatch() -> None:
    """[Invariant 1] Detector must reject baseline where distribution_ref does not match profile."""
    profile = _make_valid_baseline_profile()
    # Mismatch between baseline_distribution_ref and profile.distribution_ref
    with pytest.raises(
        ValidationError, match="baseline_distribution_ref.*does not match"
    ):
        BaselineReferenceBinding(
            evaluation_id=EvaluationId("eval-20260920-v1"),
            dataset_manifest_hash=ContentHash("d4e5f6a1" * 8),
            model_checkpoint_hash=ContentHash("c1c2c3c4" * 8),
            baseline_distribution_ref="eval-evidence://tampered/distributions",
            distribution_profile=profile,
            baseline_fps_p50=60.0,
            baseline_fps_p05=45.0,
            baseline_latency_p95_ms=25.0,
            baseline_latency_p99_ms=35.0,
            baseline_confidence_mean=0.88,
            baseline_class_distribution=profile.class_distribution,
        )


def test_baseline_primacy_fails_closed_without_evaluation_id() -> None:
    """[Invariant 1] BaselineReferenceBinding must strictly require non-empty evaluation_id."""
    profile = _make_valid_baseline_profile()
    with pytest.raises(ValidationError):
        BaselineReferenceBinding(
            evaluation_id=EvaluationId(""),
            dataset_manifest_hash=ContentHash("d4e5f6a1" * 8),
            model_checkpoint_hash=ContentHash("c1c2c3c4" * 8),
            baseline_distribution_ref=profile.distribution_ref,
            distribution_profile=profile,
            baseline_fps_p50=60.0,
            baseline_fps_p05=45.0,
            baseline_latency_p95_ms=25.0,
            baseline_latency_p99_ms=35.0,
            baseline_confidence_mean=0.88,
            baseline_class_distribution=profile.class_distribution,
        )


def test_no_synthetic_baseline_generation() -> None:
    """[Invariant 1 & PM Constraint] Drift detector must not silently approximate distributions."""
    detector = DefaultStatisticalDriftDetector()
    snap = _make_valid_telemetry_snapshot()
    baseline = _make_valid_baseline_binding()

    # Tampering snapshot with empty confidence histogram
    tampered_model_t = snap.model_telemetry.model_copy(
        update={"confidence_histogram": {}}
    )
    tampered_snap = snap.model_copy(update={"model_telemetry": tampered_model_t})

    with pytest.raises(
        DriftEvaluationError, match="Live model confidence_histogram is missing"
    ):
        detector.evaluate_drift(tampered_snap, baseline)


# =============================================================================
# Invariant 2: Tri-Dimensional Completeness Invariant ([P§28])
# =============================================================================


def test_snapshot_requires_all_three_dimensions() -> None:
    """[Invariant 2] UnifiedTelemetrySnapshot requires model, system, and data telemetry."""
    snap = _make_valid_telemetry_snapshot()
    assert snap.model_telemetry is not None
    assert snap.system_telemetry is not None
    assert snap.data_telemetry is not None


def test_snapshot_rejects_deployment_id_mismatch() -> None:
    """[Invariant 2] Snapshot must reject deployment_id mismatch across dimensions."""
    snap = _make_valid_telemetry_snapshot()
    tampered_sys = snap.system_telemetry.model_copy(
        update={"deployment_id": DeploymentId("dept/other-model/edge-99")}
    )
    with pytest.raises(ValidationError, match="deployment_id.*does not match"):
        UnifiedTelemetrySnapshot(
            snapshot_id=snap.snapshot_id,
            deployment_id=snap.deployment_id,
            stream_id=snap.stream_id,
            timestamp=snap.timestamp,
            model_telemetry=snap.model_telemetry,
            system_telemetry=tampered_sys,
            data_telemetry=snap.data_telemetry,
        )


def test_snapshot_rejects_stream_id_mismatch() -> None:
    """[Invariant 2] Snapshot must reject stream_id mismatch between snapshot and data telemetry."""
    snap = _make_valid_telemetry_snapshot()
    tampered_data = snap.data_telemetry.model_copy(
        update={"stream_id": "stream-mismatch-camera-02"}
    )
    with pytest.raises(ValidationError, match="stream_id.*does not match"):
        UnifiedTelemetrySnapshot(
            snapshot_id=snap.snapshot_id,
            deployment_id=snap.deployment_id,
            stream_id=snap.stream_id,
            timestamp=snap.timestamp,
            model_telemetry=snap.model_telemetry,
            system_telemetry=snap.system_telemetry,
            data_telemetry=tampered_data,
        )


# =============================================================================
# Invariant 3 & 4: Zero Autonomous Retraining & Mandatory Human Approval ([P§24], [P§31])
# =============================================================================


def test_zero_autonomous_retraining_proposal_is_advisory_only() -> None:
    """[Invariant 3 & 4] RetrainingTriggerProposal must enforce requires_human_approval=True."""
    pipeline = DefaultTelemetryPipeline()
    proposal = pipeline.formulate_retraining_proposal(
        deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
        trigger_reason=TriggerReason.MODEL_ACCURACY_DRIFT,
        severity=DriftSeverity.WARNING,
        evidence_summary="Kolmogorov-Smirnov confidence drift p < 0.01; latency SLA normal.",
        estimated_new_samples=250,
    )

    assert proposal.requires_human_approval is True
    assert proposal.approved is False
    assert proposal.approved_by is None
    assert proposal.approval_timestamp is None


def test_proposal_rejects_bypassing_human_approval() -> None:
    """[Invariant 4] Setting requires_human_approval=False must fail at Pydantic validator level."""
    now = datetime.now(timezone.utc)
    with pytest.raises(
        ValidationError, match="requires_human_approval must always be True"
    ):
        RetrainingTriggerProposal(
            proposal_id=TriggerProposalId("prop-test-01"),
            deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
            created_at=now,
            trigger_reason=TriggerReason.MODEL_ACCURACY_DRIFT,
            severity=DriftSeverity.CRITICAL,
            evidence_summary="Critical drift",
            estimated_new_samples=500,
            requires_human_approval=False,  # Forbidden
        )


def test_retraining_proposal_cannot_be_approved_without_formal_authorization() -> None:
    """[Invariant 4] Setting approved=True without approval_id, approved_by, and timestamp fails validation."""
    now = datetime.now(timezone.utc)
    with pytest.raises(
        ValidationError, match="Approved proposal must specify approval_id"
    ):
        RetrainingTriggerProposal(
            proposal_id=TriggerProposalId("prop-test-02"),
            deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
            created_at=now,
            trigger_reason=TriggerReason.MODEL_ACCURACY_DRIFT,
            severity=DriftSeverity.CRITICAL,
            evidence_summary="Critical drift",
            estimated_new_samples=500,
            requires_human_approval=True,
            approved=True,
            approval_id=None,
        )


def test_approved_proposal_with_valid_metadata_succeeds() -> None:
    """[Invariant 4] Properly authorized proposal with complete approval context validates cleanly."""
    now = datetime.now(timezone.utc)
    proposal = RetrainingTriggerProposal(
        proposal_id=TriggerProposalId("prop-test-03"),
        deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
        created_at=now,
        trigger_reason=TriggerReason.MODEL_ACCURACY_DRIFT,
        severity=DriftSeverity.CRITICAL,
        evidence_summary="Critical drift authorized by PM",
        estimated_new_samples=500,
        requires_human_approval=True,
        approved=True,
        approval_id=ApprovalId("appr-retrain-001"),
        approved_by="official_pm",
        approval_timestamp=now,
    )
    assert proposal.approved is True
    assert proposal.approved_by == "official_pm"


# =============================================================================
# Invariant 5: Cryptographic Frame Lineage Invariant ([P§25], [P§26])
# =============================================================================


def test_active_learning_frame_content_hash_matches_sha256() -> None:
    """[Invariant 5] Candidate frame hash must match SHA-256 of raw bytes."""
    selector = DefaultActiveLearningSelector()
    raw_frame = b"\x00\xff\xaa\xbb" * 1024  # 4KB synthetic image payload
    now = datetime.now(timezone.utc)

    # Uncertainty candidate: top confidence 0.30
    predictions = [{"class": "person", "confidence": 0.30}]
    optical = {"blur_laplacian_variance": 100.0}

    candidate = selector.evaluate_candidate(
        frame_payload=raw_frame,
        predictions=predictions,
        optical_metrics=optical,
        stream_id="stream-cam-01",
        frame_index=42,
        timestamp=now,
        temporary_payload_uri="file:///tmp/candidate_42.raw",
    )

    assert candidate is not None
    assert len(candidate.frame_content_hash) == 64
    assert (
        candidate.strategy_applied == ActiveLearningStrategy.UNCERTAINTY_LEAST_CONFIDENT
    )
    assert candidate.priority_score > 0.60


def test_candidate_tampered_payload_hash_rejection() -> None:
    """[Invariant 5] Validating CandidateSample rejects invalid or empty payload hash."""
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        CandidateSample(
            sample_id=CandidateSampleId("samp-01"),
            stream_id="stream-01",
            frame_index=1,
            timestamp=now,
            frame_content_hash=ContentHash("invalid-not-64-hex"),
            temporary_payload_uri="file:///tmp/test.raw",
            strategy_applied=ActiveLearningStrategy.UNCERTAINTY_LEAST_CONFIDENT,
            priority_score=0.8,
        )


# =============================================================================
# Invariant 6: Dataset Authority Delegation Invariant ([P§26], ADR-0009)
# =============================================================================


def test_candidate_batch_targets_dataset_id_without_manifest_tampering() -> None:
    """[Invariant 6] Assembled batch targets DatasetId cleanly for ADR-0009 staging."""
    selector = DefaultActiveLearningSelector(max_buffer_capacity=10)
    now = datetime.now(timezone.utc)

    for i in range(5):
        raw = f"frame_{i}".encode()
        selector.evaluate_candidate(
            frame_payload=raw,
            predictions=[{"class": "person", "confidence": 0.20 + (i * 0.05)}],
            optical_metrics={"blur_laplacian_variance": 100.0},
            stream_id="stream-cam-01",
            frame_index=i,
            timestamp=now,
            temporary_payload_uri=f"file:///scratch/frame_{i}.raw",
        )

    batch = selector.assemble_batch(
        min_candidates=3,
        target_dataset_id=DatasetId("cv/dataset/facility-surveillance"),
        deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
    )

    assert batch is not None
    assert batch.total_candidates == 5
    assert batch.target_dataset_id == "cv/dataset/facility-surveillance"
    assert len(batch.sample_manifest) == 5
    assert selector.buffered_count == 0  # Admitted samples cleared from buffer


# =============================================================================
# Invariant 8: Statistically Grounded Drift Invariant ([P§29.3])
# =============================================================================


def test_ks_confidence_no_drift_when_distributions_match() -> None:
    """[Invariant 8] KS test reports no drift when live confidence matches baseline."""
    detector = DefaultStatisticalDriftDetector()
    snap = _make_valid_telemetry_snapshot()
    baseline = _make_valid_baseline_binding()

    analysis = detector.evaluate_drift(snap, baseline)
    ks_results = [
        r for r in analysis.metric_results if r.metric_name == "confidence_ks"
    ]
    assert len(ks_results) == 1
    assert ks_results[0].drift_detected is False
    assert ks_results[0].severity == DriftSeverity.INFO


def test_ks_confidence_drift_detected_on_significant_shift() -> None:
    """[Invariant 8] KS test detects significant confidence distribution collapse."""
    detector = DefaultStatisticalDriftDetector()
    baseline = _make_valid_baseline_binding()

    # Severely degraded confidence distribution: predictions shifted to [0.0-0.4]
    shifted_hist = {
        "0.0-0.1": 1500,
        "0.1-0.2": 1500,
        "0.2-0.3": 1000,
        "0.3-0.4": 500,
        "0.4-0.5": 200,
        "0.5-0.6": 100,
        "0.6-0.7": 100,
        "0.7-0.8": 50,
        "0.8-0.9": 30,
        "0.9-1.0": 20,
    }

    snap = _make_valid_telemetry_snapshot(confidence_histogram=shifted_hist)
    analysis = detector.evaluate_drift(snap, baseline)

    ks_results = [
        r for r in analysis.metric_results if r.metric_name == "confidence_ks"
    ]
    assert len(ks_results) == 1
    assert ks_results[0].drift_detected is True
    assert ks_results[0].severity in (DriftSeverity.WARNING, DriftSeverity.CRITICAL)
    assert ks_results[0].statistic_value >= 0.15


def test_psi_and_wasserstein_drift_detection() -> None:
    """[Invariant 8] PSI and Wasserstein metrics detect binned distribution collapse."""
    detector = DefaultStatisticalDriftDetector()
    baseline = _make_valid_baseline_binding()

    shifted_hist = {
        "0.0-0.1": 2000,
        "0.1-0.2": 1500,
        "0.2-0.3": 1000,
        "0.3-0.4": 300,
        "0.4-0.5": 100,
        "0.5-0.6": 50,
        "0.6-0.7": 20,
        "0.7-0.8": 15,
        "0.8-0.9": 10,
        "0.9-1.0": 5,
    }

    snap = _make_valid_telemetry_snapshot(confidence_histogram=shifted_hist)
    analysis = detector.evaluate_drift(snap, baseline)

    psi_results = [
        r for r in analysis.metric_results if r.metric_name == "confidence_psi"
    ]
    wass_results = [
        r for r in analysis.metric_results if r.metric_name == "confidence_wasserstein"
    ]

    assert len(psi_results) == 1
    assert psi_results[0].drift_detected is True
    assert psi_results[0].severity == DriftSeverity.CRITICAL

    assert len(wass_results) == 1
    assert wass_results[0].drift_detected is True


def test_chi2_class_imbalance_drift_detection() -> None:
    """[Invariant 8] Chi-square test detects severe class distribution shift."""
    detector = DefaultStatisticalDriftDetector()
    baseline = _make_valid_baseline_binding()

    # Baseline class distribution is person: 0.60, vehicle: 0.30, bicycle: 0.10
    # Live shift: vehicle 90%, person 5%, bicycle 5%
    shifted_counts = {"person": 50, "vehicle": 900, "bicycle": 50}
    shifted_props = {"person": 0.05, "vehicle": 0.90, "bicycle": 0.05}

    snap = _make_valid_telemetry_snapshot(
        class_counts=shifted_counts,
        class_proportions=shifted_props,
    )
    analysis = detector.evaluate_drift(snap, baseline)

    chi2_results = [r for r in analysis.metric_results if r.metric_name == "class_chi2"]
    assert len(chi2_results) == 1
    assert chi2_results[0].drift_detected is True
    assert chi2_results[0].p_value is not None
    assert chi2_results[0].p_value < 0.05


def test_entropy_divergence_shift_detection() -> None:
    """[Invariant 8] Prediction entropy and JS divergence shift detection."""
    detector = DefaultStatisticalDriftDetector(entropy_shift_threshold=0.20)
    baseline = _make_valid_baseline_binding()

    # High prediction entropy shift (e.g. 0.95 vs baseline 0.35)
    snap = _make_valid_telemetry_snapshot(entropy_mean=0.95)
    analysis = detector.evaluate_drift(snap, baseline)

    entropy_results = [
        r for r in analysis.metric_results if r.metric_name == "entropy_divergence"
    ]
    assert len(entropy_results) == 1
    assert entropy_results[0].drift_detected is True


def test_system_latency_sla_breach_detection() -> None:
    """[Invariant 8] System latency exceeding baseline p95 and p99 triggers SLA alerts."""
    detector = DefaultStatisticalDriftDetector()
    baseline = _make_valid_baseline_binding()
    # Baseline latency: p95 = 25ms, p99 = 35ms

    # Case 1: p95 is 28ms (>25ms but <35ms) -> WARNING
    snap_warn = _make_valid_telemetry_snapshot(latency_p95_ms=28.0)
    analysis_warn = detector.evaluate_drift(snap_warn, baseline)
    lat_warn = next(
        r for r in analysis_warn.metric_results if r.metric_name == "latency_sla_breach"
    )
    assert lat_warn.drift_detected is True
    assert lat_warn.severity == DriftSeverity.WARNING

    # Case 2: p95 is 42ms (>35ms ceiling) -> CRITICAL
    snap_crit = _make_valid_telemetry_snapshot(latency_p95_ms=42.0)
    analysis_crit = detector.evaluate_drift(snap_crit, baseline)
    lat_crit = next(
        r for r in analysis_crit.metric_results if r.metric_name == "latency_sla_breach"
    )
    assert lat_crit.drift_detected is True
    assert lat_crit.severity == DriftSeverity.CRITICAL


def test_system_throughput_fps_deficit_detection() -> None:
    """[Invariant 8] Throughput dropping below baseline p05 triggers deficit alert."""
    detector = DefaultStatisticalDriftDetector()
    baseline = _make_valid_baseline_binding()
    # Baseline throughput: p50 = 60.0, p05 = 45.0

    # Case: live FPS is 30.0 (< 45.0 floor) -> CRITICAL
    snap = _make_valid_telemetry_snapshot(throughput_fps=30.0)
    analysis = detector.evaluate_drift(snap, baseline)
    fps_res = next(
        r for r in analysis.metric_results if r.metric_name == "throughput_fps_deficit"
    )
    assert fps_res.drift_detected is True
    assert fps_res.severity == DriftSeverity.CRITICAL


def test_optical_blur_and_occlusion_alerts() -> None:
    """[Invariant 8] Data dimension alerts on optical blur and camera occlusion."""
    detector = DefaultStatisticalDriftDetector()
    baseline = _make_valid_baseline_binding()

    # Camera occluded and blurry
    snap = _make_valid_telemetry_snapshot(blur_variance=15.0, occlusion=True)
    analysis = detector.evaluate_drift(snap, baseline)

    blur_res = next(
        r for r in analysis.metric_results if r.metric_name == "optical_blur"
    )
    occ_res = next(
        r for r in analysis.metric_results if r.metric_name == "camera_occlusion"
    )

    assert blur_res.drift_detected is True
    assert occ_res.drift_detected is True
    assert occ_res.severity == DriftSeverity.CRITICAL


# =============================================================================
# Invariant 9: Bounded Storage Buffer Invariant ([P§28])
# =============================================================================


def test_active_learning_buffer_capacity_and_deterministic_eviction() -> None:
    """[Invariant 9] Bounded buffer enforces capacity limits and evicts lowest-priority samples."""
    selector = DefaultActiveLearningSelector(max_buffer_capacity=3)
    now = datetime.now(timezone.utc)

    # Insert 3 candidates with distinct priorities:
    # Frame 1: least confident (score = 1.0 - 0.40 = 0.60)
    selector.evaluate_candidate(
        frame_payload=b"frame1",
        predictions=[{"class": "person", "confidence": 0.40}],
        optical_metrics={},
        stream_id="stream-01",
        frame_index=1,
        timestamp=now,
        temporary_payload_uri="file:///tmp/f1.raw",
    )
    # Frame 2: least confident (score = 1.0 - 0.30 = 0.70)
    selector.evaluate_candidate(
        frame_payload=b"frame2",
        predictions=[{"class": "person", "confidence": 0.30}],
        optical_metrics={},
        stream_id="stream-01",
        frame_index=2,
        timestamp=now,
        temporary_payload_uri="file:///tmp/f2.raw",
    )
    # Frame 3: least confident (score = 1.0 - 0.20 = 0.80)
    selector.evaluate_candidate(
        frame_payload=b"frame3",
        predictions=[{"class": "person", "confidence": 0.20}],
        optical_metrics={},
        stream_id="stream-01",
        frame_index=3,
        timestamp=now,
        temporary_payload_uri="file:///tmp/f3.raw",
    )
    assert selector.buffered_count == 3

    # Insert Frame 4 with higher priority than Frame 1 (score = 1.0 - 0.10 = 0.90)
    # Must evict Frame 1 (score 0.60)
    selector.evaluate_candidate(
        frame_payload=b"frame4",
        predictions=[{"class": "person", "confidence": 0.10}],
        optical_metrics={},
        stream_id="stream-01",
        frame_index=4,
        timestamp=now,
        temporary_payload_uri="file:///tmp/f4.raw",
    )
    assert selector.buffered_count == 3

    # Now assemble batch and verify Frame 1 was evicted while Frames 2, 3, 4 remain
    batch = selector.assemble_batch(
        min_candidates=3,
        target_dataset_id=DatasetId("cv/dataset/active-learning"),
        deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
    )
    assert batch is not None
    indices = [s.frame_index for s in batch.sample_manifest]
    assert 1 not in indices
    assert set(indices) == {2, 3, 4}


def test_temporal_eviction_purges_expired_candidates() -> None:
    """[Invariant 9] Selector purges candidate whose expires_at is past."""
    selector = DefaultActiveLearningSelector()
    now = datetime.now(timezone.utc)
    expired_time = now - timedelta(seconds=10)

    # Evaluating a candidate that already expired returns None
    res = selector.evaluate_candidate(
        frame_payload=b"expired_frame",
        predictions=[{"class": "person", "confidence": 0.10}],
        optical_metrics={},
        stream_id="stream-01",
        frame_index=100,
        timestamp=now,
        temporary_payload_uri="file:///tmp/expired.raw",
        expires_at=expired_time,
    )
    assert res is None
    assert selector.buffered_count == 0


def test_batch_assembly_with_insufficient_candidates_returns_none() -> None:
    """[Invariant 9] Assembly returns None if buffered count < min_candidates."""
    selector = DefaultActiveLearningSelector()
    now = datetime.now(timezone.utc)

    selector.evaluate_candidate(
        frame_payload=b"frame_one",
        predictions=[{"class": "person", "confidence": 0.10}],
        optical_metrics={},
        stream_id="stream-01",
        frame_index=1,
        timestamp=now,
        temporary_payload_uri="file:///tmp/f1.raw",
    )
    assert selector.buffered_count == 1

    # Request min_candidates = 5 -> should return None
    batch = selector.assemble_batch(
        min_candidates=5,
        target_dataset_id=DatasetId("cv/dataset/al"),
        deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
    )
    assert batch is None
    assert selector.buffered_count == 1  # Unassembled candidate remains in buffer


# =============================================================================
# Invariant 10: Hardware-Aware Edge Viability Invariant ([P§13], [P§29.4])
# =============================================================================


def test_drift_evaluation_performance_sub_millisecond() -> None:
    """[Invariant 10] Statistical drift detection execution must consume negligible compute (< 15ms)."""
    detector = DefaultStatisticalDriftDetector()
    snap = _make_valid_telemetry_snapshot()
    baseline = _make_valid_baseline_binding()

    # Warmup
    for _ in range(5):
        detector.evaluate_drift(snap, baseline)

    start = time.perf_counter()
    iterations = 20
    for _ in range(iterations):
        detector.evaluate_drift(snap, baseline)
    elapsed_ms = ((time.perf_counter() - start) / iterations) * 1000.0

    assert elapsed_ms < 15.0, (
        f"Drift evaluation took {elapsed_ms:.2f}ms; exceeds edge viability threshold"
    )


# =============================================================================
# Invariant 11: Immutability of Audit Records Invariant ([P§25])
# =============================================================================


def test_telemetry_records_are_frozen() -> None:
    """[Invariant 11] All domain models must be strictly immutable Pydantic models."""
    snap = _make_valid_telemetry_snapshot()
    with pytest.raises(ValidationError):
        snap.deployment_id = DeploymentId("dept/tampered/id")  # type: ignore[misc]

    with pytest.raises(ValidationError):
        snap.model_telemetry.total_inferences = 999999  # type: ignore[misc]

    with pytest.raises(ValidationError):
        snap.system_telemetry.throughput_fps = 1.0  # type: ignore[misc]


# =============================================================================
# Invariant 12: Transport Agnosticism Invariant (ADR-0005)
# =============================================================================


def test_pipeline_transport_agnostic_ingestion_and_alert_emission() -> None:
    """[Invariant 12] Telemetry pipeline accepts in-memory records and emits alerts/proposals."""
    alerts_received: list[MonitoringAlertRecord] = []
    proposals_received: list[RetrainingTriggerProposal] = []

    pipeline = DefaultTelemetryPipeline(
        alert_sink=lambda a: alerts_received.append(a),
        proposal_sink=lambda p: proposals_received.append(p),
    )

    snap = _make_valid_telemetry_snapshot(latency_p95_ms=45.0)  # Breaches SLA
    pipeline.ingest_model_telemetry(snap.model_telemetry)
    pipeline.ingest_system_telemetry(snap.system_telemetry)
    pipeline.ingest_data_telemetry(snap.data_telemetry)

    baseline = _make_valid_baseline_binding()
    analysis = pipeline.process_and_evaluate(snap, baseline)

    assert analysis.overall_drift_detected is True
    assert len(pipeline.emitted_alerts) > 0
    assert len(alerts_received) > 0

    # Verify mitigation recommendation for latency SLA breach
    lat_alert = next((a for a in alerts_received if "latency" in a.message), None)
    assert lat_alert is not None
    assert "rollback" in lat_alert.recommended_mitigation.lower()


# =============================================================================
# Cross-ADR Seams: ADR-0011 Factory Helper
# =============================================================================


def test_extract_baseline_reference_binding_from_evaluation_result() -> None:
    """Verify factory helper constructs authoritative BaselineReferenceBinding from EvaluationResult."""
    plan = EvaluationPlan(
        evaluation_id=EvaluationId("eval-20260920-offline-v1"),
        experiment_id=ExperimentId("exp-20260920-v1"),
        created_at=datetime.now(timezone.utc),
        capability_id=CapabilityId("cv.eval.detector"),
        dataset_binding=EvaluationDatasetBinding(
            dataset_id=DatasetId("cv/dataset/eval-set"),
            dataset_version=DatasetVersion("v1.0.0"),
            split_name="test",
            dataset_manifest_hash=ContentHash("1" * 64),
        ),
        model_artifact=ModelArtifactReference(
            checkpoint_id=CheckpointId("ckpt-20260920-v1"),
            architecture_name="yolov8x",
            weights_hash=ContentHash("2" * 64),
            weights_uri="file:///models/yolov8x.pt",
            precision=TrainingPrecision.FP32,
            input_resolution=(640, 640),
        ),
        task_type=TaskType.DETECTION,
        metrics=(
            MetricDefinition(
                metric_id=MetricId("conf_mean"),
                name="confidence_score_mean",
                task_type=TaskType.DETECTION,
            ),
        ),
    )

    bm = BenchmarkResult(
        benchmark_id=BenchmarkId("bm-sys-orin-01"),
        batch_size=1,
        precision=TrainingPrecision.FP16_AMP,
        latency_min_ms=10.0,
        latency_mean_ms=12.0,
        latency_p50_ms=12.0,
        latency_p90_ms=14.0,
        latency_p95_ms=15.0,
        latency_p99_ms=18.0,
        latency_max_ms=20.0,
        throughput_fps=65.0,
        device_name="jetson_orin_nano",
        target_id=TargetId("local_jetson"),
        timing_synchronized=True,
        timing_source="cuda_event",
    )

    profile = BaselineDistributionProfile(
        distribution_ref="eval-evidence://eval-20260920-offline-v1/distributions",
        confidence_histogram={"0.0-0.5": 100, "0.5-1.0": 900},
        confidence_quantiles=(0.2, 0.5, 0.7, 0.9),
        class_distribution={"person": 0.70, "vehicle": 0.30},
        prediction_entropy_mean=0.28,
        sample_count=1000,
    )
    expected_evidence_hash = compute_distribution_profile_hash(profile)

    qm = MetricResult(
        metric=plan.metrics[0],
        primary_value=0.89,
        sample_count=1000,
        metadata={"evidence_hash": str(expected_evidence_hash)},
    )

    env = EvaluationEnvironmentRecord(
        git_commit_sha="c15a68d",
        git_is_dirty=False,
        dataset_manifest_hash=plan.dataset_binding.dataset_manifest_hash,
        weights_hash=plan.model_artifact.weights_hash,
        metric_definition_versions=("v1",),
        python_version="3.11.0",
        torch_version="2.4.0",
        target_hardware="jetson_orin_nano",
    )

    eval_result = EvaluationResult(
        evaluation_id=plan.evaluation_id,
        plan=plan,
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(qm,),
        system_benchmarks=(bm,),
        environment=env,
        completed_at=datetime.now(timezone.utc),
    )

    binding = extract_baseline_reference_binding(
        evaluation_result=eval_result,
        distribution_profile=profile,
        baseline_fps_p05=45.0,
    )

    assert binding.evaluation_id == "eval-20260920-offline-v1"
    assert binding.baseline_fps_p50 == 65.0
    assert binding.baseline_fps_p05 == 45.0
    assert binding.baseline_latency_p95_ms == 15.0
    assert binding.baseline_confidence_mean == 0.89
    assert binding.distribution_profile.sample_count == 1000
    assert binding.distribution_profile.class_distribution["person"] == 0.70
    assert binding.evidence_hash == expected_evidence_hash


# =============================================================================
# AST Audits: Zero Subprocess, OS.System, SQLite, or Framework Leaks
# =============================================================================


def test_ast_audit_zero_subprocess_or_os_system_in_monitoring() -> None:
    """[AST Audit] Verify cv_agent/monitoring/ contains zero subprocess or shell calls."""
    monitoring_dir = Path(__file__).resolve().parent.parent / "cv_agent" / "monitoring"
    forbidden_calls = {"system", "popen", "spawn", "exec", "eval"}

    for py_file in monitoring_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text("utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "subprocess", (
                        f"Forbidden subprocess import in {py_file}"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "subprocess", (
                    f"Forbidden subprocess import in {py_file}"
                )
            elif isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                assert func_name not in forbidden_calls, (
                    f"Forbidden call '{func_name}' in {py_file}"
                )


def test_ast_audit_zero_sqlite_in_monitoring() -> None:
    """[AST Audit] Verify cv_agent/monitoring/ contains zero direct sqlite3 imports."""
    monitoring_dir = Path(__file__).resolve().parent.parent / "cv_agent" / "monitoring"
    for py_file in monitoring_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text("utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "sqlite3", (
                        f"Forbidden sqlite3 import in {py_file}"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "sqlite3", (
                    f"Forbidden sqlite3 import in {py_file}"
                )


def test_ast_audit_zero_pytorch_tensorflow_imports_in_monitoring() -> None:
    """[AST Audit] Verify cv_agent/monitoring/ contains zero PyTorch or TensorFlow imports."""
    monitoring_dir = Path(__file__).resolve().parent.parent / "cv_agent" / "monitoring"
    forbidden_modules = {"torch", "torchvision", "tensorflow", "tensorrt"}
    for py_file in monitoring_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text("utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    base_mod = alias.name.split(".")[0]
                    assert base_mod not in forbidden_modules, (
                        f"Forbidden import '{alias.name}' in {py_file}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                base_mod = node.module.split(".")[0]
                assert base_mod not in forbidden_modules, (
                    f"Forbidden import from '{node.module}' in {py_file}"
                )


# =============================================================================
# Additional Adversarial Boundary Tests (Contract Edge Cases & Defense-in-Depth)
# =============================================================================


def test_baseline_binding_rejects_fps_p05_exceeding_p50() -> None:
    """Verify BaselineReferenceBinding rejects baseline_fps_p05 > baseline_fps_p50."""
    profile = _make_valid_baseline_profile()
    with pytest.raises(ValidationError, match="baseline_fps_p05.*cannot exceed"):
        BaselineReferenceBinding(
            evaluation_id=EvaluationId("eval-20260920-v1"),
            dataset_manifest_hash=ContentHash("d4e5f6a1" * 8),
            model_checkpoint_hash=ContentHash("c1c2c3c4" * 8),
            baseline_distribution_ref=profile.distribution_ref,
            distribution_profile=profile,
            baseline_fps_p50=50.0,
            baseline_fps_p05=60.0,  # Invalid: p05 > p50
            baseline_latency_p95_ms=25.0,
            baseline_latency_p99_ms=35.0,
            baseline_confidence_mean=0.88,
            baseline_class_distribution=profile.class_distribution,
        )


def test_baseline_binding_rejects_latency_p95_exceeding_p99() -> None:
    """Verify BaselineReferenceBinding rejects baseline_latency_p95_ms > baseline_latency_p99_ms."""
    profile = _make_valid_baseline_profile()
    with pytest.raises(ValidationError, match="baseline_latency_p95_ms.*cannot exceed"):
        BaselineReferenceBinding(
            evaluation_id=EvaluationId("eval-20260920-v1"),
            dataset_manifest_hash=ContentHash("d4e5f6a1" * 8),
            model_checkpoint_hash=ContentHash("c1c2c3c4" * 8),
            baseline_distribution_ref=profile.distribution_ref,
            distribution_profile=profile,
            baseline_fps_p50=60.0,
            baseline_fps_p05=45.0,
            baseline_latency_p95_ms=45.0,  # Invalid: p95 > p99
            baseline_latency_p99_ms=35.0,
            baseline_confidence_mean=0.88,
            baseline_class_distribution=profile.class_distribution,
        )


def test_baseline_profile_rejects_non_monotonic_quantiles() -> None:
    """Verify BaselineDistributionProfile rejects non-monotonic confidence quantiles."""
    with pytest.raises(
        ValidationError,
        match="confidence_quantiles must be monotonically non-decreasing",
    ):
        BaselineDistributionProfile(
            distribution_ref="eval-evidence://eval-20260920/distributions",
            confidence_histogram={"0.0-1.0": 100},
            confidence_quantiles=(0.8, 0.5),  # Non-monotonic: 0.8 > 0.5
            class_distribution={"person": 1.0},
            prediction_entropy_mean=0.2,
            sample_count=100,
        )


def test_baseline_profile_rejects_class_distribution_not_summing_to_one() -> None:
    """Verify BaselineDistributionProfile rejects class distributions that do not sum to 1.0."""
    with pytest.raises(ValidationError, match="class_distribution must sum to 1.0"):
        BaselineDistributionProfile(
            distribution_ref="eval-evidence://eval-20260920/distributions",
            confidence_histogram={"0.0-1.0": 100},
            confidence_quantiles=(0.5, 0.9),
            class_distribution={"person": 0.40, "vehicle": 0.20},  # Sum = 0.60
            prediction_entropy_mean=0.2,
            sample_count=100,
        )


def test_model_telemetry_rejects_invalid_window_ordering() -> None:
    """Verify ModelTelemetryRecord rejects window_end <= window_start."""
    now = datetime.now(timezone.utc)
    with pytest.raises(
        ValidationError, match="window_end.*must be strictly after window_start"
    ):
        ModelTelemetryRecord(
            deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
            window_start=now,
            window_end=now - timedelta(seconds=10),
            window_seconds=10.0,
            total_inferences=100,
            confidence_histogram={"0.0-1.0": 100},
            confidence_mean=0.8,
            confidence_p10=0.6,
            confidence_p50=0.8,
            confidence_p90=0.9,
            class_counts={"person": 100},
            class_proportions={"person": 1.0},
            mean_detections_per_frame=1.0,
            zero_detection_frame_ratio=0.1,
            prediction_entropy_mean=0.2,
        )


def test_model_telemetry_rejects_non_monotonic_confidence_percentiles() -> None:
    """Verify ModelTelemetryRecord rejects p10 > p50 or p50 > p90."""
    now = datetime.now(timezone.utc)
    with pytest.raises(
        ValidationError, match="Confidence percentiles must satisfy p10 <= p50 <= p90"
    ):
        ModelTelemetryRecord(
            deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
            window_start=now - timedelta(seconds=60),
            window_end=now,
            window_seconds=60.0,
            total_inferences=100,
            confidence_histogram={"0.0-1.0": 100},
            confidence_mean=0.8,
            confidence_p10=0.95,  # Invalid: p10 > p50
            confidence_p50=0.80,
            confidence_p90=0.90,
            class_counts={"person": 100},
            class_proportions={"person": 1.0},
            mean_detections_per_frame=1.0,
            zero_detection_frame_ratio=0.1,
            prediction_entropy_mean=0.2,
        )


def test_model_telemetry_rejects_invalid_class_proportions() -> None:
    """Verify ModelTelemetryRecord rejects class proportions not summing to 1.0."""
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="class_proportions must sum to 1.0"):
        ModelTelemetryRecord(
            deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
            window_start=now - timedelta(seconds=60),
            window_end=now,
            window_seconds=60.0,
            total_inferences=100,
            confidence_histogram={"0.0-1.0": 100},
            confidence_mean=0.8,
            confidence_p10=0.5,
            confidence_p50=0.8,
            confidence_p90=0.9,
            class_counts={"person": 100},
            class_proportions={"person": 0.45},  # Sum = 0.45 != 1.0
            mean_detections_per_frame=1.0,
            zero_detection_frame_ratio=0.1,
            prediction_entropy_mean=0.2,
        )


def test_system_telemetry_rejects_non_monotonic_latency() -> None:
    """Verify SystemTelemetryRecord rejects non-monotonic latency percentiles."""
    now = datetime.now(timezone.utc)
    with pytest.raises(
        ValidationError, match="Latency percentiles must satisfy p50 <= p95 <= p99"
    ):
        SystemTelemetryRecord(
            deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
            timestamp=now,
            hardware_target="jetson_orin_nano",
            throughput_fps=60.0,
            latency_p50_ms=30.0,
            latency_p95_ms=20.0,  # Invalid: p50 > p95
            latency_p99_ms=40.0,
            dropped_frames_total=0,
            dropped_frame_ratio=0.0,
            gpu_utilization_pct=50.0,
            cpu_utilization_pct=40.0,
            vram_used_mb=1000.0,
            vram_total_mb=8000.0,
            ram_used_mb=2000.0,
            gpu_temperature_celsius=50.0,
        )


def test_system_telemetry_rejects_vram_exceeding_total() -> None:
    """Verify SystemTelemetryRecord rejects vram_used_mb > vram_total_mb."""
    now = datetime.now(timezone.utc)
    with pytest.raises(
        ValidationError, match="vram_used_mb.*cannot exceed vram_total_mb"
    ):
        SystemTelemetryRecord(
            deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
            timestamp=now,
            hardware_target="jetson_orin_nano",
            throughput_fps=60.0,
            latency_p50_ms=10.0,
            latency_p95_ms=20.0,
            latency_p99_ms=30.0,
            dropped_frames_total=0,
            dropped_frame_ratio=0.0,
            gpu_utilization_pct=50.0,
            cpu_utilization_pct=40.0,
            vram_used_mb=9000.0,  # Invalid: used > total
            vram_total_mb=8000.0,
            ram_used_mb=2000.0,
            gpu_temperature_celsius=50.0,
        )


def test_data_telemetry_rejects_exposure_sum_exceeding_one() -> None:
    """Verify DataTelemetryRecord rejects overexposure + underexposure > 1.0."""
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="Sum of overexposure.*cannot exceed 1.0"):
        DataTelemetryRecord(
            stream_id="stream-01",
            camera_id="cam-01",
            timestamp=now,
            mean_luminance=100.0,
            contrast_std=30.0,
            blur_laplacian_variance=80.0,
            overexposure_ratio=0.60,
            underexposure_ratio=0.60,  # Sum = 1.20 > 1.0
            resolution_width=1920,
            resolution_height=1080,
        )


def test_proposal_rejects_unapproved_with_approved_metadata() -> None:
    """Verify RetrainingTriggerProposal rejects unapproved proposals with approved_by set."""
    now = datetime.now(timezone.utc)
    with pytest.raises(
        ValidationError, match="Unapproved proposal cannot specify approved_by"
    ):
        RetrainingTriggerProposal(
            proposal_id=TriggerProposalId("prop-01"),
            deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
            created_at=now,
            trigger_reason=TriggerReason.MODEL_ACCURACY_DRIFT,
            severity=DriftSeverity.CRITICAL,
            evidence_summary="Drift",
            estimated_new_samples=100,
            requires_human_approval=True,
            approved=False,
            approved_by="sneaky_operator",  # Invalid when approved is False
        )


def test_active_learning_selector_rejects_empty_inputs() -> None:
    """Verify DefaultActiveLearningSelector rejects empty payload, stream_id, or payload_uri."""
    selector = DefaultActiveLearningSelector()
    now = datetime.now(timezone.utc)

    with pytest.raises(
        ActiveLearningBufferError, match="frame_payload cannot be empty"
    ):
        selector.evaluate_candidate(
            frame_payload=b"",
            predictions=[],
            optical_metrics={},
            stream_id="stream-01",
            frame_index=1,
            timestamp=now,
            temporary_payload_uri="file:///tmp/f.raw",
        )

    with pytest.raises(ActiveLearningBufferError, match="stream_id cannot be empty"):
        selector.evaluate_candidate(
            frame_payload=b"bytes",
            predictions=[],
            optical_metrics={},
            stream_id="",
            frame_index=1,
            timestamp=now,
            temporary_payload_uri="file:///tmp/f.raw",
        )


def test_active_learning_buffer_drops_lower_priority_when_full() -> None:
    """Verify candidate with lower priority than buffer minimum is dropped when full."""
    selector = DefaultActiveLearningSelector(max_buffer_capacity=2)
    now = datetime.now(timezone.utc)

    # Admit sample 1 (score = 1.0 - 0.20 = 0.80)
    selector.evaluate_candidate(
        frame_payload=b"f1",
        predictions=[{"class": "person", "confidence": 0.20}],
        optical_metrics={},
        stream_id="stream-01",
        frame_index=1,
        timestamp=now,
        temporary_payload_uri="file:///tmp/f1.raw",
    )
    # Admit sample 2 (score = 1.0 - 0.10 = 0.90)
    selector.evaluate_candidate(
        frame_payload=b"f2",
        predictions=[{"class": "person", "confidence": 0.10}],
        optical_metrics={},
        stream_id="stream-01",
        frame_index=2,
        timestamp=now,
        temporary_payload_uri="file:///tmp/f2.raw",
    )
    assert selector.buffered_count == 2

    # Attempt to admit sample 3 with lower priority (score = 1.0 - 0.35 = 0.65)
    selector.evaluate_candidate(
        frame_payload=b"f3",
        predictions=[{"class": "person", "confidence": 0.35}],
        optical_metrics={},
        stream_id="stream-01",
        frame_index=3,
        timestamp=now,
        temporary_payload_uri="file:///tmp/f3.raw",
    )

    batch = selector.assemble_batch(
        min_candidates=2,
        target_dataset_id=DatasetId("cv/dataset/al"),
        deployment_id=DeploymentId("dept/yolo-v8/edge-01"),
    )
    assert batch is not None
    indices = [s.frame_index for s in batch.sample_manifest]
    # Sample 3 was dropped because its priority (0.65) is lower than 0.80 and 0.90
    assert 3 not in indices
    assert set(indices) == {1, 2}


def test_deep_freeze_ensures_runtime_immutability() -> None:
    """Verify mapping fields on telemetry records are MappingProxyType and cannot be modified."""
    snap = _make_valid_telemetry_snapshot()
    with pytest.raises(TypeError):
        snap.model_telemetry.class_counts["person"] = 9999  # type: ignore[index]

    with pytest.raises(TypeError):
        snap.model_telemetry.confidence_histogram["0.0-0.1"] = 1000  # type: ignore[index]


# =============================================================================
# Adversarial Regression Tests for Official PM Blockers B19–B22
# =============================================================================


def test_b19_zero_synthetic_baseline_fallbacks() -> None:
    """[B19] Verify fail-closed behavior on missing baseline evidence; zero synthetic fallbacks."""
    plan = EvaluationPlan(
        evaluation_id=EvaluationId("eval-20260920-b19-failclosed"),
        experiment_id=ExperimentId("exp-20260920-b19"),
        created_at=datetime.now(timezone.utc),
        capability_id=CapabilityId("cv.eval.detector"),
        dataset_binding=EvaluationDatasetBinding(
            dataset_id=DatasetId("cv/dataset/eval-set"),
            dataset_version=DatasetVersion("v1.0.0"),
            split_name="test",
            dataset_manifest_hash=ContentHash("1" * 64),
        ),
        model_artifact=ModelArtifactReference(
            checkpoint_id=CheckpointId("ckpt-20260920-b19"),
            architecture_name="yolov8x",
            weights_hash=ContentHash("2" * 64),
            weights_uri="file:///models/yolov8x.pt",
            precision=TrainingPrecision.FP32,
            input_resolution=(640, 640),
        ),
        task_type=TaskType.DETECTION,
        metrics=(
            MetricDefinition(
                metric_id=MetricId("conf_metric"),
                name="confidence_score_mean",
                task_type=TaskType.DETECTION,
            ),
        ),
    )

    bm = BenchmarkResult(
        benchmark_id=BenchmarkId("bm-sys-orin-01"),
        batch_size=1,
        precision=TrainingPrecision.FP16_AMP,
        latency_min_ms=10.0,
        latency_mean_ms=12.0,
        latency_p50_ms=12.0,
        latency_p90_ms=14.0,
        latency_p95_ms=15.0,
        latency_p99_ms=18.0,
        latency_max_ms=20.0,
        throughput_fps=65.0,
        device_name="jetson_orin_nano",
        target_id=TargetId("local_jetson"),
        timing_synchronized=True,
        timing_source="cuda_event",
    )

    profile = BaselineDistributionProfile(
        distribution_ref="eval-evidence://eval-20260920-b19-failclosed/distributions",
        confidence_histogram={"0.0-0.5": 100, "0.5-1.0": 900},
        confidence_quantiles=(0.2, 0.5, 0.7, 0.9),
        class_distribution={"person": 0.70, "vehicle": 0.30},
        prediction_entropy_mean=0.28,
        sample_count=1000,
    )
    profile_hash = compute_distribution_profile_hash(profile)

    qm = MetricResult(
        metric=plan.metrics[0],
        primary_value=0.88,
        sample_count=1000,
        metadata={"evidence_hash": str(profile_hash)},
    )

    env = EvaluationEnvironmentRecord(
        git_commit_sha="c15a68d",
        git_is_dirty=False,
        dataset_manifest_hash=plan.dataset_binding.dataset_manifest_hash,
        weights_hash=plan.model_artifact.weights_hash,
        metric_definition_versions=("v1",),
        python_version="3.11.0",
        torch_version="2.4.0",
        target_hardware="jetson_orin_nano",
    )

    # 1. Missing system_benchmarks -> fail closed (no 30.0 / 20.0 / 35.0 defaults)
    eval_no_bm = EvaluationResult(
        evaluation_id=plan.evaluation_id,
        plan=plan,
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(qm,),
        system_benchmarks=(),
        environment=env,
        completed_at=datetime.now(timezone.utc),
    )
    with pytest.raises(BaselineMismatchError, match="system_benchmarks evidence"):
        extract_baseline_reference_binding(
            eval_no_bm,
            profile,
            baseline_fps_p05=45.0,
        )

    # 2a. Zero or negative baseline_fps_p05 -> fail closed (never infer from throughput * 0.70)
    eval_with_bm = EvaluationResult(
        evaluation_id=plan.evaluation_id,
        plan=plan,
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(qm,),
        system_benchmarks=(bm,),
        environment=env,
        completed_at=datetime.now(timezone.utc),
    )
    with pytest.raises(
        BaselineMismatchError, match="baseline_fps_p05 must be explicitly provided"
    ):
        extract_baseline_reference_binding(
            eval_with_bm,
            profile,
            baseline_fps_p05=0.0,
        )

    # 2b. Omitting baseline_fps_p05 argument entirely -> raises typed BaselineMismatchError (not TypeError)
    with pytest.raises(
        BaselineMismatchError, match="baseline_fps_p05 must be explicitly provided"
    ):
        extract_baseline_reference_binding(
            eval_with_bm,
            profile,
        )

    # 3. baseline_fps_p05 exceeding baseline_fps_p50 -> fail closed
    with pytest.raises(BaselineMismatchError, match="cannot exceed baseline_fps_p50"):
        extract_baseline_reference_binding(
            eval_with_bm,
            profile,
            baseline_fps_p05=70.0,  # 70 > bm.throughput_fps (65.0)
        )

    # 4. Missing confidence metric in quality_metrics -> fail closed (no 0.85 default)
    plan_no_conf = plan.model_copy(
        update={
            "metrics": (
                MetricDefinition(
                    metric_id=MetricId("map_50"),
                    name="mean_average_precision_50",
                    task_type=TaskType.DETECTION,
                ),
            )
        }
    )
    qm_no_conf = MetricResult(
        metric=plan_no_conf.metrics[0],
        primary_value=0.75,
        sample_count=1000,
    )
    eval_no_conf = EvaluationResult(
        evaluation_id=plan.evaluation_id,
        plan=plan_no_conf,
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(qm_no_conf,),
        system_benchmarks=(bm,),
        environment=env,
        completed_at=datetime.now(timezone.utc),
    )
    with pytest.raises(
        BaselineMismatchError, match="no authoritative confidence metric"
    ):
        extract_baseline_reference_binding(
            eval_no_conf,
            profile,
            baseline_fps_p05=45.0,
        )


def test_b20_histogram_interval_validation_and_piecewise_wasserstein() -> None:
    """[B20] Verify numeric interval validation, contiguity checks, and exact piecewise-constant Wasserstein integral."""
    detector = DefaultStatisticalDriftDetector()

    # 1. Non-numeric format or bounds fails closed
    with pytest.raises(DriftEvaluationError, match="does not match numeric format"):
        parse_numeric_histogram({"invalid_bin": 10})
    with pytest.raises(DriftEvaluationError, match="non-numeric bounds"):
        parse_numeric_histogram({"low-high": 10})

    # 2. Inverted bounds fail closed
    with pytest.raises(DriftEvaluationError, match="invalid: lower"):
        parse_numeric_histogram({"0.8-0.2": 10})

    # 3. Disjoint/non-contiguous intervals fail closed
    with pytest.raises(DriftEvaluationError, match="intervals are not contiguous"):
        parse_numeric_histogram({"0.0-0.4": 10, "0.5-1.0": 20})

    # 4. Negative count fails closed
    with pytest.raises(DriftEvaluationError, match="must be non-negative"):
        parse_numeric_histogram({"0.0-0.5": -5, "0.5-1.0": 10})

    # 5. Out of [0.0, 1.0] bounds fails closed
    with pytest.raises(DriftEvaluationError, match="bounds must be within"):
        parse_numeric_histogram({"0.0-1.2": 10})

    # 6. Exact piecewise-linear CDF integral with unequal bin widths
    # Distribution 1: 100% mass in [0.0, 0.4] (uniform-within-bin CDF: F1(x) = x/0.4 on [0, 0.4], 1 on [0.4, 1.0])
    # Distribution 2: 100% mass in [0.4, 1.0] (uniform-within-bin CDF: F2(x) = 0 on [0, 0.4], (x-0.4)/0.6 on [0.4, 1.0])
    # Analytically on [0.0, 0.4]: Delta x = 0.4, |F1 - F2| grows linearly from 0 to 1 -> integral = 0.5 * 0.4 * 1.0 = 0.20
    # Analytically on [0.4, 1.0]: Delta x = 0.6, |F1 - F2| decreases linearly from 1 to 0 -> integral = 0.5 * 0.6 * 1.0 = 0.30
    # Exact integral W_1 = 0.20 + 0.30 = 0.50 (the previous flawed endpoint-rectangle method yielded 0.40)
    hist1 = {"0.0-0.4": 100, "0.4-1.0": 0}
    hist2 = {"0.0-0.4": 0, "0.4-1.0": 100}
    wass_res = detector._compute_confidence_wasserstein(hist1, hist2)
    assert math.isclose(wass_res.statistic_value, 0.50, abs_tol=1e-3)
    assert (
        wass_res.details["mathematical_formulation"]
        == "exact_piecewise_linear_cdf_integral"
    )
    assert wass_res.details["distribution_representation"] == "uniform_within_bin"
    assert wass_res.details["sub_intervals_count"] == 2

    # 7. Regression case whose analytically expected answer is 0.20
    # (proves that endpoint-rectangle method fails: old method gave 0.10, off by 2x)
    # Distribution A: 100% mass in [0.0, 0.1] -> E[X] = 0.05
    # Distribution B: 100% mass in [0.1, 0.4] -> E[Y] = 0.25
    # Since X <= 0.1 <= Y almost surely, W_1 = E[Y] - E[X] = 0.25 - 0.05 = 0.20
    # - On [0.0, 0.1]: Delta x = 0.1, |FA - FB| grows linearly from 0 to 1 -> integral = 0.5 * 0.1 * 1.0 = 0.05
    # - On [0.1, 0.4]: Delta x = 0.3, |FA - FB| decreases linearly from 1 to 0 -> integral = 0.5 * 0.3 * 1.0 = 0.15
    # Total W_1 = 0.05 + 0.15 = 0.20
    hist_a = {"0.0-0.1": 100, "0.1-0.4": 0}
    hist_b = {"0.0-0.1": 0, "0.1-0.4": 100}
    wass_02 = detector._compute_confidence_wasserstein(hist_a, hist_b)
    assert math.isclose(wass_02.statistic_value, 0.20, abs_tol=1e-3)
    assert (
        wass_02.details["mathematical_formulation"]
        == "exact_piecewise_linear_cdf_integral"
    )
    assert wass_02.details["distribution_representation"] == "uniform_within_bin"


def test_b20_binned_cdf_ks_test_evaluation() -> None:
    """[B20] Verify KS calculation evaluates over binned empirical CDFs and preserves mathematical honesty."""
    detector = DefaultStatisticalDriftDetector()
    hist_base = {"0.0-0.5": 500, "0.5-1.0": 500}
    hist_live = {"0.0-0.5": 100, "0.5-1.0": 900}
    quantiles = (0.25, 0.5, 0.75)

    ks_res = detector._compute_confidence_ks(
        live_hist=hist_live,
        base_hist=hist_base,
        live_n=1000,
        base_n=1000,
        base_quantiles=quantiles,
    )
    # At x = 0.5: Base F = 0.50, Live F = 0.10. |0.10 - 0.50| = 0.40
    assert math.isclose(ks_res.statistic_value, 0.40, abs_tol=1e-3)
    assert ks_res.details["empirical_cdf_type"] == "reconstructed_binned_empirical_cdf"
    assert (
        ks_res.details["mathematical_characterization"]
        == "binned_empirical_cdf_comparison"
    )
    # Evaluation points: {0.5, 1.0, 0.25, 0.75} -> count 4
    assert ks_res.details["evaluation_points_count"] == 4


def test_b21_authoritative_evidence_binding_and_tamper_rejection() -> None:
    """[B21] Verify disentangled identity binding, cohort consistency, and cryptographic content integrity."""
    plan = EvaluationPlan(
        evaluation_id=EvaluationId("eval-20260920-b21-auth"),
        experiment_id=ExperimentId("exp-20260920-b21"),
        created_at=datetime.now(timezone.utc),
        capability_id=CapabilityId("cv.eval.detector"),
        dataset_binding=EvaluationDatasetBinding(
            dataset_id=DatasetId("cv/dataset/eval-set"),
            dataset_version=DatasetVersion("v1.0.0"),
            split_name="test",
            dataset_manifest_hash=ContentHash("1" * 64),
        ),
        model_artifact=ModelArtifactReference(
            checkpoint_id=CheckpointId("ckpt-20260920-b21"),
            architecture_name="yolov8x",
            weights_hash=ContentHash("2" * 64),
            weights_uri="file:///models/yolov8x.pt",
            precision=TrainingPrecision.FP32,
            input_resolution=(640, 640),
        ),
        task_type=TaskType.DETECTION,
        metrics=(
            MetricDefinition(
                metric_id=MetricId("conf_score"),
                name="confidence_score",
                task_type=TaskType.DETECTION,
            ),
        ),
    )

    bm = BenchmarkResult(
        benchmark_id=BenchmarkId("bm-sys-orin-01"),
        batch_size=1,
        precision=TrainingPrecision.FP16_AMP,
        latency_min_ms=10.0,
        latency_mean_ms=12.0,
        latency_p50_ms=12.0,
        latency_p90_ms=14.0,
        latency_p95_ms=15.0,
        latency_p99_ms=18.0,
        latency_max_ms=20.0,
        throughput_fps=65.0,
        device_name="jetson_orin_nano",
        target_id=TargetId("local_jetson"),
        timing_synchronized=True,
        timing_source="cuda_event",
    )

    qm = MetricResult(
        metric=plan.metrics[0],
        primary_value=0.88,
        sample_count=2000,
        per_class_values={"person": 0.88, "vehicle": 0.88},
    )

    env = EvaluationEnvironmentRecord(
        git_commit_sha="c15a68d",
        git_is_dirty=False,
        dataset_manifest_hash=plan.dataset_binding.dataset_manifest_hash,
        weights_hash=plan.model_artifact.weights_hash,
        metric_definition_versions=("v1",),
        python_version="3.11.0",
        torch_version="2.4.0",
        target_hardware="jetson_orin_nano",
    )

    eval_result = EvaluationResult(
        evaluation_id=plan.evaluation_id,
        plan=plan,
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(qm,),
        system_benchmarks=(bm,),
        environment=env,
        completed_at=datetime.now(timezone.utc),
    )

    # 1. Identity binding check fails when distribution_ref points to different eval_id
    foreign_profile = BaselineDistributionProfile(
        distribution_ref="eval-evidence://different-eval-id/distributions",
        confidence_histogram={"0.0-0.5": 500, "0.5-1.0": 1500},
        confidence_quantiles=(0.2, 0.5, 0.7, 0.9),
        class_distribution={"person": 0.60, "vehicle": 0.40},
        prediction_entropy_mean=0.30,
        sample_count=2000,
    )
    with pytest.raises(BaselineMismatchError, match="Identity check failed"):
        extract_baseline_reference_binding(
            eval_result,
            foreign_profile,
            baseline_fps_p05=45.0,
        )

    # 2. Cohort sample count mismatch fails closed
    count_mismatch_profile = BaselineDistributionProfile(
        distribution_ref="eval-evidence://eval-20260920-b21-auth/distributions",
        confidence_histogram={"0.0-0.5": 250, "0.5-1.0": 750},
        confidence_quantiles=(0.2, 0.5, 0.7, 0.9),
        class_distribution={"person": 0.60, "vehicle": 0.40},
        prediction_entropy_mean=0.30,
        sample_count=1000,  # 1000 != qm.sample_count (2000)
    )
    with pytest.raises(BaselineMismatchError, match="Sample count mismatch"):
        extract_baseline_reference_binding(
            eval_result,
            count_mismatch_profile,
            baseline_fps_p05=45.0,
        )

    # 3. Class vocabulary mismatch fails closed
    vocab_mismatch_profile = BaselineDistributionProfile(
        distribution_ref="eval-evidence://eval-20260920-b21-auth/distributions",
        confidence_histogram={"0.0-0.5": 500, "0.5-1.0": 1500},
        confidence_quantiles=(0.2, 0.5, 0.7, 0.9),
        class_distribution={
            "cat": 0.60,
            "dog": 0.40,
        },  # Mismatch against person/vehicle
        prediction_entropy_mean=0.30,
        sample_count=2000,
    )
    with pytest.raises(
        BaselineMismatchError, match="Class distribution vocabulary mismatch"
    ):
        extract_baseline_reference_binding(
            eval_result,
            vocab_mismatch_profile,
            baseline_fps_p05=45.0,
        )

    # 4. Caller-supplied profile with NO authoritative evidence hash cannot establish authority
    valid_profile = BaselineDistributionProfile(
        distribution_ref="eval-evidence://eval-20260920-b21-auth/distributions",
        confidence_histogram={"0.0-0.5": 500, "0.5-1.0": 1500},
        confidence_quantiles=(0.2, 0.5, 0.7, 0.9),
        class_distribution={"person": 0.60, "vehicle": 0.40},
        prediction_entropy_mean=0.30,
        sample_count=2000,
    )
    computed_hash = compute_distribution_profile_hash(valid_profile)
    profile_with_self_hash = valid_profile.model_copy(
        update={"evidence_hash": computed_hash}
    )

    # When EvaluationResult does not provide an authoritative evidence hash (and no evidence_hash param),
    # the factory FAILS CLOSED rather than self-generating an evidence hash
    with pytest.raises(
        BaselineMismatchError,
        match="does not provide an authoritative distribution evidence hash",
    ):
        extract_baseline_reference_binding(
            eval_result,
            profile_with_self_hash,
            baseline_fps_p05=45.0,
            evidence_hash=None,
        )

    # 5. Authoritative evidence hash carried directly in EvaluationResult metadata succeeds
    eval_with_auth_hash = eval_result.model_copy(
        update={
            "quality_metrics": (
                eval_result.quality_metrics[0].model_copy(
                    update={"metadata": {"evidence_hash": str(computed_hash)}}
                ),
            )
        }
    )
    binding_from_eval = extract_baseline_reference_binding(
        eval_with_auth_hash,
        valid_profile,
        baseline_fps_p05=45.0,
    )
    assert binding_from_eval.evidence_hash == computed_hash

    # 6. Explicit external authoritative evidence_hash succeeds
    binding_from_arg = extract_baseline_reference_binding(
        eval_result,
        valid_profile,
        baseline_fps_p05=45.0,
        evidence_hash=computed_hash,
    )
    assert binding_from_arg.evidence_hash == computed_hash

    # 7. Mismatch between caller explicit evidence_hash and EvaluationResult metadata fails closed
    conflicting_hash = ContentHash("e" * 64)
    with pytest.raises(
        BaselineMismatchError,
        match="does not match authoritative evidence_hash in EvaluationResult metadata",
    ):
        extract_baseline_reference_binding(
            eval_with_auth_hash,
            valid_profile,
            baseline_fps_p05=45.0,
            evidence_hash=conflicting_hash,
        )

    # 8. Tampered profile digest against authoritative evidence_hash fails closed
    tampered_hash = ContentHash("f" * 64)
    with pytest.raises(
        BaselineMismatchError,
        match="Cryptographic evidence hash verification failed",
    ):
        extract_baseline_reference_binding(
            eval_result,
            valid_profile,
            baseline_fps_p05=45.0,
            evidence_hash=tampered_hash,
        )


def test_b22_contingency_table_independence_test_and_goodness_of_fit() -> None:
    """[B22] Verify 2 x K contingency table test of independence and separate one-sample goodness-of-fit."""
    detector = DefaultStatisticalDriftDetector()
    baseline = _make_valid_baseline_binding()

    # Baseline: person: 0.60, vehicle: 0.30, bicycle: 0.10, sample_count = 5000

    # 1. Matching cohort: 600 person, 300 vehicle, 100 bicycle
    matching_snap = _make_valid_telemetry_snapshot(
        class_counts={"person": 600, "vehicle": 300, "bicycle": 100},
        class_proportions={"person": 0.60, "vehicle": 0.30, "bicycle": 0.10},
    )
    matching_analysis = detector.evaluate_drift(matching_snap, baseline)

    indep_matching = next(
        r for r in matching_analysis.metric_results if r.metric_name == "class_chi2"
    )
    gof_matching = next(
        r
        for r in matching_analysis.metric_results
        if r.metric_name == "class_chi2_goodness_of_fit"
    )

    assert indep_matching.drift_type == DriftMetricType.CHI_SQUARE_INDEPENDENCE
    assert indep_matching.drift_detected is False
    assert indep_matching.statistic_value < 1.0
    assert indep_matching.p_value is not None and indep_matching.p_value > 0.90
    assert indep_matching.details["contingency_table"] == "2x3"
    assert indep_matching.details["degrees_of_freedom"] == 2

    assert gof_matching.drift_type == DriftMetricType.CHI_SQUARE_GOODNESS_OF_FIT
    assert gof_matching.drift_detected is False
    assert gof_matching.statistic_value < 1.0
    assert gof_matching.p_value is not None and gof_matching.p_value > 0.90
    assert gof_matching.details["degrees_of_freedom"] == 2

    # 2. Severely shifted cohort: 50 person, 900 vehicle, 50 bicycle
    shifted_snap = _make_valid_telemetry_snapshot(
        class_counts={"person": 50, "vehicle": 900, "bicycle": 50},
        class_proportions={"person": 0.05, "vehicle": 0.90, "bicycle": 0.05},
    )
    shifted_analysis = detector.evaluate_drift(shifted_snap, baseline)

    indep_shifted = next(
        r for r in shifted_analysis.metric_results if r.metric_name == "class_chi2"
    )
    gof_shifted = next(
        r
        for r in shifted_analysis.metric_results
        if r.metric_name == "class_chi2_goodness_of_fit"
    )

    assert indep_shifted.drift_detected is True
    assert indep_shifted.statistic_value > 100.0
    assert indep_shifted.p_value is not None and indep_shifted.p_value < 0.001
    assert indep_shifted.severity == DriftSeverity.CRITICAL

    assert gof_shifted.drift_detected is True
    assert gof_shifted.statistic_value > 100.0
    assert gof_shifted.p_value is not None and gof_shifted.p_value < 0.001
    assert gof_shifted.severity == DriftSeverity.CRITICAL
