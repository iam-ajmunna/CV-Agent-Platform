"""tests/test_data_baseline_phase5_exit.py — Phase 5 Data & Baseline Exit Test Suite.

Authoritative Acceptance Suite for Issue #72 & ROADMAP.md Phase 5:
- Gate G1: Dataset versioning & integrity (ADR-0009).
- Gate G2: Leakage fail-closed gate (temporal & camera/site cross-split leakage).
- Gate G3: Clean dataset baseline evaluation on named target (ADR-0011 & ADR-0005).
- Gate G4: Ledger integrity & atomic root-baseline designation (ADR-0004).
- Gate G5: Boundary & adversarial regression (14 required adversarial test cases).
"""

from __future__ import annotations

import ast
import hashlib
from datetime import datetime, timezone
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
    DatasetVersionConflictError,
    FileSystemDatasetRegistry,
    GuidelineRef,
    ItemMetadata,
    LeakageCheckResult,
    LeakageType,
    OperationIdentity,
    PayloadIntegrityError,
    SplitLeakageError,
    SplitMembership,
    compute_payload_root_checksum,
)
from cv_agent.domain import CapabilityId
from cv_agent.evaluation import compute_evaluation_payload_hash
from cv_agent.evaluation.contracts import (
    BenchmarkConfiguration,
    EvaluationDatasetBinding,
    EvaluationPlan,
    EvaluationTargetRequirements,
    MetricDefinition,
    ModelArtifactReference,
)
from cv_agent.evaluation.errors import (
    EvaluationApprovalRequiredError,
    EvaluationError,
    EvaluationValidationError,
)
from cv_agent.evaluation.types import (
    BenchmarkId,
    EvaluationId,
    ExperimentId,
    HardwareClass,
    MetricId,
    TaskType,
)
from cv_agent.experiments import (
    ExperimentStatus,
    SQLiteExperimentLedger,
    WorkloadType,
)
from cv_agent.orchestration import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    OperationId,
)
from cv_agent.orchestration.data_baseline import (
    BaselineEstablishmentRequest,
    BaselineValidationError,
    DatasetBaselineWorkflow,
    build_baseline_evaluation_plan,
    establish_dataset_baseline,
)
from cv_agent.tools import (
    ExecutionId,
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

# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------


def _dummy_hash(seed: str) -> ContentHash:
    """Generate a deterministic 64-character lowercase hex SHA-256 hash."""
    return ContentHash(hashlib.sha256(seed.encode("utf-8")).hexdigest())


class MockToolExecutor:
    """ADR-0005 ToolExecutor test spy recording invocations and returning controlled results."""

    def __init__(
        self,
        canned_result: ToolResult | None = None,
        should_raise: Exception | None = None,
    ) -> None:
        self.invocations: list[ToolInvocation] = []
        self.canned_result = canned_result
        self.should_raise = should_raise

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
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

    async def probe(self, tool: Any, target: Any) -> Any:
        raise NotImplementedError


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
    dataset_id: str = "ds-perimeter",
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
        sources=("gate_cam_archive",),
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


def _make_temporal_leaky_manifest_and_metadata(
    dataset_id: str = "ds-temporal-leak",
) -> tuple[DatasetManifest, dict[str, ItemMetadata]]:
    """Manifest where train and test share a video clip (temporal leakage)."""
    manifest, meta = _make_clean_manifest_and_metadata(dataset_id=dataset_id)
    # Give test item the same clip_id as train item
    h_test_1 = manifest.split_membership.test_hashes[0]
    meta[str(h_test_1)] = ItemMetadata(
        content_hash=h_test_1,
        clip_id="clip-train-1",  # Straddles train split!
        camera_id="cam-03",
        site_id="site-C",
        capture_timestamp=datetime(2026, 1, 3, 10, 0, 0, tzinfo=timezone.utc),
    )
    return manifest, meta


def _make_camera_leaky_manifest_and_metadata(
    dataset_id: str = "ds-camera-leak",
) -> tuple[DatasetManifest, dict[str, ItemMetadata]]:
    """Manifest where train and test share the same camera/site (camera leakage)."""
    manifest, meta = _make_clean_manifest_and_metadata(dataset_id=dataset_id)
    # Give test items camera_id "cam-01" from train split
    h_test_1 = manifest.split_membership.test_hashes[0]
    meta[str(h_test_1)] = ItemMetadata(
        content_hash=h_test_1,
        clip_id="clip-test-1",
        camera_id="cam-01",  # Straddles camera disjointness!
        site_id="site-A",  # Straddles site disjointness!
        capture_timestamp=datetime(2026, 1, 3, 10, 0, 0, tzinfo=timezone.utc),
    )
    return manifest, meta


def _make_both_leaky_manifest_and_metadata(
    dataset_id: str = "ds-both-leaks",
) -> tuple[DatasetManifest, dict[str, ItemMetadata]]:
    """Manifest containing both temporal clip straddling and camera/site leakage."""
    manifest, meta = _make_clean_manifest_and_metadata(dataset_id=dataset_id)
    h_test_1 = manifest.split_membership.test_hashes[0]
    meta[str(h_test_1)] = ItemMetadata(
        content_hash=h_test_1,
        clip_id="clip-train-1",  # Temporal leakage
        camera_id="cam-01",  # Camera leakage
        site_id="site-A",  # Site leakage
        capture_timestamp=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
    )
    return manifest, meta


def _make_checkpoint_and_model_artifact(
    checkpoint_id: str = "ckpt-yolov8n-001",
    weights_hash: str = "1" * 64,
    weights_uri: str = "file:///weights/model.pt",
) -> tuple[CheckpointManifest, ModelArtifactReference]:
    """Construct an authoritative CheckpointManifest and corresponding ModelArtifactReference."""
    ckpt = CheckpointManifest(
        checkpoint_id=CheckpointId(checkpoint_id),
        job_id=TrainingJobId("job-baseline-001"),
        plan_id=PlanId("plan-baseline-001"),
        epoch=10,
        step=1000,
        weights_hash=ContentHash(weights_hash),
        weights_uri=weights_uri,
        metrics={"map_50": 0.88},
        created_at=datetime.now(timezone.utc),
    )
    art = ModelArtifactReference(
        checkpoint_id=CheckpointId(checkpoint_id),
        architecture_name="yolov8n",
        weights_hash=ContentHash(weights_hash),
        weights_uri=weights_uri,
        precision=TrainingPrecision.FP16_AMP,
        input_resolution=(640, 640),
    )
    return ckpt, art


def _make_approval_credentials(
    plan: EvaluationPlan,
    target_id: TargetId,
    operation_id: str = "op-eval-baseline-001",
) -> tuple[OperationIdentity, ApprovalRequest, ApprovalDecision]:
    """Construct strictly verified ADR-0003 operation identity and approval credentials."""
    payload_hash = compute_evaluation_payload_hash(plan)
    scope = f"target:{target_id}"

    op_id = OperationIdentity(
        operation_id=operation_id,
        operation_version="1",
        canonical_payload_hash=payload_hash,
        target_identity=scope,
    )

    app_req = ApprovalRequest(
        request_id="req-eval-001",
        session_id="sess-eval-001",
        operation_id=OperationId(value=operation_id),
        operation_version=1,
        canonical_payload_hash=payload_hash,
        policy_version="1.0",
        action_type="evaluation.execute",
        risk_level="HIGH",
        resource_scope=scope,
        created_at=datetime.now(timezone.utc),
    )

    app_dec = ApprovalDecision(
        request_id="req-eval-001",
        decision=ApprovalStatus.APPROVED,
        principal="human_pm",
        resolved_at=datetime.now(timezone.utc),
        bound_payload_hash=payload_hash,
        bound_resource_scope=scope,
        bound_policy_version="1.0",
    )

    return op_id, app_req, app_dec


def _make_canned_evaluation_output(
    target_id: TargetId,
    timing_synchronized: bool = True,
    timing_source: str = "cuda_event_timer",
    peak_vram_gb: float | None = 2.4,
    power_watts_avg: float | None = 14.5,
    latency_p50_ms: float = 4.8,
    throughput_fps: float = 200.0,
    map_50: float = 0.89,
) -> dict[str, Any]:
    """Construct structured output dictionary simulating an empirical on-target evaluation run."""
    bench_data = {
        "benchmark_id": "bench-baseline-001",
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
        "device_name": "NVIDIA Jetson Orin Nano",
        "target_id": target_id,
        "timing_synchronized": timing_synchronized,
        "timing_source": timing_source,
    }

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
            ],
        },
        "system_benchmarks": [bench_data],
    }


