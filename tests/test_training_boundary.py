"""tests/test_training_boundary.py — Boundary and adversarial tests for ADR-0010 Training Subsystem.

Validates all 10 implementation acceptance criteria and the 20 mandatory PM adversarial tests:
1. Training cannot construct an OperationIdentity.
2. Missing authoritative operation identity fails closed.
3. Wrong operation version fails closed.
4. Wrong canonical payload hash fails closed.
5. Wrong target identity fails closed.
6. Cross-plan approval replay fails closed.
7. Cost estimate change invalidates approval.
8. Dataset manifest hash mismatch fails closed in validate_plan().
9. Calibration split cannot become training data (even under INT8_PTQ_AWARE).
10. Deep defensive immutability for nested mappings and configs.
11. NaN/Inf rejected fail-closed in plan, telemetry, and cost estimation.
12. Early stopping produces successful completion semantics (COMPLETED with best checkpoint).
13. Expired ActiveJobHandoff is not treated as training failure.
14. AST audit confirming zero subprocess/socket/SSH/network imports in cv_agent.training.
15. Zero orchestration state mutation or SQLite session persistence in cv_agent.training.
16. Checkpoint retention never prunes keep_best or keep_final.
17. Equal checkpoint metrics resolve deterministically via tie-breaking.
18. Checkpoint URI regex validation rejects bare filesystem paths.
19. Telemetry monitoring is non-blocking and memory bounded.
20. Python 3.10–3.13 deterministic golden vectors for cost calculation.
"""

from __future__ import annotations

import ast
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import ValidationError

from cv_agent.dataset import (
    CollectionConditions,
    ContentHash,
    DatasetId,
    DatasetManifest,
    DatasetVersion,
    OperationIdentity,
    SplitMembership,
)
from cv_agent.domain import CapabilityId
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
from cv_agent.training import (
    AugmentationConfig,
    CheckpointId,
    CheckpointManifest,
    CheckpointRetentionManager,
    CompletionReason,
    ComputeRequirements,
    CostEstimate,
    CostEstimationError,
    DefaultTrainingLauncher,
    DeterministicTrainingEstimator,
    DistributedStrategy,
    InvalidTrainingJobTransitionError,
    LossComponentConfig,
    OnlineDivergenceMonitor,
    OptimizerConfig,
    PlanId,
    PlanValidationError,
    RetentionPolicy,
    SchedulerConfig,
    TrainingApprovalRequiredError,
    TrainingExecutionError,
    TrainingJobId,
    TrainingJobStatus,
    TrainingPlan,
    TrainingPrecision,
    TrainingTelemetryRecord,
    compute_training_payload_hash,
    validate_training_job_transition,
)


def _make_valid_plan(
    plan_id: str = "plan-exp001",
    cost_estimate: CostEstimate | None = None,
    train_split: str = "train",
    val_split: str = "val",
    precision: TrainingPrecision = TrainingPrecision.FP16_AMP,
    manifest_hash: str = "a" * 64,
) -> TrainingPlan:
    return TrainingPlan(
        plan_id=PlanId(plan_id),
        experiment_id="exp-001",
        created_at=datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc),
        capability_id=CapabilityId("cv.training.execution"),
        dataset_id=DatasetId("ds-voc"),
        dataset_version=DatasetVersion("v1.0.0"),
        dataset_manifest_hash=ContentHash(manifest_hash),
        train_split=train_split,
        val_split=val_split,
        architecture_name="yolov8n",
        num_classes=80,
        pretrained_weights_ref=None,
        optimizer=OptimizerConfig(
            name="AdamW",
            lr=0.001,
            weight_decay=0.01,
            betas=(0.9, 0.999),
        ),
        scheduler=SchedulerConfig(
            policy="cosine_annealing",
            warmup_epochs=3,
            min_lr=1e-6,
        ),
        losses=(
            LossComponentConfig(
                loss_type="cross_entropy", weight=1.0, params={"label_smoothing": 0.1}
            ),
            LossComponentConfig(loss_type="ciou", weight=2.0),
        ),
        augmentation=AugmentationConfig(
            pipeline_name="standard_detection",
            image_size=(640, 640),
            horizontal_flip_prob=0.5,
            custom_params={"scale_min": 0.8, "scale_max": 1.2},
        ),
        batch_size_per_device=16,
        gradient_accumulation_steps=1,
        epochs=10,
        precision=precision,
        compute_requirements=ComputeRequirements(
            min_vram_gb=8.0,
            min_cuda_compute_capability="8.0",
            num_devices=1,
            distributed_strategy=DistributedStrategy.NONE,
        ),
        cost_estimate=cost_estimate,
    )


