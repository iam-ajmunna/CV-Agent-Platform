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
    OptimizedArtifactEvaluationBinding,
    OptimizedModelArtifact,
    ReadinessProbeResult,
    validate_deployment_transition,
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
    ActiveJobHandoff,
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


def _make_valid_candidate_binding(
    artifact: OptimizedModelArtifact,
    eval_id: EvaluationId | str = "eval-exp-cand",
) -> OptimizedArtifactEvaluationBinding:
    return OptimizedArtifactEvaluationBinding(
        candidate_artifact_id=artifact.artifact_id,
        candidate_artifact_hash=artifact.artifact_hash,
        evaluation_id=EvaluationId(str(eval_id)),
    )


def _make_valid_optimization_result(
    plan: OptimizationPlan | None = None,
    artifact: OptimizedModelArtifact | None = None,
    artifact_id: str | None = None,
) -> OptimizationResult:
    opt_plan = plan or _make_valid_optimization_plan()
    opt_art = artifact or _make_valid_artifact(opt_plan)
    if artifact_id is not None:
        opt_art = opt_art.model_copy(
            update={"artifact_id": OptimizedArtifactId(artifact_id)}
        )
    cand_eval = _make_valid_evaluation_result("exp-cand")
    base_eval = _make_valid_evaluation_result("exp-base")
    cand_binding = _make_valid_candidate_binding(opt_art, cand_eval.evaluation_id)
    comp = DefaultBaselineComparator().compare(
        candidate_metrics=cand_eval.quality_metrics,
        baseline_metrics=base_eval.quality_metrics,
        candidate_benchmarks=cand_eval.system_benchmarks,
        baseline_benchmarks=base_eval.system_benchmarks,
        candidate_exp_id=ExperimentId("exp-cand"),
        baseline_exp_id=ExperimentId("exp-base"),
    )
    return OptimizationResult(
        plan=opt_plan,
        artifact=opt_art,
        evaluation_result=cand_eval,
        baseline_comparison=comp,
        accuracy_retention_ratio=0.98,
        speedup_ratio=1.45,
        memory_reduction_ratio=0.25,
        verification_passed=True,
        diagnostics="Optimization verification PASSED",
        candidate_artifact_binding=cand_binding,
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
    target_identity: str | None = None,
    policy_version: str = "1.0",
    bound_payload_hash: str | None = None,
    bound_resource_scope: str | None = None,
    bound_policy_version: str | None = None,
) -> tuple[OperationIdentity, ApprovalRequest, ApprovalDecision]:
    extract_deployment_operation_payload(plan)
    canonical_hash = compute_deployment_payload_hash(plan)
    scope = resource_scope or f"target:{plan.target.target_id}"
    tid = target_identity if target_identity is not None else str(plan.target.target_id)

    op_id = OperationIdentity(
        operation_id="op-dep-001",
        operation_version=str(operation_version),
        canonical_payload_hash=canonical_hash,
        target_identity=tid,
    )

    app_req = ApprovalRequest(
        request_id="req-dep-001",
        session_id="sess-001",
        operation_id=OperationId(value="op-dep-001"),
        operation_version=operation_version,
        canonical_payload_hash=canonical_hash,
        policy_version=policy_version,
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
        bound_payload_hash=bound_payload_hash or canonical_hash,
        bound_resource_scope=bound_resource_scope or scope,
        bound_policy_version=bound_policy_version or policy_version,
    )

    return op_id, app_req, app_dec


class _MockToolExecutor:
    """Mock ADR-0005 ToolExecutor returning synthetic telemetry."""

    def __init__(
        self,
        status: ToolExecutionStatus = ToolExecutionStatus.SUCCESS,
        error_message: str | None = None,
        structured_output: dict[str, Any] | None = None,
        active_job_ref: ActiveJobHandoff | None = None,
    ) -> None:
        self.status = status
        self.error_message = error_message
        self.active_job_ref = active_job_ref
        if structured_output is not None:
            self.structured_output = structured_output
        else:
            self.structured_output = {
                "artifact_uri": "artifact://models/opt-plan-001/model.engine",
                "artifact_size_bytes": 85000000,
                "runtime_format": "tensorrt_engine",
                "artifact_hash": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
                "startup_time_sec": 1.5,
                "probe_latency_mean_ms": 15.0,
                "vram_consumed_mb": 1024.0,
            }
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
            active_job_ref=self.active_job_ref,
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
    """Verify CalibrationDatasetBinding rejects non-calibration splits to prevent data leakage."""
    leakage_splits = ["test", "evaluation", "eval", "val_test", "train", "val"]
    for bad_split in leakage_splits:
        with pytest.raises(
            CalibrationLeakageError,
            match="must strictly be 'calibration'",
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
            checkpoint_manifest=_make_valid_checkpoint_manifest(),
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
        CalibrationLeakageError, match="Calibration split 'calibration'"
    ):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=_MockToolExecutor(),
            dataset_manifest=bad_manifest,
            checkpoint_manifest=_make_valid_checkpoint_manifest(),
        )


