"""tests/test_optimization_boundary.py — Adversarial boundary test suite for ADR-0012.

Exercises all 12 architectural invariants and review requirements:
1. Dynamic shape profile monotonicity and strict validation (Invariant 5)
2. Calibration dataset split leakage prevention (ADR-0009 Invariant 13)
3. Mandatory post-optimization verification gate against baseline (Invariant 3, [P§14])
4. Hardware-aware target compilation and compute capability consistency (Invariant 4, D-022)
5. Zero local identity / approval synthesis; strict ADR-0003 4-tuple verification (Invariant 7)
6. Automated readiness probing and health check failure containment (Invariant 8)
7. Deterministic rollback with null-predecessor OPERATOR_REQUIRED escalation (Invariant 9)
8. Mandatory ToolExecutor and execution failure containment via ADR-0005 (Invariant 10)
9. Zero physical transport leakage (AST audit proves 0 subprocess / shell calls)
10. Deep defensive immutability across contracts and runtime packaging
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

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
    OperationIdentity,
    SplitMembership,
)
from cv_agent.domain import CapabilityId
from cv_agent.evaluation.comparison import DefaultBaselineComparator
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
from cv_agent.optimization.canonicalization import (
    compute_artifact_lineage_hash,
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
    OptimizationResult,
    OptimizedModelArtifact,
    ReadinessProbeResult,
)
from cv_agent.optimization.deployment import DefaultDeploymentManager
from cv_agent.optimization.errors import (
    CalibrationLeakageError,
    DeploymentApprovalRequiredError,
    DeploymentValidationError,
    OptimizationExecutionError,
    OptimizationValidationError,
    OptimizationVerificationError,
    ReadinessProbeFailureError,
    RollbackExecutionError,
)
from cv_agent.optimization.optimizer import DefaultModelOptimizer
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
from cv_agent.training import CheckpointId, CheckpointManifest, PlanId, TrainingJobId


# =============================================================================
# Test Fixture Helpers
# =============================================================================
def _make_valid_shape_profile() -> DynamicShapeProfile:
    return DynamicShapeProfile(
        tensor_name="input_images",
        min_shape=(1, 3, 384, 384),
        opt_shape=(4, 3, 640, 640),
        max_shape=(8, 3, 1280, 1280),
    )


def _make_valid_calibration_binding() -> CalibrationDatasetBinding:
    return CalibrationDatasetBinding(
        dataset_id=DatasetId("dataset-traffic-cctv"),
        dataset_version=DatasetVersion("v1.0.0"),
        dataset_manifest_hash=ContentHash(
            "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        ),
        split_name="calibration",
        num_calibration_samples=256,
        calibration_algorithm=CalibrationAlgorithm.ENTROPY,
    )


def _make_valid_verification_binding() -> EvaluationDatasetBinding:
    return EvaluationDatasetBinding(
        dataset_id=DatasetId("dataset-traffic-cctv"),
        dataset_version=DatasetVersion("v1.0.0"),
        dataset_manifest_hash=ContentHash(
            "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        ),
        split_name="test",
    )


def _make_valid_optimization_plan(
    plan_id: str = "opt-plan-001",
    technique: OptimizationTechnique = OptimizationTechnique.TENSORRT_COMPILATION,
    precision: OptimizationPrecision = OptimizationPrecision.FP16,
    max_accuracy_drop_tolerance: float = 0.015,
) -> OptimizationPlan:
    calib = (
        _make_valid_calibration_binding()
        if precision == OptimizationPrecision.INT8
        or technique == OptimizationTechnique.PTQ_QUANTIZATION
        else None
    )
    return OptimizationPlan(
        plan_id=OptimizationPlanId(plan_id),
        source_checkpoint_id=CheckpointId("ckpt-yolo-v8x-001"),
        source_weights_hash=ContentHash(
            "sha256:2222222222222222222222222222222222222222222222222222222222222222"
        ),
        technique=technique,
        target_precision=precision,
        target_platform="nvidia_jetson_agx_orin",
        target_compute_capability="8.7",
        shape_profiles=(_make_valid_shape_profile(),),
        calibration_binding=calib,
        verification_dataset_binding=_make_valid_verification_binding(),
        max_accuracy_drop_tolerance=max_accuracy_drop_tolerance,
        parameters={"workspace_size_mb": 4096},
        created_at=datetime.now(timezone.utc),
    )


def _make_valid_artifact(plan: OptimizationPlan) -> OptimizedModelArtifact:
    lineage_hash = compute_artifact_lineage_hash(plan)
    return OptimizedModelArtifact(
        artifact_id=OptimizedArtifactId(f"art-{plan.plan_id}"),
        plan_id=plan.plan_id,
        source_checkpoint_id=plan.source_checkpoint_id,
        technique=plan.technique,
        precision=plan.target_precision,
        runtime_format="tensorrt_engine",
        target_device="target-jetson-01",
        target_compute_capability=plan.target_compute_capability,
        artifact_hash=lineage_hash,
        artifact_uri=f"artifact://models/{plan.plan_id}/model.engine",
        artifact_size_bytes=85000000,
        shape_profiles=plan.shape_profiles,
        created_at=datetime.now(timezone.utc),
    )


def _make_valid_deployment_target() -> DeploymentTarget:
    return DeploymentTarget(
        target_id=TargetId("target-jetson-01"),
        hostname_or_ip="192.168.1.100",
        platform_type="nvidia_jetson_agx_orin",
        gpu_device_name="NVIDIA Jetson AGX Orin 64GB",
        compute_capability="8.7",
        cuda_runtime_version="12.2",
        driver_version="535.104.05",
        max_memory_allocation_gb=48.0,
        power_mode="MAXN",
    )


def _make_valid_deployment_plan(
    artifact_id: str = "art-opt-plan-001",
    deployment_id: str = "dep-001",
    rollback_target_id: str | None = None,
) -> DeploymentPlan:
    return DeploymentPlan(
        deployment_id=DeploymentId(deployment_id),
        artifact_id=OptimizedArtifactId(artifact_id),
        target=_make_valid_deployment_target(),
        runtime=DeploymentRuntime.STANDALONE_TENSORRT,
        runtime_config={"cuda_streams": 2},
        health_check=HealthCheckSpec(
            startup_timeout_sec=30.0,
            readiness_probe_samples=5,
            max_latency_threshold_ms=25.0,
            memory_headroom_mb=1024.0,
        ),
        rollback_target_id=DeploymentId(rollback_target_id)
        if rollback_target_id
        else None,
        created_at=datetime.now(timezone.utc),
    )


def _make_valid_checkpoint_manifest() -> CheckpointManifest:
    return CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-yolo-v8x-001"),
        job_id=TrainingJobId("job-train-001"),
        plan_id=PlanId("plan-train-001"),
        epoch=50,
        step=25000,
        metrics={"val_loss": 0.32, "mAP50": 0.785},
        weights_hash=ContentHash(
            "sha256:2222222222222222222222222222222222222222222222222222222222222222"
        ),
        weights_uri="s3://models/checkpoints/ckpt-yolo-v8x-001.pt",
        created_at=datetime.now(timezone.utc),
    )


def _make_valid_dataset_manifest(has_calibration: bool = True) -> DatasetManifest:
    n_items = 4 if has_calibration else 3
    return DatasetManifest(
        dataset_id=DatasetId("dataset-traffic-cctv"),
        version=DatasetVersion("v1.0.0"),
        created=datetime.now(timezone.utc),
        sources=("site-1",),
        n_items=n_items,
        class_definitions=(
            ClassDefinition(
                name="vehicle",
                description="traffic vehicle",
                near_miss_negatives=(),
                ambiguity_resolution="none",
            ),
        ),
        class_distribution={"vehicle": n_items},
        split_policy="random",
        split_membership=SplitMembership(
            train_hashes=(ContentHash("1" * 64),),
            val_hashes=(ContentHash("2" * 64),),
            test_hashes=(ContentHash("3" * 64),),
            calibration_hashes=(ContentHash("4" * 64),) if has_calibration else (),
        ),
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guidelines"),
        lineage=DatasetLineage(),
        license_and_constraints="MIT",
        collection_conditions=CollectionConditions(
            camera_models=("cam1",),
            sites=("site1",),
            times_of_day=("day",),
            lighting_conditions=("bright",),
            weather_conditions=("clear",),
        ),
        payload_root_checksum=ContentHash(
            "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        ),
        leakage_audit_records=(),
    )


def _make_valid_evaluation_plan(eval_id: str, exp_id: str) -> EvaluationPlan:
    return EvaluationPlan(
        evaluation_id=EvaluationId(eval_id),
        experiment_id=ExperimentId(exp_id),
        created_at=datetime.now(timezone.utc),
        capability_id=CapabilityId("cv.eval.detector"),
        dataset_binding=_make_valid_verification_binding(),
        model_artifact=ModelArtifactReference(
            checkpoint_id=CheckpointId("ckpt-yolo-v8x-001"),
            architecture_name="yolov8x",
            weights_hash=ContentHash(
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            weights_uri="s3://models/checkpoints/ckpt-yolo-v8x-001.pt",
            precision=TrainingPrecision.FP32,
            input_resolution=(640, 640),
        ),
        task_type=TaskType.DETECTION,
        metrics=(
            MetricDefinition(
                metric_id=MetricId("val-map50"),
                name="map50",
                task_type=TaskType.DETECTION,
                version="1.0",
                higher_is_better=True,
            ),
        ),
    )


def _make_valid_evaluation_env() -> EvaluationEnvironmentRecord:
    return EvaluationEnvironmentRecord(
        git_commit_sha="0" * 40,
        git_is_dirty=False,
        dataset_manifest_hash=ContentHash(
            "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        ),
        weights_hash=ContentHash(
            "sha256:2222222222222222222222222222222222222222222222222222222222222222"
        ),
        metric_definition_versions=("1.0",),
        python_version="3.11.0",
        torch_version="2.4.0",
        target_hardware="NVIDIA Jetson AGX Orin",
    )


def _make_valid_evaluation_result(
    exp_id: str,
    map50: float = 0.785,
    fps: float = 65.0,
    latency_p50: float = 15.3,
    peak_vram_gb: float = 4.2,
) -> EvaluationResult:
    metric_def = MetricDefinition(
        metric_id=MetricId("val-map50"),
        name="map50",
        task_type=TaskType.DETECTION,
        version="1.0",
        higher_is_better=True,
    )
    metric_res = MetricResult(
        metric=metric_def,
        primary_value=map50,
        sample_count=500,
    )
    bench_res = BenchmarkResult(
        benchmark_id=BenchmarkId("bench-target-01"),
        batch_size=1,
        precision=TrainingPrecision.FP16_AMP,
        latency_min_ms=10.0,
        latency_mean_ms=latency_p50,
        latency_p50_ms=latency_p50,
        latency_p90_ms=latency_p50 + 2.0,
        latency_p95_ms=latency_p50 + 3.0,
        latency_p99_ms=latency_p50 + 4.0,
        latency_max_ms=latency_p50 + 6.0,
        throughput_fps=fps,
        peak_vram_gb=peak_vram_gb,
        cpu_utilization_pct=25.0,
        gpu_utilization_pct=85.0,
        device_name="NVIDIA Jetson AGX Orin",
        target_id=TargetId("target-jetson-01"),
        timing_synchronized=True,
        timing_source="cuda_event",
    )
    plan = _make_valid_evaluation_plan(eval_id=f"eval-{exp_id}", exp_id=exp_id)
    return EvaluationResult(
        evaluation_id=plan.evaluation_id,
        plan=plan,
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(metric_res,),
        system_benchmarks=(bench_res,),
        environment=_make_valid_evaluation_env(),
        completed_at=datetime.now(timezone.utc),
    )


def _make_approval_context(
    plan: DeploymentPlan,
    resource_scope: str | None = None,
    operation_version: int = 1,
) -> tuple[OperationIdentity, ApprovalRequest, ApprovalDecision]:
    extract_deployment_operation_payload(plan)
    canonical_hash = compute_deployment_payload_hash(plan)
    scope = resource_scope or f"target:{plan.target.target_id}"

    op_id = OperationIdentity(
        operation_id="op-dep-001",
        operation_version=str(operation_version),
        canonical_payload_hash=canonical_hash,
        target_identity=scope,
    )

    app_req = ApprovalRequest(
        request_id="req-dep-001",
        session_id="sess-001",
        operation_id=OperationId(value="op-dep-001"),
        operation_version=operation_version,
        canonical_payload_hash=canonical_hash,
        policy_version="1.0",
        action_type="deployment.activate",
        risk_level="HIGH",
        resource_scope=scope,
        created_at=datetime.now(timezone.utc),
    )

    app_dec = ApprovalDecision(
        request_id="req-dep-001",
        decision=ApprovalStatus.APPROVED,
        principal="human_pm",
        resolved_at=datetime.now(timezone.utc),
        bound_payload_hash=canonical_hash,
        bound_resource_scope=scope,
        bound_policy_version="1.0",
    )

    return op_id, app_req, app_dec


class _MockToolExecutor:
    """Mock ADR-0005 ToolExecutor returning synthetic telemetry."""

    def __init__(
        self,
        status: ToolExecutionStatus = ToolExecutionStatus.SUCCESS,
        error_message: str | None = None,
        structured_output: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.error_message = error_message
        self.structured_output = structured_output or {}
        self.invocations: list[ToolInvocation] = []

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        self.invocations.append(invocation)
        return ToolResult(
            tool_id=invocation.tool_id,
            execution_id=invocation.execution_id,
            target_id=invocation.target_id,
            status=self.status,
            error_message=self.error_message,
            structured_output=self.structured_output,
        )

    async def probe(self, tool: Any, target: Any) -> Any:
        raise NotImplementedError


# =============================================================================
# Test 1: Dynamic shape profile monotonicity validation (Invariant 5)
# =============================================================================
def test_dynamic_shape_monotonicity_violated_fails_closed() -> None:
    """Verify DynamicShapeProfile strictly enforces min_shape <= opt_shape <= max_shape."""
    # 1. Violation: min > opt
    with pytest.raises(ValueError, match="Dynamic shape monotonicity violated"):
        DynamicShapeProfile(
            tensor_name="input",
            min_shape=(4, 3, 640, 640),
            opt_shape=(2, 3, 640, 640),  # opt < min!
            max_shape=(8, 3, 640, 640),
        )

    # 2. Violation: opt > max
    with pytest.raises(ValueError, match="Dynamic shape monotonicity violated"):
        DynamicShapeProfile(
            tensor_name="input",
            min_shape=(1, 3, 640, 640),
            opt_shape=(16, 3, 640, 640),  # opt > max!
            max_shape=(8, 3, 640, 640),
        )

    # 3. Violation: rank mismatch
    with pytest.raises(
        ValueError, match="Dynamic shape dimensions must have equal rank"
    ):
        DynamicShapeProfile(
            tensor_name="input",
            min_shape=(1, 3, 640),
            opt_shape=(4, 3, 640, 640),
            max_shape=(8, 3, 640, 640),
        )

    # 4. Violation: non-positive dimension
    with pytest.raises(ValueError, match="min_shape must be strictly positive"):
        DynamicShapeProfile(
            tensor_name="input",
            min_shape=(0, 3, 640, 640),
            opt_shape=(4, 3, 640, 640),
            max_shape=(8, 3, 640, 640),
        )


# =============================================================================
# Test 2: Dynamic shape profile empty tensor name rejected
# =============================================================================
def test_dynamic_shape_empty_tensor_name_rejected() -> None:
    """Verify DynamicShapeProfile rejects empty or whitespace tensor names."""
    for bad_name in ["", "   "]:
        with pytest.raises(ValueError, match="tensor_name cannot be empty"):
            DynamicShapeProfile(
                tensor_name=bad_name,
                min_shape=(1, 3, 640, 640),
                opt_shape=(4, 3, 640, 640),
                max_shape=(8, 3, 640, 640),
            )


# =============================================================================
# Test 3: Optimization plan duplicate shape profile tensor names rejected
# =============================================================================
def test_optimization_plan_duplicate_shape_profile_names_rejected() -> None:
    """Verify OptimizationPlan rejects duplicate DynamicShapeProfile tensor names."""
    sp1 = DynamicShapeProfile(
        tensor_name="images",
        min_shape=(1, 3, 384, 384),
        opt_shape=(2, 3, 640, 640),
        max_shape=(4, 3, 640, 640),
    )
    sp2 = DynamicShapeProfile(
        tensor_name="images",  # Duplicate!
        min_shape=(1, 3, 384, 384),
        opt_shape=(4, 3, 640, 640),
        max_shape=(8, 3, 640, 640),
    )
    with pytest.raises(
        OptimizationValidationError, match="Duplicate shape profile declared"
    ):
        OptimizationPlan(
            plan_id=OptimizationPlanId("opt-dup-001"),
            source_checkpoint_id=CheckpointId("ckpt-001"),
            source_weights_hash=ContentHash(
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            technique=OptimizationTechnique.TENSORRT_COMPILATION,
            target_precision=OptimizationPrecision.FP16,
            target_platform="orin",
            target_compute_capability="8.7",
            shape_profiles=(sp1, sp2),
            verification_dataset_binding=_make_valid_verification_binding(),
            created_at=datetime.now(timezone.utc),
        )


# =============================================================================
# Test 4: PTQ quantization missing calibration binding fails closed (Invariant 2)
# =============================================================================
def test_ptq_quantization_missing_calibration_binding_fails_closed() -> None:
    """Verify INT8 PTQ quantization without CalibrationDatasetBinding fails closed."""
    # 1. PTQ technique without calibration_binding
    with pytest.raises(
        OptimizationValidationError, match="CalibrationDatasetBinding is mandatory"
    ):
        OptimizationPlan(
            plan_id=OptimizationPlanId("opt-ptq-no-calib"),
            source_checkpoint_id=CheckpointId("ckpt-001"),
            source_weights_hash=ContentHash(
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            technique=OptimizationTechnique.PTQ_QUANTIZATION,
            target_precision=OptimizationPrecision.INT8,
            target_platform="orin",
            target_compute_capability="8.7",
            calibration_binding=None,  # Missing!
            verification_dataset_binding=_make_valid_verification_binding(),
            created_at=datetime.now(timezone.utc),
        )

    # 2. Precision INT8 without calibration_binding
    with pytest.raises(
        OptimizationValidationError, match="CalibrationDatasetBinding is mandatory"
    ):
        OptimizationPlan(
            plan_id=OptimizationPlanId("opt-int8-no-calib"),
            source_checkpoint_id=CheckpointId("ckpt-001"),
            source_weights_hash=ContentHash(
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            technique=OptimizationTechnique.TENSORRT_COMPILATION,
            target_precision=OptimizationPrecision.INT8,
            target_platform="orin",
            target_compute_capability="8.7",
            calibration_binding=None,  # Missing!
            verification_dataset_binding=_make_valid_verification_binding(),
            created_at=datetime.now(timezone.utc),
        )


# =============================================================================
# Test 5: Calibration dataset split leakage rejected (ADR-0009 Invariant 13)
# =============================================================================
def test_calibration_split_leakage_rejected_invariant_13() -> None:
    """Verify CalibrationDatasetBinding rejects test/evaluation splits to prevent data leakage."""
    leakage_splits = ["test", "evaluation", "eval", "val_test"]
    for bad_split in leakage_splits:
        with pytest.raises(
            CalibrationLeakageError, match="overlaps with evaluation/test splits"
        ):
            CalibrationDatasetBinding(
                dataset_id=DatasetId("dataset-001"),
                dataset_version=DatasetVersion("v1.0.0"),
                dataset_manifest_hash=ContentHash(
                    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
                ),
                split_name=bad_split,
            )


# =============================================================================
# Test 6: Verification dataset binding calibration split rejected (Invariant 13)
# =============================================================================
def test_verification_dataset_binding_calibration_split_rejected() -> None:
    """Verify OptimizationPlan rejects 'calibration' split in verification_dataset_binding."""
    calib_as_eval = EvaluationDatasetBinding(
        dataset_id=DatasetId("dataset-001"),
        dataset_version=DatasetVersion("v1.0.0"),
        dataset_manifest_hash=ContentHash(
            "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        ),
        split_name="calibration",  # Invariant 13 violation!
    )
    with pytest.raises(
        OptimizationValidationError,
        match="verification_dataset_binding split cannot be 'calibration'",
    ):
        OptimizationPlan(
            plan_id=OptimizationPlanId("opt-plan-leak"),
            source_checkpoint_id=CheckpointId("ckpt-001"),
            source_weights_hash=ContentHash(
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            technique=OptimizationTechnique.TENSORRT_COMPILATION,
            target_precision=OptimizationPrecision.FP16,
            target_platform="orin",
            target_compute_capability="8.7",
            verification_dataset_binding=calib_as_eval,
            created_at=datetime.now(timezone.utc),
        )


# =============================================================================
# Test 7: Non-finite accuracy drop tolerance rejected
# =============================================================================
def test_non_finite_accuracy_tolerance_rejected() -> None:
    """Verify max_accuracy_drop_tolerance rejects NaN, Inf, and negative values."""
    for bad_tol in [float("nan"), float("inf"), float("-inf")]:
        with pytest.raises(ValueError, match="must be a finite float"):
            OptimizationPlan(
                plan_id=OptimizationPlanId("opt-plan-nan"),
                source_checkpoint_id=CheckpointId("ckpt-001"),
                source_weights_hash=ContentHash(
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
                ),
                technique=OptimizationTechnique.TENSORRT_COMPILATION,
                target_precision=OptimizationPrecision.FP16,
                target_platform="orin",
                target_compute_capability="8.7",
                verification_dataset_binding=_make_valid_verification_binding(),
                max_accuracy_drop_tolerance=bad_tol,
                created_at=datetime.now(timezone.utc),
            )

    with pytest.raises(ValidationError):
        OptimizationPlan(
            plan_id=OptimizationPlanId("opt-plan-neg"),
            source_checkpoint_id=CheckpointId("ckpt-001"),
            source_weights_hash=ContentHash(
                "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            ),
            technique=OptimizationTechnique.TENSORRT_COMPILATION,
            target_precision=OptimizationPrecision.FP16,
            target_platform="orin",
            target_compute_capability="8.7",
            verification_dataset_binding=_make_valid_verification_binding(),
            max_accuracy_drop_tolerance=-0.05,
            created_at=datetime.now(timezone.utc),
        )


# =============================================================================
# Test 8: Target platform or compute capability empty rejected
# =============================================================================
def test_target_platform_or_compute_capability_empty_rejected() -> None:
    """Verify target_platform and target_compute_capability cannot be empty."""
    for bad_val in ["", "   "]:
        with pytest.raises(
            OptimizationValidationError, match="target_platform cannot be empty"
        ):
            OptimizationPlan(
                plan_id=OptimizationPlanId("opt-bad-plat"),
                source_checkpoint_id=CheckpointId("ckpt-001"),
                source_weights_hash=ContentHash(
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
                ),
                technique=OptimizationTechnique.TENSORRT_COMPILATION,
                target_precision=OptimizationPrecision.FP16,
                target_platform=bad_val,
                target_compute_capability="8.7",
                verification_dataset_binding=_make_valid_verification_binding(),
                created_at=datetime.now(timezone.utc),
            )

        with pytest.raises(
            OptimizationValidationError,
            match="target_compute_capability cannot be empty",
        ):
            OptimizationPlan(
                plan_id=OptimizationPlanId("opt-bad-cc"),
                source_checkpoint_id=CheckpointId("ckpt-001"),
                source_weights_hash=ContentHash(
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222"
                ),
                technique=OptimizationTechnique.TENSORRT_COMPILATION,
                target_precision=OptimizationPrecision.FP16,
                target_platform="orin",
                target_compute_capability=bad_val,
                verification_dataset_binding=_make_valid_verification_binding(),
                created_at=datetime.now(timezone.utc),
            )


# =============================================================================
# Test 9: Defensive immutability across plan parameters and runtime config
# =============================================================================
def test_defensive_immutability_plan_parameters_and_runtime_config() -> None:
    """Verify OptimizationPlan and DeploymentPlan enforce MappingProxyType and frozen models."""
    plan = _make_valid_optimization_plan()
    dep_plan = _make_valid_deployment_plan()

    # 1. Pydantic model is frozen
    with pytest.raises(ValidationError):
        plan.target_platform = "x86"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        dep_plan.runtime = DeploymentRuntime.ONNX_RUNTIME  # type: ignore[misc]

    # 2. Parameters dictionary is MappingProxyType
    with pytest.raises(TypeError):
        plan.parameters["workspace_size_mb"] = 8192  # type: ignore[index]

    with pytest.raises(TypeError):
        dep_plan.runtime_config["cuda_streams"] = 4  # type: ignore[index]


# =============================================================================
# Test 10: Checkpoint manifest weights hash mismatch fails closed
# =============================================================================
def test_checkpoint_manifest_weights_hash_mismatch_fails_closed() -> None:
    """Verify DefaultModelOptimizer rejects checkpoint manifest with mismatched weights hash."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()
    ckpt = _make_valid_checkpoint_manifest().model_copy(
        update={
            "weights_hash": ContentHash(
                "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
            )
        }
    )

    with pytest.raises(OptimizationValidationError, match="Weights hash mismatch"):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=_MockToolExecutor(),
            checkpoint_manifest=ckpt,
        )


