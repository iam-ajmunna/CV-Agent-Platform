"""tests/test_evaluation_boundary.py — Adversarial boundary test suite for ADR-0011.

Exercises all 16 architectural invariants and review findings:
- Zero local identity synthesis (ADR-0003 authority)
- Zero local target synthesis (no 'default_evaluator')
- Scope-verified target resolution and conflict rejection
- Strict dataset (ADR-0009) and model (ADR-0010) lineage verification
- Calibration split isolation (Invariant 13)
- Fail-closed numeric, latency, throughput, and confusion matrix validation
- Reproducible baseline comparisons and statistical test validation
- Mandatory ToolExecutor and execution failure containment
- ActiveJobHandoff expiry semantics
- Zero orchestration state mutation and zero physical transport leakage
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from cv_agent.dataset import (
    AnnotationVersion,
    ClassDefinition,
    CollectionConditions,
    DatasetId,
    DatasetLineage,
    DatasetManifest,
    DatasetVersion,
    GuidelineRef,
    OperationIdentity,
    SplitMembership,
)
from cv_agent.dataset.types import ContentHash
from cv_agent.domain import CapabilityId
from cv_agent.evaluation.canonicalization import (
    extract_evaluation_operation_payload,
)
from cv_agent.evaluation.comparison import DefaultBaselineComparator
from cv_agent.evaluation.contracts import (
    BaselineComparisonResult,
    BenchmarkConfiguration,
    BenchmarkResult,
    ErrorAnalysisRecord,
    EvaluationDatasetBinding,
    EvaluationPlan,
    EvaluationResult,
    EvaluationTargetRequirements,
    MetricDefinition,
    MetricResult,
    ModelArtifactReference,
)
from cv_agent.evaluation.diagnostics import DefaultErrorAnalyzer
from cv_agent.evaluation.errors import (
    BaselineComparisonError,
    EvaluationApprovalRequiredError,
    EvaluationError,
    EvaluationValidationError,
    MetricCalculationError,
)
from cv_agent.evaluation.metrics import StandardMetricCalculator
from cv_agent.evaluation.runner import DefaultEvaluationRunner, parse_evaluation_result
from cv_agent.evaluation.types import (
    BenchmarkId,
    EvaluationId,
    EvaluationVerdict,
    ExperimentId,
    HardwareClass,
    MetricId,
    StatisticalTestType,
    TaskType,
)
from cv_agent.orchestration import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    OperationId,
    compute_payload_hash,
)
from cv_agent.tools import (
    ActiveJobHandoff,
    TargetId,
    ToolExecutionStatus,
    ToolId,
    ToolInvocation,
    ToolResult,
)
from cv_agent.training.contracts import CheckpointManifest
from cv_agent.training.types import (
    CheckpointId,
    PlanId,
    TrainingJobId,
    TrainingPrecision,
)

# =============================================================================
# Helper Fixtures & Builders
# =============================================================================


def _make_valid_dataset_binding(
    dataset_id: str = "coco-subset",
    version: str = "v1.0.0",
    split_name: str = "test",
) -> EvaluationDatasetBinding:
    return EvaluationDatasetBinding(
        dataset_id=DatasetId(dataset_id),
        dataset_version=DatasetVersion(version),
        dataset_manifest_hash=ContentHash("a" * 64),
        split_name=split_name,
        slice_filters={"lighting": "daylight"},
    )


def _make_valid_model_artifact(
    checkpoint_id: str = "ckpt-yolo-v1",
) -> ModelArtifactReference:
    return ModelArtifactReference(
        checkpoint_id=CheckpointId(checkpoint_id),
        architecture_name="yolov8n",
        weights_hash=ContentHash("b" * 64),
        weights_uri="file:///models/yolov8n.pt",
        precision=TrainingPrecision.FP32,
        input_resolution=(640, 640),
    )


def _make_valid_metric(
    metric_id: str = "val-map50", name: str = "mAP@0.5"
) -> MetricDefinition:
    return MetricDefinition(
        metric_id=MetricId(metric_id),
        name=name,
        task_type=TaskType.DETECTION,
        version="v1.0",
        higher_is_better=True,
        parameters={"iou_threshold": 0.5},
    )


def _make_valid_plan(
    eval_id: str = "eval-001",
    exp_id: str = "exp-001",
    baseline_exp_id: str | None = "exp-base-000",
) -> EvaluationPlan:
    return EvaluationPlan(
        evaluation_id=EvaluationId(eval_id),
        experiment_id=ExperimentId(exp_id),
        created_at=datetime.now(timezone.utc),
        capability_id=CapabilityId("cv.eval.detector"),
        dataset_binding=_make_valid_dataset_binding(),
        model_artifact=_make_valid_model_artifact(),
        task_type=TaskType.DETECTION,
        metrics=(_make_valid_metric(),),
        benchmark_config=BenchmarkConfiguration(
            benchmark_id=BenchmarkId("bench-throughput-001"),
            batch_sizes=(1, 4),
            warmup_iterations=10,
            measured_iterations=50,
            precision=TrainingPrecision.FP16_AMP,
            target_requirements=EvaluationTargetRequirements(
                required_hardware_class=HardwareClass.NVIDIA_GPU,
                min_cuda_compute_capability=(8, 0),
            ),
        ),
        baseline_experiment_id=ExperimentId(baseline_exp_id)
        if baseline_exp_id
        else None,
        target_requirements=EvaluationTargetRequirements(
            required_hardware_class=HardwareClass.NVIDIA_GPU,
            min_cuda_compute_capability=(8, 0),
        ),
    )


def _make_approval_context(
    plan: EvaluationPlan,
    resource_scope: str = "target:gpu-cluster-01",
    operation_version: int = 1,
) -> tuple[OperationIdentity, ApprovalRequest, ApprovalDecision]:
    payload = extract_evaluation_operation_payload(plan)
    canonical_hash = compute_payload_hash(payload)

    op_id = OperationIdentity(
        operation_id="op-eval-001",
        operation_version=str(operation_version),
        canonical_payload_hash=canonical_hash,
        target_identity=resource_scope,
    )

    app_req = ApprovalRequest(
        request_id="req-eval-001",
        session_id="sess-001",
        operation_id=OperationId(value="op-eval-001"),
        operation_version=operation_version,
        canonical_payload_hash=canonical_hash,
        policy_version="1.0",
        action_type="evaluation.execute",
        risk_level="HIGH",
        resource_scope=resource_scope,
        created_at=datetime.now(timezone.utc),
    )

    app_dec = ApprovalDecision(
        request_id="req-eval-001",
        decision=ApprovalStatus.APPROVED,
        principal="human_pm",
        resolved_at=datetime.now(timezone.utc),
        bound_payload_hash=canonical_hash,
        bound_resource_scope=resource_scope,
        bound_policy_version="1.0",
    )

    return op_id, app_req, app_dec


def _make_valid_dataset_manifest(
    dataset_id: str = "coco-subset",
    version: str = "v1.0.0",
    payload_root_checksum: str = "a" * 64,
    has_test_split: bool = True,
) -> DatasetManifest:
    item_count = 4 if has_test_split else 3
    return DatasetManifest(
        dataset_id=DatasetId(dataset_id),
        version=DatasetVersion(version),
        sources=("site-1",),
        n_items=item_count,
        class_definitions=(
            ClassDefinition(
                name="person",
                description="human",
                near_miss_negatives=(),
                ambiguity_resolution="none",
            ),
        ),
        class_distribution={"person": item_count},
        split_policy="random",
        split_membership=SplitMembership(
            train_hashes=(ContentHash("1" * 64),),
            val_hashes=(ContentHash("2" * 64),),
            test_hashes=(ContentHash("3" * 64),) if has_test_split else (),
            calibration_hashes=(ContentHash("4" * 64),),
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
        payload_root_checksum=ContentHash(payload_root_checksum),
        leakage_audit_records=(),
    )


def _make_mock_executor(
    status: ToolExecutionStatus = ToolExecutionStatus.SUCCESS,
    error_message: str | None = None,
) -> Any:
    class _MockExecutor:
        def __init__(self) -> None:
            self.invocations: list[ToolInvocation] = []

        async def execute(self, invocation: ToolInvocation) -> ToolResult:
            self.invocations.append(invocation)
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=status,
                error_message=error_message,
                structured_output={
                    "predictions": {"val-map50": 0.745},
                    "ground_truth": {"val-map50": 0.745},
                },
            )

        async def probe(self, tool: Any, target: Any) -> Any:
            raise NotImplementedError

    return _MockExecutor()


# =============================================================================
# Test 1: Evaluation cannot define or construct OperationIdentity
# =============================================================================
def test_evaluation_cannot_construct_operation_identity() -> None:
    """Verify cv_agent.evaluation does not define or synthesize OperationIdentity."""
    import cv_agent.evaluation as eval_mod

    assert not hasattr(eval_mod, "OperationIdentity")
    assert not hasattr(eval_mod, "create_operation_identity")

    source_path = Path("cv_agent/evaluation")
    for py_file in source_path.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert node.name != "OperationIdentity", (
                    f"{py_file} defines OperationIdentity"
                )


# =============================================================================
# Test 2: Missing authoritative OperationIdentity fails closed
# =============================================================================
def test_missing_authoritative_operation_identity_fails_closed() -> None:
    """Verify execute() without external OperationIdentity fails closed."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()
    _, app_req, app_dec = _make_approval_context(plan)

    with pytest.raises(
        EvaluationApprovalRequiredError,
        match="Missing authoritative ADR-0003 OperationIdentity",
    ):
        runner.execute(
            plan=plan,
            operation_identity=None,  # type: ignore[arg-type]
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.eval.execute"),
            tool_executor=_make_mock_executor(),
        )