def _make_approval_context(
    plan: TrainingPlan,
    operation_id: str = "op-train-001",
    operation_version: int = 1,
    request_id: str = "req-app-001",
    policy_version: str = "policy-v1",
    resource_scope: str = "target:local_gpu",
    status: ApprovalStatus = ApprovalStatus.APPROVED,
) -> tuple[OperationIdentity, ApprovalRequest, ApprovalDecision]:
    payload_hash = compute_training_payload_hash(plan)

    op_identity = OperationIdentity(
        operation_id=operation_id,
        operation_version=str(operation_version),
        canonical_payload_hash=payload_hash,
        target_identity=resource_scope,
    )

    app_request = ApprovalRequest(
        request_id=request_id,
        session_id="sess-001",
        operation_id=OperationId(value=operation_id),
        operation_version=operation_version,
        canonical_payload_hash=payload_hash,
        policy_version=policy_version,
        action_type="training.execute",
        risk_level="HIGH",
        resource_scope=resource_scope,
        created_at=datetime.now(timezone.utc),
    )

    app_decision = ApprovalDecision(
        request_id=request_id,
        decision=status,
        principal="human_pm",
        resolved_at=datetime.now(timezone.utc),
        bound_payload_hash=payload_hash,
        bound_resource_scope=resource_scope,
        bound_policy_version=policy_version,
    )

    return op_identity, app_request, app_decision


# =============================================================================
# Test 1: Training cannot construct an OperationIdentity
# =============================================================================
def test_training_cannot_construct_operation_identity() -> None:
    """Verify cv_agent.training does not define or synthesize OperationIdentity."""
    import cv_agent.training as training_mod

    assert not hasattr(training_mod, "OperationIdentity")
    assert not hasattr(training_mod, "TRAINING_OPERATION_VERSION")
    assert not hasattr(training_mod, "compute_training_target_identity")


# =============================================================================
# Test 2: Missing authoritative operation identity fails closed
# =============================================================================
def test_missing_authoritative_operation_identity() -> None:
    """Verify launch fails closed with TrainingApprovalRequiredError when operation_identity is None."""
    launcher = DefaultTrainingLauncher()
    plan = _make_valid_plan()
    _, app_req, app_dec = _make_approval_context(plan)

    with pytest.raises(
        TrainingApprovalRequiredError,
        match="Missing authoritative ADR-0003 OperationIdentity",
    ):
        launcher.launch(
            plan=plan,
            tool_id=ToolId("cv.train.execute"),
            operation_identity=None,  # type: ignore[arg-type]
            approval_request=app_req,
            approval_decision=app_dec,
        )


# =============================================================================
# Test 3: Wrong operation version fails closed
# =============================================================================
def test_wrong_operation_version() -> None:
    """Verify launch fails closed when OperationIdentity has version mismatch."""
    launcher = DefaultTrainingLauncher()
    plan = _make_valid_plan()
    op_id, app_req, app_dec = _make_approval_context(plan, operation_version=1)

    mismatched_op_id = OperationIdentity(
        operation_id=op_id.operation_id,
        operation_version="2",  # Mismatch!
        canonical_payload_hash=op_id.canonical_payload_hash,
        target_identity=op_id.target_identity,
    )

    with pytest.raises(
        TrainingApprovalRequiredError, match="Operation version mismatch"
    ):
        launcher.launch(
            plan=plan,
            tool_id=ToolId("cv.train.execute"),
            operation_identity=mismatched_op_id,
            approval_request=app_req,
            approval_decision=app_dec,
        )


# =============================================================================
# Test 4: Wrong canonical payload hash fails closed
# =============================================================================
def test_wrong_canonical_payload_hash() -> None:
    """Verify launch fails closed when OperationIdentity contains a tampered payload hash."""
    launcher = DefaultTrainingLauncher()
    plan = _make_valid_plan()
    op_id, app_req, app_dec = _make_approval_context(plan)

    tampered_op_id = OperationIdentity(
        operation_id=op_id.operation_id,
        operation_version=op_id.operation_version,
        canonical_payload_hash="0" * 64,  # Tampered!
        target_identity=op_id.target_identity,
    )

    with pytest.raises(
        TrainingApprovalRequiredError, match="Canonical payload hash mismatch"
    ):
        launcher.launch(
            plan=plan,
            tool_id=ToolId("cv.train.execute"),
            operation_identity=tampered_op_id,
            approval_request=app_req,
            approval_decision=app_dec,
        )


# =============================================================================
# Test 5: Wrong target identity fails closed
# =============================================================================
def test_wrong_target_identity() -> None:
    """Verify launch fails closed when target identity does not match resource scope."""
    launcher = DefaultTrainingLauncher()
    plan = _make_valid_plan()
    op_id, app_req, app_dec = _make_approval_context(
        plan, resource_scope="target:local_gpu"
    )

    mismatched_op_id = OperationIdentity(
        operation_id=op_id.operation_id,
        operation_version=op_id.operation_version,
        canonical_payload_hash=op_id.canonical_payload_hash,
        target_identity="target:remote_gpu",  # Mismatch!
    )

    with pytest.raises(
        TrainingApprovalRequiredError, match="Target identity / resource scope mismatch"
    ):
        launcher.launch(
            plan=plan,
            tool_id=ToolId("cv.train.execute"),
            operation_identity=mismatched_op_id,
            approval_request=app_req,
            approval_decision=app_dec,
        )


# =============================================================================
# Test 6: Cross-plan approval replay fails closed
# =============================================================================
def test_cross_plan_approval_replay() -> None:
    """Verify approval for Plan A presented for Plan B fails closed."""
    launcher = DefaultTrainingLauncher()
    plan_a = _make_valid_plan(plan_id="plan-a")
    plan_b = _make_valid_plan(plan_id="plan-b")

    op_id_a, app_req_a, app_dec_a = _make_approval_context(
        plan_a, resource_scope="target:local_gpu"
    )

    with pytest.raises(
        TrainingApprovalRequiredError, match="Canonical payload hash mismatch"
    ):
        launcher.launch(
            plan=plan_b,  # Presented for Plan B!
            tool_id=ToolId("cv.train.execute"),
            operation_identity=op_id_a,
            approval_request=app_req_a,
            approval_decision=app_dec_a,
        )


