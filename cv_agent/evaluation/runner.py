"""cv_agent.evaluation.runner — High-level evaluation runner and dispatch for ADR-0011."""

from __future__ import annotations

import asyncio
import inspect
import uuid
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
)
from cv_agent.evaluation.diagnostics import DefaultErrorAnalyzer, ErrorAnalyzer
from cv_agent.evaluation.errors import (
    BenchmarkExecutionError,
    EvaluationApprovalRequiredError,
    EvaluationError,
    EvaluationValidationError,
)
from cv_agent.evaluation.metrics import MetricCalculator, StandardMetricCalculator
from cv_agent.evaluation.types import EvaluationVerdict
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

        # 1. Quality metrics
        metrics = self.metric_calculator.compute(
            output_data.get("predictions", {}),
            output_data.get("ground_truth", {}),
            plan.metrics,
        )

        # 2. System benchmarks (if benchmark_config is declared)
        benchmarks: list[BenchmarkResult] = []
        if plan.benchmark_config is not None:
            raw_benchmarks = output_data.get("system_benchmarks")
            raw_observations = output_data.get("benchmark_observations")
            if raw_benchmarks is not None:
                for b in raw_benchmarks:
                    if isinstance(b, BenchmarkResult):
                        benchmarks.append(b)
                    elif isinstance(b, dict):
                        benchmarks.append(BenchmarkResult.model_validate(b))
            elif raw_observations is not None:
                if isinstance(raw_observations, list):
                    for obs in raw_observations:
                        bs = int(
                            obs.get("batch_size", plan.benchmark_config.batch_sizes[0])
                        )
                        benchmarks.append(
                            self.benchmark_runner.compute_result_from_observation(
                                plan.benchmark_config, target_id, bs, obs
                            )
                        )
                elif isinstance(raw_observations, dict):
                    for bs_str, obs in raw_observations.items():
                        bs = int(bs_str)
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

        # 3. Error analysis
        diag = self.error_analyzer.analyze(
            output_data.get("predictions", {}),
            output_data.get("ground_truth", {}),
            plan.dataset_binding,
        )

        # 4. Environment record
        env = EvaluationEnvironmentRecord(
            git_commit_sha="0" * 40,
            git_is_dirty=False,
            dataset_manifest_hash=plan.dataset_binding.dataset_manifest_hash,
            weights_hash=plan.model_artifact.weights_hash,
            metric_definition_versions=tuple(m.version for m in plan.metrics),
            python_version="3.11.0",
            torch_version="2.4.0",
            target_hardware="Target Hardware",
        )

        return EvaluationResult(
            evaluation_id=plan.evaluation_id,
            plan=plan,
            verdict=EvaluationVerdict.PASSED,
            quality_metrics=metrics,
            system_benchmarks=tuple(benchmarks),
            baseline_comparison=None,
            error_analysis=diag,
            environment=env,
            completed_at=datetime.now(timezone.utc),
        )


def parse_evaluation_result(tool_output: Any, plan: EvaluationPlan) -> EvaluationResult:
    """Stateless parser assembling an EvaluationResult from completed ADR-0005 tool execution data."""
    if isinstance(tool_output, EvaluationResult):
        return tool_output
    runner = DefaultEvaluationRunner()
    dummy_result = ToolResult(
        tool_id=ToolId("cv.eval.execute"),
        execution_id=ExecutionId("exec-parse"),
        target_id=TargetId("target-parsed"),
        status=ToolExecutionStatus.SUCCESS,
        structured_output=tool_output if isinstance(tool_output, dict) else {},
    )
    return runner._build_evaluation_result(
        plan, TargetId("target-parsed"), dummy_result, tool_id=dummy_result.tool_id
    )