# =============================================================================
# Test 11: Checkpoint manifest checkpoint ID mismatch fails closed
# =============================================================================
def test_checkpoint_manifest_checkpoint_id_mismatch_fails_closed() -> None:
    """Verify DefaultModelOptimizer rejects checkpoint manifest with mismatched checkpoint ID."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()
    ckpt = _make_valid_checkpoint_manifest().model_copy(
        update={"checkpoint_id": CheckpointId("ckpt-different-id")}
    )

    with pytest.raises(OptimizationValidationError, match="Checkpoint ID mismatch"):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=_MockToolExecutor(),
            checkpoint_manifest=ckpt,
        )


# =============================================================================
# Test 12: Dataset manifest hash mismatch fails closed
# =============================================================================
def test_dataset_manifest_hash_mismatch_fails_closed() -> None:
    """Verify DefaultModelOptimizer rejects calibration dataset manifest hash mismatch."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan(
        technique=OptimizationTechnique.PTQ_QUANTIZATION,
        precision=OptimizationPrecision.INT8,
    )
    dataset_manifest = _make_valid_dataset_manifest().model_copy(
        update={
            "payload_root_checksum": ContentHash(
                "sha256:9999999999999999999999999999999999999999999999999999999999999999"
            )
        }
    )

    with pytest.raises(
        CalibrationLeakageError, match="Calibration manifest hash mismatch"
    ):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=_MockToolExecutor(),
            dataset_manifest=dataset_manifest,
        )