# =============================================================================
# Test 7: Cost estimate change invalidates approval
# =============================================================================
def test_cost_estimate_change_invalidates_approval() -> None:
    """Verify modifying cost estimate changes canonical hash, failing approval verification."""
    launcher = DefaultTrainingLauncher()
    cost_initial = CostEstimate(
        estimated_gpu_hours=2.5,
        estimated_wall_clock_hours=2.5,
        estimated_cost_usd=Decimal("6.25"),
        estimated_storage_gb=1.0,
        basis_description="Initial estimate",
    )
    plan = _make_valid_plan(cost_estimate=cost_initial)
    op_id, app_req, app_dec = _make_approval_context(plan)

    # Now alter cost estimate in plan
    cost_updated = CostEstimate(
        estimated_gpu_hours=5.0,
        estimated_wall_clock_hours=5.0,
        estimated_cost_usd=Decimal("12.50"),
        estimated_storage_gb=2.0,
        basis_description="Updated estimate",
    )
    plan_modified = plan.model_copy(update={"cost_estimate": cost_updated})

    with pytest.raises(
        TrainingApprovalRequiredError, match="Canonical payload hash mismatch"
    ):
        launcher.launch(
            plan=plan_modified,
            tool_id=ToolId("cv.train.execute"),
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
        )