# =============================================================================
# Test 14: Optimizer compile mandatory tool_id and target_id (Zero synthesis)
# =============================================================================
def test_optimizer_compile_mandatory_tool_id_and_target_id() -> None:
    """Verify DefaultModelOptimizer requires mandatory tool_id and target_id without local fallback."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()
    ckpt = _make_valid_checkpoint_manifest()

    for bad_id in [cast(ToolId, None), cast(ToolId, ""), cast(ToolId, "   ")]:
        with pytest.raises(OptimizationExecutionError, match="tool_id is mandatory"):
            optimizer.compile(
                plan=plan,
                tool_id=bad_id,  # type: ignore[arg-type]
                target_id=TargetId("target-01"),
                tool_executor=_MockToolExecutor(),
                checkpoint_manifest=ckpt,
            )

    for bad_target in [cast(TargetId, None), cast(TargetId, ""), cast(TargetId, "   ")]:
        with pytest.raises(OptimizationExecutionError, match="target_id is mandatory"):
            optimizer.compile(
                plan=plan,
                tool_id=ToolId("cv.optimize.compile"),
                target_id=bad_target,  # type: ignore[arg-type]
                tool_executor=_MockToolExecutor(),
                checkpoint_manifest=ckpt,
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
            checkpoint_manifest=_make_valid_checkpoint_manifest(),
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
            checkpoint_manifest=_make_valid_checkpoint_manifest(),
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
            candidate_artifact_binding=_make_valid_candidate_binding(
                artifact, cand_eval.evaluation_id
            ),
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
    cand_binding = _make_valid_candidate_binding(artifact, cand_eval.evaluation_id)

    result = verifier.verify(
        artifact=artifact,
        plan=plan,
        baseline_evaluation=base_eval,
        candidate_evaluation=cand_eval,
        candidate_artifact_binding=cand_binding,
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
            candidate_artifact_binding=cand_binding,
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
        candidate_artifact_binding=_make_valid_candidate_binding(
            artifact, cand_eval.evaluation_id
        ),
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
            candidate_artifact_binding=_make_valid_candidate_binding(
                artifact, eval_cand.evaluation_id
            ),
        )


# =============================================================================
# Test 22: Deployment staging mandatory tool_id, target_id, and executor
# =============================================================================
def test_deployment_staging_mandatory_tool_id_and_executor() -> None:
    """Verify DeploymentManager.stage requires tool_id, target_id, and tool_executor without synthesis."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    opt_res = _make_valid_optimization_result(artifact_id=str(plan.artifact_id))

    for bad_id in [cast(ToolId, None), cast(ToolId, ""), cast(ToolId, "   ")]:
        with pytest.raises(OptimizationExecutionError, match="tool_id is mandatory"):
            manager.stage(
                plan=plan,
                tool_id=bad_id,  # type: ignore[arg-type]
                tool_executor=_MockToolExecutor(),
                target_id=plan.target.target_id,
                optimization_result=opt_res,
            )

    for bad_target in [cast(TargetId, None), cast(TargetId, ""), cast(TargetId, "   ")]:
        with pytest.raises(DeploymentValidationError, match="target_id is mandatory"):
            manager.stage(
                plan=plan,
                tool_id=ToolId("cv.deploy.stage"),
                tool_executor=_MockToolExecutor(),
                target_id=bad_target,  # type: ignore[arg-type]
                optimization_result=opt_res,
            )

    with pytest.raises(OptimizationExecutionError, match="tool_executor is mandatory"):
        manager.stage(
            plan=plan,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=None,  # type: ignore[arg-type]
            target_id=plan.target.target_id,
            optimization_result=opt_res,
        )


# =============================================================================
# Test 23: Deployment staging target_id conflict rejected
# =============================================================================
def test_deployment_staging_target_id_conflict_rejected() -> None:
    """Verify DeploymentManager.stage rejects conflicting target_id."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    opt_res = _make_valid_optimization_result(artifact_id=str(plan.artifact_id))

    with pytest.raises(
        DeploymentValidationError, match="conflicts with DeploymentPlan target"
    ):
        manager.stage(
            plan=plan,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=_MockToolExecutor(),
            target_id=TargetId("target-different-server"),
            optimization_result=opt_res,
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
            target_id=plan.target.target_id,
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
            target_id=plan.target.target_id,
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

    with pytest.raises(
        ReadinessProbeFailureError, match="Startup timeout exceeded"
    ) as exc_info:
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=executor,
            target_id=plan.target.target_id,
        )
    assert exc_info.value.failed_record.status == DeploymentStatus.FAILED


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

    with pytest.raises(
        ReadinessProbeFailureError, match="latency exceeded budget"
    ) as exc_info:
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=executor,
            target_id=plan.target.target_id,
        )
    assert exc_info.value.failed_record.status == DeploymentStatus.FAILED


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
    ) as exc_info:
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=executor,
            target_id=plan.target.target_id,
        )
    assert exc_info.value.failed_record.status == DeploymentStatus.FAILED


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
        readiness_result=None,
    )
    op_id, app_req, app_dec = _make_approval_context(plan)

    # 1. Calling activate on STAGING record fails closed (B7 state machine)
    with pytest.raises(
        DeploymentValidationError,
        match="must be in VERIFYING state to be activated",
    ):
        manager.activate(
            record=record,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
        )

    # 2. Calling activate in VERIFYING state but without passed readiness fails closed
    verifying_record = record.model_copy(
        update={"status": DeploymentStatus.VERIFYING, "readiness_result": None}
    )
    with pytest.raises(
        DeploymentValidationError,
        match="Deployment cannot be activated without passing readiness probing",
    ):
        manager.activate(
            record=verifying_record,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
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
        status=DeploymentStatus.VERIFYING,
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
            target_id=plan.target.target_id,
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
        status=DeploymentStatus.VERIFYING,
        readiness_result=probe_ok,
    )
    op_id, app_req, app_dec = _make_approval_context(
        plan, resource_scope="target:cluster-other-server"
    )

    with pytest.raises(
        DeploymentApprovalRequiredError,
        match="resource_scope.*does not match deployment",
    ):
        manager.activate(
            record=record,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
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
        status=DeploymentStatus.VERIFYING,
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
            target_id=plan.target.target_id,
        )


# =============================================================================
# Test 33: Deployment rollback null predecessor escalates to OPERATOR_REQUIRED (ADR-0012 §3.7)
# =============================================================================
def test_deployment_rollback_null_predecessor_escalates_to_operator_required() -> None:
    """Verify rollback of initial deployment with rollback_target_id=None escalates to OPERATOR_REQUIRED."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan(rollback_target_id=None)
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.ACTIVE,
    )
    active_deployments = {record.deployment_id: record}

    escalated, restored = manager.rollback(
        record=record,
        reason="Real-time pipeline stream buffer stall",
        tool_id=ToolId("cv.deploy.rollback"),
        tool_executor=_MockToolExecutor(),
        target_id=plan.target.target_id,
        active_deployments=active_deployments,
    )

    assert escalated.status == DeploymentStatus.OPERATOR_REQUIRED
    assert restored is None
    assert "OPERATOR_REQUIRED" in (escalated.failure_details or "")
    assert (
        active_deployments[record.deployment_id].status
        == DeploymentStatus.OPERATOR_REQUIRED
    )

    # B8 lockout: target in OPERATOR_REQUIRED locks out future staging
    new_plan = _make_valid_deployment_plan(deployment_id="dep-002")
    with pytest.raises(
        DeploymentValidationError, match="locked out by deployment.*OPERATOR_REQUIRED"
    ):
        manager.stage(
            plan=new_plan,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=_MockToolExecutor(),
            target_id=new_plan.target.target_id,
            optimization_result=_make_valid_optimization_result(
                artifact_id=str(new_plan.artifact_id)
            ),
            active_deployments=active_deployments,
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
        target_id=cand_plan.target.target_id,
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
            target_id=plan.target.target_id,
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


# =============================================================================
# Phase 2M PM Adversarial Review #5237441698 Regression Suite (B1 - B10)
# =============================================================================


# --- B1: ADR-0010 Lineage Mandatory ---
def test_b1_optimizer_requires_checkpoint_manifest() -> None:
    """[B1] Verify DefaultModelOptimizer rejects compilation without CheckpointManifest."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()

    with pytest.raises(
        OptimizationValidationError, match="checkpoint_manifest is mandatory"
    ):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=_MockToolExecutor(),
            checkpoint_manifest=None,  # type: ignore[arg-type]
        )


def test_b1_optimizer_checkpoint_lineage_mismatch_fails_closed() -> None:
    """[B1] Verify checkpoint ID or weights hash mismatch fails closed."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()

    bad_ckpt_id = _make_valid_checkpoint_manifest().model_copy(
        update={"checkpoint_id": CheckpointId("ckpt-mismatched-id")}
    )
    with pytest.raises(OptimizationValidationError, match="Checkpoint ID mismatch"):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=_MockToolExecutor(),
            checkpoint_manifest=bad_ckpt_id,
        )

    bad_weights = _make_valid_checkpoint_manifest().model_copy(
        update={
            "weights_hash": ContentHash(
                "sha256:0000000000000000000000000000000000000000000000000000000000000000"
            )
        }
    )
    with pytest.raises(OptimizationValidationError, match="Weights hash mismatch"):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=_MockToolExecutor(),
            checkpoint_manifest=bad_weights,
        )