# ===========================================================================
# 14 Authoritative Adversarial Cases & Acceptance Gates
# ===========================================================================


class TestPhase5DataBaselineAcceptance:
    """Complete exit and boundary verification suite for Phase 5."""

    # -----------------------------------------------------------------------
    # Case 1: temporal leakage only -> reject
    # -----------------------------------------------------------------------
    def test_case_01_temporal_leakage_only_rejects(self, tmp_path: Path) -> None:
        """Adversarial Case 1: Temporal clip straddling must be rejected before evaluation dispatch."""
        manifest, meta = _make_temporal_leaky_manifest_and_metadata()
        ckpt, art = _make_checkpoint_and_model_artifact()
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-001",
            experiment_id="exp-baseline-001",
            dataset_manifest=manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
        )
        op_id, req, dec = _make_approval_credentials(plan, target_id)
        mock_executor = MockToolExecutor()

        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
            dataset_registry=FileSystemDatasetRegistry(tmp_path / "registry"),
            experiment_ledger=SQLiteExperimentLedger(tmp_path),
        )

        with pytest.raises(SplitLeakageError) as exc_info:
            establish_dataset_baseline(request)

        assert "temporal" in str(exc_info.value).lower()
        # Mechanical proof: zero evaluation dispatch occurred
        assert len(mock_executor.invocations) == 0

    # -----------------------------------------------------------------------
    # Case 2: camera/site leakage only -> reject
    # -----------------------------------------------------------------------
    def test_case_02_camera_site_leakage_only_rejects(self, tmp_path: Path) -> None:
        """Adversarial Case 2: Camera/site cross-split overlap must be rejected before evaluation dispatch."""
        manifest, meta = _make_camera_leaky_manifest_and_metadata()
        ckpt, art = _make_checkpoint_and_model_artifact()
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-002",
            experiment_id="exp-baseline-002",
            dataset_manifest=manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
        )
        op_id, req, dec = _make_approval_credentials(plan, target_id)
        mock_executor = MockToolExecutor()

        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
            dataset_registry=FileSystemDatasetRegistry(tmp_path / "registry"),
            experiment_ledger=SQLiteExperimentLedger(tmp_path),
        )

        with pytest.raises(SplitLeakageError) as exc_info:
            establish_dataset_baseline(request)

        assert "camera" in str(exc_info.value).lower()
        # Mechanical proof: zero evaluation dispatch occurred
        assert len(mock_executor.invocations) == 0

    # -----------------------------------------------------------------------
    # Case 3: both leakages -> reject and zero evaluation dispatch
    # -----------------------------------------------------------------------
    def test_case_03_both_leakages_rejects_and_zero_evaluation_dispatch(
        self, tmp_path: Path
    ) -> None:
        """Adversarial Case 3: Both temporal and camera leakages must be identified and fail closed."""
        manifest, meta = _make_both_leaky_manifest_and_metadata()
        ckpt, art = _make_checkpoint_and_model_artifact()
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-003",
            experiment_id="exp-baseline-003",
            dataset_manifest=manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
        )
        op_id, req, dec = _make_approval_credentials(plan, target_id)
        mock_executor = MockToolExecutor()

        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
            dataset_registry=FileSystemDatasetRegistry(tmp_path / "registry"),
            experiment_ledger=SQLiteExperimentLedger(tmp_path),
        )

        with pytest.raises(SplitLeakageError) as exc_info:
            establish_dataset_baseline(request)

        err_msg = str(exc_info.value).lower()
        assert "temporal" in err_msg
        assert "camera_site" in err_msg
        assert len(mock_executor.invocations) == 0

    # -----------------------------------------------------------------------
    # Case 4: payload-root mismatch -> reject
    # -----------------------------------------------------------------------
    def test_case_04_payload_root_mismatch_rejects(self, tmp_path: Path) -> None:
        """Adversarial Case 4: Manifest with tampered payload root checksum must fail integrity verification."""
        manifest, meta = _make_clean_manifest_and_metadata()
        # Tamper payload root checksum
        tampered_manifest = manifest.model_copy(
            update={"payload_root_checksum": "f" * 64}
        )
        ckpt, art = _make_checkpoint_and_model_artifact()
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-004",
            experiment_id="exp-baseline-004",
            dataset_manifest=tampered_manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
        )
        op_id, req, dec = _make_approval_credentials(plan, target_id)
        mock_executor = MockToolExecutor()

        request = BaselineEstablishmentRequest(
            dataset_manifest=tampered_manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
        )

        with pytest.raises(PayloadIntegrityError):
            establish_dataset_baseline(request)

        assert len(mock_executor.invocations) == 0

    # -----------------------------------------------------------------------
    # Case 5: wrong/unregistered dataset version -> reject
    # -----------------------------------------------------------------------
    def test_case_05_wrong_dataset_version_rejects(self, tmp_path: Path) -> None:
        """Adversarial Case 5: Evaluation plan bound to mismatched dataset version must be rejected."""
        manifest, meta = _make_clean_manifest_and_metadata(version="v1.0.0")
        ckpt, art = _make_checkpoint_and_model_artifact()
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        # Create plan specifying a wrong dataset version
        mismatched_binding = EvaluationDatasetBinding(
            dataset_id=manifest.dataset_id,
            dataset_version=DatasetVersion("v9.9.9"),
            dataset_manifest_hash=ContentHash(manifest.payload_root_checksum),
            split_name="test",
        )
        plan = EvaluationPlan(
            evaluation_id=EvaluationId("eval-005"),
            experiment_id=ExperimentId("exp-baseline-005"),
            created_at=datetime.now(timezone.utc),
            capability_id=CapabilityId("cv.detection.eval"),
            dataset_binding=mismatched_binding,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
            baseline_experiment_id=None,
        )

        op_id, req, dec = _make_approval_credentials(plan, target_id)
        mock_executor = MockToolExecutor()

        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
        )

        with pytest.raises(EvaluationValidationError) as exc_info:
            establish_dataset_baseline(request)

        assert "dataset version mismatch" in str(exc_info.value).lower()
        assert len(mock_executor.invocations) == 0

    # -----------------------------------------------------------------------
    # Case 6: calibration used as ordinary baseline evaluation split -> reject
    # -----------------------------------------------------------------------
    def test_case_06_calibration_used_as_evaluation_split_rejects(
        self, tmp_path: Path
    ) -> None:
        """Adversarial Case 6: Invariant 13 - Calibration split cannot be evaluated as an evaluation split."""
        manifest, _ = _make_clean_manifest_and_metadata()
        _, art = _make_checkpoint_and_model_artifact()

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )

        # Attempting to build a baseline plan pointing to 'calibration' must be rejected
        with pytest.raises(EvaluationValidationError) as exc_info:
            build_baseline_evaluation_plan(
                evaluation_id="eval-006",
                experiment_id="exp-baseline-006",
                dataset_manifest=manifest,
                model_artifact=art,
                task_type=TaskType.DETECTION,
                metrics=(metric,),
                split_name="calibration",
            )

        assert "calibration" in str(exc_info.value).lower()

    # -----------------------------------------------------------------------
    # Case 7: target mismatch or unauthorized target -> reject
    # -----------------------------------------------------------------------
    def test_case_07_target_mismatch_or_unauthorized_target_rejects(
        self, tmp_path: Path
    ) -> None:
        """Adversarial Case 7: Target identity conflict between approval scope and execution target must fail."""
        manifest, meta = _make_clean_manifest_and_metadata()
        ckpt, art = _make_checkpoint_and_model_artifact()
        authorized_target = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-007",
            experiment_id="exp-baseline-007",
            dataset_manifest=manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
        )
        op_id, req, dec = _make_approval_credentials(plan, authorized_target)
        mock_executor = MockToolExecutor()

        # Supply unauthorized target id
        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=TargetId("unauthorized-developer-host"),
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
        )

        with pytest.raises(EvaluationApprovalRequiredError) as exc_info:
            establish_dataset_baseline(request)

        assert "conflicts with authorized scope" in str(exc_info.value).lower()
        assert len(mock_executor.invocations) == 0

    # -----------------------------------------------------------------------
    # Case 8: unsynchronized accelerator timing -> reject
    # -----------------------------------------------------------------------
    def test_case_08_unsynchronized_accelerator_timing_rejects(
        self, tmp_path: Path
    ) -> None:
        """Adversarial Case 8: Accelerator benchmark with unsynchronized timing must be rejected."""
        manifest, meta = _make_clean_manifest_and_metadata()
        ckpt, art = _make_checkpoint_and_model_artifact()
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        bench_cfg = BenchmarkConfiguration(
            benchmark_id=BenchmarkId("bench-baseline-001"),
            target_requirements=EvaluationTargetRequirements(
                required_hardware_class=HardwareClass.JETSON
            ),
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-008",
            experiment_id="exp-baseline-008",
            dataset_manifest=manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
            benchmark_config=bench_cfg,
        )
        op_id, req, dec = _make_approval_credentials(plan, target_id)

        # Output with timing_synchronized=False
        canned_output = _make_canned_evaluation_output(
            target_id=target_id,
            timing_synchronized=False,
            timing_source="wall_clock",
        )
        mock_executor = MockToolExecutor(
            canned_result=ToolResult(
                execution_id=ExecutionId("exec-008"),
                tool_id=ToolId("eval-tool"),
                target_id=target_id,
                status=ToolExecutionStatus.SUCCESS,
                structured_output=canned_output,
            )
        )

        ledger = SQLiteExperimentLedger(tmp_path)
        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
            experiment_ledger=ledger,
        )

        with pytest.raises(Exception) as exc_info:
            establish_dataset_baseline(request)

        err_text = str(exc_info.value).lower()
        assert "timing" in err_text or "synchronized" in err_text
        # Assert no active baseline was set in ledger
        assert ledger.get_baseline(WorkloadType.BENCHMARK) is None

    # -----------------------------------------------------------------------
    # Case 9: missing power -> baseline not publishable
    # -----------------------------------------------------------------------
    def test_case_09_missing_power_baseline_not_publishable(
        self, tmp_path: Path
    ) -> None:
        """Adversarial Case 9: Missing empirical power measurement cannot default to zero and cannot be published."""
        manifest, meta = _make_clean_manifest_and_metadata()
        ckpt, art = _make_checkpoint_and_model_artifact()
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        bench_cfg = BenchmarkConfiguration(
            benchmark_id=BenchmarkId("bench-baseline-001"),
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-009",
            experiment_id="exp-baseline-009",
            dataset_manifest=manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
            benchmark_config=bench_cfg,
        )
        op_id, req, dec = _make_approval_credentials(plan, target_id)

        # Output with power_watts_avg=None
        canned_output = _make_canned_evaluation_output(
            target_id=target_id,
            power_watts_avg=None,
        )
        mock_executor = MockToolExecutor(
            canned_result=ToolResult(
                execution_id=ExecutionId("exec-009"),
                tool_id=ToolId("eval-tool"),
                target_id=target_id,
                status=ToolExecutionStatus.SUCCESS,
                structured_output=canned_output,
            )
        )

        ledger = SQLiteExperimentLedger(tmp_path)
        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
            experiment_ledger=ledger,
        )

        with pytest.raises(BaselineValidationError) as exc_info:
            establish_dataset_baseline(request)

        assert "power" in str(exc_info.value).lower()
        # Assert no active baseline exists in ledger
        assert ledger.get_baseline(WorkloadType.BENCHMARK) is None

    # -----------------------------------------------------------------------
    # Case 10: missing memory -> baseline not publishable
    # -----------------------------------------------------------------------
    def test_case_10_missing_memory_baseline_not_publishable(
        self, tmp_path: Path
    ) -> None:
        """Adversarial Case 10: Missing empirical memory measurement cannot default to zero and cannot be published."""
        manifest, meta = _make_clean_manifest_and_metadata()
        ckpt, art = _make_checkpoint_and_model_artifact()
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        bench_cfg = BenchmarkConfiguration(
            benchmark_id=BenchmarkId("bench-baseline-001"),
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-010",
            experiment_id="exp-baseline-010",
            dataset_manifest=manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
            benchmark_config=bench_cfg,
        )
        op_id, req, dec = _make_approval_credentials(plan, target_id)

        # Output with peak_vram_gb=None
        canned_output = _make_canned_evaluation_output(
            target_id=target_id,
            peak_vram_gb=None,
        )
        mock_executor = MockToolExecutor(
            canned_result=ToolResult(
                execution_id=ExecutionId("exec-010"),
                tool_id=ToolId("eval-tool"),
                target_id=target_id,
                status=ToolExecutionStatus.SUCCESS,
                structured_output=canned_output,
            )
        )

        ledger = SQLiteExperimentLedger(tmp_path)
        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
            experiment_ledger=ledger,
        )

        with pytest.raises(BaselineValidationError) as exc_info:
            establish_dataset_baseline(request)

        assert "memory" in str(exc_info.value).lower()
        # Assert no active baseline exists in ledger
        assert ledger.get_baseline(WorkloadType.BENCHMARK) is None

    # -----------------------------------------------------------------------
    # Case 11: fabricated/unresolved checkpoint binding -> reject
    # -----------------------------------------------------------------------
    def test_case_11_fabricated_checkpoint_binding_rejects(
        self, tmp_path: Path
    ) -> None:
        """Adversarial Case 11: Checkpoint hash/URI mismatch between model artifact and manifest must reject."""
        manifest, meta = _make_clean_manifest_and_metadata()
        ckpt, art = _make_checkpoint_and_model_artifact(weights_hash="1" * 64)
        # Create an artifact reference with mismatched fabricated weights hash
        tampered_art = art.model_copy(update={"weights_hash": ContentHash("f" * 64)})
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-011",
            experiment_id="exp-baseline-011",
            dataset_manifest=manifest,
            model_artifact=tampered_art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
        )
        op_id, req, dec = _make_approval_credentials(plan, target_id)
        mock_executor = MockToolExecutor()

        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=tampered_art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
        )

        with pytest.raises(EvaluationValidationError) as exc_info:
            establish_dataset_baseline(request)

        assert "weights hash mismatch" in str(exc_info.value).lower()
        assert len(mock_executor.invocations) == 0

    # -----------------------------------------------------------------------
    # Case 12: partial ledger write / failed evaluation -> no active baseline
    # -----------------------------------------------------------------------
    def test_case_12_failed_evaluation_no_active_baseline(self, tmp_path: Path) -> None:
        """Adversarial Case 12: Tool failure must not produce an authoritative active baseline in the ledger."""
        manifest, meta = _make_clean_manifest_and_metadata()
        ckpt, art = _make_checkpoint_and_model_artifact()
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-012",
            experiment_id="exp-baseline-012",
            dataset_manifest=manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
        )
        op_id, req, dec = _make_approval_credentials(plan, target_id)

        # Tool execution failure simulation
        mock_executor = MockToolExecutor(
            canned_result=ToolResult(
                execution_id=ExecutionId("exec-012"),
                tool_id=ToolId("eval-tool"),
                target_id=target_id,
                status=ToolExecutionStatus.EXECUTION_FAILED,
                error_message="CUDA out of memory during benchmarking",
            )
        )

        ledger = SQLiteExperimentLedger(tmp_path)
        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
            experiment_ledger=ledger,
        )

        with pytest.raises(EvaluationError):
            establish_dataset_baseline(request)

        # Invariant: Failed evaluation must NEVER leave an active baseline
        assert ledger.get_baseline(WorkloadType.BENCHMARK) is None
        assert ledger.get_baseline(WorkloadType.EVALUATION) is None

    # -----------------------------------------------------------------------
    # Case 13: attempt to overwrite immutable dataset version -> reject
    # -----------------------------------------------------------------------
    def test_case_13_attempt_to_overwrite_immutable_dataset_version_rejects(
        self, tmp_path: Path
    ) -> None:
        """Adversarial Case 13: Mutating an accepted dataset version must fail with DatasetVersionConflictError."""
        manifest, _ = _make_clean_manifest_and_metadata(version="v1.0.0")
        registry = FileSystemDatasetRegistry(tmp_path / "registry")

        # First registration with passing records for all 5 dimensions
        now = datetime.now(timezone.utc)
        passing_records = tuple(
            LeakageCheckResult(
                leakage_type=t,
                passed=True,
                summary=f"Clean {t.value}",
                metrics={},
                timestamp=now,
                auditor_version="1.0.0",
            )
            for t in LeakageType
        )
        audited_manifest = manifest.model_copy(
            update={"leakage_audit_records": passing_records}
        )
        registered_path = registry.register(audited_manifest)
        assert registered_path.exists()

        # Attempt to register a modified manifest with the exact same version
        modified_manifest = audited_manifest.model_copy(
            update={"description": "different description"}
        )
        with pytest.raises(DatasetVersionConflictError) as exc_info:
            registry.register(modified_manifest)

        assert "already registered and immutable" in str(exc_info.value).lower()

    # -----------------------------------------------------------------------
    # Case 14: clean dataset + complete empirical evidence -> exactly one root baseline
    # -----------------------------------------------------------------------
    def test_case_14_clean_dataset_complete_evidence_creates_root_baseline(
        self, tmp_path: Path
    ) -> None:
        """Adversarial Case 14 & End-to-End Exit Test: Clean data produces exactly one active root baseline."""
        manifest, meta = _make_clean_manifest_and_metadata(
            dataset_id="ds-gate-perimeter", version="v1.0.0"
        )
        ckpt, art = _make_checkpoint_and_model_artifact(
            checkpoint_id="ckpt-yolov8n-001",
            weights_hash="a" * 64,
            weights_uri="file:///weights/yolov8n.pt",
        )
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        bench_cfg = BenchmarkConfiguration(
            benchmark_id=BenchmarkId("bench-baseline-001"),
            target_requirements=EvaluationTargetRequirements(
                required_hardware_class=HardwareClass.JETSON
            ),
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-baseline-014",
            experiment_id="exp-baseline-014",
            dataset_manifest=manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
            benchmark_config=bench_cfg,
        )
        op_id, req, dec = _make_approval_credentials(plan, target_id)

        canned_output = _make_canned_evaluation_output(
            target_id=target_id,
            timing_synchronized=True,
            timing_source="cuda_event_timer",
            peak_vram_gb=2.4,
            power_watts_avg=14.5,
            latency_p50_ms=4.8,
            throughput_fps=200.0,
            map_50=0.89,
        )
        mock_executor = MockToolExecutor(
            canned_result=ToolResult(
                execution_id=ExecutionId("exec-014"),
                tool_id=ToolId("eval-tool"),
                target_id=target_id,
                status=ToolExecutionStatus.SUCCESS,
                structured_output=canned_output,
            )
        )

        registry = FileSystemDatasetRegistry(tmp_path / "registry")
        ledger = SQLiteExperimentLedger(tmp_path)

        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
            dataset_registry=registry,
            experiment_ledger=ledger,
            workload_type=WorkloadType.BENCHMARK,
        )

        result = establish_dataset_baseline(request)

        # 1. Assert result structure
        assert result.is_root_baseline is True
        assert result.active_baseline_designated is True
        assert result.experiment_record.status == ExperimentStatus.COMPLETED

        # 2. Assert ADR-0004 persistence and baseline retrieval
        active_baseline = ledger.get_baseline(WorkloadType.BENCHMARK)
        assert active_baseline is not None
        assert active_baseline.exp_id == result.experiment_record.exp_id

        # Root baseline invariant: no parent baseline
        assert active_baseline.spec.baseline_id is None
        assert active_baseline.spec.parent_exp_id is None

        # Empirical accuracy metrics
        assert active_baseline.accuracy_metrics is not None
        assert active_baseline.accuracy_metrics.map_50 == pytest.approx(0.89)

        # Empirical system metrics (peak_vram in MB = 2.4 * 1024)
        assert active_baseline.system_metrics is not None
        assert active_baseline.system_metrics.peak_vram_mb == pytest.approx(2457.6)
        assert active_baseline.system_metrics.power_draw_watts == pytest.approx(14.5)
        assert active_baseline.system_metrics.latency_inference_ms == pytest.approx(4.8)
        assert active_baseline.system_metrics.throughput_fps == pytest.approx(200.0)

        # Dataset manifest registered in registry
        assert registry.exists(manifest.dataset_id, manifest.version)

        # Exactly 1 tool invocation occurred
        assert len(mock_executor.invocations) == 1
        assert mock_executor.invocations[0].target_id == target_id