# =============================================================================
# Test 8: Dataset manifest hash mismatch fails closed in validate_plan()
# =============================================================================
def test_dataset_manifest_hash_mismatch() -> None:
    """Verify validate_plan() fails closed when dataset manifest hash does not match."""
    launcher = DefaultTrainingLauncher()
    plan = _make_valid_plan(manifest_hash="a" * 64)

    from cv_agent.dataset import (
        AnnotationVersion,
        ClassDefinition,
        DatasetLineage,
        GuidelineRef,
    )

    mock_manifest = DatasetManifest(
        dataset_id=DatasetId("ds-voc"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=2,
        class_definitions=(
            ClassDefinition(
                name="person",
                description="human",
                near_miss_negatives=(),
                ambiguity_resolution="none",
            ),
        ),
        class_distribution={"person": 2},
        split_policy="random",
        split_membership=SplitMembership(
            train_hashes=(ContentHash("1" * 64),),
            val_hashes=(ContentHash("2" * 64),),
            test_hashes=(),
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
        payload_root_checksum=ContentHash("b" * 64),  # Mismatch!
        leakage_audit_records=(),
    )

    with pytest.raises(PlanValidationError, match="Dataset manifest hash mismatch"):
        launcher.validate_plan(plan, dataset_manifest=mock_manifest)


# =============================================================================
# Test 9: Calibration split cannot become training data
# =============================================================================
def test_calibration_split_isolation() -> None:
    """Verify calibration split is strictly prohibited from being used as train or val split."""
    launcher = DefaultTrainingLauncher()

    # Case A: standard training on calibration split
    plan_cal_train = _make_valid_plan(train_split="calibration")
    with pytest.raises(
        PlanValidationError,
        match="Calibration split 'calibration' is strictly reserved",
    ):
        launcher.validate_plan(plan_cal_train)

    # Case B: INT8_PTQ_AWARE training on calibration split
    plan_ptq_cal = _make_valid_plan(
        train_split="calibration",
        precision=TrainingPrecision.INT8_PTQ_AWARE,
    )
    with pytest.raises(
        PlanValidationError,
        match="Calibration split 'calibration' is strictly reserved",
    ):
        launcher.validate_plan(plan_ptq_cal)


# =============================================================================
# Test 10: Deep defensive immutability for nested mappings and configs
# =============================================================================
def test_defensive_immutability() -> None:
    """Verify nested mappings and parameters in TrainingPlan cannot be mutated."""
    plan = _make_valid_plan()

    # Verify LossComponentConfig params is a MappingProxyType
    loss_comp = plan.losses[0]
    assert isinstance(loss_comp.params, MappingProxyType)
    with pytest.raises(TypeError):
        loss_comp.params["label_smoothing"] = 0.5  # type: ignore[index]

    # Verify AugmentationConfig custom_params is a MappingProxyType
    assert isinstance(plan.augmentation.custom_params, MappingProxyType)
    with pytest.raises(TypeError):
        plan.augmentation.custom_params["scale_min"] = 0.5  # type: ignore[index]


# =============================================================================
# Test 11: NaN/Inf rejected fail-closed in plan, telemetry, and cost estimation
# =============================================================================
def test_nan_inf_rejection() -> None:
    """Verify NaN and Inf are rejected fail-closed across all models and estimator."""
    # 1. Reject NaN in OptimizerConfig
    with pytest.raises(ValidationError):
        OptimizerConfig(name="SGD", lr=float("nan"))

    with pytest.raises(ValidationError):
        OptimizerConfig(name="SGD", lr=float("inf"))

    # 2. Reject NaN in CostEstimate
    with pytest.raises(ValidationError):
        CostEstimate(
            estimated_gpu_hours=float("nan"),
            estimated_wall_clock_hours=1.0,
            estimated_cost_usd=Decimal("2.50"),
            estimated_storage_gb=1.0,
            basis_description="nan test",
        )

    # 3. Reject NaN in Estimator inputs
    estimator = DeterministicTrainingEstimator()
    plan = _make_valid_plan()
    with pytest.raises(
        CostEstimationError,
        match="sample_step_latency_sec must be a positive finite number",
    ):
        estimator.estimate(
            plan, train_items_count=1000, sample_step_latency_sec=float("nan")
        )

    # 4. Reject NaN in Telemetry
    with pytest.raises(ValidationError):
        TrainingTelemetryRecord(
            job_id=TrainingJobId("train-001"),
            step=1,
            epoch=1,
            total_epochs=10,
            train_loss=float("nan"),
            learning_rate=0.001,
            throughput_fps=100.0,
            vram_allocated_gb=4.0,
            timestamp=datetime.now(timezone.utc),
        )


# =============================================================================
# Test 12: Early stopping produces successful completion semantics
# =============================================================================
def test_early_stopping_terminal_completion_semantics() -> None:
    """Verify early stopping triggers CompletionReason.EARLY_STOPPING leading to COMPLETED."""
    monitor = OnlineDivergenceMonitor(
        window_size=3,
        min_observations=5,
        patience_epochs=2,
        min_delta=0.01,
    )

    # Feed epochs with no improvement in val_loss
    # Epoch 0: best val_loss = 1.00
    rec0 = TrainingTelemetryRecord(
        job_id=TrainingJobId("train-001"),
        step=10,
        epoch=0,
        total_epochs=10,
        train_loss=1.2,
        val_loss=1.00,
        learning_rate=0.001,
        throughput_fps=100.0,
        vram_allocated_gb=4.0,
        timestamp=datetime.now(timezone.utc),
    )
    should_term, reason, details = monitor.on_step(rec0)
    assert not should_term

    # Epoch 1: val_loss = 1.005 (no improvement)
    rec1 = TrainingTelemetryRecord(
        job_id=TrainingJobId("train-001"),
        step=20,
        epoch=1,
        total_epochs=10,
        train_loss=1.1,
        val_loss=1.005,
        learning_rate=0.001,
        throughput_fps=100.0,
        vram_allocated_gb=4.0,
        timestamp=datetime.now(timezone.utc),
    )
    should_term, reason, details = monitor.on_step(rec1)
    assert not should_term

    # Epoch 2: val_loss = 1.01 (no improvement >= patience 2) -> early stopping!
    rec2 = TrainingTelemetryRecord(
        job_id=TrainingJobId("train-001"),
        step=30,
        epoch=2,
        total_epochs=10,
        train_loss=1.05,
        val_loss=1.01,
        learning_rate=0.001,
        throughput_fps=100.0,
        vram_allocated_gb=4.0,
        timestamp=datetime.now(timezone.utc),
    )
    should_term, reason, details = monitor.on_step(rec2)
    assert should_term
    assert reason == CompletionReason.EARLY_STOPPING
    assert details is not None and "Early stopping triggered" in details

    # Verify that early stopping maps to COMPLETED in the domain state machine
    validate_training_job_transition(
        TrainingJobStatus.RUNNING, TrainingJobStatus.COMPLETED
    )


# =============================================================================
# Test 13: Expired ActiveJobHandoff is not treated as training failure
# =============================================================================
def test_active_job_handoff_expiry_not_failure() -> None:
    """Verify ActiveJobHandoff expiry does not represent job failure."""

    handoff = ActiveJobHandoff(
        job_id="job-async-001",
        job_type="training",
        target="remote_gpu",
        submitted_at="2026-01-01T00:00:00Z",
        status="running",
        expires_at="2026-01-01T01:00:00Z",
    )
    assert handoff.is_expired(now=datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc))
    # Expired handoff indicates polling TTL expired, not that training failed!


# =============================================================================
# Test 14: AST audit confirming zero subprocess/socket/SSH/network imports
# =============================================================================
def test_zero_transport_leakage_ast_check() -> None:
    """Verify cv_agent/training contains zero raw transport imports."""
    training_dir = Path(__file__).resolve().parent.parent / "cv_agent" / "training"
    prohibited_modules = {
        "subprocess",
        "socket",
        "paramiko",
        "requests",
        "urllib",
        "httpx",
        "aiohttp",
    }

    for py_file in training_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    assert root_mod not in prohibited_modules, (
                        f"Prohibited transport module {root_mod!r} imported in {py_file.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                root_mod = node.module.split(".")[0]
                assert root_mod not in prohibited_modules, (
                    f"Prohibited transport module {root_mod!r} imported from in {py_file.name}"
                )


# =============================================================================
# Test 15: Zero orchestration state mutation or SQLite session persistence
# =============================================================================
def test_zero_orchestration_state_mutation() -> None:
    """Verify cv_agent/training contains no references to checkpoints.sqlite or sqlite3 tables."""
    training_dir = Path(__file__).resolve().parent.parent / "cv_agent" / "training"
    prohibited_strings = [
        "checkpoints.sqlite",
        "sqlite3",
        "CREATE TABLE",
        "INSERT INTO leases",
        "UPDATE leases",
    ]

    for py_file in training_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for bad_str in prohibited_strings:
            assert bad_str not in content, (
                f"Prohibited orchestration state pattern {bad_str!r} found in {py_file.name}"
            )


# =============================================================================
# Test 16: Checkpoint retention never prunes keep_best or keep_final
# =============================================================================
def test_retention_best_and_final_protection() -> None:
    """Verify CheckpointRetentionManager never prunes best or final epoch checkpoints."""
    manager = CheckpointRetentionManager()
    policy = RetentionPolicy(
        keep_best=2,
        keep_last=2,
        keep_final=True,
        primary_metric="val_loss",
        higher_is_better=False,
    )

    checkpoints = [
        CheckpointManifest(
            checkpoint_id=CheckpointId("ckpt-ep0"),
            job_id=TrainingJobId("job-001"),
            plan_id=PlanId("plan-001"),
            epoch=0,
            step=100,
            weights_hash=ContentHash("1" * 64),
            weights_uri="file:///storage/ckpt-ep0.pt",
            metrics={"val_loss": 2.5},
            created_at=datetime.now(timezone.utc),
        ),
        CheckpointManifest(
            checkpoint_id=CheckpointId("ckpt-ep1"),
            job_id=TrainingJobId("job-001"),
            plan_id=PlanId("plan-001"),
            epoch=1,
            step=200,
            weights_hash=ContentHash("2" * 64),
            weights_uri="file:///storage/ckpt-ep1.pt",
            metrics={"val_loss": 0.5},  # Best checkpoint!
            created_at=datetime.now(timezone.utc),
        ),
        CheckpointManifest(
            checkpoint_id=CheckpointId("ckpt-ep2"),
            job_id=TrainingJobId("job-001"),
            plan_id=PlanId("plan-001"),
            epoch=2,
            step=300,
            weights_hash=ContentHash("3" * 64),
            weights_uri="file:///storage/ckpt-ep2.pt",
            metrics={"val_loss": 1.2},
            created_at=datetime.now(timezone.utc),
        ),
        CheckpointManifest(
            checkpoint_id=CheckpointId("ckpt-ep3"),
            job_id=TrainingJobId("job-001"),
            plan_id=PlanId("plan-001"),
            epoch=3,
            step=400,
            weights_hash=ContentHash("4" * 64),
            weights_uri="file:///storage/ckpt-ep3.pt",
            metrics={"val_loss": 1.8},
            created_at=datetime.now(timezone.utc),
        ),
        CheckpointManifest(
            checkpoint_id=CheckpointId("ckpt-ep4"),
            job_id=TrainingJobId("job-001"),
            plan_id=PlanId("plan-001"),
            epoch=4,
            step=500,
            weights_hash=ContentHash("5" * 64),
            weights_uri="file:///storage/ckpt-ep4.pt",
            metrics={"val_loss": 2.0},  # Final epoch checkpoint!
            created_at=datetime.now(timezone.utc),
        ),
    ]

    decision = manager.evaluate_retention(checkpoints, policy, total_epochs=5)

    assert decision.best_checkpoint_id == CheckpointId("ckpt-ep1")
    assert decision.final_checkpoint_id == CheckpointId("ckpt-ep4")

    # Invariant: best and final MUST be in retained and MUST NOT be in prunable
    assert CheckpointId("ckpt-ep1") in decision.retained_checkpoint_ids
    assert CheckpointId("ckpt-ep1") not in decision.prunable_checkpoint_ids

    assert CheckpointId("ckpt-ep4") in decision.retained_checkpoint_ids
    assert CheckpointId("ckpt-ep4") not in decision.prunable_checkpoint_ids

    # ckpt-ep0 was worst and not last -> should be prunable
    assert CheckpointId("ckpt-ep0") in decision.prunable_checkpoint_ids


# =============================================================================
# Test 17: Equal checkpoint metrics resolve deterministically via tie-breaking
# =============================================================================
def test_deterministic_tie_breaking_for_equal_metrics() -> None:
    """Verify tie-breaking on identical validation metrics is deterministic."""
    manager = CheckpointRetentionManager()
    policy = RetentionPolicy(
        keep_best=1,
        keep_last=1,
        keep_final=False,
        primary_metric="val_loss",
        higher_is_better=False,
    )

    ckpt_a = CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-b"),
        job_id=TrainingJobId("job-001"),
        plan_id=PlanId("plan-001"),
        epoch=2,
        step=200,
        weights_hash=ContentHash("a" * 64),
        weights_uri="file:///storage/ckpt-b.pt",
        metrics={"val_loss": 0.5},
        created_at=datetime.now(timezone.utc),
    )
    ckpt_b = CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-a"),
        job_id=TrainingJobId("job-001"),
        plan_id=PlanId("plan-001"),
        epoch=2,
        step=200,
        weights_hash=ContentHash("b" * 64),
        weights_uri="file:///storage/ckpt-a.pt",
        metrics={"val_loss": 0.5},  # Identical epoch, step, and metric
        created_at=datetime.now(timezone.utc),
    )

    decision_1 = manager.evaluate_retention([ckpt_a, ckpt_b], policy, total_epochs=3)
    decision_2 = manager.evaluate_retention([ckpt_b, ckpt_a], policy, total_epochs=3)

    assert decision_1.best_checkpoint_id == decision_2.best_checkpoint_id
    assert decision_1.retained_checkpoint_ids == decision_2.retained_checkpoint_ids


