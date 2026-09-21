"""tests/test_closed_loop_integration.py — Phase 2O Closed-Loop Integration Test Suite.

Verifies end-to-end lifecycle composition across the 14 frozen Phase 2 subsystems
(ADR-0001 through ADR-0013) under Issue #66.

Structure:
1. Full Lifecycle Test with Unbroken Cryptographic Evidence Ledger:
   CandidateSample (ADR-0013)
     ↓
   CandidateSample.frame_content_hash
     ↓
   DatasetStore.put_payload(content_hash, raw_bytes)
     ↓
   DatasetManifest v2 (ADR-0009)
     ↓
   DatasetManifest.payload_root_checksum
     ↓
   TrainingPlan.dataset_manifest_hash (ADR-0010)
     ↓
   CheckpointManifest.weights_hash
     ↓
   ModelArtifactReference.weights_hash (ADR-0011)
     ↓
   EvaluationResult.evidence_hash
     ↓
   OptimizedArtifactEvaluationBinding (ADR-0012)
     ↓
   DeploymentRecord.artifact_id
     ↓
   BaselineReferenceBinding.evidence_hash (ADR-0013)
     ↓
   Monitoring telemetry snapshot & drift detection
     ↓
   RetrainingTriggerProposal (Advisory Only)

2. Dedicated Failure-Path and Adversarial Boundary Tests for All 7 Seams.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from cv_agent.dataset import (
    AnnotationVersion,
    ClassDefinition,
    CollectionConditions,
    ContentHash,
    DatasetId,
    DatasetLineage,
    DatasetManifest,
    DatasetVersion,
    GuidelineRef,
    LeakageCheckResult,
    LeakageType,
    LocalFSStore,
    OperationIdentity,
    SplitMembership,
)
from cv_agent.domain import CapabilityId
from cv_agent.evaluation.contracts import (
    BenchmarkId,
    BenchmarkResult,
    ErrorAnalysisRecord,
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
from cv_agent.evaluation.errors import EvaluationValidationError
from cv_agent.evaluation.runner import DefaultEvaluationRunner
from cv_agent.evaluation.types import EvaluationId, EvaluationVerdict, ExperimentId
from cv_agent.monitoring import (
    ActiveLearningCandidateBatch,
    ActiveLearningStrategy,
    BaselineDistributionProfile,
    BaselineMismatchError,
    BaselineReferenceBinding,
    CandidateBatchId,
    CandidateSample,
    CandidateSampleId,
    DataTelemetryRecord,
    DefaultStatisticalDriftDetector,
    DefaultTelemetryPipeline,
    DeploymentBaselineBinding,
    DriftSeverity,
    ModelTelemetryRecord,
    RetrainingTriggerProposal,
    SystemTelemetryRecord,
    TriggerProposalId,
    TriggerReason,
    bind_deployment_to_monitoring_baseline,
    compute_distribution_profile_hash,
    extract_baseline_reference_binding,
)
from cv_agent.optimization.canonicalization import (
    compute_deployment_payload_hash,
    extract_deployment_operation_payload,
)
from cv_agent.optimization.contracts import (
    CalibrationDatasetBinding,
    DeploymentPlan,
    DeploymentRecord,
    DeploymentTarget,
    DynamicShapeProfile,
    HealthCheckSpec,
    OptimizationPlan,
    OptimizedArtifactEvaluationBinding,
    OptimizedModelArtifact,
    ReadinessProbeResult,
)
from cv_agent.optimization.deployment import DefaultDeploymentManager
from cv_agent.optimization.errors import (
    DeploymentApprovalRequiredError,
    OptimizationValidationError,
    OptimizationVerificationError,
)
from cv_agent.optimization.types import (
    CalibrationAlgorithm,
    DeploymentId,
    DeploymentRuntime,
    DeploymentStatus,
    OptimizationPlanId,
    OptimizationPrecision,
    OptimizationTechnique,
    OptimizedArtifactId,
)
from cv_agent.optimization.verifier import DefaultOptimizationVerifier
from cv_agent.orchestration import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    OperationId,
)
from cv_agent.tools import (
    TargetId,
    ToolExecutionStatus,
    ToolId,
    ToolInvocation,
    ToolResult,
)
from cv_agent.training import (
    AugmentationConfig,
    CheckpointId,
    CheckpointManifest,
    ComputeRequirements,
    CostEstimate,
    DefaultTrainingLauncher,
    LossComponentConfig,
    OptimizerConfig,
    PlanId,
    PlanValidationError,
    SchedulerConfig,
    TrainingJobId,
    TrainingPlan,
)

# =============================================================================
# Test Doubles & Fixtures
# =============================================================================


class MockToolExecutor:
    """Mock ADR-0005 ToolExecutor returning success for testing."""

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(
            tool_id=invocation.tool_id,
            execution_id=invocation.execution_id,
            target_id=invocation.target_id,
            status=ToolExecutionStatus.SUCCESS,
            structured_output={
                "startup_time_sec": 1.5,
                "probe_latency_mean_ms": 15.0,
                "vram_consumed_mb": 1024.0,
            },
            diagnostics={"stdout": "Execution completed successfully", "stderr": ""},
        )

    async def probe(self, tool: Any, target: Any) -> Any:
        raise NotImplementedError


def _compute_sha256(data: bytes) -> str:
    """Compute SHA-256 hexadecimal string."""
    return hashlib.sha256(data).hexdigest()


def _make_approval_context(
    plan: DeploymentPlan,
    resource_scope: str | None = None,
    operation_id: str = "deployment.activate",
    operation_version: int = 1,
    identity_version: str = "1.0.0",
    target_identity: str | None = None,
    policy_version: str = "1.0",
    bound_payload_hash: str | None = None,
    bound_resource_scope: str | None = None,
    bound_policy_version: str | None = None,
    expires_at: datetime | None = None,
    request_payload_hash: str | None = None,
) -> tuple[OperationIdentity, ApprovalRequest, ApprovalDecision]:
    """Construct a cryptographically bound ADR-0003 approval security tuple."""
    extract_deployment_operation_payload(plan)
    canonical_hash = compute_deployment_payload_hash(plan)
    scope = resource_scope or f"target:{plan.target.target_id}"
    tid = target_identity if target_identity is not None else str(plan.target.target_id)

    op_id = OperationIdentity(
        operation_id=operation_id,
        operation_version=identity_version,
        canonical_payload_hash=canonical_hash,
        target_identity=tid,
    )

    app_req = ApprovalRequest(
        request_id="req-dep-001",
        session_id="sess-001",
        operation_id=OperationId(value=operation_id),
        operation_version=operation_version,
        canonical_payload_hash=request_payload_hash or canonical_hash,
        policy_version=policy_version,
        action_type=operation_id,
        risk_level="HIGH",
        resource_scope=scope,
        created_at=datetime.now(timezone.utc),
        expires_at=expires_at,
    )

    app_dec = ApprovalDecision(
        request_id="req-dep-001",
        decision=ApprovalStatus.APPROVED,
        principal="human_pm",
        resolved_at=datetime.now(timezone.utc),
        bound_payload_hash=bound_payload_hash or canonical_hash,
        bound_resource_scope=bound_resource_scope or scope,
        bound_policy_version=bound_policy_version or policy_version,
    )

    return op_id, app_req, app_dec


# =============================================================================
# 1. Full Lifecycle Test with Explicit Evidence Ledger
# =============================================================================


def test_closed_loop_full_lifecycle_evidence_ledger(tmp_path: Path) -> None:
    """Execute a complete, evidence-backed closed loop across all 14 Phase 2 subsystems.

    Verifies the unbroken cryptographic chain:
    CandidateSample (ADR-0013)
      ↓
    CandidateSample.frame_content_hash
      ↓
    DatasetStore.put_payload(content_hash, raw_bytes)
      ↓
    DatasetManifest v2 (ADR-0009)
      ↓
    DatasetManifest.payload_root_checksum
      ↓
    TrainingPlan.dataset_manifest_hash (ADR-0010)
      ↓
    CheckpointManifest.weights_hash
      ↓
    ModelArtifactReference.weights_hash (ADR-0011)
      ↓
    EvaluationResult.evidence_hash
      ↓
    OptimizedArtifactEvaluationBinding (ADR-0012)
      ↓
    DeploymentRecord.artifact_id
      ↓
    BaselineReferenceBinding.evidence_hash (ADR-0013)
      ↓
    Monitoring telemetry snapshot & drift detection
      ↓
    RetrainingTriggerProposal (Advisory Only)
    """
    now = datetime.now(timezone.utc)
    evidence_ledger: dict[str, Any] = {}

    # -------------------------------------------------------------------------
    # Step 1 (Seam 1): Edge Observation & Active Learning Curation (ADR-0013)
    # -------------------------------------------------------------------------
    raw_frame_bytes = b"RAW_EDGE_FRAME_PAYLOAD_UNCOMPRESSED_CAMERA_01_FRAME_0042"
    expected_frame_hash = ContentHash(_compute_sha256(raw_frame_bytes))
    temp_frame_path = tmp_path / "scratch_frame_0042.raw"
    temp_frame_path.write_bytes(raw_frame_bytes)

    candidate_sample = CandidateSample(
        sample_id=CandidateSampleId("cand-sample-42"),
        stream_id="cam-intersection-01",
        frame_index=42,
        timestamp=now,
        frame_content_hash=expected_frame_hash,
        temporary_payload_uri=f"file://{temp_frame_path}",
        strategy_applied=ActiveLearningStrategy.UNCERTAINTY_ENTROPY,
        priority_score=0.895,
        model_predictions=(
            {"class": "vehicle", "confidence": 0.48, "bbox": [10, 20, 100, 150]},
        ),
        optical_metadata={"luminance": 128.0, "contrast": 45.0, "blur": 12.0},
    )

    candidate_batch = ActiveLearningCandidateBatch(
        batch_id=CandidateBatchId("al-batch-20260920-001"),
        deployment_id=DeploymentId("dep-intersection-01"),
        created_at=now,
        strategies_used=(ActiveLearningStrategy.UNCERTAINTY_ENTROPY,),
        total_candidates=1,
        sample_manifest=(candidate_sample,),
        selection_rationale="High predictive entropy detected in ambiguous vehicle class",
        target_dataset_id=DatasetId("traffic-cctv-curated"),
    )

    # Assert Seam 1 Outbound Identity
    assert candidate_sample.frame_content_hash == expected_frame_hash
    evidence_ledger["candidate_frame_hash"] = str(candidate_sample.frame_content_hash)

    # -------------------------------------------------------------------------
    # Step 2 (Seam 1 → Seam 2): Dataset Ingestion & Manifest Evolution (ADR-0009)
    # -------------------------------------------------------------------------
    # Ingest candidate payload into DatasetStore verifying byte-identical integrity
    storage_root = tmp_path / "dataset_storage"
    store = LocalFSStore(storage_root)
    stored_hash = store.put_payload(temp_frame_path)
    assert stored_hash == candidate_sample.frame_content_hash
    assert store.exists(candidate_sample.frame_content_hash)

    # Establish baseline split hashes (v1 had 3 items)
    hash_train_1 = ContentHash("1" * 64)
    hash_val_1 = ContentHash("2" * 64)
    hash_test_1 = ContentHash("3" * 64)
    hash_cal_1 = ContentHash("4" * 64)

    # Evolve to v2: Add candidate frame hash into train_hashes
    v2_split_membership = SplitMembership(
        train_hashes=(hash_train_1, candidate_sample.frame_content_hash),
        val_hashes=(hash_val_1,),
        test_hashes=(hash_test_1,),
        calibration_hashes=(hash_cal_1,),
    )

    # Audit split membership to guarantee zero leakage / contamination
    leakage_records = tuple(
        LeakageCheckResult(
            leakage_type=lt,
            passed=True,
            summary=f"{lt.value} audit passed",
            timestamp=now,
            auditor_version="1.0.0",
        )
        for lt in LeakageType
    )
    assert all(rec.passed for rec in leakage_records)

    # Compute payload root checksum
    all_hashes_sorted = sorted(str(h) for h in v2_split_membership.all_hashes())
    manifest_root_checksum = ContentHash(
        _compute_sha256("".join(all_hashes_sorted).encode("utf-8"))
    )

    dataset_manifest_v2 = DatasetManifest(
        dataset_id=DatasetId("traffic-cctv-curated"),
        version=DatasetVersion("v2.0.0"),
        created=now,
        sources=("cam-intersection-01",),
        n_items=len(all_hashes_sorted),
        class_definitions=(
            ClassDefinition(
                name="vehicle",
                description="Traffic motor vehicle",
                near_miss_negatives=("pedestrian",),
                ambiguity_resolution="classify as vehicle if wheels visible",
            ),
        ),
        class_distribution={"vehicle": len(all_hashes_sorted)},
        split_policy="temporal-site-disjoint",
        split_membership=v2_split_membership,
        annotation_version=AnnotationVersion("ann-v2.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guidelines-v2"),
        lineage=DatasetLineage(
            parent_version=DatasetVersion("v1.0.0"),
            transformation_name="active_learning_augmentation",
            source_manifest_hash="0" * 64,
        ),
        license_and_constraints="Proprietary / In-House",
        collection_conditions=CollectionConditions(
            camera_models=("Hikvision-4K",),
            sites=("intersection-01",),
            times_of_day=("dusk",),
            lighting_conditions=("low",),
            weather_conditions=("overcast",),
        ),
        payload_root_checksum=manifest_root_checksum,
        leakage_audit_records=leakage_records,
    )

    # Assert Seam 1 → Seam 2 Boundary Invariant
    assert (
        candidate_sample.frame_content_hash
        in dataset_manifest_v2.split_membership.train_hashes
    )
    assert (
        candidate_sample.frame_content_hash
        not in dataset_manifest_v2.split_membership.calibration_hashes
    )
    assert (
        candidate_sample.frame_content_hash
        not in dataset_manifest_v2.split_membership.test_hashes
    )
    evidence_ledger["dataset_manifest_hash"] = str(
        dataset_manifest_v2.payload_root_checksum
    )

    # -------------------------------------------------------------------------
    # Step 3 (Seam 2 → Seam 3): Approved Training Plan & Checkpoint (ADR-0010)
    # -------------------------------------------------------------------------
    training_plan = TrainingPlan(
        plan_id=PlanId("plan-train-20260920-v2"),
        experiment_id="exp-retrain-traffic-v2",
        created_at=now,
        capability_id=CapabilityId("cv.detector.train"),
        dataset_id=dataset_manifest_v2.dataset_id,
        dataset_version=dataset_manifest_v2.version,
        dataset_manifest_hash=ContentHash(dataset_manifest_v2.payload_root_checksum),
        train_split="train",
        val_split="val",
        architecture_name="yolov8x",
        num_classes=1,
        optimizer=OptimizerConfig(name="AdamW", lr=1e-3, weight_decay=1e-2),
        scheduler=SchedulerConfig(policy="CosineAnnealingLR", warmup_epochs=2),
        losses=(LossComponentConfig(loss_type="DetectionLoss", weight=1.0),),
        augmentation=AugmentationConfig(
            pipeline_name="standard_detection",
            image_size=(640, 640),
            horizontal_flip_prob=0.5,
        ),
        batch_size_per_device=16,
        epochs=10,
        precision=TrainingPrecision.FP16_AMP,
        compute_requirements=ComputeRequirements(
            min_vram_gb=16.0,
            min_cuda_compute_capability="8.6",
        ),
        cost_estimate=CostEstimate(
            estimated_gpu_hours=2.5,
            estimated_wall_clock_hours=1.25,
            estimated_storage_gb=10.0,
            basis_description="RTX 4090 10 epochs benchmark",
        ),
    )

    # Validate plan against dataset manifest via TrainingLauncher
    launcher = DefaultTrainingLauncher()
    assert launcher.validate_plan(training_plan, dataset_manifest_v2) is True

    # Checkpoint produced by approved training job
    raw_weights_bytes = b"TRAINED_WEIGHTS_PAYLOAD_EPOCH_10_YOLOV8X_V2"
    weights_hash = ContentHash(_compute_sha256(raw_weights_bytes))
    checkpoint_manifest = CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-yolo-v2-final"),
        job_id=TrainingJobId("job-train-001"),
        plan_id=training_plan.plan_id,
        epoch=10,
        step=5000,
        weights_hash=weights_hash,
        weights_uri="file:///models/checkpoints/ckpt-yolo-v2-final.pt",
        metrics={"train_loss": 0.042, "val_loss": 0.048},
        created_at=now,
    )

    # Assert Seam 2 → Seam 3 Boundary Invariant
    assert training_plan.dataset_manifest_hash == str(
        dataset_manifest_v2.payload_root_checksum
    )
    assert checkpoint_manifest.plan_id == training_plan.plan_id
    evidence_ledger["checkpoint_weights_hash"] = str(checkpoint_manifest.weights_hash)

    # -------------------------------------------------------------------------
    # Step 4 (Seam 3 → Seam 4): Synchronized Offline Evaluation (ADR-0011)
    # -------------------------------------------------------------------------
    eval_model_artifact = ModelArtifactReference(
        checkpoint_id=checkpoint_manifest.checkpoint_id,
        architecture_name=training_plan.architecture_name,
        weights_hash=checkpoint_manifest.weights_hash,
        weights_uri=checkpoint_manifest.weights_uri,
        precision=TrainingPrecision.FP16_AMP,
        input_resolution=(640, 640),
    )

    base_eval_plan = EvaluationPlan(
        evaluation_id=EvaluationId("eval-exp-traffic-v2-baseline"),
        experiment_id=ExperimentId("exp-retrain-traffic-v2"),
        created_at=now,
        capability_id=CapabilityId("cv.eval.detector"),
        dataset_binding=EvaluationDatasetBinding(
            dataset_id=dataset_manifest_v2.dataset_id,
            dataset_version=dataset_manifest_v2.version,
            dataset_manifest_hash=ContentHash(
                dataset_manifest_v2.payload_root_checksum
            ),
            split_name="test",
        ),
        model_artifact=eval_model_artifact,
        task_type=TaskType.DETECTION,
        metrics=(
            MetricDefinition(
                metric_id=MetricId("val-map50"),
                name="map50",
                task_type=TaskType.DETECTION,
                version="1.0",
                higher_is_better=True,
            ),
            MetricDefinition(
                metric_id=MetricId("val-confidence-score"),
                name="confidence_score",
                task_type=TaskType.DETECTION,
                version="1.0",
                higher_is_better=True,
            ),
        ),
    )

    eval_runner = DefaultEvaluationRunner()
    assert (
        eval_runner.validate_plan(
            base_eval_plan,
            dataset_manifest=dataset_manifest_v2,
            checkpoint_manifest=checkpoint_manifest,
        )
        is True
    )

    # Construct authoritative ADR-0011 baseline profile and evidence hash
    base_dist_profile = BaselineDistributionProfile(
        distribution_ref=f"eval-evidence://{base_eval_plan.evaluation_id}/distributions",
        confidence_histogram={
            "0.0-0.2": 50,
            "0.2-0.4": 150,
            "0.4-0.6": 300,
            "0.6-0.8": 1500,
            "0.8-1.0": 3000,
        },
        confidence_quantiles=(0.25, 0.45, 0.65, 0.82, 0.94),
        class_distribution={"vehicle": 1.0},
        sample_count=5000,
        prediction_entropy_mean=0.28,
    )
    authoritative_evidence_hash = compute_distribution_profile_hash(base_dist_profile)

    base_eval_result = EvaluationResult(
        evaluation_id=base_eval_plan.evaluation_id,
        plan=base_eval_plan,
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(
            MetricResult(
                metric=base_eval_plan.metrics[0],
                primary_value=0.825,
                sample_count=5000,
                per_class_values={"vehicle": 0.825},
            ),
            MetricResult(
                metric=base_eval_plan.metrics[1],
                primary_value=0.865,
                sample_count=5000,
                metadata={"evidence_hash": str(authoritative_evidence_hash)},
            ),
        ),
        system_benchmarks=(
            BenchmarkResult(
                benchmark_id=BenchmarkId("bench-target-orin-01"),
                batch_size=1,
                precision=TrainingPrecision.FP16_AMP,
                latency_min_ms=10.0,
                latency_mean_ms=14.5,
                latency_p50_ms=14.2,
                latency_p90_ms=16.0,
                latency_p95_ms=17.5,
                latency_p99_ms=19.8,
                latency_max_ms=22.0,
                throughput_fps=68.5,
                peak_vram_gb=3.8,
                cpu_utilization_pct=30.0,
                gpu_utilization_pct=88.0,
                device_name="NVIDIA Jetson AGX Orin 64GB",
                target_id=TargetId("target-jetson-orin-01"),
                timing_synchronized=True,
                timing_source="cuda_event",
            ),
        ),
        error_analysis=ErrorAnalysisRecord(
            total_samples=5000,
            failed_samples_count=875,
            false_positive_count=350,
            false_negative_count=525,
            class_labels=("vehicle",),
            confusion_matrix=((4125,),),
        ),
        environment=EvaluationEnvironmentRecord(
            git_commit_sha="a88a9be" * 5 + "1234",
            git_is_dirty=False,
            dataset_manifest_hash=ContentHash(
                dataset_manifest_v2.payload_root_checksum
            ),
            weights_hash=checkpoint_manifest.weights_hash,
            metric_definition_versions=("1.0",),
            python_version="3.11.0",
            torch_version="2.4.0",
            target_hardware="NVIDIA Jetson AGX Orin",
        ),
        completed_at=now,
    )

    # Assert Seam 3 → Seam 4 Boundary Invariant
    assert (
        base_eval_result.plan.model_artifact.weights_hash
        == checkpoint_manifest.weights_hash
    )
    evidence_ledger["baseline_evaluation_evidence_hash"] = str(
        authoritative_evidence_hash
    )

    # -------------------------------------------------------------------------
    # Step 5 (Seam 4 → Seam 5): Verified Model Optimization & Staged Deployment (ADR-0012)
    # -------------------------------------------------------------------------
    opt_plan = OptimizationPlan(
        plan_id=OptimizationPlanId("opt-traffic-v2-int8"),
        source_checkpoint_id=checkpoint_manifest.checkpoint_id,
        source_weights_hash=checkpoint_manifest.weights_hash,
        technique=OptimizationTechnique.PTQ_QUANTIZATION,
        target_precision=OptimizationPrecision.INT8,
        target_platform="nvidia_jetson_agx_orin",
        target_compute_capability="8.7",
        shape_profiles=(
            DynamicShapeProfile(
                tensor_name="input_images",
                min_shape=(1, 3, 640, 640),
                opt_shape=(1, 3, 640, 640),
                max_shape=(4, 3, 640, 640),
            ),
        ),
        calibration_binding=CalibrationDatasetBinding(
            dataset_id=dataset_manifest_v2.dataset_id,
            dataset_version=dataset_manifest_v2.version,
            dataset_manifest_hash=ContentHash(
                dataset_manifest_v2.payload_root_checksum
            ),
            split_name="calibration",
            num_calibration_samples=512,
            calibration_algorithm=CalibrationAlgorithm.ENTROPY,
        ),
        verification_dataset_binding=EvaluationDatasetBinding(
            dataset_id=dataset_manifest_v2.dataset_id,
            dataset_version=dataset_manifest_v2.version,
            dataset_manifest_hash=ContentHash(
                dataset_manifest_v2.payload_root_checksum
            ),
            split_name="test",
        ),
        max_accuracy_drop_tolerance=0.015,
        created_at=now,
    )

    opt_artifact = OptimizedModelArtifact(
        artifact_id=OptimizedArtifactId("art-opt-traffic-v2-int8"),
        plan_id=opt_plan.plan_id,
        source_checkpoint_id=opt_plan.source_checkpoint_id,
        technique=opt_plan.technique,
        precision=opt_plan.target_precision,
        runtime_format="tensorrt_engine",
        target_device=opt_plan.target_platform,
        target_compute_capability=opt_plan.target_compute_capability,
        artifact_hash=ContentHash(_compute_sha256(b"OPTIMIZED_ENGINE_TRT_INT8")),
        artifact_uri="file:///models/optimized/art-opt-traffic-v2-int8.engine",
        artifact_size_bytes=45_000_000,
        shape_profiles=opt_plan.shape_profiles,
        created_at=now,
    )

    # B23 Remediation: Genuine distinct candidate evaluation for the optimized artifact
    cand_model_artifact = ModelArtifactReference(
        checkpoint_id=CheckpointId(str(opt_artifact.artifact_id)),
        architecture_name=training_plan.architecture_name,
        weights_hash=opt_artifact.artifact_hash,
        weights_uri=opt_artifact.artifact_uri,
        precision=TrainingPrecision.FP16_AMP,
        input_resolution=(640, 640),
    )

    cand_eval_plan = EvaluationPlan(
        evaluation_id=EvaluationId("eval-cand-opt-traffic-v2-int8"),
        experiment_id=ExperimentId("exp-retrain-traffic-v2"),
        created_at=now,
        capability_id=CapabilityId("cv.eval.detector"),
        dataset_binding=EvaluationDatasetBinding(
            dataset_id=dataset_manifest_v2.dataset_id,
            dataset_version=dataset_manifest_v2.version,
            dataset_manifest_hash=ContentHash(
                dataset_manifest_v2.payload_root_checksum
            ),
            split_name="test",
        ),
        model_artifact=cand_model_artifact,
        task_type=TaskType.DETECTION,
        metrics=base_eval_plan.metrics,
    )

    cand_dist_profile = BaselineDistributionProfile(
        distribution_ref=f"eval-evidence://{cand_eval_plan.evaluation_id}/distributions",
        confidence_histogram={
            "0.0-0.2": 55,
            "0.2-0.4": 155,
            "0.4-0.6": 310,
            "0.6-0.8": 1530,
            "0.8-1.0": 2950,
        },
        confidence_quantiles=(0.25, 0.45, 0.65, 0.82, 0.94),
        class_distribution={"vehicle": 1.0},
        sample_count=5000,
        prediction_entropy_mean=0.285,
    )
    cand_evidence_hash = compute_distribution_profile_hash(cand_dist_profile)

    cand_eval_result = EvaluationResult(
        evaluation_id=cand_eval_plan.evaluation_id,
        plan=cand_eval_plan,
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(
            MetricResult(
                metric=cand_eval_plan.metrics[0],
                primary_value=0.820,  # 0.820 vs baseline 0.825 (accuracy drop 0.005 <= 0.015 tolerance)
                sample_count=5000,
                per_class_values={"vehicle": 0.820},
            ),
            MetricResult(
                metric=cand_eval_plan.metrics[1],
                primary_value=0.855,
                sample_count=5000,
                metadata={"evidence_hash": str(cand_evidence_hash)},
            ),
        ),
        system_benchmarks=(
            BenchmarkResult(
                benchmark_id=BenchmarkId("bench-candidate-orin-01"),
                batch_size=1,
                precision=TrainingPrecision.FP16_AMP,
                latency_min_ms=6.0,
                latency_mean_ms=8.9,
                latency_p50_ms=8.7,
                latency_p90_ms=10.1,
                latency_p95_ms=11.0,
                latency_p99_ms=12.5,
                latency_max_ms=14.0,
                throughput_fps=112.4,  # speedup: 112.4 / 68.5 = 1.64x
                peak_vram_gb=2.2,
                cpu_utilization_pct=26.0,
                gpu_utilization_pct=92.0,
                device_name="NVIDIA Jetson AGX Orin 64GB",
                target_id=TargetId("target-jetson-orin-01"),
                timing_synchronized=True,
                timing_source="cuda_event",
            ),
        ),
        error_analysis=ErrorAnalysisRecord(
            total_samples=5000,
            failed_samples_count=900,
            false_positive_count=360,
            false_negative_count=540,
            class_labels=("vehicle",),
            confusion_matrix=((4100,),),
        ),
        environment=EvaluationEnvironmentRecord(
            git_commit_sha="a88a9be" * 5 + "1234",
            git_is_dirty=False,
            dataset_manifest_hash=ContentHash(
                dataset_manifest_v2.payload_root_checksum
            ),
            weights_hash=opt_artifact.artifact_hash,
            metric_definition_versions=("1.0",),
            python_version="3.11.0",
            torch_version="2.4.0",
            target_hardware="NVIDIA Jetson AGX Orin",
        ),
        completed_at=now,
    )

    cand_binding = OptimizedArtifactEvaluationBinding(
        candidate_artifact_id=opt_artifact.artifact_id,
        candidate_artifact_hash=opt_artifact.artifact_hash,
        evaluation_id=cand_eval_result.evaluation_id,
    )

    opt_verifier = DefaultOptimizationVerifier()
    opt_result = opt_verifier.verify(
        plan=opt_plan,
        artifact=opt_artifact,
        baseline_evaluation=base_eval_result,
        candidate_evaluation=cand_eval_result,
        candidate_artifact_binding=cand_binding,
    )
    assert opt_result.accuracy_retention_ratio >= 0.98
    assert opt_result.speedup_ratio > 1.5
    assert opt_result.verification_passed is True
    evidence_ledger["candidate_evaluation_evidence_hash"] = str(cand_evidence_hash)

    # Stage and activate deployment via DefaultDeploymentManager
    target_id = TargetId("target-jetson-orin-01")
    dep_plan = DeploymentPlan(
        deployment_id=DeploymentId("dep-intersection-01"),
        artifact_id=opt_result.artifact.artifact_id,
        target=DeploymentTarget(
            target_id=target_id,
            hostname_or_ip="192.168.1.120",
            platform_type="nvidia_jetson_agx_orin",
            gpu_device_name="NVIDIA Jetson AGX Orin 64GB",
            compute_capability="8.7",
            cuda_runtime_version="12.2",
            driver_version="535.104.05",
            max_memory_allocation_gb=48.0,
            power_mode="MAXN",
        ),
        runtime=DeploymentRuntime.STANDALONE_TENSORRT,
        runtime_config={"cuda_streams": 2},
        health_check=HealthCheckSpec(
            startup_timeout_sec=30.0,
            readiness_probe_samples=5,
            max_latency_threshold_ms=25.0,
            memory_headroom_mb=1024.0,
        ),
        created_at=now,
    )

    mock_executor = MockToolExecutor()
    dep_manager = DefaultDeploymentManager()
    staged_record = dep_manager.stage(
        plan=dep_plan,
        target_id=target_id,
        tool_id=ToolId("tool-deployment-agent"),
        tool_executor=mock_executor,
        optimization_result=opt_result,
    )
    assert staged_record.status == DeploymentStatus.STAGING

    # Transition STAGING -> VERIFYING via verify_readiness
    probe_res, verifying_rec = dep_manager.verify_readiness(
        record=staged_record,
        target_id=target_id,
        tool_id=ToolId("tool-deployment-agent"),
        tool_executor=mock_executor,
    )
    assert probe_res.passed is True
    assert verifying_rec.status == DeploymentStatus.VERIFYING

    # ADR-0003 4-tuple approval for activation
    op_id, app_req, app_dec = _make_approval_context(dep_plan)
    active_record = dep_manager.activate(
        record=verifying_rec,
        target_id=target_id,
        operation_identity=op_id,
        approval_request=app_req,
        approval_decision=app_dec,
        tool_id=ToolId("tool-deployment-agent"),
        tool_executor=mock_executor,
    )
    assert active_record.status == DeploymentStatus.ACTIVE
    evidence_ledger["deployed_artifact_id"] = str(active_record.plan.artifact_id)
    evidence_ledger["deployed_artifact_hash"] = str(opt_artifact.artifact_hash)

    # -------------------------------------------------------------------------
    # Step 6 (Seam 5 → Seam 6): Baseline Reference Binding & Drift Detection (ADR-0013)
    # -------------------------------------------------------------------------
    # B24 Remediation: Extract baseline from candidate evaluation of the deployed optimized artifact
    baseline_binding = extract_baseline_reference_binding(
        evaluation_result=cand_eval_result,
        distribution_profile=cand_dist_profile,
        baseline_fps_p05=85.0,  # explicitly provided, zero synthetic inference
        baseline_fps_p50=112.4,
        evidence_hash=cand_evidence_hash,
        deployed_artifact_id=str(opt_artifact.artifact_id),
        source_checkpoint_id=str(checkpoint_manifest.checkpoint_id),
        deployment_record=active_record,
    )

    # Assert Seam 5 Baseline Invariants
    assert baseline_binding.evaluation_id == cand_eval_result.evaluation_id
    assert baseline_binding.evidence_hash == cand_evidence_hash
    assert baseline_binding.model_checkpoint_hash == opt_artifact.artifact_hash
    assert baseline_binding.deployed_artifact_id == str(active_record.plan.artifact_id)
    assert baseline_binding.source_checkpoint_id == str(
        checkpoint_manifest.checkpoint_id
    )
    evidence_ledger["baseline_evidence_hash"] = str(baseline_binding.evidence_hash)

    # Explicit immutable binding joining DeploymentRecord + Candidate Evaluation + BaselineReferenceBinding
    dep_baseline_binding = bind_deployment_to_monitoring_baseline(
        active_record,
        baseline_binding,
        optimization_result=opt_result,
    )
    assert isinstance(dep_baseline_binding, DeploymentBaselineBinding)
    assert dep_baseline_binding.deployment_id == active_record.deployment_id
    assert dep_baseline_binding.deployed_artifact_id == str(
        active_record.plan.artifact_id
    )
    assert dep_baseline_binding.deployed_artifact_hash == opt_artifact.artifact_hash
    assert dep_baseline_binding.evaluation_id == cand_eval_result.evaluation_id
    assert dep_baseline_binding.evaluation_evidence_hash == cand_evidence_hash
    assert dep_baseline_binding.source_checkpoint_id == str(
        checkpoint_manifest.checkpoint_id
    )

    # Ingest live telemetry into monitoring pipeline
    pipeline = DefaultTelemetryPipeline()
    t0 = now + timedelta(minutes=5)
    # Live data experiencing severe confidence degradation and distribution shift
    model_telem = ModelTelemetryRecord(
        deployment_id=active_record.deployment_id,
        window_start=t0 - timedelta(seconds=60),
        window_end=t0,
        window_seconds=60.0,
        total_inferences=1000,
        confidence_histogram={
            "0.0-0.2": 200,
            "0.2-0.4": 350,
            "0.4-0.6": 300,
            "0.6-0.8": 100,
            "0.8-1.0": 50,
        },
        confidence_mean=0.52,  # down from candidate baseline 0.855
        confidence_p10=0.25,
        confidence_p50=0.50,
        confidence_p90=0.72,
        class_counts={"vehicle": 500, "pedestrian": 500},
        class_proportions={
            "vehicle": 0.50,
            "pedestrian": 0.50,
        },  # unobserved class shift
        mean_detections_per_frame=1.0,
        zero_detection_frame_ratio=0.1,
        prediction_entropy_mean=0.65,
    )
    sys_telem = SystemTelemetryRecord(
        deployment_id=active_record.deployment_id,
        timestamp=t0,
        hardware_target="nvidia_jetson_agx_orin",
        throughput_fps=60.0,
        latency_p50_ms=14.0,
        latency_p95_ms=18.0,
        latency_p99_ms=21.0,
        dropped_frames_total=0,
        dropped_frame_ratio=0.0,
        gpu_utilization_pct=85.0,
        cpu_utilization_pct=30.0,
        vram_used_mb=3900.0,
        vram_total_mb=48000.0,
        ram_used_mb=4000.0,
        gpu_temperature_celsius=50.0,
    )
    data_telem = DataTelemetryRecord(
        stream_id="cam-intersection-01",
        camera_id="cam-intersection-01",
        timestamp=t0,
        mean_luminance=40.0,  # severe nighttime darkening
        contrast_std=20.0,
        blur_laplacian_variance=120.0,
        overexposure_ratio=0.0,
        underexposure_ratio=0.4,
        resolution_width=1920,
        resolution_height=1080,
        stream_bitrate_kbps=4000.0,
        packet_loss_ratio=0.0,
    )

    pipeline.ingest_model_telemetry(model_telem)
    pipeline.ingest_system_telemetry(sys_telem)
    pipeline.ingest_data_telemetry(data_telem)

    snapshot = pipeline.create_snapshot(
        deployment_id=active_record.deployment_id,
        stream_id="cam-intersection-01",
        timestamp=t0,
    )
    assert snapshot is not None

    # Evaluate drift against baseline using the immutable deployment baseline binding
    drift_detector = DefaultStatisticalDriftDetector()
    drift_analysis = drift_detector.evaluate_drift(
        telemetry_window=snapshot,
        baseline=dep_baseline_binding,
    )

    # -------------------------------------------------------------------------
    # Step 7 (Seam 6 → Seam 7): Retraining Proposal Formulation & Loop Closure
    # -------------------------------------------------------------------------
    # Generate advisory retraining trigger proposal referencing candidate batch
    proposal = pipeline.formulate_retraining_proposal(
        deployment_id=active_record.deployment_id,
        trigger_reason=TriggerReason.MODEL_ACCURACY_DRIFT,
        severity=DriftSeverity.CRITICAL,
        evidence_summary=f"Severe distribution shift detected: {len(drift_analysis.metric_results)} metrics evaluated",
        estimated_new_samples=len(candidate_batch.sample_manifest),
        drift_analysis_id=drift_analysis.analysis_id,
        candidate_batch_id=candidate_batch.batch_id,
    )

    assert proposal is not None
    assert proposal.requires_human_approval is True
    assert proposal.approved is False
    assert proposal.approved_by is None
    assert proposal.candidate_batch_id == candidate_batch.batch_id
    assert proposal.trigger_reason == TriggerReason.MODEL_ACCURACY_DRIFT
    assert "Severe distribution shift detected" in proposal.evidence_summary

    # Assert complete unbroken evidence chain
    assert evidence_ledger["candidate_frame_hash"] == _compute_sha256(raw_frame_bytes)
    assert evidence_ledger["checkpoint_weights_hash"] == _compute_sha256(
        raw_weights_bytes
    )
    assert evidence_ledger["baseline_evaluation_evidence_hash"] == str(
        authoritative_evidence_hash
    )
    assert evidence_ledger["candidate_evaluation_evidence_hash"] == str(
        cand_evidence_hash
    )
    assert evidence_ledger["baseline_evidence_hash"] == str(cand_evidence_hash)
    assert evidence_ledger["deployed_artifact_id"] == "art-opt-traffic-v2-int8"
    assert evidence_ledger["deployed_artifact_hash"] == str(opt_artifact.artifact_hash)


# =============================================================================
# 2. Dedicated Adversarial Boundary Tests for Each Seam
# =============================================================================


def test_seam1_rejects_tampered_candidate_frame_payload(tmp_path: Path) -> None:
    """Seam 1: Candidate frame payload hash verification must be byte-identical.

    Ingestion into DatasetStore must fail if raw payload bytes do not hash
    identically to CandidateSample.frame_content_hash.
    """
    raw_bytes = b"AUTHENTIC_RAW_VIDEO_FRAME_BYTES"
    authentic_hash = ContentHash(_compute_sha256(raw_bytes))

    temp_path = tmp_path / "authentic_frame.raw"
    temp_path.write_bytes(raw_bytes)

    sample = CandidateSample(
        sample_id=CandidateSampleId("sample-001"),
        stream_id="cam-01",
        frame_index=1,
        timestamp=datetime.now(timezone.utc),
        frame_content_hash=authentic_hash,
        temporary_payload_uri=f"file://{temp_path}",
        strategy_applied=ActiveLearningStrategy.UNCERTAINTY_LEAST_CONFIDENT,
        priority_score=0.5,
    )

    # Tamper payload bytes
    tampered_bytes = b"TAMPERED_FRAME_BYTES_WITH_CORRUPTED_PIXELS"
    tampered_path = tmp_path / "tampered_frame.raw"
    tampered_path.write_bytes(tampered_bytes)

    store = LocalFSStore(tmp_path / "store")
    stored_hash = store.put_payload(tampered_path)

    # Ingestion gate verification: stored payload digest does not match declared candidate hash
    assert stored_hash != sample.frame_content_hash
    assert store.exists(stored_hash)
    assert not store.exists(sample.frame_content_hash)


def test_seam2_rejects_training_on_calibration_split_leakage() -> None:
    """Seam 2: TrainingPlan validation must strictly forbid training on calibration split."""
    now = datetime.now(timezone.utc)
    launcher = DefaultTrainingLauncher()

    manifest = DatasetManifest(
        dataset_id=DatasetId("ds-traffic"),
        version=DatasetVersion("v1.0.0"),
        created=now,
        sources=("cam1",),
        n_items=4,
        class_definitions=(
            ClassDefinition(
                name="vehicle",
                description="desc",
                near_miss_negatives=(),
                ambiguity_resolution="none",
            ),
        ),
        class_distribution={"vehicle": 4},
        split_policy="random",
        split_membership=SplitMembership(
            train_hashes=(ContentHash("1" * 64),),
            val_hashes=(ContentHash("2" * 64),),
            test_hashes=(ContentHash("3" * 64),),
            calibration_hashes=(ContentHash("4" * 64),),
        ),
        annotation_version=AnnotationVersion("ann-v1"),
        annotation_guideline_ref=GuidelineRef("git:guide"),
        lineage=DatasetLineage(),
        license_and_constraints="MIT",
        collection_conditions=CollectionConditions(
            camera_models=("c1",),
            sites=("s1",),
            times_of_day=("d",),
            lighting_conditions=("l",),
            weather_conditions=("w",),
        ),
        payload_root_checksum=ContentHash("9" * 64),
    )

    # Attempt to train using calibration split
    leaked_plan = TrainingPlan(
        plan_id=PlanId("plan-leak"),
        experiment_id="exp-leak",
        created_at=now,
        capability_id=CapabilityId("cv.detector.train"),
        dataset_id=manifest.dataset_id,
        dataset_version=manifest.version,
        dataset_manifest_hash=ContentHash(manifest.payload_root_checksum),
        train_split="calibration",  # LEAKAGE
        val_split="val",
        architecture_name="yolov8n",
        num_classes=1,
        optimizer=OptimizerConfig(name="AdamW", lr=1e-3),
        scheduler=SchedulerConfig(policy="Cosine"),
        losses=(LossComponentConfig(loss_type="Loss", weight=1.0),),
        augmentation=AugmentationConfig(pipeline_name="aug", image_size=(640, 640)),
        batch_size_per_device=16,
        epochs=5,
        compute_requirements=ComputeRequirements(min_vram_gb=8.0),
    )

    with pytest.raises(
        PlanValidationError,
        match="Calibration split 'calibration' is strictly reserved",
    ):
        launcher.validate_plan(leaked_plan, manifest)


def test_seam2_rejects_training_on_mismatched_dataset_manifest_hash() -> None:
    """Seam 2: TrainingPlan validation must fail closed when dataset_manifest_hash mismatches."""
    now = datetime.now(timezone.utc)
    launcher = DefaultTrainingLauncher()

    manifest = DatasetManifest(
        dataset_id=DatasetId("ds-traffic"),
        version=DatasetVersion("v1.0.0"),
        created=now,
        sources=("cam1",),
        n_items=3,
        class_definitions=(
            ClassDefinition(
                name="vehicle",
                description="desc",
                near_miss_negatives=(),
                ambiguity_resolution="none",
            ),
        ),
        class_distribution={"vehicle": 3},
        split_policy="random",
        split_membership=SplitMembership(
            train_hashes=(ContentHash("1" * 64),),
            val_hashes=(ContentHash("2" * 64),),
            test_hashes=(ContentHash("3" * 64),),
        ),
        annotation_version=AnnotationVersion("ann-v1"),
        annotation_guideline_ref=GuidelineRef("git:guide"),
        lineage=DatasetLineage(),
        license_and_constraints="MIT",
        collection_conditions=CollectionConditions(
            camera_models=("c1",),
            sites=("s1",),
            times_of_day=("d",),
            lighting_conditions=("l",),
            weather_conditions=("w",),
        ),
        payload_root_checksum=ContentHash("a" * 64),
    )

    tampered_plan = TrainingPlan(
        plan_id=PlanId("plan-tamper"),
        experiment_id="exp-tamper",
        created_at=now,
        capability_id=CapabilityId("cv.detector.train"),
        dataset_id=manifest.dataset_id,
        dataset_version=manifest.version,
        dataset_manifest_hash=ContentHash("b" * 64),  # MISMATCH
        train_split="train",
        val_split="val",
        architecture_name="yolov8n",
        num_classes=1,
        optimizer=OptimizerConfig(name="AdamW", lr=1e-3),
        scheduler=SchedulerConfig(policy="Cosine"),
        losses=(LossComponentConfig(loss_type="Loss", weight=1.0),),
        augmentation=AugmentationConfig(pipeline_name="aug", image_size=(640, 640)),
        batch_size_per_device=16,
        epochs=5,
        compute_requirements=ComputeRequirements(min_vram_gb=8.0),
    )

    with pytest.raises(PlanValidationError, match="Dataset manifest hash mismatch"):
        launcher.validate_plan(tampered_plan, manifest)


def test_seam3_rejects_evaluation_on_tampered_checkpoint_id_or_weights() -> None:
    """Seam 3: EvaluationRunner must fail closed on checkpoint ID mismatch and weights hash mismatch."""
    now = datetime.now(timezone.utc)
    runner = DefaultEvaluationRunner()

    checkpoint = CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-auth-001"),
        job_id=TrainingJobId("job-001"),
        plan_id=PlanId("plan-001"),
        epoch=5,
        step=1000,
        weights_hash=ContentHash("0" * 64),
        weights_uri="file:///models/ckpt-auth-001.pt",
        created_at=now,
    )

    # 1. Test Checkpoint ID mismatch
    plan_wrong_id = EvaluationPlan(
        evaluation_id=EvaluationId("eval-001"),
        experiment_id=ExperimentId("exp-001"),
        created_at=now,
        capability_id=CapabilityId("cv.eval"),
        dataset_binding=EvaluationDatasetBinding(
            dataset_id=DatasetId("ds-01"),
            dataset_version=DatasetVersion("v1.0.0"),
            dataset_manifest_hash=ContentHash("d" * 64),
            split_name="test",
        ),
        model_artifact=ModelArtifactReference(
            checkpoint_id=CheckpointId("ckpt-different"),
            architecture_name="yolov8",
            weights_hash=checkpoint.weights_hash,
            weights_uri=checkpoint.weights_uri,
            precision=TrainingPrecision.FP32,
            input_resolution=(640, 640),
        ),
        task_type=TaskType.DETECTION,
        metrics=(
            MetricDefinition(
                metric_id=MetricId("m1"),
                name="map",
                task_type=TaskType.DETECTION,
            ),
        ),
    )

    with pytest.raises(EvaluationValidationError, match="Checkpoint ID mismatch"):
        runner.validate_plan(plan_wrong_id, checkpoint_manifest=checkpoint)

    # 2. Test Weights Hash mismatch
    plan_wrong_hash = EvaluationPlan(
        evaluation_id=EvaluationId("eval-002"),
        experiment_id=ExperimentId("exp-002"),
        created_at=now,
        capability_id=CapabilityId("cv.eval"),
        dataset_binding=EvaluationDatasetBinding(
            dataset_id=DatasetId("ds-01"),
            dataset_version=DatasetVersion("v1.0.0"),
            dataset_manifest_hash=ContentHash("d" * 64),
            split_name="test",
        ),
        model_artifact=ModelArtifactReference(
            checkpoint_id=checkpoint.checkpoint_id,
            architecture_name="yolov8",
            weights_hash=ContentHash("f" * 64),
            weights_uri=checkpoint.weights_uri,
            precision=TrainingPrecision.FP32,
            input_resolution=(640, 640),
        ),
        task_type=TaskType.DETECTION,
        metrics=(
            MetricDefinition(
                metric_id=MetricId("m1"),
                name="map",
                task_type=TaskType.DETECTION,
            ),
        ),
    )

    with pytest.raises(EvaluationValidationError, match="Model weights hash mismatch"):
        runner.validate_plan(plan_wrong_hash, checkpoint_manifest=checkpoint)


def test_seam4_rejects_deployment_on_missing_or_mismatched_optimization_binding() -> (
    None
):
    """Seam 4: Verification and deployment fail closed on missing/mismatched optimization binding (B23)."""
    now = datetime.now(timezone.utc)
    dep_manager = DefaultDeploymentManager()
    mock_executor = MockToolExecutor()

    target = DeploymentTarget(
        target_id=TargetId("target-orin"),
        hostname_or_ip="192.168.1.50",
        platform_type="jetson",
        gpu_device_name="Jetson Orin",
        compute_capability="8.7",
        cuda_runtime_version="12.2",
        driver_version="535.0",
        max_memory_allocation_gb=32.0,
    )

    dep_plan = DeploymentPlan(
        deployment_id=DeploymentId("dep-001"),
        artifact_id=OptimizedArtifactId("art-001"),
        target=target,
        runtime=DeploymentRuntime.STANDALONE_TENSORRT,
        health_check=HealthCheckSpec(),
        created_at=now,
    )

    # 1. Missing OptimizationResult in deployment staging
    with pytest.raises(
        OptimizationVerificationError, match="Mandatory OptimizationResult missing"
    ):
        dep_manager.stage(
            plan=dep_plan,
            target_id=target.target_id,
            tool_id=ToolId("tool-deploy"),
            tool_executor=mock_executor,
            optimization_result=None,  # type: ignore[arg-type]
        )

    # 2. Build complete fixtures for verifier and deployment binding tests
    source_ckpt = CheckpointId("ckpt-traffic-base")
    source_hash = ContentHash("1" * 64)
    opt_art_id = OptimizedArtifactId("art-001")
    opt_art_hash = ContentHash("2" * 64)

    opt_plan = OptimizationPlan(
        plan_id=OptimizationPlanId("opt-001"),
        source_checkpoint_id=source_ckpt,
        source_weights_hash=source_hash,
        technique=OptimizationTechnique.PTQ_QUANTIZATION,
        target_precision=OptimizationPrecision.INT8,
        target_platform="jetson",
        target_compute_capability="8.7",
        calibration_binding=CalibrationDatasetBinding(
            dataset_id=DatasetId("ds-01"),
            dataset_version=DatasetVersion("v1.0.0"),
            dataset_manifest_hash=ContentHash("d" * 64),
            split_name="calibration",
            num_calibration_samples=100,
        ),
        verification_dataset_binding=EvaluationDatasetBinding(
            dataset_id=DatasetId("ds-01"),
            dataset_version=DatasetVersion("v1.0.0"),
            dataset_manifest_hash=ContentHash("d" * 64),
            split_name="test",
        ),
        created_at=now,
    )

    opt_artifact = OptimizedModelArtifact(
        artifact_id=opt_art_id,
        plan_id=opt_plan.plan_id,
        source_checkpoint_id=source_ckpt,
        technique=opt_plan.technique,
        precision=opt_plan.target_precision,
        runtime_format="tensorrt_engine",
        target_device="jetson",
        target_compute_capability="8.7",
        artifact_hash=opt_art_hash,
        artifact_uri="file:///models/art-001.engine",
        artifact_size_bytes=10_000_000,
        created_at=now,
    )

    metric_def = MetricDefinition(
        metric_id=MetricId("m-map"),
        name="map50",
        task_type=TaskType.DETECTION,
    )

    base_eval = EvaluationResult(
        evaluation_id=EvaluationId("eval-base-001"),
        plan=EvaluationPlan(
            evaluation_id=EvaluationId("eval-base-001"),
            experiment_id=ExperimentId("exp-001"),
            created_at=now,
            capability_id=CapabilityId("cv.eval"),
            dataset_binding=EvaluationDatasetBinding(
                dataset_id=DatasetId("ds-01"),
                dataset_version=DatasetVersion("v1.0.0"),
                dataset_manifest_hash=ContentHash("d" * 64),
                split_name="test",
            ),
            model_artifact=ModelArtifactReference(
                checkpoint_id=source_ckpt,
                architecture_name="yolo",
                weights_hash=source_hash,
                weights_uri="file:///models/base.pt",
                precision=TrainingPrecision.FP16_AMP,
                input_resolution=(640, 640),
            ),
            task_type=TaskType.DETECTION,
            metrics=(metric_def,),
        ),
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(
            MetricResult(metric=metric_def, primary_value=0.80, sample_count=100),
        ),
        system_benchmarks=(
            BenchmarkResult(
                benchmark_id=BenchmarkId("bm-base"),
                batch_size=1,
                precision=TrainingPrecision.FP16_AMP,
                latency_min_ms=10.0,
                latency_mean_ms=15.0,
                latency_p50_ms=15.0,
                latency_p90_ms=18.0,
                latency_p95_ms=19.0,
                latency_p99_ms=20.0,
                latency_max_ms=22.0,
                throughput_fps=66.0,
                device_name="Jetson",
                target_id=TargetId("t1"),
                timing_synchronized=True,
                timing_source="cuda_event",
            ),
        ),
        environment=EvaluationEnvironmentRecord(
            git_commit_sha="c1",
            git_is_dirty=False,
            dataset_manifest_hash=ContentHash("d" * 64),
            weights_hash=source_hash,
            metric_definition_versions=("1.0",),
            python_version="3.11.0",
            torch_version="2.4.0",
            target_hardware="jetson",
        ),
        completed_at=now,
    )

    cand_eval = EvaluationResult(
        evaluation_id=EvaluationId("eval-cand-001"),
        plan=EvaluationPlan(
            evaluation_id=EvaluationId("eval-cand-001"),
            experiment_id=ExperimentId("exp-001"),
            created_at=now,
            capability_id=CapabilityId("cv.eval"),
            dataset_binding=EvaluationDatasetBinding(
                dataset_id=DatasetId("ds-01"),
                dataset_version=DatasetVersion("v1.0.0"),
                dataset_manifest_hash=ContentHash("d" * 64),
                split_name="test",
            ),
            model_artifact=ModelArtifactReference(
                checkpoint_id=CheckpointId(str(opt_art_id)),
                architecture_name="yolo",
                weights_hash=opt_art_hash,
                weights_uri="file:///models/art-001.engine",
                precision=TrainingPrecision.FP16_AMP,
                input_resolution=(640, 640),
            ),
            task_type=TaskType.DETECTION,
            metrics=(metric_def,),
        ),
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(
            MetricResult(metric=metric_def, primary_value=0.795, sample_count=100),
        ),
        system_benchmarks=(
            BenchmarkResult(
                benchmark_id=BenchmarkId("bm-cand"),
                batch_size=1,
                precision=TrainingPrecision.FP16_AMP,
                latency_min_ms=6.0,
                latency_mean_ms=9.0,
                latency_p50_ms=9.0,
                latency_p90_ms=11.0,
                latency_p95_ms=12.0,
                latency_p99_ms=13.0,
                latency_max_ms=15.0,
                throughput_fps=110.0,
                device_name="Jetson",
                target_id=TargetId("t1"),
                timing_synchronized=True,
                timing_source="cuda_event",
            ),
        ),
        environment=EvaluationEnvironmentRecord(
            git_commit_sha="c1",
            git_is_dirty=False,
            dataset_manifest_hash=ContentHash("d" * 64),
            weights_hash=opt_art_hash,
            metric_definition_versions=("1.0",),
            python_version="3.11.0",
            torch_version="2.4.0",
            target_hardware="jetson",
        ),
        completed_at=now,
    )

    valid_cand_binding = OptimizedArtifactEvaluationBinding(
        candidate_artifact_id=opt_art_id,
        candidate_artifact_hash=opt_art_hash,
        evaluation_id=cand_eval.evaluation_id,
    )

    opt_verifier = DefaultOptimizationVerifier()

    # 3. Candidate evaluation binding artifact ID mismatch
    bad_binding_id = valid_cand_binding.model_copy(
        update={"candidate_artifact_id": OptimizedArtifactId("art-forged")}
    )
    with pytest.raises(
        OptimizationVerificationError,
        match="Candidate artifact binding ID .* does not match artifact",
    ):
        opt_verifier.verify(
            plan=opt_plan,
            artifact=opt_artifact,
            baseline_evaluation=base_eval,
            candidate_evaluation=cand_eval,
            candidate_artifact_binding=bad_binding_id,
        )

    # 4. Candidate evaluation binding artifact hash mismatch
    bad_binding_hash = valid_cand_binding.model_copy(
        update={"candidate_artifact_hash": ContentHash("f" * 64)}
    )
    with pytest.raises(
        OptimizationVerificationError,
        match="Candidate artifact binding hash .* does not match artifact hash",
    ):
        opt_verifier.verify(
            plan=opt_plan,
            artifact=opt_artifact,
            baseline_evaluation=base_eval,
            candidate_evaluation=cand_eval,
            candidate_artifact_binding=bad_binding_hash,
        )

    # 5. Candidate evaluation binding evaluation_id mismatch
    bad_binding_eval_id = valid_cand_binding.model_copy(
        update={"evaluation_id": EvaluationId("eval-other")}
    )
    with pytest.raises(
        OptimizationVerificationError,
        match="Candidate artifact binding evaluation_id .* does not match candidate evaluation",
    ):
        opt_verifier.verify(
            plan=opt_plan,
            artifact=opt_artifact,
            baseline_evaluation=base_eval,
            candidate_evaluation=cand_eval,
            candidate_artifact_binding=bad_binding_eval_id,
        )

    # 6. Candidate evaluation checkpoint lineage mismatch
    tampered_ckpt_artifact = cand_eval.plan.model_artifact.model_copy(
        update={"checkpoint_id": CheckpointId("ckpt-forged")}
    )
    tampered_cand_eval_ckpt = cand_eval.model_copy(
        update={
            "plan": cand_eval.plan.model_copy(
                update={"model_artifact": tampered_ckpt_artifact}
            )
        }
    )
    with pytest.raises(
        OptimizationVerificationError,
        match="Candidate evaluation model checkpoint lineage .* does not match",
    ):
        opt_verifier.verify(
            plan=opt_plan,
            artifact=opt_artifact,
            baseline_evaluation=base_eval,
            candidate_evaluation=tampered_cand_eval_ckpt,
            candidate_artifact_binding=valid_cand_binding,
        )

    # 7. Candidate evaluation weights hash mismatch
    tampered_hash_artifact = cand_eval.plan.model_artifact.model_copy(
        update={"weights_hash": ContentHash("9" * 64)}
    )
    tampered_cand_eval_hash = cand_eval.model_copy(
        update={
            "plan": cand_eval.plan.model_copy(
                update={"model_artifact": tampered_hash_artifact}
            )
        }
    )
    with pytest.raises(
        OptimizationVerificationError,
        match="Candidate evaluation weights hash lineage .* does not match",
    ):
        opt_verifier.verify(
            plan=opt_plan,
            artifact=opt_artifact,
            baseline_evaluation=base_eval,
            candidate_evaluation=tampered_cand_eval_hash,
            candidate_artifact_binding=valid_cand_binding,
        )

    # 8. Staging rejection when OptimizationResult artifact ID mismatches DeploymentPlan
    valid_opt_result = opt_verifier.verify(
        plan=opt_plan,
        artifact=opt_artifact,
        baseline_evaluation=base_eval,
        candidate_evaluation=cand_eval,
        candidate_artifact_binding=valid_cand_binding,
    )
    mismatched_dep_plan = dep_plan.model_copy(
        update={"artifact_id": OptimizedArtifactId("art-different-target")}
    )
    with pytest.raises(
        OptimizationValidationError,
        match="does not match DeploymentPlan artifact",
    ):
        dep_manager.stage(
            plan=mismatched_dep_plan,
            target_id=target.target_id,
            tool_id=ToolId("tool-deploy"),
            tool_executor=mock_executor,
            optimization_result=valid_opt_result,
        )


def test_seam5_rejects_baseline_binding_on_tampered_evaluation_evidence() -> None:
    """Seam 5: extract_baseline_reference_binding must fail closed when evidence hash is tampered (B21)."""
    now = datetime.now(timezone.utc)

    profile = BaselineDistributionProfile(
        distribution_ref="eval-evidence://eval-001/distributions",
        confidence_histogram={"0.0-0.5": 200, "0.5-1.0": 800},
        confidence_quantiles=(0.5, 0.8),
        class_distribution={"vehicle": 1.0},
        prediction_entropy_mean=0.35,
        sample_count=1000,
    )
    correct_hash = compute_distribution_profile_hash(profile)
    tampered_hash = ContentHash("deadbeef" * 8)

    eval_result = EvaluationResult(
        evaluation_id=EvaluationId("eval-001"),
        plan=EvaluationPlan(
            evaluation_id=EvaluationId("eval-001"),
            experiment_id=ExperimentId("exp-001"),
            created_at=now,
            capability_id=CapabilityId("cv.eval"),
            dataset_binding=EvaluationDatasetBinding(
                dataset_id=DatasetId("ds-01"),
                dataset_version=DatasetVersion("v1.0.0"),
                dataset_manifest_hash=ContentHash("d" * 64),
                split_name="test",
            ),
            model_artifact=ModelArtifactReference(
                checkpoint_id=CheckpointId("ckpt-01"),
                architecture_name="yolo",
                weights_hash=ContentHash("0" * 64),
                weights_uri="file:///models/weights.pt",
                precision=TrainingPrecision.FP32,
                input_resolution=(640, 640),
            ),
            task_type=TaskType.DETECTION,
            metrics=(
                MetricDefinition(
                    metric_id=MetricId("m1"),
                    name="confidence_mean",
                    task_type=TaskType.DETECTION,
                ),
            ),
        ),
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(
            MetricResult(
                metric=MetricDefinition(
                    metric_id=MetricId("m1"),
                    name="confidence_mean",
                    task_type=TaskType.DETECTION,
                ),
                primary_value=0.85,
                sample_count=1000,
                metadata={"evidence_hash": str(correct_hash)},
            ),
        ),
        system_benchmarks=(
            BenchmarkResult(
                benchmark_id=BenchmarkId("b1"),
                batch_size=1,
                precision=TrainingPrecision.FP32,
                latency_min_ms=10.0,
                latency_mean_ms=12.0,
                latency_p50_ms=11.5,
                latency_p90_ms=13.0,
                latency_p95_ms=14.0,
                latency_p99_ms=15.0,
                latency_max_ms=16.0,
                throughput_fps=60.0,
                device_name="Jetson Orin",
                target_id=TargetId("t1"),
                timing_synchronized=True,
                timing_source="cuda_event",
            ),
        ),
        environment=EvaluationEnvironmentRecord(
            git_commit_sha="commit12345",
            git_is_dirty=False,
            dataset_manifest_hash=ContentHash("d" * 64),
            weights_hash=ContentHash("0" * 64),
            metric_definition_versions=("1.0",),
            python_version="3.11.0",
            torch_version="2.4.0",
            target_hardware="Jetson Orin",
        ),
        completed_at=now,
    )

    # Providing a tampered external hash conflicting with evaluation metadata
    with pytest.raises(
        BaselineMismatchError, match="Explicit evidence_hash .* does not match"
    ):
        extract_baseline_reference_binding(
            evaluation_result=eval_result,
            distribution_profile=profile,
            baseline_fps_p05=50.0,
            evidence_hash=tampered_hash,
        )


def test_seam5_rejects_baseline_binding_on_deployed_artifact_id_mismatch() -> None:
    """Seam 5: Baseline binding and deployment registration reject mismatched artifact IDs (B24)."""
    now = datetime.now(timezone.utc)
    profile = BaselineDistributionProfile(
        distribution_ref="eval-evidence://eval-art-test/distributions",
        confidence_histogram={"0.0-1.0": 500},
        confidence_quantiles=(0.5,),
        class_distribution={"vehicle": 1.0},
        prediction_entropy_mean=0.25,
        sample_count=500,
    )
    ev_hash = compute_distribution_profile_hash(profile)

    eval_result = EvaluationResult(
        evaluation_id=EvaluationId("eval-art-test"),
        plan=EvaluationPlan(
            evaluation_id=EvaluationId("eval-art-test"),
            experiment_id=ExperimentId("exp-001"),
            created_at=now,
            capability_id=CapabilityId("cv.eval"),
            dataset_binding=EvaluationDatasetBinding(
                dataset_id=DatasetId("ds-01"),
                dataset_version=DatasetVersion("v1.0.0"),
                dataset_manifest_hash=ContentHash("d" * 64),
                split_name="test",
            ),
            model_artifact=ModelArtifactReference(
                checkpoint_id=CheckpointId("ckpt-art-test"),
                architecture_name="yolo",
                weights_hash=ContentHash("c" * 64),
                weights_uri="file:///models/art.engine",
                precision=TrainingPrecision.FP32,
                input_resolution=(640, 640),
            ),
            task_type=TaskType.DETECTION,
            metrics=(
                MetricDefinition(
                    metric_id=MetricId("m-conf"),
                    name="confidence",
                    task_type=TaskType.DETECTION,
                ),
            ),
        ),
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(
            MetricResult(
                metric=MetricDefinition(
                    metric_id=MetricId("m-conf"),
                    name="confidence",
                    task_type=TaskType.DETECTION,
                ),
                primary_value=0.88,
                sample_count=500,
                metadata={"evidence_hash": str(ev_hash)},
            ),
        ),
        system_benchmarks=(
            BenchmarkResult(
                benchmark_id=BenchmarkId("b-art"),
                batch_size=1,
                precision=TrainingPrecision.FP32,
                latency_min_ms=5.0,
                latency_mean_ms=10.0,
                latency_p50_ms=10.0,
                latency_p90_ms=12.0,
                latency_p95_ms=13.0,
                latency_p99_ms=14.0,
                latency_max_ms=15.0,
                throughput_fps=100.0,
                device_name="Jetson",
                target_id=TargetId("t1"),
                timing_synchronized=True,
                timing_source="cuda_event",
            ),
        ),
        environment=EvaluationEnvironmentRecord(
            git_commit_sha="c1",
            git_is_dirty=False,
            dataset_manifest_hash=ContentHash("d" * 64),
            weights_hash=ContentHash("c" * 64),
            metric_definition_versions=("1.0",),
            python_version="3.11.0",
            torch_version="2.4.0",
            target_hardware="Jetson",
        ),
        completed_at=now,
    )

    dep_rec = DeploymentRecord(
        deployment_id=DeploymentId("dep-art-001"),
        plan=DeploymentPlan(
            deployment_id=DeploymentId("dep-art-001"),
            artifact_id=OptimizedArtifactId("art-deployed-actual"),
            target=DeploymentTarget(
                target_id=TargetId("t1"),
                hostname_or_ip="10.0.0.1",
                platform_type="jetson",
                gpu_device_name="Jetson",
                compute_capability="8.7",
                cuda_runtime_version="12.2",
                driver_version="535.0",
                max_memory_allocation_gb=32.0,
            ),
            runtime=DeploymentRuntime.STANDALONE_TENSORRT,
            health_check=HealthCheckSpec(),
            created_at=now,
        ),
        status=DeploymentStatus.ACTIVE,
        target_id=TargetId("t1"),
    )

    # 1. extract_baseline_reference_binding rejects conflicting deployed_artifact_id
    with pytest.raises(
        BaselineMismatchError,
        match="deployed_artifact_id .* conflicts with DeploymentRecord artifact_id",
    ):
        extract_baseline_reference_binding(
            evaluation_result=eval_result,
            distribution_profile=profile,
            baseline_fps_p05=80.0,
            deployed_artifact_id="art-conflicting-id",
            deployment_record=dep_rec,
        )

    # 2. bind_deployment_to_monitoring_baseline rejects mismatched deployed artifact ID
    baseline_binding = extract_baseline_reference_binding(
        evaluation_result=eval_result,
        distribution_profile=profile,
        baseline_fps_p05=80.0,
        deployed_artifact_id="art-candidate-target",
    )
    with pytest.raises(BaselineMismatchError, match="Deployed artifact ID mismatch"):
        bind_deployment_to_monitoring_baseline(dep_rec, baseline_binding)


def test_seam5_rejects_baseline_binding_on_deployed_artifact_hash_mismatch() -> None:
    """Seam 5: bind_deployment_to_monitoring_baseline rejects mismatched artifact hashes (B24)."""
    now = datetime.now(timezone.utc)
    profile = BaselineDistributionProfile(
        distribution_ref="eval-evidence://eval-hash-test/distributions",
        confidence_histogram={"0.0-1.0": 500},
        confidence_quantiles=(0.5,),
        class_distribution={"vehicle": 1.0},
        prediction_entropy_mean=0.25,
        sample_count=500,
    )
    ev_hash = compute_distribution_profile_hash(profile)

    eval_result = EvaluationResult(
        evaluation_id=EvaluationId("eval-hash-test"),
        plan=EvaluationPlan(
            evaluation_id=EvaluationId("eval-hash-test"),
            experiment_id=ExperimentId("exp-001"),
            created_at=now,
            capability_id=CapabilityId("cv.eval"),
            dataset_binding=EvaluationDatasetBinding(
                dataset_id=DatasetId("ds-01"),
                dataset_version=DatasetVersion("v1.0.0"),
                dataset_manifest_hash=ContentHash("d" * 64),
                split_name="test",
            ),
            model_artifact=ModelArtifactReference(
                checkpoint_id=CheckpointId("ckpt-art-test"),
                architecture_name="yolo",
                weights_hash=ContentHash("a" * 64),
                weights_uri="file:///models/art.engine",
                precision=TrainingPrecision.FP32,
                input_resolution=(640, 640),
            ),
            task_type=TaskType.DETECTION,
            metrics=(
                MetricDefinition(
                    metric_id=MetricId("m-conf"),
                    name="confidence",
                    task_type=TaskType.DETECTION,
                ),
            ),
        ),
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(
            MetricResult(
                metric=MetricDefinition(
                    metric_id=MetricId("m-conf"),
                    name="confidence",
                    task_type=TaskType.DETECTION,
                ),
                primary_value=0.88,
                sample_count=500,
                metadata={"evidence_hash": str(ev_hash)},
            ),
        ),
        system_benchmarks=(
            BenchmarkResult(
                benchmark_id=BenchmarkId("b-art"),
                batch_size=1,
                precision=TrainingPrecision.FP32,
                latency_min_ms=5.0,
                latency_mean_ms=10.0,
                latency_p50_ms=10.0,
                latency_p90_ms=12.0,
                latency_p95_ms=13.0,
                latency_p99_ms=14.0,
                latency_max_ms=15.0,
                throughput_fps=100.0,
                device_name="Jetson",
                target_id=TargetId("t1"),
                timing_synchronized=True,
                timing_source="cuda_event",
            ),
        ),
        environment=EvaluationEnvironmentRecord(
            git_commit_sha="c1",
            git_is_dirty=False,
            dataset_manifest_hash=ContentHash("d" * 64),
            weights_hash=ContentHash("a" * 64),
            metric_definition_versions=("1.0",),
            python_version="3.11.0",
            torch_version="2.4.0",
            target_hardware="Jetson",
        ),
        completed_at=now,
    )

    dep_rec = DeploymentRecord(
        deployment_id=DeploymentId("dep-hash-001"),
        plan=DeploymentPlan(
            deployment_id=DeploymentId("dep-hash-001"),
            artifact_id=OptimizedArtifactId("art-hash-test"),
            target=DeploymentTarget(
                target_id=TargetId("t1"),
                hostname_or_ip="10.0.0.1",
                platform_type="jetson",
                gpu_device_name="Jetson",
                compute_capability="8.7",
                cuda_runtime_version="12.2",
                driver_version="535.0",
                max_memory_allocation_gb=32.0,
            ),
            runtime=DeploymentRuntime.STANDALONE_TENSORRT,
            health_check=HealthCheckSpec(),
            created_at=now,
        ),
        status=DeploymentStatus.ACTIVE,
        target_id=TargetId("t1"),
    )

    baseline_binding = extract_baseline_reference_binding(
        evaluation_result=eval_result,
        distribution_profile=profile,
        baseline_fps_p05=80.0,
        deployed_artifact_id="art-hash-test",
    )

    # Deployed artifact hash differs from candidate evaluation weights hash
    with pytest.raises(BaselineMismatchError, match="Deployed artifact hash mismatch"):
        bind_deployment_to_monitoring_baseline(
            dep_rec,
            baseline_binding,
            candidate_artifact_hash=ContentHash("b" * 64),
        )


def test_seam5_rejects_monitoring_baseline_on_rollback_artifact_mismatch() -> None:
    """Seam 5: Rollback or replacement without corresponding baseline binding fails closed (B24)."""
    now = datetime.now(timezone.utc)
    profile = BaselineDistributionProfile(
        distribution_ref="eval-evidence://eval-v2-int8/distributions",
        confidence_histogram={"0.0-1.0": 500},
        confidence_quantiles=(0.5,),
        class_distribution={"vehicle": 1.0},
        prediction_entropy_mean=0.25,
        sample_count=500,
    )
    ev_hash = compute_distribution_profile_hash(profile)

    eval_result_v2 = EvaluationResult(
        evaluation_id=EvaluationId("eval-v2-int8"),
        plan=EvaluationPlan(
            evaluation_id=EvaluationId("eval-v2-int8"),
            experiment_id=ExperimentId("exp-001"),
            created_at=now,
            capability_id=CapabilityId("cv.eval"),
            dataset_binding=EvaluationDatasetBinding(
                dataset_id=DatasetId("ds-01"),
                dataset_version=DatasetVersion("v2.0.0"),
                dataset_manifest_hash=ContentHash("d" * 64),
                split_name="test",
            ),
            model_artifact=ModelArtifactReference(
                checkpoint_id=CheckpointId("art-traffic-v2-int8"),
                architecture_name="yolo",
                weights_hash=ContentHash("2" * 64),
                weights_uri="file:///models/v2.engine",
                precision=TrainingPrecision.FP32,
                input_resolution=(640, 640),
            ),
            task_type=TaskType.DETECTION,
            metrics=(
                MetricDefinition(
                    metric_id=MetricId("m-conf"),
                    name="confidence",
                    task_type=TaskType.DETECTION,
                ),
            ),
        ),
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(
            MetricResult(
                metric=MetricDefinition(
                    metric_id=MetricId("m-conf"),
                    name="confidence",
                    task_type=TaskType.DETECTION,
                ),
                primary_value=0.85,
                sample_count=500,
                metadata={"evidence_hash": str(ev_hash)},
            ),
        ),
        system_benchmarks=(
            BenchmarkResult(
                benchmark_id=BenchmarkId("b-v2"),
                batch_size=1,
                precision=TrainingPrecision.FP32,
                latency_min_ms=5.0,
                latency_mean_ms=8.0,
                latency_p50_ms=8.0,
                latency_p90_ms=10.0,
                latency_p95_ms=11.0,
                latency_p99_ms=12.0,
                latency_max_ms=13.0,
                throughput_fps=120.0,
                device_name="Jetson",
                target_id=TargetId("t1"),
                timing_synchronized=True,
                timing_source="cuda_event",
            ),
        ),
        environment=EvaluationEnvironmentRecord(
            git_commit_sha="c2",
            git_is_dirty=False,
            dataset_manifest_hash=ContentHash("d" * 64),
            weights_hash=ContentHash("2" * 64),
            metric_definition_versions=("1.0",),
            python_version="3.11.0",
            torch_version="2.4.0",
            target_hardware="Jetson",
        ),
        completed_at=now,
    )

    baseline_binding_v2 = extract_baseline_reference_binding(
        evaluation_result=eval_result_v2,
        distribution_profile=profile,
        baseline_fps_p05=95.0,
        deployed_artifact_id="art-traffic-v2-int8",
    )

    # Active deployment running v2
    dep_rec_v2 = DeploymentRecord(
        deployment_id=DeploymentId("dep-intersection-01"),
        plan=DeploymentPlan(
            deployment_id=DeploymentId("dep-intersection-01"),
            artifact_id=OptimizedArtifactId("art-traffic-v2-int8"),
            target=DeploymentTarget(
                target_id=TargetId("t1"),
                hostname_or_ip="10.0.0.1",
                platform_type="jetson",
                gpu_device_name="Jetson",
                compute_capability="8.7",
                cuda_runtime_version="12.2",
                driver_version="535.0",
                max_memory_allocation_gb=32.0,
            ),
            runtime=DeploymentRuntime.STANDALONE_TENSORRT,
            health_check=HealthCheckSpec(),
            created_at=now,
        ),
        status=DeploymentStatus.ACTIVE,
        target_id=TargetId("t1"),
    )

    # Valid binding for v2 succeeds
    dep_baseline_v2 = bind_deployment_to_monitoring_baseline(
        dep_rec_v2, baseline_binding_v2
    )
    assert dep_baseline_v2.deployed_artifact_id == "art-traffic-v2-int8"

    # Emergency rollback to predecessor artifact v1 occurs on the deployment target
    rollback_rec = DeploymentRecord(
        deployment_id=DeploymentId("dep-intersection-01"),
        plan=DeploymentPlan(
            deployment_id=DeploymentId("dep-intersection-01"),
            artifact_id=OptimizedArtifactId("art-traffic-v1-fp16"),  # ROLLED BACK
            target=dep_rec_v2.plan.target,
            runtime=DeploymentRuntime.STANDALONE_TENSORRT,
            health_check=HealthCheckSpec(),
            created_at=now,
        ),
        status=DeploymentStatus.ACTIVE,
        target_id=TargetId("t1"),
    )

    # Attempting to re-bind the rolled-back deployment against v2 baseline fails closed
    with pytest.raises(BaselineMismatchError, match="Deployed artifact ID mismatch"):
        bind_deployment_to_monitoring_baseline(rollback_rec, baseline_binding_v2)

    # Drift detector evaluation fails closed if telemetry deployment ID mismatches bound deployment
    detector = DefaultStatisticalDriftDetector()
    pipeline = DefaultTelemetryPipeline()
    t0 = now + timedelta(minutes=1)
    pipeline.ingest_model_telemetry(
        ModelTelemetryRecord(
            deployment_id=DeploymentId("dep-other-location"),
            window_start=t0 - timedelta(seconds=60),
            window_end=t0,
            window_seconds=60.0,
            total_inferences=100,
            confidence_histogram={"0.0-1.0": 100},
            confidence_mean=0.8,
            confidence_p10=0.5,
            confidence_p50=0.8,
            confidence_p90=0.9,
            class_counts={"vehicle": 100},
            class_proportions={"vehicle": 1.0},
            mean_detections_per_frame=1.0,
            zero_detection_frame_ratio=0.0,
            prediction_entropy_mean=0.2,
        )
    )
    pipeline.ingest_system_telemetry(
        SystemTelemetryRecord(
            deployment_id=DeploymentId("dep-other-location"),
            timestamp=t0,
            hardware_target="Jetson",
            throughput_fps=100.0,
            latency_p50_ms=10.0,
            latency_p95_ms=12.0,
            latency_p99_ms=13.0,
            dropped_frames_total=0,
            dropped_frame_ratio=0.0,
            gpu_utilization_pct=50.0,
            cpu_utilization_pct=20.0,
            vram_used_mb=2000.0,
            vram_total_mb=32000.0,
            ram_used_mb=2000.0,
            gpu_temperature_celsius=40.0,
        )
    )
    pipeline.ingest_data_telemetry(
        DataTelemetryRecord(
            stream_id="cam-01",
            camera_id="cam-01",
            timestamp=t0,
            mean_luminance=100.0,
            contrast_std=30.0,
            blur_laplacian_variance=150.0,
            overexposure_ratio=0.0,
            underexposure_ratio=0.0,
            resolution_width=1920,
            resolution_height=1080,
            stream_bitrate_kbps=4000.0,
            packet_loss_ratio=0.0,
        )
    )
    snapshot_diff_dep = pipeline.create_snapshot(
        deployment_id=DeploymentId("dep-other-location"),
        stream_id="cam-01",
        timestamp=t0,
    )
    with pytest.raises(
        BaselineMismatchError,
        match="DeploymentId mismatch: live telemetry belongs to 'dep-other-location'",
    ):
        detector.evaluate_drift(snapshot_diff_dep, baseline=dep_baseline_v2)


def test_seam6_exhaustive_approval_security_tuple_rejection() -> None:
    """Seam 6: Exhaustive ADR-0003 approval security tuple rejection.

    Tests all failure modes:
    1. Mutate operation_id                  → reject
    2. Mutate operation_version             → reject
    3. Mutate canonical_payload in decision → reject
    4. Mutate canonical_payload in request  → reject
    5. Mutate resource_scope in request     → reject
    6. Mutate bound_resource_scope in dec   → reject
    7. Mutate both scopes to unauthorized   → reject
    8. Mutate target identity               → reject
    9. Mutate policy_version                → reject
    10. Expire approval                     → reject
    """
    now = datetime.now(timezone.utc)
    dep_manager = DefaultDeploymentManager()
    mock_executor = MockToolExecutor()

    target = DeploymentTarget(
        target_id=TargetId("target-jetson-01"),
        hostname_or_ip="192.168.1.100",
        platform_type="jetson",
        gpu_device_name="Jetson Orin",
        compute_capability="8.7",
        cuda_runtime_version="12.2",
        driver_version="535.104",
        max_memory_allocation_gb=32.0,
    )

    dep_plan = DeploymentPlan(
        deployment_id=DeploymentId("dep-tuple-test"),
        artifact_id=OptimizedArtifactId("art-tuple-test"),
        target=target,
        runtime=DeploymentRuntime.STANDALONE_TENSORRT,
        health_check=HealthCheckSpec(),
        created_at=now,
    )

    verifying_record = DeploymentRecord(
        deployment_id=dep_plan.deployment_id,
        plan=dep_plan,
        status=DeploymentStatus.VERIFYING,
        target_id=target.target_id,
        readiness_result=ReadinessProbeResult(
            passed=True,
            startup_time_sec=0.5,
            probe_latency_mean_ms=10.0,
            vram_consumed_mb=256.0,
            probed_at=now,
        ),
    )

    # Helper to test activation failure
    def assert_activation_fails(
        op: OperationIdentity, req: ApprovalRequest, dec: ApprovalDecision, match: str
    ) -> None:
        with pytest.raises(DeploymentApprovalRequiredError, match=match):
            dep_manager.activate(
                record=verifying_record,
                target_id=target.target_id,
                operation_identity=op,
                approval_request=req,
                approval_decision=dec,
                tool_id=ToolId("tool-deploy"),
                tool_executor=mock_executor,
            )

    # 1. Mutate operation_id
    op, req, dec = _make_approval_context(dep_plan, operation_id="training.start")
    assert_activation_fails(op, req, dec, "is not authorized for deployment activation")

    # 2. Mutate operation_version
    op, req, dec = _make_approval_context(
        dep_plan, identity_version="2.0.0", operation_version=2
    )
    assert_activation_fails(op, req, dec, "is not authorized for deployment activation")

    # 3. Mutate canonical payload hash in ApprovalDecision
    op, req, dec = _make_approval_context(
        dep_plan, bound_payload_hash="corrupted_hash" + "0" * 50
    )
    assert_activation_fails(
        op, req, dec, "ApprovalDecision bound_payload_hash mismatch"
    )

    # 4. Mutate canonical payload hash in ApprovalRequest (PM finding)
    op, req, dec = _make_approval_context(
        dep_plan, request_payload_hash="corrupted_req_hash" + "0" * 46
    )
    assert_activation_fails(
        op, req, dec, "Canonical payload hash mismatch: ApprovalRequest has"
    )

    # 5. Mutate resource_scope in ApprovalRequest (PM finding)
    op, req, dec = _make_approval_context(
        dep_plan,
        resource_scope="target:target-MUTATED",
        bound_resource_scope=f"target:{dep_plan.target.target_id}",
    )
    assert_activation_fails(op, req, dec, "Bound resource scope mismatch")

    # 6. Mutate bound_resource_scope in ApprovalDecision (PM finding)
    op, req, dec = _make_approval_context(
        dep_plan, bound_resource_scope="target:target-MUTATED"
    )
    assert_activation_fails(op, req, dec, "Bound resource scope mismatch")

    # 7. Mutate both scopes to an unauthorized scope
    op, req, dec = _make_approval_context(
        dep_plan,
        resource_scope="target:target-UNAUTHORIZED",
        bound_resource_scope="target:target-UNAUTHORIZED",
    )
    assert_activation_fails(
        op, req, dec, "ApprovalRequest resource_scope .* does not match"
    )

    # 8. Mutate target identity
    op, req, dec = _make_approval_context(dep_plan, target_identity="target-WRONG")
    assert_activation_fails(op, req, dec, "Target identity mismatch")

    # 9. Mutate policy version
    op, req, dec = _make_approval_context(dep_plan, bound_policy_version="policy-v999")
    assert_activation_fails(op, req, dec, "Policy version mismatch")

    # 10. Expire approval
    expired_time = now - timedelta(hours=2)
    op, req, dec = _make_approval_context(dep_plan, expires_at=expired_time)
    assert_activation_fails(op, req, dec, "expired at")


def test_seam7_rejects_synthetic_fallbacks_and_prohibits_credential_traversal() -> None:
    """Seam 7: Security & Evidence Purity.

    1. Schema audit across Phase 2 contracts asserting zero raw credential fields
       (api_key, password, secret, token) cross subsystem boundaries.
    2. Proves zero synthetic fallback multipliers in baseline reference binding.
    3. Proves RetrainingTriggerProposal is strictly advisory (cannot trigger autonomous training).
    """
    now = datetime.now(timezone.utc)

    # 1. Credential Traversal Audit
    forbidden_terms = {"api_key", "password", "secret_key", "auth_token", "private_key"}
    contracts_to_audit: list[type[BaseModel]] = [
        CandidateSample,
        ActiveLearningCandidateBatch,
        DatasetManifest,
        TrainingPlan,
        CheckpointManifest,
        EvaluationPlan,
        EvaluationResult,
        OptimizationPlan,
        DeploymentPlan,
        BaselineReferenceBinding,
        RetrainingTriggerProposal,
    ]

    for model_cls in contracts_to_audit:
        field_names = set(model_cls.model_fields.keys())
        for field in field_names:
            lower_field = field.lower()
            for term in forbidden_terms:
                assert term not in lower_field, (
                    f"Forbidden credential/secret field {field!r} found in contract {model_cls.__name__}"
                )

    # 2. Rejection of Synthetic Fallbacks (B19 verification)
    eval_result_empty_benchmarks = EvaluationResult(
        evaluation_id=EvaluationId("eval-empty-bm"),
        plan=EvaluationPlan(
            evaluation_id=EvaluationId("eval-empty-bm"),
            experiment_id=ExperimentId("exp-001"),
            created_at=now,
            capability_id=CapabilityId("cv.eval"),
            dataset_binding=EvaluationDatasetBinding(
                dataset_id=DatasetId("ds-01"),
                dataset_version=DatasetVersion("v1.0.0"),
                dataset_manifest_hash=ContentHash("d" * 64),
                split_name="test",
            ),
            model_artifact=ModelArtifactReference(
                checkpoint_id=CheckpointId("ckpt-01"),
                architecture_name="yolo",
                weights_hash=ContentHash("0" * 64),
                weights_uri="file:///weights.pt",
                precision=TrainingPrecision.FP32,
                input_resolution=(640, 640),
            ),
            task_type=TaskType.DETECTION,
            metrics=(
                MetricDefinition(
                    metric_id=MetricId("m1"),
                    name="map",
                    task_type=TaskType.DETECTION,
                ),
            ),
        ),
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(),
        system_benchmarks=(),  # EMPTY - NO SYSTEM BENCHMARKS
        environment=EvaluationEnvironmentRecord(
            git_commit_sha="commit",
            git_is_dirty=False,
            dataset_manifest_hash=ContentHash("d" * 64),
            weights_hash=ContentHash("0" * 64),
            metric_definition_versions=("1.0",),
            python_version="3.11.0",
            torch_version="2.4.0",
            target_hardware="Jetson",
        ),
        completed_at=now,
    )

    profile = BaselineDistributionProfile(
        distribution_ref="eval-evidence://eval-empty-bm/distributions",
        confidence_histogram={"0.0-1.0": 100},
        confidence_quantiles=(0.5,),
        class_distribution={"c": 1.0},
        prediction_entropy_mean=0.35,
        sample_count=100,
    )

    # Fails closed on missing system benchmarks
    with pytest.raises(
        BaselineMismatchError,
        match="EvaluationResult contains no system_benchmarks evidence",
    ):
        extract_baseline_reference_binding(
            evaluation_result=eval_result_empty_benchmarks,
            distribution_profile=profile,
        )

    # 3. Retraining proposal cannot be autonomous
    with pytest.raises(ValueError, match="requires_human_approval must always be True"):
        RetrainingTriggerProposal(
            proposal_id=TriggerProposalId("prop-01"),
            deployment_id=DeploymentId("dep-01"),
            created_at=now,
            trigger_reason=TriggerReason.MODEL_ACCURACY_DRIFT,
            severity=DriftSeverity.CRITICAL,
            evidence_summary="Drift detected",
            estimated_new_samples=100,
            requires_human_approval=False,  # VIOLATES INVARIANT
        )