# =============================================================================
# Test 3: Approval request operation version mismatch fails closed
# =============================================================================
def test_approval_request_operation_version_mismatch() -> None:
    """Verify mismatch between OperationIdentity version and ApprovalRequest version fails closed."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()
    op_id, app_req, app_dec = _make_approval_context(plan)

    mismatched_req = app_req.model_copy(update={"operation_version": 2})

    with pytest.raises(
        EvaluationApprovalRequiredError, match="Operation version mismatch"
    ):
        runner.execute(
            plan=plan,
            operation_identity=op_id,
            approval_request=mismatched_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.eval.execute"),
            tool_executor=_make_mock_executor(),
        )


# =============================================================================
# Test 4: Canonical payload hash tampering fails closed
# =============================================================================
def test_canonical_payload_hash_tamper_fails_closed() -> None:
    """Verify modifying any functional field in EvaluationPlan invalidates authorization."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()
    op_id, app_req, app_dec = _make_approval_context(plan)

    # Tamper with plan (e.g. different model weights)
    tampered_artifact = ModelArtifactReference(
        checkpoint_id=plan.model_artifact.checkpoint_id,
        architecture_name=plan.model_artifact.architecture_name,
        weights_hash=ContentHash("f" * 64),  # Tampered!
        weights_uri=plan.model_artifact.weights_uri,
        precision=plan.model_artifact.precision,
        input_resolution=plan.model_artifact.input_resolution,
    )
    tampered_plan = EvaluationPlan(
        evaluation_id=plan.evaluation_id,
        experiment_id=plan.experiment_id,
        created_at=plan.created_at,
        capability_id=plan.capability_id,
        dataset_binding=plan.dataset_binding,
        model_artifact=tampered_artifact,
        task_type=plan.task_type,
        metrics=plan.metrics,
        benchmark_config=plan.benchmark_config,
        baseline_experiment_id=plan.baseline_experiment_id,
        target_requirements=plan.target_requirements,
    )

    with pytest.raises(
        EvaluationApprovalRequiredError, match="Canonical payload hash mismatch"
    ):
        runner.execute(
            plan=tampered_plan,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.eval.execute"),
            tool_executor=_make_mock_executor(),
        )