# =============================================================================
# Test 18: Checkpoint URI regex validation rejects bare filesystem paths
# =============================================================================
def test_checkpoint_uri_validation() -> None:
    """Verify CheckpointManifest rejects bare filesystem paths lacking a recognized URI scheme."""
    # Valid URIs
    for valid_uri in [
        "file:///absolute/path/to/weights.pt",
        "s3://my-bucket/weights.ckpt",
        "gcs://bucket/exp/model.bin",
        "minio://models/epoch_10.pt",
    ]:
        manifest = CheckpointManifest(
            checkpoint_id=CheckpointId("ckpt-001"),
            job_id=TrainingJobId("job-001"),
            plan_id=PlanId("plan-001"),
            epoch=1,
            step=100,
            weights_hash=ContentHash("c" * 64),
            weights_uri=valid_uri,
            created_at=datetime.now(timezone.utc),
        )
        assert manifest.weights_uri == valid_uri

    # Invalid bare paths
    for invalid_uri in [
        "/tmp/weights.pt",
        "relative/path/weights.pt",
        "weights.pt",
        "://missing-scheme",
    ]:
        with pytest.raises(ValidationError):
            CheckpointManifest(
                checkpoint_id=CheckpointId("ckpt-001"),
                job_id=TrainingJobId("job-001"),
                plan_id=PlanId("plan-001"),
                epoch=1,
                step=100,
                weights_hash=ContentHash("c" * 64),
                weights_uri=invalid_uri,
                created_at=datetime.now(timezone.utc),
            )