# --- B2: ADR-0009 Calibration Binding Fail-Closed ---
def test_b2_calibration_split_must_strictly_be_calibration() -> None:
    """[B2] Verify CalibrationDatasetBinding rejects non-calibration splits."""
    for bad_split in ["train", "val", "test", "holdout", "custom"]:
        with pytest.raises(
            CalibrationLeakageError, match="must strictly be 'calibration'"
        ):
            CalibrationDatasetBinding(
                dataset_id=DatasetId("dataset-001"),
                dataset_version=DatasetVersion("v1.0.0"),
                dataset_manifest_hash=ContentHash("sha256:" + "1" * 64),
                split_name=bad_split,
            )


def test_b2_calibration_manifest_mandatory_for_ptq_fails_closed() -> None:
    """[B2] Verify DefaultModelOptimizer requires dataset_manifest for PTQ/INT8."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan(
        technique=OptimizationTechnique.PTQ_QUANTIZATION,
        precision=OptimizationPrecision.INT8,
    )
    with pytest.raises(
        CalibrationLeakageError,
        match="dataset_manifest is mandatory",
    ):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=_MockToolExecutor(),
            checkpoint_manifest=_make_valid_checkpoint_manifest(),
            dataset_manifest=None,
        )


def test_b2_calibration_manifest_empty_split_fails_closed() -> None:
    """[B2] Verify DefaultModelOptimizer rejects manifest with empty calibration split."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan(
        technique=OptimizationTechnique.PTQ_QUANTIZATION,
        precision=OptimizationPrecision.INT8,
    )
    empty_calib_manifest = _make_valid_dataset_manifest(has_calibration=False)
    with pytest.raises(CalibrationLeakageError, match="has no items"):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=_MockToolExecutor(),
            checkpoint_manifest=_make_valid_checkpoint_manifest(),
            dataset_manifest=empty_calib_manifest,
        )


# --- B3: Authoritative Compilation Outputs, Zero Synthetic Metadata ---
def test_b3_compilation_missing_artifact_uri_fails_closed() -> None:
    """[B3] Verify compilation tool output missing artifact_uri fails closed."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()
    executor = _MockToolExecutor(
        structured_output={
            "artifact_size_bytes": 85000000,
            "runtime_format": "tensorrt_engine",
        }
    )
    with pytest.raises(
        OptimizationExecutionError, match="missing mandatory non-empty 'artifact_uri'"
    ):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=executor,
            checkpoint_manifest=_make_valid_checkpoint_manifest(),
        )


def test_b3_compilation_missing_artifact_size_bytes_fails_closed() -> None:
    """[B3] Verify compilation tool output missing artifact_size_bytes fails closed."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()
    executor = _MockToolExecutor(
        structured_output={
            "artifact_uri": "artifact://model.engine",
            "runtime_format": "tensorrt_engine",
        }
    )
    with pytest.raises(
        OptimizationExecutionError,
        match="missing mandatory.*'artifact_size_bytes'",
    ):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=executor,
            checkpoint_manifest=_make_valid_checkpoint_manifest(),
        )


def test_b3_compilation_missing_runtime_format_fails_closed() -> None:
    """[B3] Verify compilation tool output missing runtime_format fails closed."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()
    executor = _MockToolExecutor(
        structured_output={
            "artifact_uri": "artifact://model.engine",
            "artifact_size_bytes": 85000000,
        }
    )
    with pytest.raises(
        OptimizationExecutionError,
        match="missing mandatory non-empty 'runtime_format'",
    ):
        optimizer.compile(
            plan=plan,
            tool_id=ToolId("cv.optimize.compile"),
            target_id=TargetId("target-jetson-01"),
            tool_executor=executor,
            checkpoint_manifest=_make_valid_checkpoint_manifest(),
        )


# --- B4: Authoritative Readiness Telemetry, Zero Synthetic Defaults ---
def test_b4_readiness_probe_missing_telemetry_fails_closed() -> None:
    """[B4] Verify readiness probing fails closed without synthetic defaults on missing metrics."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
    )

    # Missing startup_time_sec
    exec1 = _MockToolExecutor(
        structured_output={"probe_latency_mean_ms": 15.0, "vram_consumed_mb": 1024.0}
    )
    with pytest.raises(
        ReadinessProbeFailureError,
        match="probe telemetry missing required metric 'startup_time_sec'",
    ) as exc_info1:
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=exec1,
            target_id=plan.target.target_id,
        )
    assert exc_info1.value.failed_record.status == DeploymentStatus.FAILED

    # Missing probe_latency_mean_ms
    exec2 = _MockToolExecutor(
        structured_output={"startup_time_sec": 1.5, "vram_consumed_mb": 1024.0}
    )
    with pytest.raises(
        ReadinessProbeFailureError,
        match="probe telemetry missing required metric 'probe_latency_mean_ms'",
    ) as exc_info2:
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=exec2,
            target_id=plan.target.target_id,
        )
    assert exc_info2.value.failed_record.status == DeploymentStatus.FAILED

    # Missing vram_consumed_mb
    exec3 = _MockToolExecutor(
        structured_output={"startup_time_sec": 1.5, "probe_latency_mean_ms": 15.0}
    )
    with pytest.raises(
        ReadinessProbeFailureError,
        match="probe telemetry missing required metric 'vram_consumed_mb'",
    ) as exc_info3:
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=exec3,
            target_id=plan.target.target_id,
        )
    assert exc_info3.value.failed_record.status == DeploymentStatus.FAILED


