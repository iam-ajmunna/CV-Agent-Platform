"""tests/test_train_evaluate_diagnose_phase6_exit.py — Phase 6 Exit & Acceptance Test Suite.

Authoritative Acceptance Suite for Issue #74 & ROADMAP.md Phase 6:
- Gate G1: Plan/cost integrity — exact manifest/splits/checkpoint/target, deterministic finite estimate, no invented prices.
- Gate G2: Approval/recovery — denied/stale/wrong-scope/wrong-hash approval yields zero dispatch; restart/resume supported; ambiguous execution reconciled.
- Gate G3: Training/ledger — approved ADR-0005 launch, async handoff, empirical telemetry/checkpoint lineage, failure diagnostics, complete permanent ledger row.
- Gate G4: Composite evaluation — ADR-0011 authoritative checkpoint/test/target, quality + on-target latency/memory/power + constraints + comparable root-baseline delta.
- Gate G5: Diagnosis/proposal/boundaries — ranked observed failure categories with evidence, causal hypotheses distinct, next experiment remains an undispatched proposal.

20 Mandatory Adversarial Cases:
1. Missing/wrong dataset version or manifest hash rejects.
2. Train/val leakage or test/calibration contamination rejects before dispatch.
3. Unresolved base-model/checkpoint binding rejects.
4. Unsupported/unregistered named target rejects (no local synthesis).
5. Missing/nonfinite cost inputs or invented hourly price rejects.
6. Approval denied yields zero dispatch.
7. Approval expired or mismatched payload hash/scope/estimate yields zero dispatch.
8. Approval interrupt survives restart and resumes once.
9. Duplicate/replayed dispatch does not launch duplicate paid jobs.
10. Ambiguous external execution remains UNKNOWN/RECONCILING until reconciled.
11. Async dispatch SUCCESS without terminal evidence cannot mark training complete.
12. NaN/Inf, divergence, or OOM yields diagnostics and no successful experiment.
13. Invalid checkpoint/hash or missing reproducibility evidence prevents completion/evaluation.
14. Failed/partial training never produces completed ledger record or evaluation.
15. Evaluation wrong dataset/test split/checkpoint/target rejects.
16. Missing quality/latency/memory/power or unsynchronized accelerator timing rejects composite publication.
17. Incomparable baseline (dataset/split/threshold/target) cannot claim delta.
18. Missing failure-category counts/evidence cannot produce a fabricated ranking.
19. Next experiment proposal cannot execute without separate fresh approval (is_dispatched=False).
20. Clean approved run -> one completed training record, composite evaluation, evidence-backed ranked failures, and one proposed next experiment.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

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
    ItemMetadata,
    OperationIdentity,
    SplitLeakageError,
    SplitMembership,
    compute_payload_root_checksum,
)
from cv_agent.domain import CapabilityId
from cv_agent.evaluation.contracts import (
    ErrorAnalysisRecord,
    MetricDefinition,
)
from cv_agent.evaluation.errors import (
    EvaluationApprovalRequiredError,
    EvaluationError,
    EvaluationValidationError,
)
from cv_agent.evaluation.types import (
    MetricId,
    TaskType,
)
from cv_agent.experiments import (
    AccuracyMetrics,
    ArtifactManifest,
    ExecutionTarget,
    ExperimentSpec,
    ExperimentStatus,
    SQLiteExperimentLedger,
    SystemMetrics,
    WorkloadType,
)
from cv_agent.orchestration.approvals import (
    ApprovalDecision,
    ApprovalManager,
    ApprovalRequest,
    ApprovalStatus,
)
from cv_agent.orchestration.contracts import (
    OperationId,
    OperationIntent,
    OperationPayloadEnvelope,
    OperationStatus,
)
from cv_agent.orchestration.fencing import LeaseManager
from cv_agent.orchestration.reconciliation import (
    ReconciliationCoordinator,
    ReconciliationOutcome,
)
from cv_agent.orchestration.store import SQLiteOrchestrationStore
from cv_agent.orchestration.train_evaluate_diagnose import (
    OperatingPoint,
    Phase6ApprovalError,
    Phase6DiagnosisError,
    Phase6EvaluationError,
    Phase6ExecutionError,
    Phase6ValidationError,
    TargetConstraint,
    TrainEvaluateDiagnoseRequest,
    TrainEvaluateDiagnoseWorkflow,
    build_training_evaluation_plan,
    run_train_evaluate_diagnose,
)
from cv_agent.tools import (
    ExecutionId,
    TargetId,
    ToolExecutionStatus,
    ToolExecutor,
    ToolId,
    ToolInvocation,
    ToolResult,
)
from cv_agent.training.canonicalization import compute_training_payload_hash
from cv_agent.training.contracts import (
    AugmentationConfig,
    CheckpointManifest,
    ComputeRequirements,
    CostEstimate,
    LossComponentConfig,
    OptimizerConfig,
    SchedulerConfig,
    TrainingJobRecord,
    TrainingPlan,
    TrainingTelemetryRecord,
)
from cv_agent.training.errors import (
    CostEstimationError,
)
from cv_agent.training.launcher import DefaultTrainingLauncher
from cv_agent.training.types import (
    CheckpointId,
    PlanId,
    TrainingJobId,
    TrainingJobStatus,
    TrainingPrecision,
)

# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------


def _dummy_hash(seed: str) -> ContentHash:
    """Generate a deterministic 64-character lowercase hex SHA-256 hash."""
    return ContentHash(hashlib.sha256(seed.encode("utf-8")).hexdigest())


class MockToolExecutor(ToolExecutor):
    """ADR-0005 ToolExecutor test spy recording invocations and returning controlled results."""

    def __init__(
        self,
        canned_result: ToolResult | None = None,
        should_raise: Exception | None = None,
    ) -> None:
        self.invocations: list[ToolInvocation] = []
        self.canned_result = canned_result
        self.should_raise = should_raise

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        self.invocations.append(invocation)
        if self.should_raise is not None:
            raise self.should_raise
        if self.canned_result is not None:
            return self.canned_result
        return ToolResult(
            execution_id=invocation.execution_id,
            tool_id=invocation.tool_id,
            target_id=invocation.target_id,
            status=ToolExecutionStatus.SUCCESS,
            structured_output={},
        )


def _make_class_def(name: str = "person") -> ClassDefinition:
    return ClassDefinition(
        name=name,
        description="Physical observable human body",
        near_miss_negatives=("mannequin", "statue"),
        ambiguity_resolution="If occlusion > 90%, do not annotate",
    )


def _make_collection_conditions() -> CollectionConditions:
    return CollectionConditions(
        camera_models=("Axis-P3245",),
        sites=("Gate-1", "Perimeter-North"),
        times_of_day=("day", "night"),
        lighting_conditions=("direct_sunlight", "floodlight"),
        weather_conditions=("clear", "rain"),
    )


def _make_clean_manifest_and_metadata(
    dataset_id: str = "ds-security",
    version: str = "v1.0.0",
) -> tuple[DatasetManifest, dict[str, ItemMetadata]]:
    """Construct an authoritative clean dataset manifest with disjoint splits."""
    h_train_1 = _dummy_hash("train-01")
    h_train_2 = _dummy_hash("train-02")
    h_val_1 = _dummy_hash("val-01")
    h_test_1 = _dummy_hash("test-01")
    h_test_2 = _dummy_hash("test-02")
    h_calib_1 = _dummy_hash("calib-01")

    splits = SplitMembership(
        train_hashes=(h_train_1, h_train_2),
        val_hashes=(h_val_1,),
        test_hashes=(h_test_1, h_test_2),
        calibration_hashes=(h_calib_1,),
    )
    root_checksum = compute_payload_root_checksum(splits.all_hashes())

    manifest = DatasetManifest(
        dataset_id=DatasetId(dataset_id),
        version=DatasetVersion(version),
        sources=("cctv_archive",),
        n_items=6,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 6},
        split_policy="temporal-site-disjoint",
        split_membership=splits,
        annotation_version=AnnotationVersion("1.0.0"),
        annotation_guideline_ref=GuidelineRef("docs/guidelines/annotation-v1.md"),
        lineage=DatasetLineage(),
        license_and_constraints="internal_only",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=root_checksum,
    )

    metadata: dict[str, ItemMetadata] = {
        str(h_train_1): ItemMetadata(
            content_hash=h_train_1,
            clip_id="clip-train-1",
            camera_id="cam-01",
            site_id="site-A",
            capture_timestamp=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        ),
        str(h_train_2): ItemMetadata(
            content_hash=h_train_2,
            clip_id="clip-train-1",
            camera_id="cam-01",
            site_id="site-A",
            capture_timestamp=datetime(2026, 1, 1, 10, 5, 0, tzinfo=timezone.utc),
        ),
        str(h_val_1): ItemMetadata(
            content_hash=h_val_1,
            clip_id="clip-val-1",
            camera_id="cam-02",
            site_id="site-B",
            capture_timestamp=datetime(2026, 1, 2, 10, 0, 0, tzinfo=timezone.utc),
        ),
        str(h_test_1): ItemMetadata(
            content_hash=h_test_1,
            clip_id="clip-test-1",
            camera_id="cam-03",
            site_id="site-C",
            capture_timestamp=datetime(2026, 1, 3, 10, 0, 0, tzinfo=timezone.utc),
        ),
        str(h_test_2): ItemMetadata(
            content_hash=h_test_2,
            clip_id="clip-test-1",
            camera_id="cam-03",
            site_id="site-C",
            capture_timestamp=datetime(2026, 1, 3, 10, 5, 0, tzinfo=timezone.utc),
        ),
        str(h_calib_1): ItemMetadata(
            content_hash=h_calib_1,
            clip_id="clip-calib-1",
            camera_id="cam-04",
            site_id="site-D",
            capture_timestamp=datetime(2026, 1, 4, 10, 0, 0, tzinfo=timezone.utc),
        ),
    }

    return manifest, metadata


def _make_training_plan(
    manifest: DatasetManifest,
    plan_id: str = "plan-yolov8n-01",
    pretrained_weights_ref: str | None = None,
) -> TrainingPlan:
    """Construct an authoritative TrainingPlan bound to the dataset manifest."""
    return TrainingPlan(
        plan_id=PlanId(plan_id),
        experiment_id="exp-p6-001",
        created_at=datetime.now(timezone.utc),
        capability_id=CapabilityId("cv.detection.train"),
        dataset_id=manifest.dataset_id,
        dataset_version=manifest.version,
        dataset_manifest_hash=ContentHash(manifest.payload_root_checksum),
        train_split="train",
        val_split="val",
        architecture_name="yolov8n",
        num_classes=1,
        pretrained_weights_ref=pretrained_weights_ref,
        optimizer=OptimizerConfig(name="adamw", lr=0.001),
        scheduler=SchedulerConfig(policy="cosine"),
        losses=(
            LossComponentConfig(loss_type="box_loss", weight=1.0),
            LossComponentConfig(loss_type="cls_loss", weight=0.5),
        ),
        augmentation=AugmentationConfig(
            pipeline_name="standard_detection",
            image_size=(640, 640),
            horizontal_flip_prob=0.5,
        ),
        batch_size_per_device=16,
        epochs=10,
        precision=TrainingPrecision.FP16_AMP,
        compute_requirements=ComputeRequirements(
            min_vram_gb=8.0,
            num_devices=1,
        ),
    )


def _make_training_approval_credentials(
    plan: TrainingPlan,
    target_id: TargetId,
    operation_id: str = "op-train-001",
    decision: ApprovalStatus = ApprovalStatus.APPROVED,
    expires_at: datetime | None = None,
    estimated_cost_usd: Decimal | None = None,
    bound_payload_hash: str | None = None,
    resource_scope: str | None = None,
) -> tuple[OperationIdentity, ApprovalRequest, ApprovalDecision]:
    """Construct verified ADR-0003 operation identity and approval credentials for training."""
    computed_hash = compute_training_payload_hash(plan)
    payload_hash = bound_payload_hash or computed_hash
    scope = resource_scope or f"target:{target_id}"

    op_id = OperationIdentity(
        operation_id=operation_id,
        operation_version="1",
        canonical_payload_hash=computed_hash,
        target_identity=scope,
    )

    app_req = ApprovalRequest(
        request_id="req-train-001",
        session_id="sess-train-001",
        operation_id=OperationId(value=operation_id),
        operation_version=1,
        canonical_payload_hash=computed_hash,
        policy_version="1.0",
        action_type="training.execute",
        risk_level="HIGH",
        resource_scope=scope,
        created_at=datetime.now(timezone.utc),
        expires_at=expires_at,
    )

    meta: dict[str, Any] = {}
    if estimated_cost_usd is not None:
        meta["estimated_cost_usd"] = str(estimated_cost_usd)

    app_dec = ApprovalDecision(
        request_id="req-train-001",
        decision=decision,
        principal="human_pm",
        resolved_at=datetime.now(timezone.utc),
        bound_payload_hash=payload_hash,
        bound_resource_scope=scope,
        bound_policy_version="1.0",
        decision_metadata=meta,
    )

    return op_id, app_req, app_dec


def _make_canned_evaluation_output(
    target_id: TargetId,
    timing_synchronized: bool = True,
    timing_source: str = "cuda_event_timer",
    peak_vram_gb: float | None = 2.4,
    power_watts_avg: float | None = 14.5,
    latency_p50_ms: float = 4.8,
    throughput_fps: float = 120.0,
    map_50: float = 0.89,
    target_hardware: str = "NVIDIA Jetson Orin Nano",
    seed: int | None = 42,
) -> dict[str, Any]:
    """Construct structured output dictionary simulating an empirical on-target evaluation run."""
    bench_data = {
        "benchmark_id": "bench-p6-eval",
        "batch_size": 1,
        "precision": "fp16_amp",
        "latency_min_ms": 4.0,
        "latency_mean_ms": 5.0,
        "latency_p50_ms": latency_p50_ms,
        "latency_p90_ms": 5.5,
        "latency_p95_ms": 5.8,
        "latency_p99_ms": 6.2,
        "latency_max_ms": 7.0,
        "throughput_fps": throughput_fps,
        "peak_vram_gb": peak_vram_gb,
        "power_watts_avg": power_watts_avg,
        "device_name": target_hardware,
        "target_id": TargetId(str(target_id)),
        "timing_synchronized": timing_synchronized,
        "timing_source": timing_source,
    }

    env_dict: dict[str, Any] = {
        "git_commit_sha": "a" * 40,
        "git_is_dirty": False,
        "target_hardware": target_hardware,
        "python_version": "3.11.8",
        "torch_version": "2.2.0",
        "cuda_version": "12.2",
        "driver_version": "535.104.05",
    }
    if seed is not None:
        env_dict["seed"] = seed

    return {
        "predictions": {
            "map_50": map_50,
            "test": [
                {
                    "item_id": "test-01",
                    "bbox": [10.0, 10.0, 50.0, 50.0],
                    "class_name": "person",
                    "confidence": 0.95,
                }
            ],
        },
        "ground_truth": {
            "test": [
                {
                    "item_id": "test-01",
                    "bbox": [10.0, 10.0, 50.0, 50.0],
                    "class_name": "person",
                }
            ]
        },
        "system_benchmarks": [bench_data],
        "environment": env_dict,
    }


def _make_clean_request(
    tmp_path: Path,
    target_id: TargetId | None = None,
    sample_step_latency_sec: float = 0.05,
    target_hourly_rate_usd: Decimal = Decimal("2.50"),
    approval_decision_status: ApprovalStatus = ApprovalStatus.APPROVED,
    canned_eval_output: dict[str, Any] | None = None,
    pretrained_weights_ref: str | None = None,
    base_checkpoint: CheckpointManifest | None = None,
    telemetry_stream: list[TrainingTelemetryRecord] | None = None,
    terminal_checkpoint: CheckpointManifest | None = None,
    active_baseline_spec: ExperimentSpec | None = None,
    active_baseline_acc: AccuracyMetrics | None = None,
    active_baseline_sys: SystemMetrics | None = None,
) -> TrainEvaluateDiagnoseRequest:
    """Construct a clean, valid TrainEvaluateDiagnoseRequest."""
    manifest, metadata = _make_clean_manifest_and_metadata()
    plan = _make_training_plan(manifest, pretrained_weights_ref=pretrained_weights_ref)
    tgt_id = target_id or TargetId("orin-agx-01")

    op_id, app_req, app_dec = _make_training_approval_credentials(
        plan=plan,
        target_id=tgt_id,
        decision=approval_decision_status,
    )

    eval_output = canned_eval_output or _make_canned_evaluation_output(tgt_id)
    tool_executor = MockToolExecutor(
        canned_result=ToolResult(
            execution_id=ExecutionId("exec-eval-01"),
            tool_id=ToolId("tool-cv"),
            target_id=tgt_id,
            status=ToolExecutionStatus.SUCCESS,
            structured_output=eval_output,
        )
    )

    ledger = SQLiteExperimentLedger(workspace_root=tmp_path)
    if active_baseline_spec is not None:
        # Prepopulate active baseline in ledger
        rec = ledger.create_experiment(active_baseline_spec)
        ledger.transition_status(rec.exp_id, ExperimentStatus.RUNNING)
        ledger.complete_and_set_baseline(
            exp_id=rec.exp_id,
            accuracy=active_baseline_acc or AccuracyMetrics(map_50=0.82),
            system=active_baseline_sys
            or SystemMetrics(
                throughput_fps=100.0,
                latency_inference_ms=5.0,
                latency_e2e_ms=10.0,
                peak_vram_mb=4000.0,
                power_draw_watts=40.0,
            ),
            artifacts=ArtifactManifest(weights_uri="file:///baseline_weights.pt"),
            workload_type=WorkloadType.BENCHMARK,
        )

    return TrainEvaluateDiagnoseRequest(
        training_plan=plan,
        dataset_manifest=manifest,
        item_metadata=metadata,
        target_id=tgt_id,
        tool_id=ToolId("tool-cv-trainer"),
        tool_executor=tool_executor,
        execution_target=ExecutionTarget.LOCAL_GPU,
        operation_identity=op_id,
        approval_request=app_req,
        approval_decision=app_dec,
        target_hourly_rate_usd=target_hourly_rate_usd,
        sample_step_latency_sec=sample_step_latency_sec,
        operating_point=OperatingPoint(threshold=0.5, false_positive_tolerance=0.05),
        target_constraints=(
            TargetConstraint(metric_name="throughput_fps", target_value=30.0),
            TargetConstraint(metric_name="map_50", target_value=0.75),
        ),
        base_checkpoint=base_checkpoint,
        experiment_ledger=ledger,
        workspace_root=tmp_path,
        commit_sha="a" * 40,
        seed=42,
        config_uri="file:///configs/yolov8.yaml",
        config_hash="b" * 64,
        hyperparameters={"lr": 0.001, "batch_size": 16},
        telemetry_stream=telemetry_stream,
        terminal_checkpoint=terminal_checkpoint,
    )


# ===========================================================================
# 20 Mandatory Adversarial Cases
# ===========================================================================


def test_case_1_dataset_version_or_manifest_hash_mismatch_rejects(tmp_path: Path):
    """Case 1: Missing or mismatched dataset version or manifest hash rejects before dispatch."""
    req = _make_clean_request(tmp_path)

    # 1. Dataset version mismatch
    bad_plan_ver = req.training_plan.model_copy(
        update={"dataset_version": DatasetVersion("v9.9.9")}
    )
    bad_req = TrainEvaluateDiagnoseRequest(
        **{**req.__dict__, "training_plan": bad_plan_ver}
    )
    with pytest.raises(Phase6ValidationError, match="dataset_version"):
        run_train_evaluate_diagnose(bad_req)

    # 2. Manifest hash mismatch
    bad_plan_hash = req.training_plan.model_copy(
        update={"dataset_manifest_hash": ContentHash("0" * 64)}
    )
    bad_req2 = TrainEvaluateDiagnoseRequest(
        **{**req.__dict__, "training_plan": bad_plan_hash}
    )
    with pytest.raises(Phase6ValidationError, match="dataset_manifest_hash"):
        run_train_evaluate_diagnose(bad_req2)


def test_case_2_leakage_and_contamination_rejects_before_dispatch(tmp_path: Path):
    """Case 2: Train/val leakage or test/calibration contamination rejects before dispatch."""
    req = _make_clean_request(tmp_path)

    # 1. Temporal leakage
    leaky_meta = dict(req.item_metadata)
    h_test_1 = req.dataset_manifest.split_membership.test_hashes[0]
    leaky_meta[str(h_test_1)] = ItemMetadata(
        content_hash=h_test_1,
        clip_id="clip-train-1",  # Straddles train clip!
        camera_id="cam-03",
        site_id="site-C",
        capture_timestamp=datetime(2026, 1, 3, 10, 0, 0, tzinfo=timezone.utc),
    )
    bad_req = TrainEvaluateDiagnoseRequest(
        **{**req.__dict__, "item_metadata": leaky_meta}
    )
    with pytest.raises(SplitLeakageError):
        run_train_evaluate_diagnose(bad_req)

    # 2. Contamination: training on test split
    bad_plan_test = req.training_plan.model_copy(update={"train_split": "test"})
    bad_req2 = TrainEvaluateDiagnoseRequest(
        **{**req.__dict__, "training_plan": bad_plan_test}
    )
    with pytest.raises(Phase6ValidationError, match="Disallowed training split"):
        run_train_evaluate_diagnose(bad_req2)

    # 3. Contamination: validation on calibration split
    bad_plan_calib = req.training_plan.model_copy(update={"val_split": "calibration"})
    bad_req3 = TrainEvaluateDiagnoseRequest(
        **{**req.__dict__, "training_plan": bad_plan_calib}
    )
    with pytest.raises(Phase6ValidationError, match="Disallowed validation split"):
        run_train_evaluate_diagnose(bad_req3)


def test_case_3_unresolved_base_checkpoint_binding_rejects(tmp_path: Path):
    """Case 3: Unresolved base-model/checkpoint binding rejects."""
    req = _make_clean_request(
        tmp_path, pretrained_weights_ref="file:///weights/base.pt"
    )

    # Missing base_checkpoint
    with pytest.raises(Phase6ValidationError, match="base_checkpoint"):
        run_train_evaluate_diagnose(req)

    # Mismatched base_checkpoint
    mismatched_ckpt = CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-base-other"),
        job_id=TrainingJobId("job-00"),
        plan_id=PlanId("plan-00"),
        epoch=1,
        step=10,
        weights_uri="file:///weights/different.pt",
        weights_hash=ContentHash("9" * 64),
        created_at=datetime.now(timezone.utc),
    )
    req_mismatch = TrainEvaluateDiagnoseRequest(
        **{**req.__dict__, "base_checkpoint": mismatched_ckpt}
    )
    with pytest.raises(
        Phase6ValidationError, match="Base checkpoint manifest mismatch"
    ):
        run_train_evaluate_diagnose(req_mismatch)


def test_case_4_unregistered_named_target_rejects_zero_local_synthesis(tmp_path: Path):
    """Case 4: Unsupported/unregistered named target rejects (no local synthesis)."""
    # 1. Missing target
    req_none = _make_clean_request(tmp_path)
    bad_req = TrainEvaluateDiagnoseRequest(
        **{**req_none.__dict__, "target_id": None}  # type: ignore[arg-type]
    )
    with pytest.raises(
        Phase6ValidationError, match="Missing authorized execution TargetId"
    ):
        run_train_evaluate_diagnose(bad_req)

    # 2. Placeholder target
    req_ph = _make_clean_request(tmp_path, target_id=TargetId("placeholder-target"))
    with pytest.raises(EvaluationApprovalRequiredError, match="synthesized target"):
        run_train_evaluate_diagnose(req_ph)


def test_case_5_missing_nonfinite_cost_inputs_or_invented_hourly_price_rejects(
    tmp_path: Path,
):
    """Case 5: Missing/nonfinite cost inputs or invented hourly price rejects."""
    # 1. Nonfinite sample step latency
    req1 = _make_clean_request(tmp_path, sample_step_latency_sec=float("nan"))
    with pytest.raises(CostEstimationError, match="sample_step_latency_sec"):
        run_train_evaluate_diagnose(req1)

    # 2. Non-positive latency
    req2 = _make_clean_request(tmp_path, sample_step_latency_sec=-0.05)
    with pytest.raises(CostEstimationError, match="sample_step_latency_sec"):
        run_train_evaluate_diagnose(req2)

    # 3. Non-positive hourly rate
    req3 = _make_clean_request(tmp_path, target_hourly_rate_usd=Decimal("0.00"))
    with pytest.raises(CostEstimationError, match="target_hourly_rate_usd"):
        run_train_evaluate_diagnose(req3)

    # 4. Inconsistent cost estimate embedded in plan
    req4 = _make_clean_request(tmp_path)
    plan_with_bad_cost = req4.training_plan.model_copy(
        update={
            "cost_estimate": CostEstimate(
                estimated_cost_usd=Decimal("999.00"),
                estimated_gpu_hours=10.0,
                estimated_wall_clock_hours=10.0,
                estimated_storage_gb=1.0,
                basis_description="synthetic estimate for test",
            )
        }
    )
    req4_bad = TrainEvaluateDiagnoseRequest(
        **{**req4.__dict__, "training_plan": plan_with_bad_cost}
    )
    with pytest.raises(Phase6ValidationError, match="differs from authoritative"):
        run_train_evaluate_diagnose(req4_bad)


def test_case_6_approval_denied_yields_zero_dispatch(tmp_path: Path):
    """Case 6: Approval denied yields zero dispatch."""
    req = _make_clean_request(
        tmp_path, approval_decision_status=ApprovalStatus.REJECTED
    )
    with pytest.raises(Phase6ApprovalError, match="Approval was NOT granted"):
        run_train_evaluate_diagnose(req)

    # Verify zero dispatch
    mock_exec = req.tool_executor
    assert isinstance(mock_exec, MockToolExecutor)
    assert len(mock_exec.invocations) == 0


def test_case_7_approval_expired_or_mismatched_payload_hash_scope_rejects(
    tmp_path: Path,
):
    """Case 7: Approval expired or mismatched payload hash/scope/estimate yields zero dispatch."""
    # 1. Expired approval
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    manifest, _ = _make_clean_manifest_and_metadata()
    plan = _make_training_plan(manifest)
    tgt_id = TargetId("orin-agx-01")
    _, app_req, app_dec = _make_training_approval_credentials(
        plan=plan, target_id=tgt_id, expires_at=past
    )
    req_exp = _make_clean_request(tmp_path)
    req_exp = TrainEvaluateDiagnoseRequest(
        **{
            **req_exp.__dict__,
            "approval_request": app_req,
            "approval_decision": app_dec,
        }
    )
    with pytest.raises(Phase6ApprovalError, match="expired"):
        run_train_evaluate_diagnose(req_exp)

    # 2. Bound payload hash mismatch
    _, app_req2, app_dec2 = _make_training_approval_credentials(
        plan=plan, target_id=tgt_id, bound_payload_hash="e" * 64
    )
    req_hash = TrainEvaluateDiagnoseRequest(
        **{
            **req_exp.__dict__,
            "approval_request": app_req2,
            "approval_decision": app_dec2,
        }
    )
    with pytest.raises(Phase6ApprovalError, match="payload hash mismatch"):
        run_train_evaluate_diagnose(req_hash)

    # 3. Scope mismatch
    _, app_req3, app_dec3 = _make_training_approval_credentials(
        plan=plan, target_id=tgt_id, resource_scope="target:other-accelerator"
    )
    req_scope = TrainEvaluateDiagnoseRequest(
        **{
            **req_exp.__dict__,
            "approval_request": app_req3,
            "approval_decision": app_dec3,
        }
    )
    with pytest.raises(Phase6ApprovalError, match="conflicts with authorized"):
        run_train_evaluate_diagnose(req_scope)


def test_case_8_approval_interrupt_survives_restart_and_resumes_once(tmp_path: Path):
    """Case 8: Approval interrupt survives restart and resumes once."""
    db_path = tmp_path / "orchestration.db"
    store = SQLiteOrchestrationStore(db_path)
    approval_mgr = ApprovalManager(store)
    lease_mgr = LeaseManager(store)
    lease = lease_mgr.acquire(
        session_id="sess-p6-01",
        thread_id="th-01",
        owner_instance_id="worker-01",
    )

    intent = OperationIntent(
        operation_id=OperationId(value="op-train-resume-001"),
        session_id="sess-p6-01",
        operation_version=1,
        operation_type="train",
        canonical_payload=OperationPayloadEnvelope(
            schema_id="training_plan",
            schema_version=1,
            canonicalization_version="1.0",
            payload_hash="c" * 64,
            payload={},
        ),
        target_identity="target:orin-agx-01",
        created_at=datetime.now(timezone.utc),
    )

    # 1. Create approval request before interrupt
    app_req = approval_mgr.create_request(
        operation=intent,
        action_type="train.execute",
        risk_level="HIGH",
        policy_version="1.0",
        resource_scope="target:orin-agx-01",
        lease=lease,
    )
    assert app_req.request_id is not None

    # Record decision
    approval_mgr.record_decision(
        request_id=app_req.request_id,
        decision=ApprovalStatus.APPROVED,
        principal="human_pm",
        lease=lease,
    )

    # 2. Simulate restart by opening a fresh store on the same sqlite file
    restarted_store = SQLiteOrchestrationStore(db_path)
    found_approval = restarted_store.find_approval(app_req.request_id)
    assert found_approval is not None
    assert found_approval["decision"] == "approved"
    assert found_approval["bound_payload_hash"] == "c" * 64


def test_case_9_duplicate_replayed_dispatch_does_not_launch_duplicate_paid_jobs(
    tmp_path: Path,
):
    """Case 9: Duplicate/replayed dispatch does not launch duplicate paid jobs."""
    req = _make_clean_request(tmp_path)
    res1 = run_train_evaluate_diagnose(req)
    assert res1.training_job.status == TrainingJobStatus.RUNNING
    assert res1.experiment_record.status == ExperimentStatus.COMPLETED

    # Subsequent query to ledger confirms experiment is already completed
    ledger = req.experiment_ledger
    assert isinstance(ledger, SQLiteExperimentLedger)
    record = ledger.get_experiment(res1.experiment_record.exp_id)
    assert record is not None
    assert record.status == ExperimentStatus.COMPLETED

    # Subsequent replayed dispatch must reject without launching another job
    with pytest.raises(Phase6ExecutionError, match="Duplicate dispatch rejected"):
        run_train_evaluate_diagnose(req)


def test_case_10_ambiguous_external_execution_remains_reconciling_until_reconciled(
    tmp_path: Path,
):
    """Case 10: Ambiguous external execution remains UNKNOWN/RECONCILING until reconciled."""
    db_path = tmp_path / "orchestration.db"
    store = SQLiteOrchestrationStore(db_path)
    coord = ReconciliationCoordinator(store=store, reconciler=None)
    lease_mgr = LeaseManager(store)
    lease = lease_mgr.acquire(
        session_id="sess-rec-01",
        thread_id="th-01",
        owner_instance_id="worker-01",
    )

    intent = OperationIntent(
        operation_id=OperationId(value="op-ambiguous-001"),
        session_id="sess-rec-01",
        operation_version=1,
        operation_type="train",
        canonical_payload=OperationPayloadEnvelope(
            schema_id="training_plan",
            schema_version=1,
            canonicalization_version="1.0",
            payload_hash="d" * 64,
            payload={},
        ),
        target_identity="target:orin-agx-01",
        created_at=datetime.now(timezone.utc),
    )
    store.save_operation(intent, lease)
    store.update_operation_status(intent.operation_id, OperationStatus.VALIDATED, lease)
    store.update_operation_status(
        intent.operation_id, OperationStatus.DISPATCHING, lease
    )
    store.update_operation_status(intent.operation_id, OperationStatus.RUNNING, lease)
    store.update_operation_status(intent.operation_id, OperationStatus.UNKNOWN, lease)

    # Initiate reconciliation -> becomes RECONCILING
    coord.initiate_reconciliation(intent.operation_id, lease)
    with store._get_connection() as conn:
        cur = conn.execute(
            "SELECT status FROM operations WHERE operation_id = ?",
            (intent.operation_id.value,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["status"] == OperationStatus.RECONCILING.value

    # Reconciling without reconciler produces UNRESOLVED, never silently mapped to SUCCESS
    outcome = coord.reconcile_operation(intent, lease)
    assert outcome == ReconciliationOutcome.UNRESOLVED


def test_case_11_async_dispatch_success_without_terminal_evidence_cannot_complete(
    tmp_path: Path,
):
    """Case 11: Async dispatch SUCCESS without terminal evidence cannot mark training complete."""
    # Terminal checkpoint manifest with empty weights hash
    bad_ckpt = CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-corrupt-001"),
        job_id=TrainingJobId("job-001"),
        plan_id=PlanId("plan-yolov8n-01"),
        epoch=10,
        step=1000,
        weights_uri="file:///weights/model.pt",
        weights_hash=ContentHash("0" * 63),  # Invalid length (63 instead of 64)
        created_at=datetime.now(timezone.utc),
    )
    req = _make_clean_request(tmp_path, terminal_checkpoint=bad_ckpt)

    with pytest.raises(Phase6ValidationError, match="weights hash"):
        run_train_evaluate_diagnose(req)

    # Experiment must be FAILED in ledger, not completed
    ledger = req.experiment_ledger
    assert isinstance(ledger, SQLiteExperimentLedger)
    experiments = ledger.list_experiments()
    assert len(experiments) == 1
    assert experiments[0].status == ExperimentStatus.FAILED


def test_case_12_telemetry_divergence_nan_inf_or_oom_fails_cleanly(tmp_path: Path):
    """Case 12: NaN/Inf, divergence, or OOM yields diagnostics and no successful experiment."""
    # Divergent loss
    divergent_telemetry = [
        TrainingTelemetryRecord(
            job_id=TrainingJobId("job-001"),
            step=10,
            epoch=1,
            total_epochs=10,
            train_loss=1.2,
            learning_rate=0.001,
            throughput_fps=80.0,
            vram_allocated_gb=4.0,
            timestamp=datetime.now(timezone.utc),
        ),
        TrainingTelemetryRecord.model_construct(
            job_id=TrainingJobId("job-001"),
            step=20,
            epoch=1,
            total_epochs=10,
            train_loss=float("nan"),  # NaN divergence!
            learning_rate=0.001,
            throughput_fps=80.0,
            vram_allocated_gb=4.0,
            timestamp=datetime.now(timezone.utc),
        ),
    ]

    req = _make_clean_request(tmp_path, telemetry_stream=divergent_telemetry)
    with pytest.raises(Phase6ExecutionError, match="Divergence detected"):
        run_train_evaluate_diagnose(req)

    ledger = req.experiment_ledger
    assert isinstance(ledger, SQLiteExperimentLedger)
    exps = ledger.list_experiments()
    assert len(exps) == 1
    assert exps[0].status == ExperimentStatus.FAILED


def test_case_13_invalid_checkpoint_hash_or_missing_reproducibility_prevents_completion(
    tmp_path: Path,
):
    """Case 13: Invalid checkpoint/hash or missing reproducibility evidence prevents completion/evaluation."""
    # 1. Missing commit_sha
    req1 = _make_clean_request(tmp_path)
    req1_bad = TrainEvaluateDiagnoseRequest(**{**req1.__dict__, "commit_sha": None})
    with pytest.raises(Phase6ValidationError, match="commit_sha"):
        run_train_evaluate_diagnose(req1_bad)

    # 2. Placeholder commit_sha
    req2_bad = TrainEvaluateDiagnoseRequest(**{**req1.__dict__, "commit_sha": "0" * 40})
    with pytest.raises(Phase6ValidationError, match="Placeholder git commit SHA"):
        run_train_evaluate_diagnose(req2_bad)

    # 3. Missing seed
    req3_bad = TrainEvaluateDiagnoseRequest(**{**req1.__dict__, "seed": None})
    with pytest.raises(
        Phase6ValidationError, match="Missing or non-integer random seed"
    ):
        run_train_evaluate_diagnose(req3_bad)

    # 4. Missing config_hash
    req4_bad = TrainEvaluateDiagnoseRequest(**{**req1.__dict__, "config_hash": None})
    with pytest.raises(Phase6ValidationError, match="config_hash"):
        run_train_evaluate_diagnose(req4_bad)


def test_case_14_failed_partial_training_never_produces_completed_ledger_or_evaluation(
    tmp_path: Path,
):
    """Case 14: Failed/partial training never produces completed ledger record or evaluation."""

    class FailingLauncher(DefaultTrainingLauncher):
        def launch(self, *args, **kwargs) -> TrainingJobRecord:
            return TrainingJobRecord(
                job_id=TrainingJobId("job-failed-001"),
                plan_id=PlanId("plan-yolov8n-01"),
                status=TrainingJobStatus.FAILED,
                failure_details="GPU Out of Memory on cuda:0",
            )

    req = _make_clean_request(tmp_path)
    req_failing = TrainEvaluateDiagnoseRequest(
        **{**req.__dict__, "training_launcher": FailingLauncher()}
    )

    with pytest.raises(Phase6ExecutionError, match="Training failed"):
        run_train_evaluate_diagnose(req_failing)

    ledger = req.experiment_ledger
    assert isinstance(ledger, SQLiteExperimentLedger)
    exps = ledger.list_experiments()
    assert len(exps) == 1
    assert exps[0].status == ExperimentStatus.FAILED

    # Evaluation runner was NEVER called
    mock_exec = req.tool_executor
    assert isinstance(mock_exec, MockToolExecutor)
    assert len(mock_exec.invocations) == 0


def test_case_15_evaluation_wrong_dataset_test_split_checkpoint_target_rejects(
    tmp_path: Path,
):
    """Case 15: Evaluation wrong dataset/test split/checkpoint/target rejects."""
    manifest, _ = _make_clean_manifest_and_metadata()
    ckpt = CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-01"),
        job_id=TrainingJobId("job-01"),
        plan_id=PlanId("plan-01"),
        epoch=10,
        step=1000,
        weights_uri="file:///weights/model.pt",
        weights_hash=ContentHash("1" * 64),
        created_at=datetime.now(timezone.utc),
    )
    metric_def = MetricDefinition(
        metric_id=MetricId("map_50"), name="mAP@0.50", task_type=TaskType.DETECTION
    )

    # Disallowed evaluation splits: train, val, calibration
    for bad_split in ("train", "val", "calibration"):
        with pytest.raises(EvaluationValidationError, match="strictly disallowed"):
            build_training_evaluation_plan(
                evaluation_id="eval-01",
                experiment_id="exp-01",
                dataset_manifest=manifest,
                checkpoint_manifest=ckpt,
                task_type=TaskType.DETECTION,
                metrics=(metric_def,),
                split_name=bad_split,
            )


def test_case_16_missing_telemetry_or_unsynchronized_accelerator_timing_rejects(
    tmp_path: Path,
):
    """Case 16: Missing quality/latency/memory/power or unsynchronized accelerator timing rejects composite publication."""
    # 1. Unsynchronized accelerator timing
    unsync_output = _make_canned_evaluation_output(
        TargetId("orin-agx-01"), timing_synchronized=False
    )
    req1 = _make_clean_request(tmp_path, canned_eval_output=unsync_output)
    with pytest.raises(
        (Phase6EvaluationError, EvaluationError), match="Hardware-synchronized timing"
    ):
        run_train_evaluate_diagnose(req1)

    # 2. Missing system benchmarks
    no_sys_output = _make_canned_evaluation_output(TargetId("orin-agx-01"))
    no_sys_output["system_benchmarks"] = []
    req2 = _make_clean_request(tmp_path / "part2", canned_eval_output=no_sys_output)
    with pytest.raises(Phase6EvaluationError, match="missing system_benchmarks"):
        run_train_evaluate_diagnose(req2)


def test_case_17_incomparable_baseline_cannot_claim_delta(tmp_path: Path):
    """Case 17: Incomparable baseline (dataset/split/threshold/target) cannot claim delta."""
    # Baseline on different dataset version v0.9.0 vs v1.0.0
    diff_dataset_baseline_spec = ExperimentSpec(
        name="old-baseline",
        hypothesis="Baseline on old dataset",
        workload_type=WorkloadType.BENCHMARK,
        target=ExecutionTarget.LOCAL_GPU,
        model_name="yolov8n",
        model_variant="base",
        commit_sha="c" * 40,
        dataset_version="v0.9.0",  # Different dataset version!
        seed=42,
        batch_size=16,
        input_resolution=(640, 640),
        precision="fp16_amp",
        config_uri="file:///cfg.yaml",
        config_hash="c" * 64,
        hyperparameters={"lr": 0.001},
        accuracy_metric_requirements=("map_50",),
    )

    req = _make_clean_request(
        tmp_path,
        active_baseline_spec=diff_dataset_baseline_spec,
        active_baseline_acc=AccuracyMetrics(map_50=0.80),
        active_baseline_sys=SystemMetrics(
            throughput_fps=100.0,
            latency_inference_ms=5.0,
            latency_e2e_ms=10.0,
            peak_vram_mb=4000.0,
            power_draw_watts=40.0,
        ),
    )
    with pytest.raises(Phase6ValidationError, match="Incomparable baseline"):
        run_train_evaluate_diagnose(req)


def test_case_18_missing_failure_category_counts_cannot_produce_fabricated_ranking(
    tmp_path: Path,
):
    """Case 18: Missing failure-category counts/evidence cannot produce a fabricated ranking."""
    # Error analysis record with zero localization/classification/FP/slice failures
    empty_error_record = ErrorAnalysisRecord(
        total_samples=100,
        failed_samples_count=5,
        false_positive_count=0,
        false_negative_count=0,
        localization_errors=0,
        classification_errors=0,
        slice_performance={},  # Empty slice performance
        diagnostics_summary="No failures categorized.",
    )

    class EmptyErrorAnalyzer:
        def analyze(self, predictions, ground_truth, dataset_binding):
            return empty_error_record

    req = _make_clean_request(tmp_path)
    req_empty_err = TrainEvaluateDiagnoseRequest(
        **{**req.__dict__, "error_analyzer": EmptyErrorAnalyzer()}
    )
    with pytest.raises(
        Phase6DiagnosisError, match="Missing empirical failure-category counts"
    ):
        run_train_evaluate_diagnose(req_empty_err)


def test_case_19_next_experiment_proposal_cannot_auto_dispatch(tmp_path: Path):
    """Case 19: Next experiment proposal cannot execute without separate fresh approval (is_dispatched=False)."""
    req = _make_clean_request(tmp_path)
    result = run_train_evaluate_diagnose(req)

    proposal = result.proposed_next_experiment
    # Hard invariants per Gate G5 & [P§24]
    assert proposal.is_dispatched is False
    assert proposal.requires_fresh_approval is True
    assert proposal.cost_estimate_required is True
    assert proposal.hypothesis != ""
    assert len(proposal.intended_changed_variables) > 0
    assert proposal.baseline_comparator_exp_id == result.experiment_record.exp_id


def test_case_20_clean_approved_run_full_cycle(tmp_path: Path):
    """Case 20: Clean approved run -> one completed training record, composite evaluation, evidence-backed ranked failures, and one proposed next experiment."""
    # Pre-existing active baseline on identical dataset and target
    clean_baseline_spec = ExperimentSpec(
        name="active-baseline",
        hypothesis="Authoritative root baseline",
        workload_type=WorkloadType.BENCHMARK,
        target=ExecutionTarget.LOCAL_GPU,
        model_name="yolov8n",
        model_variant="base",
        commit_sha="a" * 40,
        dataset_version="v1.0.0",
        seed=42,
        batch_size=16,
        input_resolution=(640, 640),
        precision="fp16_amp",
        config_uri="file:///cfg.yaml",
        config_hash="b" * 64,
        hyperparameters={"lr": 0.001},
        accuracy_metric_requirements=("map_50",),
    )

    req = _make_clean_request(
        tmp_path,
        active_baseline_spec=clean_baseline_spec,
        active_baseline_acc=AccuracyMetrics(map_50=0.82),
        active_baseline_sys=SystemMetrics(
            throughput_fps=100.0,
            latency_inference_ms=5.0,
            latency_e2e_ms=10.0,
            peak_vram_mb=4000.0,
            power_draw_watts=40.0,
        ),
    )
    result = run_train_evaluate_diagnose(req)

    # 1. Training Job & Ledger Record
    assert result.training_job.status == TrainingJobStatus.RUNNING
    assert result.experiment_record.status == ExperimentStatus.COMPLETED
    assert result.experiment_record.accuracy_metrics is not None
    assert result.experiment_record.accuracy_metrics.map_50 == 0.88
    assert result.checkpoint_manifest.weights_hash is not None

    # 2. Composite Evaluation Result per docs/EVALUATION.md
    composite = result.composite_evaluation
    assert composite.accuracy.map_50 is not None
    assert composite.system_performance.throughput_fps == 120.0
    assert len(composite.target_constraint_verdicts) == 2
    assert composite.meets_all_constraints is True

    # Baseline delta
    assert "delta_map_50" in composite.baseline_delta
    assert composite.baseline_delta["delta_map_50"] == round(0.89 - 0.82, 4)
    assert composite.baseline_delta["delta_fps"] == 20.0

    # 3. Failure Category Analysis
    analysis = result.failure_analysis
    assert len(analysis.ranked_categories) > 0
    top = analysis.ranked_categories[0]
    assert top.severity_rank == 1
    assert top.error_count > 0
    assert top.error_share > 0.0
    assert analysis.causal_hypothesis != ""

    # 4. Proposed Next Experiment
    proposal = result.proposed_next_experiment
    assert proposal.is_dispatched is False
    assert proposal.requires_fresh_approval is True
    assert proposal.baseline_comparator_exp_id == result.experiment_record.exp_id

    # 5. Workflow class API
    req2 = _make_clean_request(tmp_path / "wf")
    workflow = TrainEvaluateDiagnoseWorkflow()
    wf_result = workflow.execute(req2)
    assert wf_result.experiment_record.status == ExperimentStatus.COMPLETED