# =============================================================================
# Test 5: Wrong target identity fails closed
# =============================================================================
def test_wrong_target_identity_fails_closed() -> None:
    """Verify target identity mismatch between OperationIdentity and ApprovalRequest fails closed."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()
    op_id, app_req, app_dec = _make_approval_context(
        plan, resource_scope="target:gpu-cluster-01"
    )

    mismatched_req = app_req.model_copy(
        update={"resource_scope": "target:other-cluster-99"}
    )

    with pytest.raises(
        EvaluationApprovalRequiredError,
        match="Target identity / resource scope mismatch",
    ):
        runner.execute(
            plan=plan,
            operation_identity=op_id,
            approval_request=mismatched_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.eval.execute"),
            tool_executor=_make_mock_executor(),
        )


# =============================================================================
# Test 6: Cross-plan approval replay rejected
# =============================================================================
def test_cross_plan_approval_replay_rejected() -> None:
    """Verify approval for Plan A presented for Plan B fails closed."""
    runner = DefaultEvaluationRunner()
    plan_a = _make_valid_plan(eval_id="eval-plan-a")
    plan_b = _make_valid_plan(eval_id="eval-plan-b")

    op_id_a, app_req_a, app_dec_a = _make_approval_context(plan_a)

    with pytest.raises(
        EvaluationApprovalRequiredError, match="Canonical payload hash mismatch"
    ):
        runner.execute(
            plan=plan_b,
            operation_identity=op_id_a,
            approval_request=app_req_a,
            approval_decision=app_dec_a,
            tool_id=ToolId("cv.eval.execute"),
            tool_executor=_make_mock_executor(),
        )


# =============================================================================
# Test 7: Zero local target synthesis (no 'default_evaluator')
# =============================================================================
def test_zero_local_target_synthesis() -> None:
    """Verify omitting target fails closed when not derivable from approval scope; no default synthesized."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()

    # Approval scope does not declare a target (e.g. general evaluation resource scope)
    op_id, app_req, app_dec = _make_approval_context(
        plan, resource_scope="evaluation_plan:eval-001"
    )

    with pytest.raises(
        EvaluationApprovalRequiredError,
        match="No authorized execution target_id supplied or derivable",
    ):
        runner.execute(
            plan=plan,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.eval.execute"),
            tool_executor=_make_mock_executor(),
            target_id=None,
        )

    # prepare_invocation fails closed without explicit TargetId
    with pytest.raises(
        EvaluationApprovalRequiredError,
        match="Cannot prepare invocation without an authorized TargetId",
    ):
        runner.prepare_invocation(
            plan=plan,
            tool_id=ToolId("cv.eval.execute"),
            target_id=None,  # type: ignore[arg-type]
        )