def test_b4_readiness_probe_malformed_telemetry_fails_closed() -> None:
    """[B4] Verify non-finite or negative telemetry metrics fail closed."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
    )

    exec_nan = _MockToolExecutor(
        structured_output={
            "startup_time_sec": float("nan"),
            "probe_latency_mean_ms": 15.0,
            "vram_consumed_mb": 1024.0,
        }
    )
    with pytest.raises(ReadinessProbeFailureError, match="must be a finite float"):
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=exec_nan,
            target_id=plan.target.target_id,
        )

    exec_neg = _MockToolExecutor(
        structured_output={
            "startup_time_sec": 1.5,
            "probe_latency_mean_ms": -1.0,
            "vram_consumed_mb": 1024.0,
        }
    )
    with pytest.raises(
        ReadinessProbeFailureError, match="telemetry metrics must be non-negative"
    ):
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=exec_neg,
            target_id=plan.target.target_id,
        )


# --- B5: External TargetId Mandatory Across Operations ---
def test_b5_external_target_id_mandatory_across_operations() -> None:
    """[B5] Verify target_id is mandatory across stage, verify_readiness, activate, and rollback."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    record_staging = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
    )
    record_verifying = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.VERIFYING,
        readiness_result=ReadinessProbeResult(
            passed=True,
            startup_time_sec=1.0,
            probe_latency_mean_ms=10.0,
            vram_consumed_mb=1024.0,
            probed_at=datetime.now(timezone.utc),
        ),
    )
    record_active = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.ACTIVE,
    )
    op_id, app_req, app_dec = _make_approval_context(plan)

    for bad_target in [cast(TargetId, None), cast(TargetId, ""), cast(TargetId, "   ")]:
        with pytest.raises(DeploymentValidationError, match="target_id is mandatory"):
            manager.stage(
                plan=plan,
                tool_id=ToolId("cv.deploy.stage"),
                tool_executor=_MockToolExecutor(),
                target_id=bad_target,  # type: ignore[arg-type]
                optimization_result=_make_valid_optimization_result(
                    artifact_id=str(plan.artifact_id)
                ),
            )

        with pytest.raises(DeploymentValidationError, match="target_id is mandatory"):
            manager.verify_readiness(
                record=record_staging,
                tool_id=ToolId("cv.deploy.probe"),
                tool_executor=_MockToolExecutor(),
                target_id=bad_target,  # type: ignore[arg-type]
            )

        with pytest.raises(DeploymentValidationError, match="target_id is mandatory"):
            manager.activate(
                record=record_verifying,
                operation_identity=op_id,
                approval_request=app_req,
                approval_decision=app_dec,
                tool_id=ToolId("cv.deploy.activate"),
                tool_executor=_MockToolExecutor(),
                target_id=bad_target,  # type: ignore[arg-type]
            )

        with pytest.raises(DeploymentValidationError, match="target_id is mandatory"):
            manager.rollback(
                record=record_active,
                reason="emergency rollback",
                tool_id=ToolId("cv.deploy.rollback"),
                tool_executor=_MockToolExecutor(),
                target_id=bad_target,  # type: ignore[arg-type]
            )


def test_b5_external_target_id_mismatch_fails_closed() -> None:
    """[B5] Verify target_id conflicting with plan or record fails closed."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    diff_target = TargetId("target-different-server")
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
    )

    with pytest.raises(
        DeploymentValidationError, match="conflicts with DeploymentPlan target"
    ):
        manager.stage(
            plan=plan,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=_MockToolExecutor(),
            target_id=diff_target,
            optimization_result=_make_valid_optimization_result(
                artifact_id=str(plan.artifact_id)
            ),
        )

    with pytest.raises(DeploymentValidationError, match="conflicts with record target"):
        manager.verify_readiness(
            record=record,
            tool_id=ToolId("cv.deploy.probe"),
            tool_executor=_MockToolExecutor(),
            target_id=diff_target,
        )


# --- B6: Strict ADR-0003 4-Tuple Approval Cross-Checking ---
def test_b6_approval_operation_id_mismatch_fails_closed() -> None:
    """[B6] Verify OperationIdentity operation_id mismatch against ApprovalRequest fails closed."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    probe_ok = ReadinessProbeResult(
        passed=True,
        startup_time_sec=1.0,
        probe_latency_mean_ms=10.0,
        vram_consumed_mb=1024.0,
        probed_at=datetime.now(timezone.utc),
    )
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.VERIFYING,
        readiness_result=probe_ok,
    )
    op_id, app_req, app_dec = _make_approval_context(plan)

    mismatched_op = op_id.model_copy(update={"operation_id": "op-different-001"})
    with pytest.raises(
        DeploymentApprovalRequiredError, match="Operation ID mismatch across 4-tuple"
    ):
        manager.activate(
            record=record,
            operation_identity=mismatched_op,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
        )


def test_b6_approval_operation_version_mismatch_fails_closed() -> None:
    """[B6] Verify OperationIdentity operation_version mismatch against ApprovalRequest fails closed."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    probe_ok = ReadinessProbeResult(
        passed=True,
        startup_time_sec=1.0,
        probe_latency_mean_ms=10.0,
        vram_consumed_mb=1024.0,
        probed_at=datetime.now(timezone.utc),
    )
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.VERIFYING,
        readiness_result=probe_ok,
    )
    op_id, app_req, app_dec = _make_approval_context(plan)

    mismatched_ver = op_id.model_copy(update={"operation_version": "99"})
    with pytest.raises(
        DeploymentApprovalRequiredError,
        match="Operation version mismatch across 4-tuple",
    ):
        manager.activate(
            record=record,
            operation_identity=mismatched_ver,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
        )


def test_b6_approval_target_identity_mismatch_fails_closed() -> None:
    """[B6] Verify OperationIdentity target_identity mismatch against plan target fails closed."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    probe_ok = ReadinessProbeResult(
        passed=True,
        startup_time_sec=1.0,
        probe_latency_mean_ms=10.0,
        vram_consumed_mb=1024.0,
        probed_at=datetime.now(timezone.utc),
    )
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.VERIFYING,
        readiness_result=probe_ok,
    )
    op_id, app_req, app_dec = _make_approval_context(plan)

    mismatched_target = op_id.model_copy(update={"target_identity": "target-fake"})
    with pytest.raises(
        DeploymentApprovalRequiredError, match="Target identity mismatch"
    ):
        manager.activate(
            record=record,
            operation_identity=mismatched_target,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
        )


def test_b6_approval_request_id_mismatch_fails_closed() -> None:
    """[B6] Verify ApprovalDecision request_id mismatch against ApprovalRequest fails closed."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    probe_ok = ReadinessProbeResult(
        passed=True,
        startup_time_sec=1.0,
        probe_latency_mean_ms=10.0,
        vram_consumed_mb=1024.0,
        probed_at=datetime.now(timezone.utc),
    )
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.VERIFYING,
        readiness_result=probe_ok,
    )
    op_id, app_req, app_dec = _make_approval_context(plan)

    bad_dec = app_dec.model_copy(update={"request_id": "req-rogue-001"})
    with pytest.raises(
        DeploymentApprovalRequiredError, match="Approval decision request_id mismatch"
    ):
        manager.activate(
            record=record,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=bad_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
        )


def test_b6_approval_policy_version_mismatch_fails_closed() -> None:
    """[B6] Verify policy_version mismatch between request and decision fails closed."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    probe_ok = ReadinessProbeResult(
        passed=True,
        startup_time_sec=1.0,
        probe_latency_mean_ms=10.0,
        vram_consumed_mb=1024.0,
        probed_at=datetime.now(timezone.utc),
    )
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.VERIFYING,
        readiness_result=probe_ok,
    )
    op_id, app_req, app_dec = _make_approval_context(plan)

    bad_policy = app_dec.model_copy(update={"bound_policy_version": "9.9"})
    with pytest.raises(
        DeploymentApprovalRequiredError, match="Policy version mismatch"
    ):
        manager.activate(
            record=record,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=bad_policy,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
        )