# =============================================================================
# Test 13: Dataset manifest missing calibration split fails closed
# =============================================================================
def test_dataset_manifest_missing_calibration_split_fails_closed() -> None:
    """Verify DefaultModelOptimizer rejects dataset manifest missing declared calibration split."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan(
        technique=OptimizationTechnique.PTQ_QUANTIZATION,
        precision=OptimizationPrecision.INT8,
    )
    # Manifest without 'calibration' split
    bad_manifest = _make_valid_dataset_manifest(has_calibration=False)

    with pytest.raises(
        CalibrationLeakageError, match="Calibration split 'calibration' not declared"
    ):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=_MockToolExecutor(),
            dataset_manifest=bad_manifest,
        )


# =============================================================================
# Test 14: Optimizer compile mandatory tool_id and target_id (Zero synthesis)
# =============================================================================
def test_optimizer_compile_mandatory_tool_id_and_target_id() -> None:
    """Verify DefaultModelOptimizer requires mandatory tool_id and target_id without local fallback."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()

    for bad_id in [cast(ToolId, None), cast(ToolId, ""), cast(ToolId, "   ")]:
        with pytest.raises(OptimizationExecutionError, match="tool_id is mandatory"):
            optimizer.compile(
                plan=plan,
                tool_id=bad_id,  # type: ignore[arg-type]
                target_id=TargetId("target-01"),
                tool_executor=_MockToolExecutor(),
            )

    for bad_target in [cast(TargetId, None), cast(TargetId, ""), cast(TargetId, "   ")]:
        with pytest.raises(OptimizationExecutionError, match="target_id is mandatory"):
            optimizer.compile(
                plan=plan,
                tool_id=ToolId("cv.optimize.compile"),
                target_id=bad_target,  # type: ignore[arg-type]
                tool_executor=_MockToolExecutor(),
            )