# =============================================================================
# Test 8: Conflicting target_id rejected (PM Hard Invariant)
# =============================================================================
def test_conflicting_target_id_rejected() -> None:
    """Verify passing target_id that conflicts with approved resource scope fails closed."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()
    op_id, app_req, app_dec = _make_approval_context(
        plan, resource_scope="target:cluster-h100"
    )

    with pytest.raises(
        EvaluationApprovalRequiredError, match="conflicts with authorized scope target"
    ):
        runner.execute(
            plan=plan,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.eval.execute"),
            tool_executor=_make_mock_executor(),
            target_id=TargetId("other-jetson-target"),  # Disagrees with scope!
        )


# =============================================================================
# Test 9: Target derived from scope when omitted
# =============================================================================
def test_target_derived_from_scope_when_omitted() -> None:
    """Verify target_id is cleanly derived from target:<id> in approval scope when omitted."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()
    op_id, app_req, app_dec = _make_approval_context(
        plan, resource_scope="target:cluster-h100"
    )

    result = runner.execute(
        plan=plan,
        operation_identity=op_id,
        approval_request=app_req,
        approval_decision=app_dec,
        tool_id=ToolId("cv.eval.execute"),
        tool_executor=_make_mock_executor(),
        target_id=None,  # Derived!
    )

    assert result.verdict == EvaluationVerdict.PASSED
    assert len(result.system_benchmarks) > 0
    for bench in result.system_benchmarks:
        assert bench.target_id == TargetId("cluster-h100")