def test_b6_approval_bound_scope_mismatch_fails_closed() -> None:
    """[B6] Verify bound_resource_scope mismatch against authorized target fails closed."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    probe_ok = ReadinessProbeResult(
        passed=True,
        startup_time_sec=1.0,
        probe_latency_mean_ms=10.0,
        vram_consumed_mb=1024.0,
        probed_at=datetime.now(timezone.utc),
    )
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.VERIFYING,
        readiness_result=probe_ok,
    )
    op_id, app_req, app_dec = _make_approval_context(plan)

    bad_scope = app_dec.model_copy(
        update={"bound_resource_scope": "target:cluster-rogue"}
    )
    with pytest.raises(
        DeploymentApprovalRequiredError, match="Bound resource scope mismatch"
    ):
        manager.activate(
            record=record,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=bad_scope,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
        )


# --- B7: Enforced Deployment Lifecycle State Machine ---
def test_b7_deployment_lifecycle_state_machine_invalid_transition_fails_closed() -> (
    None
):
    """[B7] Verify validate_deployment_transition strictly rejects illegal lifecycle transitions."""
    invalid_transitions = [
        (DeploymentStatus.DRAFT, DeploymentStatus.ACTIVE),
        (DeploymentStatus.STAGING, DeploymentStatus.ACTIVE),
        (DeploymentStatus.FAILED, DeploymentStatus.ACTIVE),
        (DeploymentStatus.ROLLED_BACK, DeploymentStatus.ACTIVE),
        (DeploymentStatus.OPERATOR_REQUIRED, DeploymentStatus.ACTIVE),
        (DeploymentStatus.REJECTED, DeploymentStatus.ACTIVE),
        (DeploymentStatus.ACTIVE, DeploymentStatus.STAGING),
    ]
    for current, target in invalid_transitions:
        with pytest.raises(
            DeploymentValidationError, match="Invalid deployment lifecycle transition"
        ):
            validate_deployment_transition(current, target)


def test_b7_verify_readiness_transitions_staging_to_verifying() -> None:
    """[B7] Verify verify_readiness() transitions STAGING record to VERIFYING."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.STAGING,
    )
    probe_res, verifying_rec = manager.verify_readiness(
        record=record,
        tool_id=ToolId("cv.deploy.probe"),
        tool_executor=_MockToolExecutor(),
        target_id=plan.target.target_id,
    )
    assert probe_res.passed is True
    assert verifying_rec.status == DeploymentStatus.VERIFYING
    assert verifying_rec.readiness_result == probe_res


def test_b7_activate_rejects_non_verifying_record() -> None:
    """[B7] Verify activate() rejects any record not in VERIFYING state."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    op_id, app_req, app_dec = _make_approval_context(plan)

    for bad_status in [
        DeploymentStatus.DRAFT,
        DeploymentStatus.STAGING,
        DeploymentStatus.FAILED,
        DeploymentStatus.ACTIVE,
        DeploymentStatus.ROLLED_BACK,
    ]:
        record = DeploymentRecord(
            deployment_id=plan.deployment_id,
            plan=plan,
            status=bad_status,
        )
        with pytest.raises(
            DeploymentValidationError,
            match="must be in VERIFYING state to be activated",
        ):
            manager.activate(
                record=record,
                operation_identity=op_id,
                approval_request=app_req,
                approval_decision=app_dec,
                tool_id=ToolId("cv.deploy.activate"),
                tool_executor=_MockToolExecutor(),
                target_id=plan.target.target_id,
            )


# --- B8: Null-Predecessor Rollback Produces OPERATOR_REQUIRED & Lockout ---
def test_b8_null_predecessor_rollback_escalates_to_operator_required_record() -> None:
    """[B8] Verify rollback without predecessor returns record in OPERATOR_REQUIRED state."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan(rollback_target_id=None)
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.ACTIVE,
    )
    active_deployments = {record.deployment_id: record}

    escalated, restored = manager.rollback(
        record=record,
        reason="Pipeline stall on initial deployment",
        tool_id=ToolId("cv.deploy.rollback"),
        tool_executor=_MockToolExecutor(),
        target_id=plan.target.target_id,
        active_deployments=active_deployments,
    )
    assert escalated.status == DeploymentStatus.OPERATOR_REQUIRED
    assert restored is None
    assert "OPERATOR_REQUIRED" in (escalated.failure_details or "")
    assert (
        active_deployments[record.deployment_id].status
        == DeploymentStatus.OPERATOR_REQUIRED
    )


def test_b8_operator_required_locks_out_subsequent_staging_and_activation() -> None:
    """[B8] Verify an OPERATOR_REQUIRED record for a target locks out staging and activation."""
    manager = DefaultDeploymentManager()
    plan1 = _make_valid_deployment_plan(deployment_id="dep-initial")
    op_record = DeploymentRecord(
        deployment_id=plan1.deployment_id,
        plan=plan1,
        status=DeploymentStatus.OPERATOR_REQUIRED,
    )
    active_deployments = {op_record.deployment_id: op_record}

    plan2 = _make_valid_deployment_plan(deployment_id="dep-next")
    with pytest.raises(
        DeploymentValidationError, match="locked out by deployment.*OPERATOR_REQUIRED"
    ):
        manager.stage(
            plan=plan2,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=_MockToolExecutor(),
            target_id=plan2.target.target_id,
            optimization_result=_make_valid_optimization_result(
                artifact_id=str(plan2.artifact_id)
            ),
            active_deployments=active_deployments,
        )

    verifying_rec = DeploymentRecord(
        deployment_id=plan2.deployment_id,
        plan=plan2,
        status=DeploymentStatus.VERIFYING,
        readiness_result=ReadinessProbeResult(
            passed=True,
            startup_time_sec=1.0,
            probe_latency_mean_ms=10.0,
            vram_consumed_mb=1024.0,
            probed_at=datetime.now(timezone.utc),
        ),
    )
    op_id, app_req, app_dec = _make_approval_context(plan2)
    with pytest.raises(
        DeploymentValidationError, match="locked out by deployment.*OPERATOR_REQUIRED"
    ):
        manager.activate(
            record=verifying_rec,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.deploy.activate"),
            tool_executor=_MockToolExecutor(),
            target_id=plan2.target.target_id,
            active_deployments=active_deployments,
        )