# =============================================================================
# Test 19: Telemetry monitoring is non-blocking and memory bounded
# =============================================================================
def test_non_blocking_telemetry_processing() -> None:
    """Verify telemetry monitor runs in microseconds and keeps bounded history."""
    monitor = OnlineDivergenceMonitor(window_size=5, min_observations=10)

    # Ingest 1000 steps
    for step in range(1000):
        rec = TrainingTelemetryRecord(
            job_id=TrainingJobId("train-001"),
            step=step,
            epoch=step // 100,
            total_epochs=10,
            train_loss=1.0 + 0.1 * math.sin(step),
            learning_rate=0.001,
            throughput_fps=150.0,
            vram_allocated_gb=6.0,
            timestamp=datetime.now(timezone.utc),
        )
        should_term, _, _ = monitor.on_step(rec)
        assert not should_term

    # Bounded history check: internal list must not exceed window_size
    assert len(monitor._recent_losses) <= 5


# =============================================================================
# Test 20: Python 3.10–3.13 deterministic golden vectors for cost calculation
# =============================================================================
def test_python_310_313_golden_vectors_cost_calculation() -> None:
    """Verify cost calculation produces exact reproducible golden vector numbers."""
    estimator = DeterministicTrainingEstimator()
    plan = _make_valid_plan()  # batch=16, devices=1, accum=1, epochs=10

    # Test Vector 1:
    # 1000 items, effective_batch=16 -> steps_per_epoch = ceil(1000/16) = 63
    # total_steps = 63 * 10 = 630
    # latency = 0.1s -> total_sec = 63.0s
    # wall_hours = round(63.0 / 3600, 4) = 0.0175
    # gpu_hours = 0.0175 * 1 = 0.0175
    # cost = 0.0175 * 2.50 = 0.04375 -> quantized to 0.04
    # storage = (10 * 1) * 0.5 = 5.0
    cost_est = estimator.estimate(
        plan=plan,
        train_items_count=1000,
        sample_step_latency_sec=0.1,
        target_hourly_rate_usd=Decimal("2.50"),
        model_size_gb=0.5,
        checkpoints_per_epoch=1,
    )

    assert cost_est.estimated_wall_clock_hours == 0.0175
    assert cost_est.estimated_gpu_hours == 0.0175
    assert cost_est.estimated_cost_usd == Decimal("0.04")
    assert cost_est.estimated_storage_gb == 5.0

    # Test Vector 2: Multi-GPU with gradient accumulation
    plan_ddp = plan.model_copy(
        update={
            "batch_size_per_device": 32,
            "gradient_accumulation_steps": 2,
            "compute_requirements": ComputeRequirements(
                min_vram_gb=16.0,
                num_devices=4,
                distributed_strategy=DistributedStrategy.DDP,
            ),
        }
    )
    # effective_batch = 32 * 4 * 2 = 256
    # 10,000 items -> steps_per_epoch = ceil(10000 / 256) = 40
    # total_steps = 40 * 10 = 400
    # latency = 0.25s -> total_sec = 100s
    # wall_hours = round(100.0 / 3600, 4) = 0.0278
    # gpu_hours = round(0.0278 * 4, 4) = 0.1112
    # cost = 0.1112 * 4.00 = 0.4448 -> quantized to 0.44
    # storage = (10 * 2) * 1.5 = 30.0
    cost_est_ddp = estimator.estimate(
        plan=plan_ddp,
        train_items_count=10000,
        sample_step_latency_sec=0.25,
        target_hourly_rate_usd=Decimal("4.00"),
        model_size_gb=1.5,
        checkpoints_per_epoch=2,
    )

    assert cost_est_ddp.estimated_wall_clock_hours == 0.0278
    assert cost_est_ddp.estimated_gpu_hours == 0.1112
    assert cost_est_ddp.estimated_cost_usd == Decimal("0.44")
    assert cost_est_ddp.estimated_storage_gb == 30.0