# =============================================================================
# Test 15: Optimizer compile mandatory tool_executor (Invariant 10)
# =============================================================================
def test_optimizer_compile_mandatory_tool_executor() -> None:
    """Verify DefaultModelOptimizer strictly requires ToolExecutor."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()

    with pytest.raises(OptimizationExecutionError, match="tool_executor is mandatory"):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-01"),
            tool_executor=None,  # type: ignore[arg-type]
        )


# =============================================================================
# Test 16: Optimizer compile execution failure fails closed
# =============================================================================
def test_optimizer_compile_execution_failure_fails_closed() -> None:
    """Verify DefaultModelOptimizer raises OptimizationExecutionError when tool execution fails."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()
    executor = _MockToolExecutor(
        status=ToolExecutionStatus.EXECUTION_FAILED,
        error_message="CUDA out of memory during layer fusion auto-tuning",
    )

    with pytest.raises(
        OptimizationExecutionError,
        match="CUDA out of memory during layer fusion auto-tuning",
    ):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=executor,
        )


# =============================================================================
# Test 17: Cryptographic transformation lineage hash integrity (ADR-0012 §3.1)
# =============================================================================
def test_cryptographic_transformation_lineage_hash_integrity() -> None:
    """Verify artifact content hash is deterministically derived from parent checkpoint and plan."""
    plan1 = _make_valid_optimization_plan(plan_id="opt-plan-001")
    plan2 = _make_valid_optimization_plan(plan_id="opt-plan-002")

    hash1 = compute_artifact_lineage_hash(plan1)
    hash1_repeat = compute_artifact_lineage_hash(plan1)
    hash2 = compute_artifact_lineage_hash(plan2)

    assert hash1.startswith("sha256:")
    assert hash1 == hash1_repeat
    assert hash1 != hash2  # Different plan produces different lineage hash!