# --- B9: Post-Optimization Verification Strict Binding & Evidence Checks ---
def test_b9_verifier_dataset_binding_mismatch_fails_closed() -> None:
    """[B9] Verify verifier rejects evaluation if dataset binding does not match plan."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(plan)

    bad_ds_eval = _make_valid_evaluation_result("exp-base")
    bad_ds_binding = _make_valid_verification_binding().model_copy(
        update={"split_name": "validation"}
    )
    bad_plan = bad_ds_eval.plan.model_copy(update={"dataset_binding": bad_ds_binding})
    cand_eval = bad_ds_eval.model_copy(update={"plan": bad_plan})

    with pytest.raises(
        OptimizationVerificationError, match="split_name 'validation' does not match"
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=bad_ds_eval,
            candidate_evaluation=cand_eval,
            candidate_artifact_binding=_make_valid_candidate_binding(
                artifact, cand_eval.evaluation_id
            ),
        )


def test_b9_verifier_candidate_artifact_binding_mismatch_fails_closed() -> None:
    """[B9] Verify verifier rejects candidate evaluation if model checkpoint does not match source checkpoint."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(plan)
    base_eval = _make_valid_evaluation_result("exp-base")
    cand_eval = _make_valid_evaluation_result("exp-cand")

    bad_cand_model = cand_eval.plan.model_artifact.model_copy(
        update={"checkpoint_id": CheckpointId("ckpt-completely-unrelated")}
    )
    bad_plan = cand_eval.plan.model_copy(update={"model_artifact": bad_cand_model})
    bad_cand_eval = cand_eval.model_copy(update={"plan": bad_plan})
    cand_binding = _make_valid_candidate_binding(artifact, bad_cand_eval.evaluation_id)

    with pytest.raises(
        OptimizationVerificationError, match="does not match source checkpoint"
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=base_eval,
            candidate_evaluation=bad_cand_eval,
            candidate_artifact_binding=cand_binding,
        )


def test_b9_verifier_hardware_mismatch_fails_closed() -> None:
    """[B9] Verify verifier rejects evaluation with hardware environment mismatch."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(plan)
    base_eval = _make_valid_evaluation_result("exp-base")

    diff_env = base_eval.environment.model_copy(
        update={"target_hardware": "NVIDIA A100-SXM4-80GB"}
    )
    cand_eval = _make_valid_evaluation_result("exp-cand").model_copy(
        update={"environment": diff_env}
    )

    with pytest.raises(
        OptimizationVerificationError, match="Hardware environment mismatch"
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=base_eval,
            candidate_evaluation=cand_eval,
            candidate_artifact_binding=_make_valid_candidate_binding(
                artifact, cand_eval.evaluation_id
            ),
        )


def test_b9_verifier_non_passed_verdict_fails_closed() -> None:
    """[B9] Verify verifier strictly rejects candidate or baseline evaluations with non-PASSED verdict."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(plan)
    base_eval = _make_valid_evaluation_result("exp-base")
    failed_cand_eval = _make_valid_evaluation_result("exp-cand").model_copy(
        update={"verdict": EvaluationVerdict.FAILED_QUALITY}
    )

    with pytest.raises(
        OptimizationVerificationError,
        match="Candidate evaluation verdict must be PASSED",
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=base_eval,
            candidate_evaluation=failed_cand_eval,
            candidate_artifact_binding=_make_valid_candidate_binding(
                artifact, failed_cand_eval.evaluation_id
            ),
        )


def test_b9_verifier_unsynchronized_timing_benchmark_fails_closed() -> None:
    """[B9] Verify verifier strictly requires timing_synchronized=True on benchmark evidence."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(plan)
    base_eval = _make_valid_evaluation_result("exp-base")
    cand_eval = _make_valid_evaluation_result("exp-cand")

    unsync_bench = cand_eval.system_benchmarks[0].model_copy(
        update={"timing_synchronized": False}
    )
    cand_eval_unsync = cand_eval.model_copy(
        update={"system_benchmarks": (unsync_bench,)}
    )

    with pytest.raises(
        OptimizationVerificationError, match="timing is not hardware-synchronized"
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=base_eval,
            candidate_evaluation=cand_eval_unsync,
            candidate_artifact_binding=_make_valid_candidate_binding(
                artifact, cand_eval_unsync.evaluation_id
            ),
        )


# --- B10: Pydantic Contract Field Identifier Validators Attached ---
def test_b10_pydantic_contract_rejects_malformed_optimization_plan_id() -> None:
    """[B10] Verify OptimizationPlan rejects malformed plan_id via Pydantic validator."""
    bad_ids = ["", "   ", "OPT_PLAN_UPPER", "plan with spaces", "plan!special#"]
    for bad_id in bad_ids:
        with pytest.raises(ValidationError):
            OptimizationPlan(
                plan_id=bad_id,  # type: ignore[arg-type]
                source_checkpoint_id=CheckpointId("ckpt-001"),
                source_weights_hash=ContentHash("sha256:" + "2" * 64),
                technique=OptimizationTechnique.TENSORRT_COMPILATION,
                target_precision=OptimizationPrecision.FP16,
                target_platform="nvidia_jetson_agx_orin",
                target_compute_capability="8.7",
                shape_profiles=(_make_valid_shape_profile(),),
                verification_dataset_binding=_make_valid_verification_binding(),
                created_at=datetime.now(timezone.utc),
            )


def test_b10_pydantic_contract_rejects_malformed_optimized_artifact_id() -> None:
    """[B10] Verify OptimizedModelArtifact rejects malformed artifact_id via Pydantic validator."""
    plan = _make_valid_optimization_plan()
    bad_ids = ["", "   ", "ART_UPPER", "art with spaces", "art%bad"]
    for bad_id in bad_ids:
        with pytest.raises(ValidationError):
            OptimizedModelArtifact(
                artifact_id=bad_id,  # type: ignore[arg-type]
                plan_id=plan.plan_id,
                source_checkpoint_id=plan.source_checkpoint_id,
                technique=plan.technique,
                precision=plan.target_precision,
                runtime_format="tensorrt_engine",
                target_device="target-jetson-01",
                target_compute_capability=plan.target_compute_capability,
                artifact_hash=ContentHash("sha256:" + "3" * 64),
                artifact_uri="s3://model.engine",
                artifact_size_bytes=85000000,
                shape_profiles=plan.shape_profiles,
                created_at=datetime.now(timezone.utc),
            )


def test_b10_pydantic_contract_rejects_malformed_deployment_id() -> None:
    """[B10] Verify DeploymentPlan rejects malformed deployment_id via Pydantic validator."""
    bad_ids = ["", "   ", "DEP_UPPER", "dep with spaces", "dep@bad"]
    for bad_id in bad_ids:
        with pytest.raises(ValidationError):
            DeploymentPlan(
                deployment_id=bad_id,  # type: ignore[arg-type]
                artifact_id=OptimizedArtifactId("art-001"),
                target=_make_valid_deployment_target(),
                runtime=DeploymentRuntime.STANDALONE_TENSORRT,
                health_check=HealthCheckSpec(
                    startup_timeout_sec=30.0,
                    readiness_probe_samples=5,
                    max_latency_threshold_ms=25.0,
                    memory_headroom_mb=1024.0,
                ),
                created_at=datetime.now(timezone.utc),
            )


# =============================================================================
# B11: Async Execution Semantics (ADR-0005 & ADR-0003 Boundary)
# =============================================================================
def test_b11_async_tool_dispatch_returns_active_job_handoff_without_artifact() -> None:
    """[B11] Verify DefaultModelOptimizer.compile returns ActiveJobHandoff on async dispatch without constructing artifact."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()
    ckpt = _make_valid_checkpoint_manifest()

    active_handoff = ActiveJobHandoff(
        job_id="remote-opt-job-001",
        job_type="export",
        target="remote_gpu",
        submitted_at=datetime.now(timezone.utc).isoformat(),
        status="running",
    )
    executor = _MockToolExecutor(
        status=ToolExecutionStatus.SUCCESS,
        active_job_ref=active_handoff,
        structured_output=None,
    )

    result = optimizer.compile(
        plan=plan,
        tool_id=ToolId("cv.optimize.compile"),
        target_id=TargetId("target-jetson-01"),
        tool_executor=executor,
        checkpoint_manifest=ckpt,
    )

    assert isinstance(result, ActiveJobHandoff)
    assert not isinstance(result, OptimizedModelArtifact)
    assert result.job_id == "remote-opt-job-001"
    assert result.target == "remote_gpu"