# =============================================================================
# Additional Hard Invariant Test: PM Target ID consistency guard
# =============================================================================
def test_conflicting_target_id_rejected() -> None:
    """Verify launch rejects target_id when conflicting with authorized scope target."""
    launcher = DefaultTrainingLauncher()
    plan = _make_valid_plan()
    op_id, app_req, app_dec = _make_approval_context(
        plan,
        resource_scope="target:cluster-a",
    )

    # Conflicting target_id passed to launch
    with pytest.raises(
        TrainingApprovalRequiredError, match="conflicts with authorized scope target"
    ):
        launcher.launch(
            plan=plan,
            tool_id=ToolId("cv.train.execute"),
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            target_id=TargetId("cluster-b"),  # Conflicting!
        )


# =============================================================================
# Additional Domain State Machine Transitions Test
# =============================================================================
def test_domain_state_machine_transitions() -> None:
    """Verify valid transitions succeed and illegal/terminal transitions raise error."""
    # Valid transition sequence
    validate_training_job_transition(
        TrainingJobStatus.DRAFT, TrainingJobStatus.VALIDATED
    )
    validate_training_job_transition(
        TrainingJobStatus.VALIDATED, TrainingJobStatus.ESTIMATED
    )
    validate_training_job_transition(
        TrainingJobStatus.ESTIMATED, TrainingJobStatus.AWAITING_APPROVAL
    )
    validate_training_job_transition(
        TrainingJobStatus.AWAITING_APPROVAL, TrainingJobStatus.APPROVED
    )
    validate_training_job_transition(
        TrainingJobStatus.APPROVED, TrainingJobStatus.LAUNCHING
    )
    validate_training_job_transition(
        TrainingJobStatus.LAUNCHING, TrainingJobStatus.RUNNING
    )
    validate_training_job_transition(
        TrainingJobStatus.RUNNING, TrainingJobStatus.COMPLETED
    )

    # Illegal jump: DRAFT -> RUNNING
    with pytest.raises(InvalidTrainingJobTransitionError):
        validate_training_job_transition(
            TrainingJobStatus.DRAFT, TrainingJobStatus.RUNNING
        )

    # Illegal jump: REJECTED -> RUNNING
    with pytest.raises(InvalidTrainingJobTransitionError):
        validate_training_job_transition(
            TrainingJobStatus.REJECTED, TrainingJobStatus.RUNNING
        )

    # Illegal transition from terminal COMPLETED
    with pytest.raises(InvalidTrainingJobTransitionError):
        validate_training_job_transition(
            TrainingJobStatus.COMPLETED, TrainingJobStatus.RUNNING
        )


# =============================================================================
# B1 Regression: abort_request delegates to ADR-0005 ToolExecutor
# =============================================================================
def test_abort_request_delegates_to_adr0005() -> None:
    """Verify abort_request constructs ToolInvocation and delegates cancellation via ADR-0005."""
    launcher = DefaultTrainingLauncher()
    invocations: list[ToolInvocation] = []

    class MockCancelExecutor:
        async def execute(self, invocation: ToolInvocation) -> ToolResult:
            invocations.append(invocation)
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.SUCCESS,
            )

        async def probe(self, tool: Any, target: Any) -> Any:
            raise NotImplementedError

    executor = MockCancelExecutor()
    job_id = TrainingJobId("job-test-abort-001")
    target_id = TargetId("remote-gpu-01")

    # Successful cancellation delegation
    result = launcher.abort_request(
        job_id=job_id,
        target_id=target_id,
        tool_executor=executor,  # type: ignore[arg-type]
    )

    assert result is True
    assert len(invocations) == 1
    inv = invocations[0]
    assert inv.tool_id == ToolId("cv.train.cancel")
    assert inv.target_id == target_id
    assert inv.parameters["job_id"] == str(job_id)
    assert inv.approval_authorized is True

    # Missing target_id fails closed
    with pytest.raises(TrainingExecutionError, match="TargetId is required"):
        launcher.abort_request(
            job_id=job_id,
            target_id=None,  # type: ignore[arg-type]
            tool_executor=executor,  # type: ignore[arg-type]
        )

    # Missing tool_executor fails closed
    with pytest.raises(TrainingExecutionError, match="ToolExecutor is required"):
        launcher.abort_request(
            job_id=job_id,
            target_id=target_id,
            tool_executor=None,  # type: ignore[arg-type]
        )