# =============================================================================
# Test 10: Dataset manifest hash mismatch fails closed
# =============================================================================
def test_dataset_manifest_hash_mismatch_fails_closed() -> None:
    """Verify dataset manifest hash mismatch against plan fails closed."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()

    manifest = _make_valid_dataset_manifest(payload_root_checksum="9" * 64)

    with pytest.raises(
        EvaluationValidationError, match="Dataset manifest hash mismatch"
    ):
        runner.validate_plan(plan, dataset_manifest=manifest)


# =============================================================================
# Test 11: Dataset split not in manifest fails closed
# =============================================================================
def test_dataset_split_not_in_manifest_fails_closed() -> None:
    """Verify split_name not existing in dataset manifest fails closed."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()

    manifest = _make_valid_dataset_manifest(has_test_split=False)

    # Change plan to point to non-existent split
    unsupported_plan = EvaluationPlan(
        evaluation_id=plan.evaluation_id,
        experiment_id=plan.experiment_id,
        created_at=plan.created_at,
        capability_id=plan.capability_id,
        dataset_binding=EvaluationDatasetBinding(
            dataset_id=DatasetId("coco-subset"),
            dataset_version=DatasetVersion("v1.0.0"),
            dataset_manifest_hash=ContentHash("a" * 64),
            split_name="unsupported_split",
        ),
        model_artifact=plan.model_artifact,
        task_type=plan.task_type,
        metrics=plan.metrics,
        benchmark_config=plan.benchmark_config,
        baseline_experiment_id=plan.baseline_experiment_id,
        target_requirements=plan.target_requirements,
    )

    with pytest.raises(
        EvaluationValidationError, match="does not exist in dataset splits"
    ):
        runner.validate_plan(unsupported_plan, dataset_manifest=manifest)


# =============================================================================
# Test 12: Calibration split isolation (Invariant 13)
# =============================================================================
def test_calibration_split_isolation() -> None:
    """Verify evaluation binding to 'calibration' split fails closed."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()

    manifest = _make_valid_dataset_manifest()

    calib_plan = EvaluationPlan(
        evaluation_id=plan.evaluation_id,
        experiment_id=plan.experiment_id,
        created_at=plan.created_at,
        capability_id=plan.capability_id,
        dataset_binding=EvaluationDatasetBinding(
            dataset_id=DatasetId("coco-subset"),
            dataset_version=DatasetVersion("v1.0.0"),
            dataset_manifest_hash=ContentHash("a" * 64),
            split_name="calibration",  # Illegal for evaluation!
        ),
        model_artifact=plan.model_artifact,
        task_type=plan.task_type,
        metrics=plan.metrics,
        benchmark_config=plan.benchmark_config,
        baseline_experiment_id=plan.baseline_experiment_id,
        target_requirements=plan.target_requirements,
    )

    with pytest.raises(
        EvaluationValidationError,
        match="Calibration split 'calibration' is strictly reserved",
    ):
        runner.validate_plan(calib_plan, dataset_manifest=manifest)


# =============================================================================
# Test 13: Model checkpoint hash mismatch fails closed
# =============================================================================
def test_model_checkpoint_hash_mismatch_fails_closed() -> None:
    """Verify model weights hash mismatch against CheckpointManifest fails closed."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()

    ckpt_manifest = CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-yolo-v1"),
        job_id=TrainingJobId("job-001"),
        plan_id=PlanId("plan-001"),
        epoch=10,
        step=5000,
        weights_hash=ContentHash("9" * 64),  # Mismatch!
        weights_uri="file:///models/yolov8n.pt",
        created_at=datetime.now(timezone.utc),
    )

    with pytest.raises(EvaluationValidationError, match="Model weights hash mismatch"):
        runner.validate_plan(plan, checkpoint_manifest=ckpt_manifest)