def test_b11_sync_tool_dispatch_returns_optimized_model_artifact() -> None:
    """[B11] Verify DefaultModelOptimizer.compile returns OptimizedModelArtifact on synchronous completion."""
    optimizer = DefaultModelOptimizer()
    plan = _make_valid_optimization_plan()
    ckpt = _make_valid_checkpoint_manifest()

    executor = _MockToolExecutor(
        status=ToolExecutionStatus.SUCCESS,
        active_job_ref=None,
        structured_output={
            "artifact_uri": "artifact://models/opt-plan-001/model.engine",
            "artifact_size_bytes": 85000000,
            "runtime_format": "tensorrt_engine",
            "artifact_hash": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
        },
    )

    result = optimizer.compile(
        plan=plan,
        tool_id=ToolId("cv.optimize.compile"),
        target_id=TargetId("target-jetson-01"),
        tool_executor=executor,
        checkpoint_manifest=ckpt,
    )

    assert isinstance(result, OptimizedModelArtifact)
    assert result.plan_id == plan.plan_id
    assert result.runtime_format == "tensorrt_engine"


# =============================================================================
# B12: Mandatory Empirical OptimizationResult on Staging (Criterion 5)
# =============================================================================
def test_b12_stage_fails_closed_when_optimization_result_none() -> None:
    """[B12] Verify DeploymentManager.stage strictly fails closed when optimization_result is None."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()

    with pytest.raises(
        OptimizationVerificationError,
        match="Mandatory OptimizationResult missing for deployment staging",
    ):
        manager.stage(
            plan=plan,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
            optimization_result=None,  # type: ignore[arg-type]
        )


def test_b12_stage_fails_closed_when_optimization_result_unverified() -> None:
    """[B12] Verify DeploymentManager.stage strictly fails closed when verification_passed is False."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan()
    failed_opt_res = _make_valid_optimization_result(artifact_id=str(plan.artifact_id))
    failed_opt_res = failed_opt_res.model_copy(update={"verification_passed": False})

    with pytest.raises(
        OptimizationVerificationError,
        match="Cannot deploy unverified or degraded artifact",
    ):
        manager.stage(
            plan=plan,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
            optimization_result=failed_opt_res,
        )