# =============================================================================
# Test 18: Verifier artifact plan mismatch fails closed
# =============================================================================
def test_verifier_artifact_plan_mismatch_fails_closed() -> None:
    """Verify DefaultOptimizationVerifier raises error when artifact plan_id mismatches plan."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan(plan_id="opt-plan-001")
    artifact = _make_valid_artifact(plan).model_copy(
        update={"plan_id": OptimizationPlanId("opt-plan-different")}
    )
    base_eval = _make_valid_evaluation_result("exp-base")
    cand_eval = _make_valid_evaluation_result("exp-cand")

    with pytest.raises(
        OptimizationValidationError, match="does not match OptimizationPlan"
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=base_eval,
            candidate_evaluation=cand_eval,
        )


# =============================================================================
# Test 19: Verifier accuracy tolerance exceeded fails verification (Acceptance Criterion 5)
# =============================================================================
def test_verifier_accuracy_tolerance_exceeded_fails_verification() -> None:
    """Verify candidate model exceeding max_accuracy_drop_tolerance fails verification gate."""
    verifier = DefaultOptimizationVerifier()
    # Tolerance is 0.015 drop
    plan = _make_valid_optimization_plan(max_accuracy_drop_tolerance=0.015)
    artifact = _make_valid_artifact(plan)

    base_eval = _make_valid_evaluation_result("exp-base", map50=0.785)
    # Drop is 0.785 - 0.750 = 0.035 > 0.015!
    cand_eval = _make_valid_evaluation_result("exp-cand", map50=0.750, fps=120.0)

    result = verifier.verify(
        artifact=artifact,
        plan=plan,
        baseline_evaluation=base_eval,
        candidate_evaluation=cand_eval,
    )

    assert result.verification_passed is False
    assert "FAILED accuracy tolerance gate" in result.diagnostics

    # When raise_on_failure=True, strictly raises OptimizationVerificationError
    with pytest.raises(
        OptimizationVerificationError, match="FAILED accuracy tolerance gate"
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=base_eval,
            candidate_evaluation=cand_eval,
            raise_on_failure=True,
        )


# =============================================================================
# Test 20: Verifier accuracy retention and speedup calculation
# =============================================================================
def test_verifier_accuracy_retention_and_speedup_calculation() -> None:
    """Verify DefaultOptimizationVerifier correctly calculates retention ratio, speedup, and memory reduction."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan(max_accuracy_drop_tolerance=0.015)
    artifact = _make_valid_artifact(plan)

    # Base: map50=0.80, fps=50.0, vram=4.0GB
    base_eval = _make_valid_evaluation_result(
        "exp-base", map50=0.80, fps=50.0, peak_vram_gb=4.0
    )
    # Candidate: map50=0.795, fps=100.0 (2x speedup), vram=2.0GB (50% reduction)
    cand_eval = _make_valid_evaluation_result(
        "exp-cand", map50=0.795, fps=100.0, peak_vram_gb=2.0
    )

    res = verifier.verify(
        artifact=artifact,
        plan=plan,
        baseline_evaluation=base_eval,
        candidate_evaluation=cand_eval,
    )

    assert res.verification_passed is True
    assert res.accuracy_retention_ratio == pytest.approx(0.9938, abs=0.001)
    assert res.speedup_ratio == pytest.approx(2.0, abs=0.01)
    assert res.memory_reduction_ratio == pytest.approx(0.5, abs=0.01)
    assert "PASSED" in res.diagnostics