# =============================================================================
# Test 14: Defensive immutability
# =============================================================================
def test_defensive_immutability() -> None:
    """Verify frozen models and MappingProxyType prevent dictionary/attribute mutation."""
    plan = _make_valid_plan()

    with pytest.raises(TypeError):
        plan.dataset_binding.slice_filters["new_key"] = "tampered"  # type: ignore[index]

    with pytest.raises(TypeError):
        plan.metrics[0].parameters["iou_threshold"] = 0.99  # type: ignore[index]

    diag = DefaultErrorAnalyzer().analyze({}, {}, plan.dataset_binding)
    with pytest.raises(TypeError):
        diag.slice_performance["new_slice"] = 0.5  # type: ignore[index]


# =============================================================================
# Test 15: Non-finite metric values rejected
# =============================================================================
def test_non_finite_metric_values_rejected() -> None:
    """Verify NaN or Inf in MetricResult raises ValueError."""
    mdef = _make_valid_metric()

    with pytest.raises(ValueError, match="must be a finite float"):
        MetricResult(metric=mdef, primary_value=float("nan"))

    with pytest.raises(ValueError, match="must be a finite float"):
        MetricResult(metric=mdef, primary_value=float("inf"))

    with pytest.raises(ValueError, match="must be a finite float"):
        MetricResult(
            metric=mdef,
            primary_value=0.5,
            per_class_values={"car": float("nan")},
        )


# =============================================================================
# Test 16: Probability-bounded metrics range validation
# =============================================================================
def test_probability_bounded_metrics_range() -> None:
    """Verify accuracy/rate metrics outside [0.0, 1.0] raise MetricCalculationError."""
    calc = StandardMetricCalculator()
    mdef = _make_valid_metric(name="mAP@0.5:0.95")

    with pytest.raises(
        MetricCalculationError, match="outside valid probability/rate bounds"
    ):
        calc.compute({"map@0.5:0.95": 1.25}, {}, [mdef])

    with pytest.raises(
        MetricCalculationError, match="outside valid probability/rate bounds"
    ):
        calc.compute({"map@0.5:0.95": -0.05}, {}, [mdef])


# =============================================================================
# Test 17: Benchmark latency monotonic percentiles
# =============================================================================
def test_benchmark_latency_monotonic_percentiles() -> None:
    """Verify latency percentiles must strictly satisfy 0 <= min <= p50 <= p90 <= p95 <= p99 <= max."""
    with pytest.raises(
        ValueError, match="Latency percentiles violate monotonic ordering"
    ):
        BenchmarkResult(
            benchmark_id=BenchmarkId("bench-001"),
            batch_size=1,
            precision=TrainingPrecision.FP16_AMP,
            latency_min_ms=10.0,
            latency_mean_ms=12.0,
            latency_p50_ms=15.0,
            latency_p90_ms=14.0,  # Violation: p90 < p50!
            latency_p95_ms=18.0,
            latency_p99_ms=20.0,
            latency_max_ms=25.0,
            throughput_fps=80.0,
            peak_vram_gb=2.0,
            cpu_utilization_pct=30.0,
            gpu_utilization_pct=75.0,
            device_name="RTX 4090",
            target_id=TargetId("target-01"),
        )


# =============================================================================
# Test 18: Benchmark strictly positive throughput
# =============================================================================
def test_benchmark_strictly_positive_throughput() -> None:
    """Verify non-positive throughput raises validation error."""
    with pytest.raises(ValueError):
        BenchmarkResult(
            benchmark_id=BenchmarkId("bench-001"),
            batch_size=1,
            precision=TrainingPrecision.FP16_AMP,
            latency_min_ms=10.0,
            latency_mean_ms=12.0,
            latency_p50_ms=12.0,
            latency_p90_ms=14.0,
            latency_p95_ms=15.0,
            latency_p99_ms=16.0,
            latency_max_ms=18.0,
            throughput_fps=0.0,  # Must be > 0!
            peak_vram_gb=2.0,
            cpu_utilization_pct=30.0,
            gpu_utilization_pct=75.0,
            device_name="RTX 4090",
            target_id=TargetId("target-01"),
        )