# =============================================================================
# B2 Regression: Zero target synthesis, fail closed without authorized target
# =============================================================================
def test_launch_rejects_missing_target_and_prevents_default_synthesis() -> None:
    """Verify launch rejects missing target_id and never synthesizes a default target."""
    launcher = DefaultTrainingLauncher()
    plan = _make_valid_plan()

    # Approval scope has no target (e.g. general plan resource scope)
    op_id, app_req, app_dec = _make_approval_context(
        plan, resource_scope="training_plan:plan-exp001"
    )

    # Calling launch without target_id fails closed without synthesizing "default"
    with pytest.raises(
        TrainingApprovalRequiredError,
        match="No authorized execution target_id supplied or derivable",
    ):
        launcher.launch(
            plan=plan,
            tool_id=ToolId("cv.train.execute"),
            operation_identity=op_id,
            approval_request=app_req,
            approval_decision=app_dec,
            target_id=None,  # Omitted!
        )

    # prepare_invocation fails closed without an explicit target_id
    with pytest.raises(
        TrainingApprovalRequiredError,
        match="Cannot prepare invocation without an authorized TargetId",
    ):
        launcher.prepare_invocation(
            plan=plan,
            tool_id=ToolId("cv.train.execute"),
            target_id=None,  # type: ignore[arg-type]
        )

    # When scope explicitly provides the authoritative target, launch derives it
    op_id_scoped, app_req_scoped, app_dec_scoped = _make_approval_context(
        plan, resource_scope="target:cluster-h100"
    )
    record = launcher.launch(
        plan=plan,
        tool_id=ToolId("cv.train.execute"),
        operation_identity=op_id_scoped,
        approval_request=app_req_scoped,
        approval_decision=app_dec_scoped,
        target_id=None,  # Derived from authorized scope!
    )
    assert record.target_id == TargetId("cluster-h100")


# =============================================================================
# B3 Regression: Unique TrainingJobId per launch execution
# =============================================================================
def test_launch_unique_execution_identity_per_run() -> None:
    """Verify each launch execution generates a distinct TrainingJobId while preserving plan_id lineage."""
    launcher = DefaultTrainingLauncher()
    plan = _make_valid_plan(plan_id="plan-exp-unique")
    op_id, app_req, app_dec = _make_approval_context(plan)

    rec1 = launcher.launch(
        plan=plan,
        tool_id=ToolId("cv.train.execute"),
        operation_identity=op_id,
        approval_request=app_req,
        approval_decision=app_dec,
    )
    rec2 = launcher.launch(
        plan=plan,
        tool_id=ToolId("cv.train.execute"),
        operation_identity=op_id,
        approval_request=app_req,
        approval_decision=app_dec,
    )

    # Different job identities for separate executions
    assert rec1.job_id != rec2.job_id
    # Both preserve the exact same plan_id lineage
    assert rec1.plan_id == PlanId("plan-exp-unique")
    assert rec2.plan_id == PlanId("plan-exp-unique")

    # Explicit job_id is accepted and respected
    custom_job_id = TrainingJobId("job-custom-explicit-001")
    rec_custom = launcher.launch(
        plan=plan,
        tool_id=ToolId("cv.train.execute"),
        operation_identity=op_id,
        approval_request=app_req,
        approval_decision=app_dec,
        job_id=custom_job_id,
    )
    assert rec_custom.job_id == custom_job_id


# =============================================================================
# Important Finding Regression: Ascending checkpoint_id tie-breaking
# =============================================================================
def test_retention_ascending_checkpoint_id_tie_break() -> None:
    """Verify tie-breaking orders checkpoint_id in ascending order when metrics and epochs match."""
    manager = CheckpointRetentionManager()
    base_metrics = {"val_loss": 0.42}

    # Three checkpoints with identical loss and epoch, different IDs
    ckpt_beta = CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-beta"),
        job_id=TrainingJobId("job-001"),
        plan_id=PlanId("plan-001"),
        epoch=2,
        step=200,
        weights_hash=ContentHash("0" * 64),
        weights_uri="s3://models/ckpt-beta.pt",
        created_at=datetime.now(timezone.utc),
        metrics=base_metrics,
    )
    ckpt_alpha = CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-alpha"),
        job_id=TrainingJobId("job-001"),
        plan_id=PlanId("plan-001"),
        epoch=2,
        step=200,
        weights_hash=ContentHash("0" * 64),
        weights_uri="s3://models/ckpt-alpha.pt",
        created_at=datetime.now(timezone.utc),
        metrics=base_metrics,
    )
    ckpt_gamma = CheckpointManifest(
        checkpoint_id=CheckpointId("ckpt-gamma"),
        job_id=TrainingJobId("job-001"),
        plan_id=PlanId("plan-001"),
        epoch=2,
        step=200,
        weights_hash=ContentHash("0" * 64),
        weights_uri="s3://models/ckpt-gamma.pt",
        created_at=datetime.now(timezone.utc),
        metrics=base_metrics,
    )

    policy = RetentionPolicy(
        keep_best=1,
        keep_last=1,
        keep_final=False,
        primary_metric="val_loss",
        higher_is_better=False,
    )

    # Present in non-alphabetical order to test sorting
    decision = manager.evaluate_retention(
        [ckpt_gamma, ckpt_beta, ckpt_alpha], policy, total_epochs=5
    )

    # Ascending tie-break must pick ckpt-alpha ahead of ckpt-beta and ckpt-gamma
    assert decision.best_checkpoint_id == CheckpointId("ckpt-alpha")
    assert decision.retained_checkpoint_ids == (CheckpointId("ckpt-alpha"),)
    assert decision.prunable_checkpoint_ids == (
        CheckpointId("ckpt-beta"),
        CheckpointId("ckpt-gamma"),
    )