# =============================================================================
# Test 21: Verifier missing overlapping metrics fails closed
# =============================================================================
def test_verifier_missing_overlapping_metrics_fails_closed() -> None:
    """Verify verifier raises OptimizationVerificationError when metrics do not overlap."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(plan)

    m1 = MetricResult(
        metric=MetricDefinition(
            metric_id=MetricId("m1"),
            name="precision_score",
            task_type=TaskType.DETECTION,
            version="1.0",
            higher_is_better=True,
        ),
        primary_value=0.85,
        sample_count=100,
    )
    m2 = MetricResult(
        metric=MetricDefinition(
            metric_id=MetricId("m2"),
            name="recall_score",
            task_type=TaskType.DETECTION,
            version="1.0",
            higher_is_better=True,
        ),
        primary_value=0.82,
        sample_count=100,
    )

    eval_base = EvaluationResult(
        evaluation_id=EvaluationId("eval-base"),
        plan=_make_valid_evaluation_plan("eval-base", "exp-base"),
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(m1,),
        environment=_make_valid_evaluation_env(),
        completed_at=datetime.now(timezone.utc),
    )
    eval_cand = EvaluationResult(
        evaluation_id=EvaluationId("eval-cand"),
        plan=_make_valid_evaluation_plan("eval-cand", "exp-cand"),
        verdict=EvaluationVerdict.PASSED,
        quality_metrics=(m2,),
        environment=_make_valid_evaluation_env(),
        completed_at=datetime.now(timezone.utc),
    )

    with pytest.raises(
        OptimizationVerificationError, match="No overlapping quality metrics"
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=eval_base,
            candidate_evaluation=eval_cand,
        )


# =============================================================================
# Test 22: Deployment staging mandatory tool_id and executor
# =============================================================================
def test_deployment_staging_mandatory_tool_id_and_executor() -> None:
    """Verify DeploymentManager.stage requires tool_id and tool_executor without synthesis."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()

    for bad_id in [cast(ToolId, None), cast(ToolId, ""), cast(ToolId, "   ")]:
        with pytest.raises(OptimizationExecutionError, match="tool_id is mandatory"):
            manager.stage(plan=plan, tool_id=bad_id, tool_executor=_MockToolExecutor())  # type: ignore[arg-type]

    with pytest.raises(OptimizationExecutionError, match="tool_executor is mandatory"):
        manager.stage(plan=plan, tool_id=ToolId("cv.deploy.stage"), tool_executor=None)  # type: ignore[arg-type]