# =============================================================================
# Test 19: Confusion matrix dimension mismatch
# =============================================================================
def test_confusion_matrix_dimension_mismatch() -> None:
    """Verify confusion matrix must be square K x K matching class_labels."""
    with pytest.raises(ValueError, match="does not match class_labels count"):
        ErrorAnalysisRecord(
            total_samples=100,
            failed_samples_count=10,
            false_positive_count=5,
            false_negative_count=5,
            class_labels=("person", "vehicle"),
            confusion_matrix=(
                (40, 5, 2),  # 3 columns for 2 labels!
                (3, 50, 0),
            ),
            diagnostics_summary="Invalid matrix",
        )


# =============================================================================
# Test 20: Confidence interval bounds
# =============================================================================
def test_confidence_interval_bounds() -> None:
    """Verify confidence interval lower > upper or non-finite values are rejected."""
    mdef = _make_valid_metric()

    with pytest.raises(ValueError, match="lower bound 0.85 exceeds upper bound 0.75"):
        MetricResult(
            metric=mdef,
            primary_value=0.80,
            confidence_interval=(0.85, 0.75),
        )


# =============================================================================
# Test 21: Statistical significance invalid p-value
# =============================================================================
def test_statistical_significance_invalid_p_value() -> None:
    """Verify p_value outside [0.0, 1.0] raises error in BaselineComparisonResult."""
    with pytest.raises(ValueError, match="p_value must be in \\[0.0, 1.0\\]"):
        BaselineComparisonResult(
            baseline_experiment_id=ExperimentId("exp-base"),
            candidate_experiment_id=ExperimentId("exp-cand"),
            comparable_evidence_ref="baseline:exp-base",
            test_type=StatisticalTestType.PAIRED_T_TEST,
            test_version="1.0",
            sample_count=30,
            confidence_level=0.95,
            p_value=1.5,  # Invalid p_value!
            statistically_significant=True,
            quality_metric_deltas={MetricId("val-map50"): 0.05},
            quality_improved=True,
            pareto_dominant=True,
            summary="Invalid comparison",
        )


# =============================================================================
# Test 22: Baseline comparison missing overlap
# =============================================================================
def test_baseline_comparison_missing_overlap() -> None:
    """Verify baseline comparison fails closed when metrics have no common metric_id."""
    comp = DefaultBaselineComparator()
    m_cand = MetricResult(
        metric=_make_valid_metric(metric_id="metric-alpha"), primary_value=0.8
    )
    m_base = MetricResult(
        metric=_make_valid_metric(metric_id="metric-beta"), primary_value=0.7
    )

    with pytest.raises(
        BaselineComparisonError, match="No overlapping metrics between candidate"
    ):
        comp.compare(
            candidate_metrics=[m_cand],
            baseline_metrics=[m_base],
            candidate_benchmarks=[],
            baseline_benchmarks=[],
            candidate_exp_id=ExperimentId("exp-c"),
            baseline_exp_id=ExperimentId("exp-b"),
        )


# =============================================================================
# Test 23: Root baseline None permitted, derived requires baseline
# =============================================================================
def test_root_baseline_none_permitted() -> None:
    """Verify root baseline evaluation allows baseline_experiment_id = None."""
    plan_root = _make_valid_plan(
        eval_id="eval-root", exp_id="exp-root-000", baseline_exp_id=None
    )
    assert plan_root.baseline_experiment_id is None

    runner = DefaultEvaluationRunner()
    assert runner.validate_plan(plan_root) is True

    # Derived plan without baseline_experiment_id fails validation
    plan_derived = _make_valid_plan(
        eval_id="eval-der", exp_id="exp-001-derived", baseline_exp_id=None
    )
    with pytest.raises(
        EvaluationValidationError, match="Derived evaluation experiment must declare"
    ):
        runner.validate_plan(plan_derived)