def test_b12_stage_fails_closed_when_optimization_result_artifact_mismatched() -> None:
    """[B12] Verify DeploymentManager.stage strictly fails closed when artifact_id does not match plan."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan(artifact_id="art-expected-001")
    mismatched_opt_res = _make_valid_optimization_result(artifact_id="art-other-999")

    with pytest.raises(
        OptimizationValidationError,
        match="does not match DeploymentPlan artifact",
    ):
        manager.stage(
            plan=plan,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=_MockToolExecutor(),
            target_id=plan.target.target_id,
            optimization_result=mismatched_opt_res,
        )


# =============================================================================
# B13: Target-Scoped Operational Lockout Index (ADR-0012 §3.7)
# =============================================================================
def test_b13_operator_required_lockout_persists_in_safety_index_without_external_registry() -> (
    None
):
    """[B13] Verify rollback escalation to OPERATOR_REQUIRED records in safety_index and locks subsequent staging without caller registry."""
    manager = DefaultDeploymentManager()
    plan = _make_valid_deployment_plan(deployment_id="dep-001")
    record = DeploymentRecord(
        deployment_id=plan.deployment_id,
        plan=plan,
        status=DeploymentStatus.ACTIVE,
    )

    # Rollback without predecessor escalates to OPERATOR_REQUIRED and updates manager._safety_index
    escalated, restored = manager.rollback(
        record=record,
        reason="Pipeline hardware watchdog timeout",
        tool_id=ToolId("cv.deploy.rollback"),
        tool_executor=_MockToolExecutor(),
        target_id=plan.target.target_id,
        active_deployments=None,  # Zero external registry passed!
    )
    assert escalated.status == DeploymentStatus.OPERATOR_REQUIRED
    assert restored is None

    # Subsequent staging on the same target fails closed even with active_deployments=None
    new_plan = _make_valid_deployment_plan(deployment_id="dep-002")
    with pytest.raises(
        DeploymentValidationError, match="locked out by deployment.*OPERATOR_REQUIRED"
    ):
        manager.stage(
            plan=new_plan,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=_MockToolExecutor(),
            target_id=new_plan.target.target_id,
            optimization_result=_make_valid_optimization_result(
                artifact_id=str(new_plan.artifact_id)
            ),
            active_deployments=None,
        )


def test_b13_operator_required_lockout_is_strictly_target_scoped() -> None:
    """[B13] Verify target lockout strictly affects the locked TargetId, permitting unaffected targets."""
    target_b = DeploymentTarget(
        target_id=TargetId("target-jetson-02"),
        hostname_or_ip="192.168.1.101",
        platform_type="nvidia_jetson_agx_orin",
        gpu_device_name="NVIDIA Jetson AGX Orin 64GB",
        compute_capability="8.7",
        cuda_runtime_version="12.2",
        driver_version="535.104.05",
        max_memory_allocation_gb=48.0,
        power_mode="MAXN",
    )

    plan_a = _make_valid_deployment_plan(deployment_id="dep-a")
    op_record_a = DeploymentRecord(
        deployment_id=plan_a.deployment_id,
        plan=plan_a,
        status=DeploymentStatus.OPERATOR_REQUIRED,
    )

    # Initialize manager with target A locked out in safety index
    manager = DefaultDeploymentManager(
        safety_index={op_record_a.deployment_id: op_record_a}
    )

    # Staging on Target A fails closed
    plan_a_next = _make_valid_deployment_plan(deployment_id="dep-a-next")
    with pytest.raises(
        DeploymentValidationError, match="locked out by deployment.*OPERATOR_REQUIRED"
    ):
        manager.stage(
            plan=plan_a_next,
            tool_id=ToolId("cv.deploy.stage"),
            tool_executor=_MockToolExecutor(),
            target_id=plan_a_next.target.target_id,
            optimization_result=_make_valid_optimization_result(
                artifact_id=str(plan_a_next.artifact_id)
            ),
        )

    # Staging on Target B SUCCEEDS without obstruction
    plan_b = DeploymentPlan(
        deployment_id=DeploymentId("dep-b"),
        artifact_id=OptimizedArtifactId("art-opt-plan-001"),
        target=target_b,
        runtime=DeploymentRuntime.STANDALONE_TENSORRT,
        health_check=HealthCheckSpec(
            startup_timeout_sec=30.0,
            readiness_probe_samples=5,
            max_latency_threshold_ms=25.0,
            memory_headroom_mb=1024.0,
        ),
        created_at=datetime.now(timezone.utc),
    )
    rec_b = manager.stage(
        plan=plan_b,
        tool_id=ToolId("cv.deploy.stage"),
        tool_executor=_MockToolExecutor(),
        target_id=target_b.target_id,
        optimization_result=_make_valid_optimization_result(
            artifact_id=str(plan_b.artifact_id)
        ),
    )
    assert rec_b.status == DeploymentStatus.STAGING


# =============================================================================
# B14: Explicit Optimized-Artifact Evaluation Binding (ADR-0011 Untouched)
# =============================================================================
def test_b14_verifier_rejects_missing_candidate_artifact_binding() -> None:
    """[B14] Verify verifier strictly rejects verification when candidate_artifact_binding is None."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(plan)
    base_eval = _make_valid_evaluation_result("exp-base")
    cand_eval = _make_valid_evaluation_result("exp-cand")

    with pytest.raises(
        OptimizationVerificationError,
        match="candidate_artifact_binding is mandatory",
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=base_eval,
            candidate_evaluation=cand_eval,
            candidate_artifact_binding=None,
        )


def test_b14_verifier_rejects_candidate_artifact_id_mismatch() -> None:
    """[B14] Verify verifier rejects binding where candidate_artifact_id does not match artifact.artifact_id."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(plan)
    base_eval = _make_valid_evaluation_result("exp-base")
    cand_eval = _make_valid_evaluation_result("exp-cand")

    bad_binding = OptimizedArtifactEvaluationBinding(
        candidate_artifact_id=OptimizedArtifactId("art-completely-different"),
        candidate_artifact_hash=artifact.artifact_hash,
        evaluation_id=cand_eval.evaluation_id,
    )

    with pytest.raises(
        OptimizationVerificationError,
        match="Candidate artifact binding ID.*does not match",
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=base_eval,
            candidate_evaluation=cand_eval,
            candidate_artifact_binding=bad_binding,
        )


def test_b14_verifier_rejects_candidate_artifact_hash_mismatch() -> None:
    """[B14] Verify verifier rejects binding where candidate_artifact_hash does not match artifact.artifact_hash."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(plan)
    base_eval = _make_valid_evaluation_result("exp-base")
    cand_eval = _make_valid_evaluation_result("exp-cand")

    bad_binding = OptimizedArtifactEvaluationBinding(
        candidate_artifact_id=artifact.artifact_id,
        candidate_artifact_hash=ContentHash("sha256:" + "9" * 64),
        evaluation_id=cand_eval.evaluation_id,
    )

    with pytest.raises(
        OptimizationVerificationError,
        match="Candidate artifact binding hash.*does not match",
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=base_eval,
            candidate_evaluation=cand_eval,
            candidate_artifact_binding=bad_binding,
        )


def test_b14_verifier_rejects_candidate_evaluation_id_mismatch() -> None:
    """[B14] Verify verifier rejects binding where evaluation_id does not match candidate_evaluation.evaluation_id."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(plan)
    base_eval = _make_valid_evaluation_result("exp-base")
    cand_eval = _make_valid_evaluation_result("exp-cand")

    bad_binding = OptimizedArtifactEvaluationBinding(
        candidate_artifact_id=artifact.artifact_id,
        candidate_artifact_hash=artifact.artifact_hash,
        evaluation_id=EvaluationId("eval-exp-other"),
    )

    with pytest.raises(
        OptimizationVerificationError,
        match="does not match candidate evaluation",
    ):
        verifier.verify(
            artifact=artifact,
            plan=plan,
            baseline_evaluation=base_eval,
            candidate_evaluation=cand_eval,
            candidate_artifact_binding=bad_binding,
        )


def test_b14_verifier_accepts_valid_candidate_binding_and_retains_adr0011_types() -> (
    None
):
    """[B14] Verify verifier accepts valid binding, preserving ADR-0011 ModelArtifactReference as pure CheckpointId."""
    verifier = DefaultOptimizationVerifier()
    plan = _make_valid_optimization_plan()
    artifact = _make_valid_artifact(plan)
    base_eval = _make_valid_evaluation_result("exp-base")
    cand_eval = _make_valid_evaluation_result("exp-cand")

    # ADR-0011 ModelArtifactReference has source checkpoint ID, NOT an OptimizedArtifactId
    assert cand_eval.plan.model_artifact.checkpoint_id == plan.source_checkpoint_id
    assert isinstance(cand_eval.plan.model_artifact.checkpoint_id, str)

    binding = _make_valid_candidate_binding(artifact, cand_eval.evaluation_id)

    res = verifier.verify(
        artifact=artifact,
        plan=plan,
        baseline_evaluation=base_eval,
        candidate_evaluation=cand_eval,
        candidate_artifact_binding=binding,
    )

    assert res.verification_passed is True
    assert res.candidate_artifact_binding == binding
    assert res.candidate_artifact_binding.candidate_artifact_id == artifact.artifact_id
    assert (
        res.candidate_artifact_binding.candidate_artifact_hash == artifact.artifact_hash
    )