# =============================================================================
# Test 23: Deployment staging target_id conflict rejected
# =============================================================================
def test_deployment_staging_target_id_conflict_rejected() -> None:
    """Verify DeploymentManager.stage rejects conflicting target_id."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()

    with pytest.raises(
        DeploymentValidationError, match="conflicts with DeploymentPlan target"
    ):
        manager.stage(
            plan=plan,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=_MockToolExecutor(),
            target_id=TargetId("target-different-server"),
        )


# =============================================================================
# Test 24: Deployment staging unverified optimization result rejected (Criterion 5)
# =============================================================================
def test_deployment_staging_unverified_optimization_result_rejected() -> None:
    """Verify staging rejects candidate artifact whose post-optimization verification failed."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    opt_plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(opt_plan)

    cand_eval = _make_valid_evaluation_result("exp-cand")
    base_eval = _make_valid_evaluation_result("exp-base")
    comp = DefaultBaselineComparator().compare(
        candidate_metrics=cand_eval.quality_metrics,
        baseline_metrics=base_eval.quality_metrics,
        candidate_benchmarks=cand_eval.system_benchmarks,
        baseline_benchmarks=base_eval.system_benchmarks,
        candidate_exp_id=ExperimentId("exp-cand"),
        baseline_exp_id=ExperimentId("exp-base"),
    )

    failed_opt_res = OptimizationResult(
        plan=opt_plan,
        artifact=artifact,
        evaluation_result=cand_eval,
        baseline_comparison=comp,
        accuracy_retention_ratio=0.90,
        speedup_ratio=1.2,
        memory_reduction_ratio=0.1,
        verification_passed=False,  # FAILED!
        diagnostics="Accuracy cliff: dropped 0.05",
    )

    with pytest.raises(
        OptimizationVerificationError,
        match="Cannot deploy unverified or degraded artifact",
    ):
        manager.stage(
            plan=plan,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=_MockToolExecutor(),
            optimization_result=failed_opt_res,
        )


# =============================================================================
# Test 25: Deployment readiness probe requires staging state
# =============================================================================
def test_deployment_readiness_probe_requires_staging_state() -> None:
    """Verify verify_readiness() fails closed if deployment is not in STAGING."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.DRAFT,  # Not in STAGING!
    )

    with pytest.raises(
        DeploymentValidationError, match="Deployment must be in STAGING"
    ):
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=_MockToolExecutor(),
        )


# =============================================================================
# Test 26: Deployment readiness probe startup timeout fails closed (Invariant 8)
# =============================================================================
def test_deployment_readiness_probe_startup_timeout_fails_closed() -> None:
    """Verify readiness probe fails closed when startup time exceeds timeout budget."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
    )
    # Timeout is 30s, mock reports 45.5s
    executor = _MockToolExecutor(
        structured_output={
            "startup_time_sec": 45.5,
            "probe_latency_mean_ms": 15.0,
            "vram_consumed_mb": 1024.0,
        }
    )

    with pytest.raises(ReadinessProbeFailureError, match="Startup timeout exceeded"):
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=executor,
        )


# =============================================================================
# Test 27: Deployment readiness probe latency budget exceeded fails closed
# =============================================================================
def test_deployment_readiness_probe_latency_budget_exceeded_fails_closed() -> None:
    """Verify readiness probe fails closed when mean latency exceeds threshold budget."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
    )
    # Threshold is 25.0ms, mock reports 38.2ms
    executor = _MockToolExecutor(
        structured_output={
            "startup_time_sec": 5.0,
            "probe_latency_mean_ms": 38.2,
            "vram_consumed_mb": 1024.0,
        }
    )

    with pytest.raises(ReadinessProbeFailureError, match="latency exceeded budget"):
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=executor,
        )


# =============================================================================
# Test 28: Deployment readiness probe memory headroom exceeded fails closed
# =============================================================================
def test_deployment_readiness_probe_memory_headroom_exceeded_fails_closed() -> None:
    """Verify readiness probe fails closed when VRAM exceeds allowable memory budget."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
    )
    # Total memory 48GB (49152MB) - headroom 1024MB = max allowed 48128MB. Mock reports 48500MB!
    executor = _MockToolExecutor(
        structured_output={
            "startup_time_sec": 5.0,
            "probe_latency_mean_ms": 15.0,
            "vram_consumed_mb": 48500.0,
        }
    )

    with pytest.raises(
        ReadinessProbeFailureError, match="Memory headroom budget violated"
    ):
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=executor,
        )


# =============================================================================
# Test 29: Deployment activate requires passed readiness probe (Invariant 8)
# =============================================================================
def test_deployment_activate_requires_passed_readiness_probe() -> None:
    """Verify activation fails closed if readiness probe was not performed or failed."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
        readiness_result=None,  # No probe!
    )
    op_id, app_req, app_dec = _make_approval_context(plan)

    with pytest.raises(
        DeploymentValidationError,
        match="Deployment cannot be activated without passing readiness probing",
    ):
        manager.activate(
            record=record,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
        )


# =============================================================================
# Test 30: Deployment activate payload hash tamper fails closed (Invariant 7)
# =============================================================================
def test_deployment_activate_payload_hash_tamper_fails_closed() -> None:
    """Verify activation fails closed if OperationIdentity canonical hash does not match DeploymentPlan."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    probe_ok = ReadinessProbeResult(
        passed=True,
        startup_time_sec=2.0,
        probe_latency_mean_ms=12.0,
        vram_consumed_mb=2048.0,
        probed_at=datetime.now(timezone.utc),
    )
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
        readiness_result=probe_ok,
    )
    op_id, app_req, app_dec = _make_approval_context(plan)

    tampered_op_id = OperationIdentity(
        operation_id=op_id.operation_id,
        operation_version=op_id.operation_version,
        canonical_payload_hash="sha256:tampered_hash_00000000000000000000000000000000000000000000000000",
        target_identity=op_id.target_identity,
    )

    with pytest.raises(
        DeploymentApprovalRequiredError, match="Canonical payload hash mismatch"
    ):
        manager.activate(
            record=record,
            operation_identity=tampered_op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
        )


