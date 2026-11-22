"""cv_agent.evaluation.runner — High-level evaluation runner and dispatch for ADR-0011."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol, cast, runtime_checkable

from cv_agent.dataset import DatasetManifest, OperationIdentity
from cv_agent.evaluation.benchmarks import BenchmarkRunner, DefaultBenchmarkRunner
from cv_agent.evaluation.canonicalization import (
    compute_evaluation_payload_hash,
    extract_evaluation_operation_payload,
)
from cv_agent.evaluation.contracts import (
    BenchmarkResult,
    EvaluationEnvironmentRecord,
    EvaluationPlan,
    EvaluationResult,
    MetricResult,
)
from cv_agent.evaluation.diagnostics import DefaultErrorAnalyzer, ErrorAnalyzer
from cv_agent.evaluation.errors import (
    BenchmarkExecutionError,
    EvaluationApprovalRequiredError,
    EvaluationError,
    EvaluationValidationError,
)
from cv_agent.evaluation.metrics import MetricCalculator, StandardMetricCalculator
from cv_agent.evaluation.types import EvaluationVerdict, HardwareClass
from cv_agent.orchestration import ApprovalDecision, ApprovalRequest, ApprovalStatus
from cv_agent.tools import (
    ExecutionId,
    TargetId,
    ToolExecutionStatus,
    ToolExecutor,
    ToolId,
    ToolInvocation,
    ToolResult,
)
from cv_agent.training import CheckpointManifest


@runtime_checkable
class EvaluationRunner(Protocol):
    """High-level dispatch protocol coordinating evaluation execution via ADR-0005."""

    def validate_plan(
        self,
        plan: EvaluationPlan,
        dataset_manifest: DatasetManifest | None = None,
        checkpoint_manifest: CheckpointManifest | None = None,
    ) -> bool: ...

    def verify_approval(
        self,
        plan: EvaluationPlan,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        *,
        target_id: TargetId | None = None,
    ) -> None: ...

    def prepare_invocation(
        self,
        plan: EvaluationPlan,
        tool_id: ToolId,
        target_id: TargetId,
        *,
        approval_authorized: bool = False,
        execution_id: ExecutionId | None = None,
        timeout_seconds: int = 86400,
    ) -> ToolInvocation: ...

    def execute(
        self,
        plan: EvaluationPlan,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        target_id: TargetId | None = None,
        benchmark_tool_id: ToolId | None = None,
    ) -> EvaluationResult: ...


class DefaultEvaluationRunner:
    """Standard evaluation runner coordinating plan validation, approval verification, and ADR-0005 dispatch."""

    def __init__(
        self,
        metric_calculator: MetricCalculator | None = None,
        error_analyzer: ErrorAnalyzer | None = None,
        benchmark_runner: BenchmarkRunner | None = None,
    ) -> None:
        self.metric_calculator = metric_calculator or StandardMetricCalculator()
        self.error_analyzer = error_analyzer or DefaultErrorAnalyzer()
        self.benchmark_runner = benchmark_runner or DefaultBenchmarkRunner()

    def validate_plan(
        self,
        plan: EvaluationPlan,
        dataset_manifest: DatasetManifest | None = None,
        checkpoint_manifest: CheckpointManifest | None = None,
    ) -> bool:
        """Validate structural and architectural constraints on an EvaluationPlan.

        Invariants:
            1. Non-root baseline plans must declare a non-null baseline_experiment_id.
            2. When dataset_manifest is supplied, dataset_id, version, and manifest hash must match.
            3. Calibration split 'calibration' cannot be evaluated as an evaluation split (Invariant 13).
            4. When checkpoint_manifest is supplied, checkpoint_id and weights_hash must match.
        """
        # Invariant 3: Root baseline exception
        # baseline_experiment_id is None permitted ONLY for root baseline runs
        if plan.baseline_experiment_id is None and str(plan.experiment_id).endswith(
            "-derived"
        ):
            raise EvaluationValidationError(
                "Derived evaluation experiment must declare an authoritative baseline_experiment_id"
            )

        if dataset_manifest is not None:
            if str(plan.dataset_binding.dataset_id) != str(dataset_manifest.dataset_id):
                raise EvaluationValidationError(
                    f"Dataset ID mismatch: plan specifies {plan.dataset_binding.dataset_id!r}, "
                    f"manifest specifies {dataset_manifest.dataset_id!r}"
                )
            if str(plan.dataset_binding.dataset_version) != str(
                dataset_manifest.version
            ):
                raise EvaluationValidationError(
                    f"Dataset version mismatch: plan specifies {plan.dataset_binding.dataset_version!r}, "
                    f"manifest specifies {dataset_manifest.version!r}"
                )
            if str(plan.dataset_binding.dataset_manifest_hash) != str(
                dataset_manifest.payload_root_checksum
            ):
                raise EvaluationValidationError(
                    f"Dataset manifest hash mismatch: plan specifies {plan.dataset_binding.dataset_manifest_hash!r}, "
                    f"manifest specifies {dataset_manifest.payload_root_checksum!r}"
                )

            available_splits: set[str] = set()
            if dataset_manifest.split_membership.train_hashes:
                available_splits.add("train")
            if dataset_manifest.split_membership.val_hashes:
                available_splits.add("val")
            if dataset_manifest.split_membership.test_hashes:
                available_splits.add("test")
            if dataset_manifest.split_membership.calibration_hashes:
                available_splits.add("calibration")

            # Invariant 13: calibration split isolation
            if plan.dataset_binding.split_name == "calibration":
                raise EvaluationValidationError(
                    "Calibration split 'calibration' is strictly reserved for post-training quantization "
                    "and cannot be used as an evaluation test split."
                )

            if plan.dataset_binding.split_name not in available_splits:
                raise EvaluationValidationError(
                    f"split_name {plan.dataset_binding.split_name!r} does not exist in dataset splits {sorted(available_splits)}"
                )

        if checkpoint_manifest is not None:
            if str(plan.model_artifact.checkpoint_id) != str(
                checkpoint_manifest.checkpoint_id
            ):
                raise EvaluationValidationError(
                    f"Checkpoint ID mismatch: plan specifies {plan.model_artifact.checkpoint_id!r}, "
                    f"manifest specifies {checkpoint_manifest.checkpoint_id!r}"
                )
            if str(plan.model_artifact.weights_hash) != str(
                checkpoint_manifest.weights_hash
            ):
                raise EvaluationValidationError(
                    f"Model weights hash mismatch: plan specifies {plan.model_artifact.weights_hash!r}, "
                    f"manifest specifies {checkpoint_manifest.weights_hash!r}"
                )

        return True

    def _dispatch_invocation(
        self,
        tool_executor: ToolExecutor,
        invocation: ToolInvocation,
    ) -> ToolResult:
        """Dispatch ToolInvocation to ADR-0005 ToolExecutor synchronously or via async event loop."""
        try:
            coro_or_res = tool_executor.execute(invocation)
            if inspect.iscoroutine(coro_or_res):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        return pool.submit(asyncio.run, coro_or_res).result()
                else:
                    return asyncio.run(coro_or_res)
            return cast(ToolResult, coro_or_res)
        except Exception as exc:
            if isinstance(exc, EvaluationError):
                raise
            raise EvaluationError(f"Dispatch to tool executor failed: {exc}") from exc

    def prepare_invocation(
        self,
        plan: EvaluationPlan,
        tool_id: ToolId,
        target_id: TargetId,
        *,
        approval_authorized: bool = False,
        execution_id: ExecutionId | None = None,
        timeout_seconds: int = 86400,
    ) -> ToolInvocation:
        """Format evaluation payload into a typed ADR-0005 ToolInvocation.

        Zero shell interpretation, zero subprocess calls, zero local target synthesis.
        """
        if target_id is None or not str(target_id).strip():
            raise EvaluationApprovalRequiredError(
                "Cannot prepare invocation without an authorized TargetId. "
                "Local target synthesis is strictly prohibited."
            )

        exec_id = execution_id or ExecutionId(
            f"exec-eval-{plan.evaluation_id}-{uuid.uuid4().hex[:8]}"
        )
        payload = extract_evaluation_operation_payload(plan)

        return ToolInvocation(
            tool_id=tool_id,
            parameters={"evaluation_payload": payload},
            target_id=target_id,
            execution_id=exec_id,
            timeout_seconds=timeout_seconds,
            approval_authorized=approval_authorized,
        )

    def verify_approval(
        self,
        plan: EvaluationPlan,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        *,
        target_id: TargetId | None = None,
    ) -> None:
        """Verify evaluation plan against authoritative ADR-0003 OperationIdentity and Approval credentials."""
        if operation_identity is None:
            raise EvaluationApprovalRequiredError(
                "Missing authoritative ADR-0003 OperationIdentity"
            )
        if approval_request is None:
            raise EvaluationApprovalRequiredError(
                "Missing authoritative ADR-0003 ApprovalRequest"
            )
        if approval_decision is None:
            raise EvaluationApprovalRequiredError(
                "Missing authoritative ADR-0003 ApprovalDecision"
            )

        computed_hash = compute_evaluation_payload_hash(plan)

        # 1. Verify computed payload hash matches operation identity
        if operation_identity.canonical_payload_hash != computed_hash:
            raise EvaluationApprovalRequiredError(
                f"Canonical payload hash mismatch: OperationIdentity has {operation_identity.canonical_payload_hash!r}, "
                f"but EvaluationPlan computes to {computed_hash!r}"
            )

        # 2. Verify operation identity matches approval request
        if str(operation_identity.operation_id) != str(approval_request.operation_id):
            raise EvaluationApprovalRequiredError(
                f"Operation ID mismatch: OperationIdentity has {operation_identity.operation_id!r}, "
                f"ApprovalRequest has {approval_request.operation_id!r}"
            )
        if str(operation_identity.operation_version) != str(
            approval_request.operation_version
        ):
            raise EvaluationApprovalRequiredError(
                f"Operation version mismatch: OperationIdentity has {operation_identity.operation_version!r}, "
                f"ApprovalRequest has {approval_request.operation_version!r}"
            )
        if (
            operation_identity.canonical_payload_hash
            != approval_request.canonical_payload_hash
        ):
            raise EvaluationApprovalRequiredError(
                f"ApprovalRequest payload hash mismatch: expected {operation_identity.canonical_payload_hash!r}, "
                f"got {approval_request.canonical_payload_hash!r}"
            )
        if operation_identity.target_identity != approval_request.resource_scope:
            raise EvaluationApprovalRequiredError(
                f"Target identity / resource scope mismatch: OperationIdentity has {operation_identity.target_identity!r}, "
                f"ApprovalRequest has {approval_request.resource_scope!r}"
            )

        # 3. Verify approval decision matches approval request & operation identity
        if approval_decision.request_id != approval_request.request_id:
            raise EvaluationApprovalRequiredError(
                f"Approval decision request_id mismatch: expected {approval_request.request_id!r}, "
                f"got {approval_decision.request_id!r}"
            )
        if approval_decision.decision != ApprovalStatus.APPROVED:
            raise EvaluationApprovalRequiredError(
                f"Approval status is not APPROVED: got {approval_decision.decision.value!r}"
            )
        if (
            approval_decision.bound_payload_hash
            != operation_identity.canonical_payload_hash
        ):
            raise EvaluationApprovalRequiredError(
                f"ApprovalDecision bound_payload_hash mismatch: expected {operation_identity.canonical_payload_hash!r}, "
                f"got {approval_decision.bound_payload_hash!r}"
            )
        if approval_decision.bound_resource_scope != approval_request.resource_scope:
            raise EvaluationApprovalRequiredError(
                f"ApprovalDecision bound_resource_scope mismatch: expected {approval_request.resource_scope!r}, "
                f"got {approval_decision.bound_resource_scope!r}"
            )
        if approval_decision.bound_policy_version != approval_request.policy_version:
            raise EvaluationApprovalRequiredError(
                f"ApprovalDecision bound_policy_version mismatch: expected {approval_request.policy_version!r}, "
                f"got {approval_decision.bound_policy_version!r}"
            )

        # 4. Target ID consistency check (PM hard invariant)
        if target_id is not None:
            scope = approval_request.resource_scope
            if ":" in scope:
                prefix, scope_target = scope.split(":", 1)
                if prefix == "target" and scope_target != str(target_id):
                    raise EvaluationApprovalRequiredError(
                        f"Supplied target_id {target_id!r} conflicts with authorized scope target {scope_target!r}"
                    )
            elif str(operation_identity.target_identity).startswith("target:"):
                _, op_target = str(operation_identity.target_identity).split(":", 1)
                if op_target != str(target_id):
                    raise EvaluationApprovalRequiredError(
                        f"Supplied target_id {target_id!r} conflicts with authorized operation target {op_target!r}"
                    )

    def execute(
        self,
        plan: EvaluationPlan,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        target_id: TargetId | None = None,
        benchmark_tool_id: ToolId | None = None,
    ) -> EvaluationResult:
        """Execute evaluation plan by verifying approval and delegating execution transport to ADR-0005.

        Invariants:
        1. Approval verification is performed strictly against authoritative ADR-0003 credentials.
        2. Target identity is derived from the authoritative approval context if not explicitly passed.
           Local target synthesis is strictly prohibited.
        3. ToolExecutor is mandatory; execute() cannot return a result without actual ADR-0005 dispatch.
        4. Evaluation Subsystem owns measurement semantics and verdicts; orchestration lifecycle remains ADR-0003.
        """
        if tool_executor is None:
            raise EvaluationValidationError(
                "ToolExecutor is mandatory for execute() to delegate execution transport to ADR-0005"
            )

        self.validate_plan(plan)
        self.verify_approval(
            plan=plan,
            operation_identity=operation_identity,
            approval_request=approval_request,
            approval_decision=approval_decision,
            target_id=target_id,
        )

        # Authoritative target resolution (zero local synthesis):
        resolved_target_id = target_id
        if resolved_target_id is None:
            scope = approval_request.resource_scope
            if scope.startswith("target:"):
                resolved_target_id = TargetId(scope.split(":", 1)[1])
            elif str(operation_identity.target_identity).startswith("target:"):
                resolved_target_id = TargetId(
                    str(operation_identity.target_identity).split(":", 1)[1]
                )

        if resolved_target_id is None or not str(resolved_target_id).strip():
            raise EvaluationApprovalRequiredError(
                "No authorized execution target_id supplied or derivable from authoritative approval context. "
                "Local target synthesis is strictly prohibited."
            )

        invocation = self.prepare_invocation(
            plan,
            tool_id,
            target_id=resolved_target_id,
            approval_authorized=True,
        )
        result = self._dispatch_invocation(tool_executor, invocation)

        if result.status != ToolExecutionStatus.SUCCESS:
            raise EvaluationError(
                f"Tool execution failed for tool {tool_id!r}: {result.error_message}"
            )

        # Parse tool result into immutable EvaluationResult
        return self._build_evaluation_result(
            plan,
            resolved_target_id,
            result,
            tool_executor=tool_executor,
            tool_id=tool_id,
            benchmark_tool_id=benchmark_tool_id,
            approval_authorized=True,
        )

    def _build_evaluation_result(
        self,
        plan: EvaluationPlan,
        target_id: TargetId,
        tool_result: ToolResult,
        tool_executor: ToolExecutor | None = None,
        tool_id: ToolId | None = None,
        benchmark_tool_id: ToolId | None = None,
        approval_authorized: bool = False,
    ) -> EvaluationResult:
        """Assemble typed EvaluationResult from execution outputs."""
        output_data = tool_result.structured_output or {}

        # 1. Environment record (B1: Empirical reproducibility provenance, zero synthetic defaults)
        env_raw = output_data.get("environment")
        if env_raw is None or not env_raw:
            raise EvaluationValidationError(
                "Missing authoritative environment telemetry in evaluation output. "
                "Reproducibility metadata is mandatory per [P§25]."
            )

        if isinstance(env_raw, EvaluationEnvironmentRecord):
            env = env_raw
        elif isinstance(env_raw, (dict, Mapping)):
            env_dict = dict(env_raw)
            if "dataset_manifest_hash" in env_dict and str(
                env_dict["dataset_manifest_hash"]
            ) != str(plan.dataset_binding.dataset_manifest_hash):
                raise EvaluationValidationError(
                    f"Environment dataset_manifest_hash mismatch: expected {plan.dataset_binding.dataset_manifest_hash!r}, "
                    f"got {env_dict['dataset_manifest_hash']!r}"
                )
            if "weights_hash" in env_dict and str(env_dict["weights_hash"]) != str(
                plan.model_artifact.weights_hash
            ):
                raise EvaluationValidationError(
                    f"Environment weights_hash mismatch: expected {plan.model_artifact.weights_hash!r}, "
                    f"got {env_dict['weights_hash']!r}"
                )
            env_dict.setdefault(
                "dataset_manifest_hash", plan.dataset_binding.dataset_manifest_hash
            )
            env_dict.setdefault("weights_hash", plan.model_artifact.weights_hash)
            env_dict.setdefault(
                "metric_definition_versions", tuple(m.version for m in plan.metrics)
            )
            try:
                env = EvaluationEnvironmentRecord.model_validate(env_dict)
            except Exception as exc:
                raise EvaluationValidationError(
                    f"Invalid environment telemetry: {exc}"
                ) from exc
        else:
            raise EvaluationValidationError(
                f"Invalid environment telemetry type: {type(env_raw).__name__}"
            )

        if str(env.dataset_manifest_hash) != str(
            plan.dataset_binding.dataset_manifest_hash
        ):
            raise EvaluationValidationError(
                f"Environment dataset_manifest_hash mismatch: expected {plan.dataset_binding.dataset_manifest_hash!r}, "
                f"got {env.dataset_manifest_hash!r}"
            )
        if str(env.weights_hash) != str(plan.model_artifact.weights_hash):
            raise EvaluationValidationError(
                f"Environment weights_hash mismatch: expected {plan.model_artifact.weights_hash!r}, "
                f"got {env.weights_hash!r}"
            )

        # 2. Quality metrics
        metrics = self.metric_calculator.compute(
            output_data.get("predictions", {}),
            output_data.get("ground_truth", {}),
            plan.metrics,
        )

        # 3. System benchmarks (if benchmark_config is declared)
        benchmarks: list[BenchmarkResult] = []
        if plan.benchmark_config is not None:
            raw_benchmarks = output_data.get("system_benchmarks")
            raw_observations = output_data.get("benchmark_observations")
            if raw_benchmarks is not None:
                for b in raw_benchmarks:
                    if isinstance(b, BenchmarkResult):
                        bench_obj = b
                    elif isinstance(b, (dict, Mapping)):
                        bench_obj = BenchmarkResult.model_validate(b)
                    else:
                        raise BenchmarkExecutionError(
                            f"Invalid benchmark item in system_benchmarks: {b!r}"
                        )

                    # B3: Strict target_id, benchmark_id, precision, and batch_size binding
                    if bench_obj.target_id != target_id:
                        raise BenchmarkExecutionError(
                            f"Benchmark result target_id {bench_obj.target_id!r} does not match "
                            f"authorized target_id {target_id!r}"
                        )
                    if bench_obj.benchmark_id != plan.benchmark_config.benchmark_id:
                        raise BenchmarkExecutionError(
                            f"Benchmark result benchmark_id {bench_obj.benchmark_id!r} does not match "
                            f"plan benchmark_id {plan.benchmark_config.benchmark_id!r}"
                        )
                    if bench_obj.precision != plan.benchmark_config.precision:
                        raise BenchmarkExecutionError(
                            f"Benchmark result precision {bench_obj.precision!r} does not match "
                            f"plan precision {plan.benchmark_config.precision!r}"
                        )
                    if bench_obj.batch_size not in plan.benchmark_config.batch_sizes:
                        raise BenchmarkExecutionError(
                            f"Benchmark result batch_size {bench_obj.batch_size} is not among "
                            f"plan configured batch_sizes {plan.benchmark_config.batch_sizes}"
                        )

                    req = plan.benchmark_config.target_requirements
                    plan_is_accel = (
                        req.required_hardware_class is not None
                        and req.required_hardware_class != HardwareClass.CPU
                    ) or (req.min_cuda_compute_capability is not None)
                    if plan_is_accel and bench_obj.timing_synchronized is not True:
                        raise BenchmarkExecutionError(
                            f"Hardware-synchronized timing is required for accelerator benchmark on target {target_id} "
                            f"per ADR-0011 §3.13, but timing_synchronized is {bench_obj.timing_synchronized!r}."
                        )
                    benchmarks.append(bench_obj)
            elif raw_observations is not None:
                if isinstance(raw_observations, list):
                    for obs in raw_observations:
                        bs = int(
                            obs.get("batch_size", plan.benchmark_config.batch_sizes[0])
                        )
                        if bs not in plan.benchmark_config.batch_sizes:
                            raise BenchmarkExecutionError(
                                f"Observation batch_size {bs} is not among plan batch_sizes {plan.benchmark_config.batch_sizes}"
                            )
                        benchmarks.append(
                            self.benchmark_runner.compute_result_from_observation(
                                plan.benchmark_config, target_id, bs, obs
                            )
                        )
                elif isinstance(raw_observations, dict):
                    for bs_str, obs in raw_observations.items():
                        bs = int(bs_str)
                        if bs not in plan.benchmark_config.batch_sizes:
                            raise BenchmarkExecutionError(
                                f"Observation batch_size {bs} is not among plan batch_sizes {plan.benchmark_config.batch_sizes}"
                            )
                        benchmarks.append(
                            self.benchmark_runner.compute_result_from_observation(
                                plan.benchmark_config, target_id, bs, obs
                            )
                        )
            elif tool_executor is not None:
                effective_tool_id = benchmark_tool_id or tool_id
                if effective_tool_id is None:
                    raise BenchmarkExecutionError(
                        "tool_id is mandatory for benchmark execution. "
                        "Local synthesis of ToolId is strictly prohibited."
                    )
                benchmarks.extend(
                    self.benchmark_runner.run_benchmark(
                        model_artifact=plan.model_artifact,
                        config=plan.benchmark_config,
                        target_id=target_id,
                        tool_executor=tool_executor,
                        tool_id=effective_tool_id,
                        approval_authorized=approval_authorized,
                    )
                )

        # 4. Error analysis
        diag = self.error_analyzer.analyze(
            output_data.get("predictions", {}),
            output_data.get("ground_truth", {}),
            plan.dataset_binding,
        )
        if str(env.weights_hash) != str(plan.model_artifact.weights_hash):
            raise EvaluationValidationError(
                f"Environment weights_hash mismatch: expected {plan.model_artifact.weights_hash!r}, "
                f"got {env.weights_hash!r}"
            )

        # 5. Verdict derivation (B5: Derived from empirical quality and systems criteria)
        verdict = compute_evaluation_verdict(
            plan=plan,
            quality_metrics=metrics,
            system_benchmarks=benchmarks,
            baseline_comparison=None,
        )

        return EvaluationResult(
            evaluation_id=plan.evaluation_id,
            plan=plan,
            verdict=verdict,
            quality_metrics=metrics,
            system_benchmarks=tuple(benchmarks),
            baseline_comparison=None,
            error_analysis=diag,
            environment=env,
            completed_at=datetime.now(timezone.utc),
        )


def compute_evaluation_verdict(
    plan: EvaluationPlan,
    quality_metrics: Sequence[MetricResult],
    system_benchmarks: Sequence[BenchmarkResult],
    baseline_comparison: Any | None = None,
) -> EvaluationVerdict:
    """Derive domain EvaluationVerdict strictly from empirical quality and systems criteria."""
    # 1. Missing evidence check
    if not quality_metrics:
        return EvaluationVerdict.INCONCLUSIVE

    if plan.benchmark_config is not None and not system_benchmarks:
        return EvaluationVerdict.INCONCLUSIVE

    reqs = plan.evaluation_requirements

    # If baseline comparison is required by plan
    if plan.baseline_experiment_id is not None and reqs.get(
        "require_baseline_comparison", False
    ):
        if baseline_comparison is None:
            return EvaluationVerdict.INCONCLUSIVE
        if baseline_comparison.statistically_significant is False:
            return EvaluationVerdict.FAILED_QUALITY
        if (
            reqs.get("require_quality_improvement", False)
            and not baseline_comparison.quality_improved
        ):
            return EvaluationVerdict.FAILED_QUALITY

    # 2. Quality criteria checks
    quality_thresholds: dict[str, float] = {}
    if "quality_thresholds" in reqs and isinstance(reqs["quality_thresholds"], Mapping):
        quality_thresholds.update(
            {str(k): float(v) for k, v in reqs["quality_thresholds"].items()}
        )
    if "min_quality" in reqs:
        min_q = reqs["min_quality"]
        if isinstance(min_q, (int, float)):
            for qm in quality_metrics:
                quality_thresholds.setdefault(str(qm.metric.metric_id), float(min_q))
        elif isinstance(min_q, Mapping):
            quality_thresholds.update({str(k): float(v) for k, v in min_q.items()})

    for qm in quality_metrics:
        m_key = str(qm.metric.metric_id)
        m_name = qm.metric.name
        threshold = quality_thresholds.get(m_key)
        if threshold is None:
            threshold = quality_thresholds.get(m_name)
        if threshold is not None:
            if qm.metric.higher_is_better:
                if qm.primary_value < threshold:
                    return EvaluationVerdict.FAILED_QUALITY
            else:
                if qm.primary_value > threshold:
                    return EvaluationVerdict.FAILED_QUALITY

    # 3. Systems SLA checks
    system_sla = (
        dict(reqs.get("system_sla", {}))
        if isinstance(reqs.get("system_sla"), Mapping)
        else {}
    )
    max_latency_ms = reqs.get("max_latency_ms", system_sla.get("max_latency_ms"))
    min_throughput_fps = reqs.get(
        "min_throughput_fps", system_sla.get("min_throughput_fps")
    )
    max_peak_vram_gb = reqs.get("max_peak_vram_gb", system_sla.get("max_peak_vram_gb"))

    if max_latency_ms is not None:
        max_lat = float(max_latency_ms)
        for b in system_benchmarks:
            if b.latency_p95_ms > max_lat or b.latency_mean_ms > max_lat:
                return EvaluationVerdict.FAILED_SYSTEMS

    if min_throughput_fps is not None:
        min_fps = float(min_throughput_fps)
        for b in system_benchmarks:
            if b.throughput_fps < min_fps:
                return EvaluationVerdict.FAILED_SYSTEMS

    if max_peak_vram_gb is not None:
        max_vram = float(max_peak_vram_gb)
        for b in system_benchmarks:
            if b.peak_vram_gb is not None and b.peak_vram_gb > max_vram:
                return EvaluationVerdict.FAILED_SYSTEMS

    required_metrics = reqs.get("required_metrics")
    if required_metrics:
        evaluated_ids = {str(m.metric.metric_id) for m in quality_metrics}
        evaluated_names = {m.metric.name for m in quality_metrics}
        for rm in required_metrics:
            if str(rm) not in evaluated_ids and str(rm) not in evaluated_names:
                return EvaluationVerdict.INCONCLUSIVE

    return EvaluationVerdict.PASSED


def parse_evaluation_result(
    tool_output: Any,
    plan: EvaluationPlan,
    *,
    execution_id: ExecutionId | None = None,
    target_id: TargetId | None = None,
) -> EvaluationResult:
    """Stateless parser assembling an EvaluationResult from completed ADR-0005 tool execution data."""
    if isinstance(tool_output, EvaluationResult):
        return tool_output

    if not isinstance(tool_output, (dict, Mapping)):
        raise EvaluationValidationError(
            f"Expected tool_output mapping or EvaluationResult, got {type(tool_output).__name__}"
        )

    resolved_target_id = target_id
    if resolved_target_id is None:
        raw_tid = tool_output.get("target_id")
        if raw_tid is not None and str(raw_tid).strip():
            resolved_target_id = TargetId(str(raw_tid).strip())
        else:
            raise EvaluationValidationError(
                "Cannot parse evaluation result without an authorized target_id. "
                "Local target synthesis is strictly prohibited."
            )

    resolved_exec_id = execution_id
    if resolved_exec_id is None:
        raw_eid = tool_output.get("execution_id")
        if raw_eid is not None and str(raw_eid).strip():
            resolved_exec_id = ExecutionId(str(raw_eid).strip())
        else:
            raise EvaluationValidationError(
                "Cannot parse evaluation result without an authoritative execution_id. "
                "Local execution synthesis is strictly prohibited."
            )

    runner = DefaultEvaluationRunner()
    dummy_result = ToolResult(
        tool_id=ToolId("cv.eval.execute"),
        execution_id=resolved_exec_id,
        target_id=resolved_target_id,
        status=ToolExecutionStatus.SUCCESS,
        structured_output=dict(tool_output),
    )
    return runner._build_evaluation_result(
        plan, resolved_target_id, dummy_result, tool_id=dummy_result.tool_id
    )