# =============================================================================
# Test 24: Mandatory ToolExecutor
# =============================================================================
def test_mandatory_tool_executor() -> None:
    """Verify execute() requires mandatory tool_executor, fails closed if omitted."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()
    op_id, app_req, app_dec = _make_approval_context(plan)

    with pytest.raises(
        EvaluationValidationError, match="ToolExecutor is mandatory for execute\\(\\)"
    ):
        runner.execute(
            plan=plan,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.eval.execute"),
            tool_executor=None,  # type: ignore[arg-type]
        )


# =============================================================================
# Test 25: Tool execution failure fails closed
# =============================================================================
def test_tool_execution_failure_fails_closed() -> None:
    """Verify tool execution failure raises EvaluationError, never emitting false PASSED verdict."""
    runner = DefaultEvaluationRunner()
    plan = _make_valid_plan()
    op_id, app_req, app_dec = _make_approval_context(plan)
    failing_executor = _make_mock_executor(
        status=ToolExecutionStatus.EXECUTION_FAILED,
        error_message="Remote node out of memory",
    )

    with pytest.raises(
        EvaluationError, match="Tool execution failed.*Remote node out of memory"
    ):
        runner.execute(
            plan=plan,
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            tool_id=ToolId("cv.eval.execute"),
            tool_executor=failing_executor,
        )


# =============================================================================
# Test 26: ActiveJobHandoff expiry is not failure
# =============================================================================
def test_active_job_handoff_expiry_not_failure() -> None:
    """Verify handoff expiry does not indicate execution failure per ADR-0005 §3.8."""
    handoff = ActiveJobHandoff(
        job_id="job-async-eval-001",
        job_type="evaluation",
        target="remote_gpu",
        submitted_at="2026-09-15T00:00:00Z",
        status="running",
        expires_at="2026-09-15T00:05:00Z",
    )

    # Time after expiry
    future_time = datetime(2026, 9, 15, 0, 10, 0, tzinfo=timezone.utc)
    assert handoff.is_expired(future_time) is True
    # Invariant: status remains 'running'; expiry is an orchestration tracking TTL, not execution failure
    assert handoff.status == "running"


# =============================================================================
# Test 27: Zero orchestration state mutation
# =============================================================================
def test_zero_orchestration_state_mutation() -> None:
    """Verify AST confirms cv_agent/evaluation/ never references SQLite orchestration store or tables."""
    forbidden = {
        "SQLiteOrchestrationStore",
        "checkpoints.sqlite",
        "PRAGMA",
        "sqlite3",
        "transition_lifecycle",
        "save_checkpoint",
    }

    eval_dir = Path("cv_agent/evaluation")
    for py_file in eval_dir.glob("*.py"):
        code = py_file.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in code, f"Forbidden token {token!r} found in {py_file}"


# =============================================================================
# Test 28: Zero physical transport leakage
# =============================================================================
def test_zero_physical_transport_leakage() -> None:
    """Verify AST confirms cv_agent/evaluation/ has zero subprocess, socket, or paramiko imports."""
    forbidden_modules = {"subprocess", "socket", "paramiko", "pty", "posix_ipc"}

    eval_dir = Path("cv_agent/evaluation")
    for py_file in eval_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden_modules, (
                        f"Forbidden import {alias.name} in {py_file}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in forbidden_modules, (
                    f"Forbidden import from {node.module} in {py_file}"
                )


# =============================================================================
# Test 29: Reproducibility environment packaging & parse helper
# =============================================================================
def test_reproducibility_environment_packaging() -> None:
    """Verify parse_evaluation_result packages full EvaluationEnvironmentRecord and verdict."""
    plan = _make_valid_plan()
    tool_output = {
        "predictions": {"val-map50": 0.82},
        "ground_truth": {"val-map50": 0.82},
    }

    result = parse_evaluation_result(tool_output, plan)
    assert isinstance(result, EvaluationResult)
    assert result.verdict == EvaluationVerdict.PASSED
    assert result.evaluation_id == plan.evaluation_id
    assert len(result.quality_metrics) == 1
    assert result.quality_metrics[0].primary_value == 0.82
    assert result.environment.git_commit_sha == "0" * 40
    assert result.environment.python_version == "3.11.0"