# ===========================================================================
# Architectural Boundary & Gate G5 Regression Tests
# ===========================================================================


class TestPhase5ArchitecturalBoundaryInvariants:
    """Verifies that Phase 5 strictly respects architectural boundaries and doesn't violate rules."""

    def test_no_direct_subprocess_or_ssh_in_workflow(self) -> None:
        """Gate G5: AST analysis confirms zero direct subprocess/os.system/ssh calls in data_baseline.py."""
        target_file = Path("cv_agent/orchestration/data_baseline.py")
        tree = ast.parse(target_file.read_text("utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Disallow subprocess calls
                if isinstance(func, ast.Attribute):
                    assert func.attr not in {
                        "Popen",
                        "run",
                        "call",
                        "check_output",
                        "system",
                    }, f"Disallowed direct process invocation: {func.attr}"
                elif isinstance(func, ast.Name):
                    assert func.id not in {
                        "system",
                        "exec",
                        "eval",
                    }, f"Disallowed direct execution: {func.id}"

    def test_no_target_synthesis_permitted(self, tmp_path: Path) -> None:
        """Gate G5: Evaluation must fail closed if target_id is empty or synthesized."""
        manifest, meta = _make_clean_manifest_and_metadata()
        ckpt, art = _make_checkpoint_and_model_artifact()

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-synthesize",
            experiment_id="exp-synthesize",
            dataset_manifest=manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
        )
        op_id, req, dec = _make_approval_credentials(
            plan, TargetId("placeholder-target")
        )

        with pytest.raises(EvaluationApprovalRequiredError) as exc_info:
            establish_dataset_baseline(
                BaselineEstablishmentRequest(
                    dataset_manifest=manifest,
                    item_metadata=meta,
                    checkpoint_manifest=ckpt,
                    model_artifact=art,
                    evaluation_plan=plan,
                    operation_identity=op_id,
                    approval_request=req,
                    approval_decision=dec,
                    target_id=None,  # No target supplied
                    tool_id=ToolId("eval-tool"),
                    tool_executor=MockToolExecutor(),
                )
            )

        assert "authorized named targetid" in str(exc_info.value).lower()

    def test_workflow_service_class_instantiation(self, tmp_path: Path) -> None:
        """Service class DatasetBaselineWorkflow delegates cleanly."""
        manifest, meta = _make_clean_manifest_and_metadata()
        ckpt, art = _make_checkpoint_and_model_artifact()
        target_id = TargetId("jetson-orin-01")

        metric = MetricDefinition(
            metric_id=MetricId("map_50"),
            name="mAP@0.50",
            task_type=TaskType.DETECTION,
        )
        plan = build_baseline_evaluation_plan(
            evaluation_id="eval-svc",
            experiment_id="exp-svc",
            dataset_manifest=manifest,
            model_artifact=art,
            task_type=TaskType.DETECTION,
            metrics=(metric,),
        )
        op_id, req, dec = _make_approval_credentials(plan, target_id)

        canned = _make_canned_evaluation_output(target_id=target_id)
        mock_executor = MockToolExecutor(
            canned_result=ToolResult(
                execution_id=ExecutionId("exec-svc"),
                tool_id=ToolId("eval-tool"),
                target_id=target_id,
                status=ToolExecutionStatus.SUCCESS,
                structured_output=canned,
            )
        )

        ledger = SQLiteExperimentLedger(tmp_path)
        workflow = DatasetBaselineWorkflow(experiment_ledger=ledger)

        request = BaselineEstablishmentRequest(
            dataset_manifest=manifest,
            item_metadata=meta,
            checkpoint_manifest=ckpt,
            model_artifact=art,
            evaluation_plan=plan,
            operation_identity=op_id,
            approval_request=req,
            approval_decision=dec,
            target_id=target_id,
            tool_id=ToolId("eval-tool"),
            tool_executor=mock_executor,
        )

        result = workflow.establish_baseline(request)
        assert result.is_root_baseline is True
        assert ledger.get_baseline(WorkloadType.BENCHMARK) is not None