# =============================================================================
# Test 31: Deployment activate resource scope conflict fails closed
# =============================================================================
def test_deployment_activate_resource_scope_conflict_fails_closed() -> None:
    """Verify activation fails closed if ApprovalRequest resource scope conflicts with target."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    probe_ok = ReadinessProbeResult(
        passed=True,
        startup_time_sec=2.0,
        probe_latency_mean_ms=12.0,
        vram_consumed_mb=2048.0,
        probed_at=datetime.now(timezone.utc),
    )
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
        readiness_result=probe_ok,
    )
    op_id, app_req, app_dec = _make_approval_context(
        plan, resource_scope="target:cluster-other-server"
    )

    with pytest.raises(
        DeploymentApprovalRequiredError, match="conflicts with authorized scope target"
    ):
        manager.activate(
            record=record,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
        )


# =============================================================================
# Test 32: Deployment activate approval decision denied fails closed
# =============================================================================
def test_deployment_activate_approval_decision_denied_fails_closed() -> None:
    """Verify activation fails closed if ApprovalDecision status is not APPROVED."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    probe_ok = ReadinessProbeResult(
        passed=True,
        startup_time_sec=2.0,
        probe_latency_mean_ms=12.0,
        vram_consumed_mb=2048.0,
        probed_at=datetime.now(timezone.utc),
    )
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
        readiness_result=probe_ok,
    )
    op_id, app_req, app_dec = _make_approval_context(plan)

    denied_dec = ApprovalDecision(
        request_id=app_dec.request_id,
        decision=ApprovalStatus.REJECTED,  # DENIED!
        principal="security_officer",
        resolved_at=datetime.now(timezone.utc),
        bound_payload_hash=app_dec.bound_payload_hash,
        bound_resource_scope=app_dec.bound_resource_scope,
        bound_policy_version=app_dec.bound_policy_version,
    )

    with pytest.raises(
        DeploymentApprovalRequiredError,
        match="Deployment activation rejected: approval status is",
    ):
        manager.activate(
            record=record,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=denied_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
        )


# =============================================================================
# Test 33: Deployment rollback null predecessor escalates to OPERATOR_REQUIRED (ADR-0012 §3.7)
# =============================================================================
def test_deployment_rollback_null_predecessor_escalates_to_operator_required() -> None:
    """Verify rollback of initial deployment with rollback_target_id=None escalates to OPERATOR_REQUIRED."""
    manager = DefaultDeploymentManager()
    # Initial deployment has rollback_target_id = None
    plan = _make_valid_deployment_plan(rollback_target_id=None)
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.ACTIVE,
    )

    with pytest.raises(RollbackExecutionError, match="Escalated to OPERATOR_REQUIRED"):
        manager.rollback(
            record=record,
            reason="Real-time pipeline stream buffer stall",
            tool_id=ToolId("cv.deploy.rollback"),
            tool_executor=_MockToolExecutor(),
        )


# =============================================================================
# Test 34: Deployment rollback restores predecessor manifest (Invariant 9)
# =============================================================================
def test_deployment_rollback_restores_predecessor_manifest() -> None:
    """Verify rollback with designated rollback_target_id restores predecessor to ACTIVE."""
    manager = DefaultDeploymentManager()
    pred_plan = _make_valid_deployment_plan(deployment_id="dep-v1-prod")
    pred_record = DeploymentRecord(
        deployment_id=pred_plan.deployment_id,
        plan=pred_plan,
        status=DeploymentStatus.SUPERSEDED,
    )

    # Current candidate deployment designating pred_plan as rollback target
    cand_plan = _make_valid_deployment_plan(
        deployment_id="dep-v2-cand", rollback_target_id="dep-v1-prod"
    )
    cand_record = DeploymentRecord(
        deployment_id=cand_plan.deployment_id,
        plan=cand_plan,
        status=DeploymentStatus.ACTIVE,
    )

    executor = _MockToolExecutor()
    active_deployments = {
        pred_plan.deployment_id: pred_record,
        cand_plan.deployment_id: cand_record,
    }

    rolled_back, restored = manager.rollback(
        record=cand_record,
        reason="Edge latency spikes observed",
        tool_id=ToolId("cv.deploy.rollback"),
        tool_executor=executor,
        active_deployments=active_deployments,
    )

    assert rolled_back.status == DeploymentStatus.ROLLED_BACK
    assert "Rolled back to dep-v1-prod" in (rolled_back.failure_details or "")
    assert restored is not None
    assert restored.deployment_id == pred_plan.deployment_id
    assert restored.status == DeploymentStatus.ACTIVE


# =============================================================================
# Test 35: Deployment rollback missing predecessor in registry fails closed
# =============================================================================
def test_deployment_rollback_missing_predecessor_in_registry_fails_closed() -> None:
    """Verify rollback raises RollbackExecutionError if predecessor is missing from registry."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan(rollback_target_id="dep-nonexistent-001")
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.ACTIVE,
    )

    with pytest.raises(
        RollbackExecutionError, match="not found in active deployments registry"
    ):
        manager.rollback(
            record=record,
            reason="Immediate failure",
            tool_id=ToolId("cv.deploy.rollback"),
            tool_executor=_MockToolExecutor(),
            active_deployments={},  # Predecessor missing!
        )


# =============================================================================
# Test 36: Zero subsystem subprocess or shell leakage (Invariant 10)
# =============================================================================
def test_zero_subsystem_subprocess_or_shell_leakage() -> None:
    """Verify AST audit proves cv_agent/optimization/ contains zero subprocess or shell execution."""
    optimization_dir = (
        Path(__file__).resolve().parent.parent / "cv_agent" / "optimization"
    )
    assert optimization_dir.exists() and optimization_dir.is_dir()

    forbidden_calls = {
        "subprocess",
        "Popen",
        "check_output",
        "check_call",
        "system",
        "popen",
        "spawn",
        "fork",
        "exec",
        "eval",
    }

    for py_file in optimization_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text("utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            # Check import statements
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "subprocess", (
                        f"Forbidden subprocess import in {py_file}"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "subprocess", (
                    f"Forbidden subprocess import in {py_file}"
                )

            # Check function calls
            elif isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                assert func_name not in forbidden_calls, (
                    f"Forbidden call '{func_name}' in {py_file}"
                )


# =============================================================================
# Test 37: Zero SQLite queries or database state mutations in optimization subsystem
# =============================================================================
def test_zero_sqlite_or_db_mutations_in_optimization() -> None:
    """Verify AST audit proves cv_agent/optimization/ contains zero SQLite queries or DB ownership."""
    optimization_dir = (
        Path(__file__).resolve().parent.parent / "cv_agent" / "optimization"
    )
    for py_file in optimization_dir.glob("*.py"):
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
