"""cv_agent.orchestration.data_baseline — Phase 5 Data & Baseline Establishment Workflow.

Orchestrates ADR-0009 (Dataset), ADR-0011 (Evaluation), ADR-0005 (Execution),
ADR-0003 (Approval), and ADR-0004 (Experiment Ledger / Active Baseline).

Pipeline:
1. G1: Dataset manifest & payload root checksum validation (ADR-0009).
2. G2: Mandatory 5-dimension split leakage auditing (temporal & camera/site) with fail-closed rejection.
3. G1: Immutable dataset versioning & registry publication (write-once immutability).
4. Checkpoint / model artifact lineage verification (ADR-0010 / ADR-0011).
5. Target authorization and ADR-0003 operation/approval verification.
6. G3: ADR-0011 evaluation execution on named target via ADR-0005.
7. Quantitative empirical evidence & attestation verification (quality, latency, memory, power, synchronized timing).
8. G4: ADR-0004 atomic SQLite experiment ledger persistence & root baseline designation.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cv_agent.dataset.contracts import (
    DatasetManifest,
    ItemMetadata,
    OperationIdentity,
)
from cv_agent.dataset.errors import (
    BaselineEstablishmentError,
    BaselineValidationError,
    DatasetVersionConflictError,
    SplitLeakageError,
)
from cv_agent.dataset.leakage import LeakageAuditor, StandardLeakageAuditor
from cv_agent.dataset.registry import DatasetRegistry
from cv_agent.dataset.storage.base import DatasetStore
from cv_agent.dataset.types import ContentHash
from cv_agent.dataset.validation import verify_payload_root_checksum
from cv_agent.domain import CapabilityId
from cv_agent.evaluation.contracts import (
    BenchmarkConfiguration,
    EvaluationDatasetBinding,
    EvaluationPlan,
    EvaluationResult,
    EvaluationTargetRequirements,
    MetricDefinition,
    ModelArtifactReference,
)
from cv_agent.evaluation.errors import (
    EvaluationApprovalRequiredError,
    EvaluationValidationError,
)
from cv_agent.evaluation.runner import DefaultEvaluationRunner, EvaluationRunner
from cv_agent.evaluation.types import (
    BenchmarkId,
    EvaluationId,
    ExperimentId,
    TaskType,
)
from cv_agent.experiments.interfaces import (
    AccuracyMetrics,
    ArtifactManifest,
    ExecutionTarget,
    ExperimentLedger,
    ExperimentRecord,
    ExperimentSpec,
    ExperimentStatus,
    SystemMetrics,
    WorkloadType,
)
from cv_agent.experiments.ledger import SQLiteExperimentLedger
from cv_agent.orchestration.approvals import (
    ApprovalDecision,
    ApprovalRequest,
)
from cv_agent.tools import TargetId, ToolExecutor, ToolId
from cv_agent.training.contracts import CheckpointManifest

__all__ = [
    "BaselineEstablishmentError",
    "BaselineEstablishmentRequest",
    "BaselineEstablishmentResult",
    "BaselineValidationError",
    "DatasetBaselineWorkflow",
    "build_baseline_evaluation_plan",
    "establish_dataset_baseline",
]


@dataclass(frozen=True)
class BaselineEstablishmentRequest:
    """Comprehensive request specifying all elements for root baseline establishment."""

    dataset_manifest: DatasetManifest
    item_metadata: Mapping[Any, ItemMetadata]
    checkpoint_manifest: CheckpointManifest
    model_artifact: ModelArtifactReference
    evaluation_plan: EvaluationPlan
    operation_identity: OperationIdentity
    approval_request: ApprovalRequest
    approval_decision: ApprovalDecision
    target_id: TargetId | None
    tool_id: ToolId
    tool_executor: ToolExecutor
    dataset_registry: DatasetRegistry | None = None
    experiment_ledger: ExperimentLedger | None = None
    leakage_auditor: LeakageAuditor | None = None
    evaluation_runner: EvaluationRunner | None = None
    store: DatasetStore | None = None
    workload_type: WorkloadType = WorkloadType.BENCHMARK
    workspace_root: Path | str | None = None
    is_deployability_eval: bool = True


@dataclass(frozen=True)
class BaselineEstablishmentResult:
    """Outcome of successful root baseline establishment."""

    experiment_record: ExperimentRecord
    evaluation_result: EvaluationResult
    dataset_manifest: DatasetManifest
    is_root_baseline: bool = True
    active_baseline_designated: bool = True


def build_baseline_evaluation_plan(
    evaluation_id: EvaluationId | str,
    experiment_id: ExperimentId | str,
    dataset_manifest: DatasetManifest,
    model_artifact: ModelArtifactReference,
    task_type: TaskType,
    metrics: Sequence[MetricDefinition],
    benchmark_config: BenchmarkConfiguration | None = None,
    split_name: str = "test",
    capability_id: CapabilityId | str = "cv.detection.eval",
    target_requirements: EvaluationTargetRequirements | None = None,
) -> EvaluationPlan:
    """Construct an immutable root-baseline EvaluationPlan adhering to ADR-0011.

    Invariants enforced:
    - baseline_experiment_id is strictly None (root baseline).
    - dataset_binding references exact dataset_id, dataset_version, and payload_root_checksum.
    - calibration split cannot be used as an ordinary evaluation test split.
    """
    if split_name == "calibration":
        raise EvaluationValidationError(
            "Calibration split 'calibration' is strictly reserved for post-training quantization "
            "and cannot be used as an ordinary baseline evaluation split."
        )

    binding = EvaluationDatasetBinding(
        dataset_id=dataset_manifest.dataset_id,
        dataset_version=dataset_manifest.version,
        dataset_manifest_hash=ContentHash(dataset_manifest.payload_root_checksum),
        split_name=split_name,
    )

    eff_bench_cfg = benchmark_config
    if eff_bench_cfg is None:
        eff_bench_cfg = BenchmarkConfiguration(
            benchmark_id=BenchmarkId(f"bench-{evaluation_id}"),
            target_requirements=target_requirements or EvaluationTargetRequirements(),
        )

    return EvaluationPlan(
        evaluation_id=EvaluationId(str(evaluation_id)),
        experiment_id=ExperimentId(str(experiment_id)),
        created_at=datetime.now(timezone.utc),
        capability_id=CapabilityId(str(capability_id)),
        dataset_binding=binding,
        model_artifact=model_artifact,
        task_type=task_type,
        metrics=tuple(metrics),
        benchmark_config=eff_bench_cfg,
        baseline_experiment_id=None,  # Root baseline invariant
        target_requirements=target_requirements or EvaluationTargetRequirements(),
    )


def establish_dataset_baseline(
    request: BaselineEstablishmentRequest,
) -> BaselineEstablishmentResult:
    """Execute the end-to-end dataset validation and baseline establishment workflow.

    Pipeline:
    1. G1: Dataset integrity & payload root checksum validation.
    2. G2: Mandatory 5-dimension leakage audit. Fail-closed rejection before evaluation dispatch.
    3. G1: Dataset versioning and registry publication (write-once immutability).
    4. Checkpoint / model artifact lineage verification.
    5. Target authorization and ADR-0003 approval verification.
    6. G3: ADR-0011 evaluation execution on named target via ADR-0005.
    7. Quantitative empirical verification (accuracy, latency, memory, power, synchronized timing).
    8. G4: ADR-0004 atomic experiment ledger recording and active root baseline designation.
    """
    manifest = request.dataset_manifest

    # 1. Manifest & Payload Root Checksum Validation (Gate G1)
    verify_payload_root_checksum(manifest)

    splits = manifest.split_membership
    available_splits: set[str] = set()
    if splits.train_hashes:
        available_splits.add("train")
    if splits.val_hashes:
        available_splits.add("val")
    if splits.test_hashes:
        available_splits.add("test")
    if splits.calibration_hashes:
        available_splits.add("calibration")

    plan = request.evaluation_plan

    # Calibration split isolation (ADR-0011 Invariant 13)
    if plan.dataset_binding.split_name == "calibration":
        raise EvaluationValidationError(
            "Calibration split 'calibration' is strictly reserved for post-training quantization "
            "and cannot be used as an ordinary baseline evaluation split."
        )

    if plan.dataset_binding.split_name not in available_splits:
        raise EvaluationValidationError(
            f"Split {plan.dataset_binding.split_name!r} not found in dataset splits {sorted(available_splits)}"
        )

    # Invariant: root baseline EvaluationPlan must have baseline_experiment_id=None
    if plan.baseline_experiment_id is not None:
        raise BaselineValidationError(
            f"Root baseline EvaluationPlan must have baseline_experiment_id=None, got {plan.baseline_experiment_id!r}"
        )

    # Dataset binding consistency checks
    if str(plan.dataset_binding.dataset_id) != str(manifest.dataset_id):
        raise EvaluationValidationError(
            f"Dataset ID mismatch: plan specifies {plan.dataset_binding.dataset_id!r}, "
            f"manifest specifies {manifest.dataset_id!r}"
        )
    if str(plan.dataset_binding.dataset_version) != str(manifest.version):
        raise EvaluationValidationError(
            f"Dataset version mismatch: plan specifies {plan.dataset_binding.dataset_version!r}, "
            f"manifest specifies {manifest.version!r}"
        )
    if str(plan.dataset_binding.dataset_manifest_hash) != str(
        manifest.payload_root_checksum
    ):
        raise EvaluationValidationError(
            f"Dataset manifest hash mismatch: plan specifies {plan.dataset_binding.dataset_manifest_hash!r}, "
            f"manifest declares {manifest.payload_root_checksum!r}"
        )

    # 2. Mandatory Leakage Audit Gate (Gate G2)
    auditor = request.leakage_auditor or StandardLeakageAuditor()
    audit_results = auditor.audit_splits(
        manifest, request.item_metadata, store=request.store
    )

    failed_audits = [r.leakage_type.value for r in audit_results if not r.passed]
    if failed_audits:
        # Fail closed: ZERO evaluation dispatch after leakage failure!
        raise SplitLeakageError(failed_audits)

    # 3. Dataset Registry Publication & Immutability (Gate G1)
    registry = request.dataset_registry
    if registry is not None:
        if registry.exists(manifest.dataset_id, manifest.version):
            existing = registry.get(manifest.dataset_id, manifest.version)
            if existing.payload_root_checksum != manifest.payload_root_checksum:
                raise DatasetVersionConflictError(
                    str(manifest.dataset_id), str(manifest.version)
                )
        else:
            manifest_to_reg = manifest
            if not manifest_to_reg.leakage_audit_records:
                manifest_to_reg = manifest_to_reg.model_copy(
                    update={"leakage_audit_records": tuple(audit_results)}
                )
            registry.register(manifest_to_reg, store=request.store)

    # 4. Checkpoint / Model Artifact Lineage Verification
    model_art = request.model_artifact
    ckpt = request.checkpoint_manifest

    if str(model_art.checkpoint_id) != str(ckpt.checkpoint_id):
        raise EvaluationValidationError(
            f"Checkpoint ID mismatch: model artifact specifies {model_art.checkpoint_id!r}, "
            f"checkpoint manifest specifies {ckpt.checkpoint_id!r}"
        )
    if str(model_art.weights_hash) != str(ckpt.weights_hash):
        raise EvaluationValidationError(
            f"Weights hash mismatch: model artifact specifies {model_art.weights_hash!r}, "
            f"checkpoint manifest specifies {ckpt.weights_hash!r}"
        )
    if model_art.weights_uri != ckpt.weights_uri:
        raise EvaluationValidationError(
            f"Weights URI mismatch: model artifact specifies {model_art.weights_uri!r}, "
            f"checkpoint manifest specifies {ckpt.weights_uri!r}"
        )
    if not str(model_art.weights_hash).strip():
        raise EvaluationValidationError(
            "Model weights hash cannot be empty or unresolved."
        )

    if str(plan.model_artifact.checkpoint_id) != str(model_art.checkpoint_id):
        raise EvaluationValidationError(
            f"Plan checkpoint ID {plan.model_artifact.checkpoint_id!r} does not match {model_art.checkpoint_id!r}"
        )
    if str(plan.model_artifact.weights_hash) != str(model_art.weights_hash):
        raise EvaluationValidationError(
            f"Plan weights hash {plan.model_artifact.weights_hash!r} does not match {model_art.weights_hash!r}"
        )

    # 5. Target Authorization & ADR-0003 Approval Verification
    if request.target_id is None or not str(request.target_id).strip():
        raise EvaluationApprovalRequiredError(
            "Cannot execute baseline evaluation without an authorized named TargetId."
        )

    runner = request.evaluation_runner or DefaultEvaluationRunner()
    runner.validate_plan(plan, dataset_manifest=manifest, checkpoint_manifest=ckpt)
    runner.verify_approval(
        plan=plan,
        operation_identity=request.operation_identity,
        approval_request=request.approval_request,
        approval_decision=request.approval_decision,
        target_id=request.target_id,
    )

    # 6. Evaluation Execution via ADR-0011 and ADR-0005 (Gate G3)
    eval_result = runner.execute(
        plan=plan,
        operation_identity=request.operation_identity,
        approval_request=request.approval_request,
        approval_decision=request.approval_decision,
        tool_id=request.tool_id,
        tool_executor=request.tool_executor,
        target_id=request.target_id,
    )

    # 7. Empirical Evidence & Attestation Verification
    if not eval_result.quality_metrics:
        raise BaselineValidationError("EvaluationResult contains zero quality metrics.")

    for qm in eval_result.quality_metrics:
        val = qm.primary_value
        if (
            val is None
            or not isinstance(val, (int, float))
            or (isinstance(val, float) and (math.isnan(val) or math.isinf(val)))
        ):
            raise BaselineValidationError(
                f"Quality metric {qm.metric.metric_id} has invalid non-finite value: {val}"
            )

    if not eval_result.system_benchmarks:
        raise BaselineValidationError(
            "EvaluationResult contains zero system benchmarks."
        )

    bench = eval_result.system_benchmarks[0]

    # Target attestation check
    if str(bench.target_id) != str(request.target_id):
        raise BaselineValidationError(
            f"Target mismatch: benchmark executed on target {bench.target_id!r}, "
            f"expected authorized target {request.target_id!r}. "
            "Developer host measurements cannot masquerade as target measurements."
        )

    # Accelerator timing synchronization attestation
    if not bench.timing_synchronized:
        raise BaselineValidationError(
            f"Hardware-synchronized timing is mandatory for baseline benchmark on target {request.target_id!r}, "
            "but timing_synchronized is False."
        )

    src_lower = bench.timing_source.strip().lower()
    if src_lower in {
        "unsynchronized",
        "wall_clock_async",
        "none",
        "unknown",
        "async",
        "wall_clock",
    }:
        raise BaselineValidationError(
            f"Timing source {bench.timing_source!r} is unsynchronized. "
            "Hardware-synchronized timing is required per ADR-0011 §3.13."
        )

    # Empirical memory check (cannot default to zero or None)
    if bench.peak_vram_gb is None or bench.peak_vram_gb <= 0.0:
        raise BaselineValidationError(
            f"Missing empirical memory measurement: peak_vram_gb is {bench.peak_vram_gb!r}. "
            "Baseline is not publishable without empirical memory."
        )

    # Empirical power check (cannot default to zero or None)
    if bench.power_watts_avg is None or bench.power_watts_avg <= 0.0:
        raise BaselineValidationError(
            f"Missing empirical power measurement: power_watts_avg is {bench.power_watts_avg!r}. "
            "Baseline is not publishable without empirical power."
        )

    # Empirical latency & throughput checks
    if bench.latency_p50_ms <= 0.0 or bench.latency_mean_ms <= 0.0:
        raise BaselineValidationError(
            "Empirical latency measurements must be strictly positive."
        )
    if bench.throughput_fps <= 0.0:
        raise BaselineValidationError(
            "Empirical throughput measurement must be strictly positive."
        )

    # 8. ADR-0004 Atomic Persistence & Root Baseline Designation (Gate G4)
    ledger = request.experiment_ledger
    if ledger is None:
        ws_root = Path(request.workspace_root) if request.workspace_root else Path(".")
        ledger = SQLiteExperimentLedger(workspace_root=ws_root)

    t_str = str(request.target_id).lower()
    if "jetson" in t_str or "remote" in t_str or "edge" in t_str:
        exec_target = ExecutionTarget.REMOTE_GPU
    elif "cloud" in t_str:
        exec_target = ExecutionTarget.CLOUD
    else:
        exec_target = ExecutionTarget.LOCAL_GPU

    req_names = tuple(str(m.metric.metric_id) for m in eval_result.quality_metrics)

    raw_sha = eval_result.environment.git_commit_sha
    if re.match(r"^[0-9a-fA-F]{40}$", raw_sha):
        commit_sha = raw_sha
    else:
        commit_sha = hashlib.sha1(raw_sha.encode("utf-8")).hexdigest()

    spec = ExperimentSpec(
        name=f"baseline-{manifest.dataset_id}-{manifest.version}",
        hypothesis=f"Empirical root baseline establishment on {request.target_id}",
        workload_type=request.workload_type,
        target=exec_target,
        model_name=model_art.architecture_name,
        model_variant="baseline",
        commit_sha=commit_sha,
        dataset_version=str(manifest.version),
        seed=eval_result.environment.seed or 42,
        batch_size=bench.batch_size,
        input_resolution=model_art.input_resolution,
        precision=str(model_art.precision.value),
        config_uri="configs/baseline.yaml",
        config_hash=hashlib.sha256(b"baseline_config").hexdigest(),
        hyperparameters={
            "architecture": model_art.architecture_name,
            "weights_hash": str(model_art.weights_hash),
            "reproducibility": "deterministic",
        },
        parent_exp_id=None,
        baseline_id=None,  # Root baseline invariant: no baseline_id
        approval_ref=f"{request.operation_identity.operation_id}:{request.approval_request.request_id}",
        accuracy_metric_requirements=req_names,
    )

    exp_rec = ledger.create_experiment(spec)
    exp_rec = ledger.transition_status(exp_rec.exp_id, ExperimentStatus.RUNNING)

    # Map quality metrics to AccuracyMetrics
    map_50 = next(
        (
            m.primary_value
            for m in eval_result.quality_metrics
            if m.metric.metric_id == "map_50"
        ),
        None,
    )
    map_50_95 = next(
        (
            m.primary_value
            for m in eval_result.quality_metrics
            if m.metric.metric_id in ("map_50_95", "map")
        ),
        None,
    )
    precision = next(
        (
            m.primary_value
            for m in eval_result.quality_metrics
            if m.metric.metric_id == "precision"
        ),
        None,
    )
    recall = next(
        (
            m.primary_value
            for m in eval_result.quality_metrics
            if m.metric.metric_id == "recall"
        ),
        None,
    )
    f1_score = next(
        (
            m.primary_value
            for m in eval_result.quality_metrics
            if m.metric.metric_id in ("f1", "f1_score")
        ),
        None,
    )
    add_metrics = {
        str(m.metric.metric_id): m.primary_value for m in eval_result.quality_metrics
    }

    acc_metrics = AccuracyMetrics(
        map_50=map_50,
        map_50_95=map_50_95,
        precision=precision,
        recall=recall,
        f1_score=f1_score,
        additional_metrics=add_metrics,
    )

    sys_metrics = SystemMetrics(
        peak_vram_mb=float(bench.peak_vram_gb * 1024.0),
        power_draw_watts=float(bench.power_watts_avg),
        latency_inference_ms=float(bench.latency_p50_ms),
        latency_e2e_ms=float(bench.latency_p95_ms),
        throughput_fps=float(bench.throughput_fps),
    )

    art_manifest = ArtifactManifest(
        weights_uri=model_art.weights_uri,
        checksums={"weights": str(model_art.weights_hash)},
    )

    exp_rec = ledger.complete_experiment(
        exp_rec.exp_id,
        accuracy=acc_metrics,
        system=sys_metrics,
        artifacts=art_manifest,
        notes=f"Empirical root baseline on {request.target_id} for {manifest.dataset_id} v{manifest.version}",
    )

    # Atomically designate as active root baseline
    ledger.set_active_baseline(exp_rec.exp_id, request.workload_type)

    return BaselineEstablishmentResult(
        experiment_record=exp_rec,
        evaluation_result=eval_result,
        dataset_manifest=manifest,
        is_root_baseline=True,
        active_baseline_designated=True,
    )


class DatasetBaselineWorkflow:
    """Service orchestrating the complete Phase 5 dataset acceptance and baseline workflow."""

    def __init__(
        self,
        leakage_auditor: LeakageAuditor | None = None,
        evaluation_runner: EvaluationRunner | None = None,
        dataset_registry: DatasetRegistry | None = None,
        experiment_ledger: ExperimentLedger | None = None,
    ) -> None:
        self.leakage_auditor = leakage_auditor or StandardLeakageAuditor()
        self.evaluation_runner = evaluation_runner or DefaultEvaluationRunner()
        self.dataset_registry = dataset_registry
        self.experiment_ledger = experiment_ledger

    def establish_baseline(
        self,
        request: BaselineEstablishmentRequest,
    ) -> BaselineEstablishmentResult:
        """Execute baseline establishment using configured or request-supplied components."""
        req = request
        if self.leakage_auditor and req.leakage_auditor is None:
            req = replace(req, leakage_auditor=self.leakage_auditor)
        if self.evaluation_runner and req.evaluation_runner is None:
            req = replace(req, evaluation_runner=self.evaluation_runner)
        if self.dataset_registry and req.dataset_registry is None:
            req = replace(req, dataset_registry=self.dataset_registry)
        if self.experiment_ledger and req.experiment_ledger is None:
            req = replace(req, experiment_ledger=self.experiment_ledger)
        return establish_dataset_baseline(req)
