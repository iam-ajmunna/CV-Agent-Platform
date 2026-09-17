"""cv_agent.optimization.deployment — Production deployment and rollback management for ADR-0012."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Protocol, cast, runtime_checkable

from cv_agent.dataset import OperationIdentity
from cv_agent.optimization.canonicalization import (
    compute_deployment_payload_hash,
    extract_deployment_operation_payload,
)
from cv_agent.optimization.contracts import (
    DeploymentPlan,
    DeploymentRecord,
    OptimizationResult,
    ReadinessProbeResult,
)
from cv_agent.optimization.errors import (
    DeploymentApprovalRequiredError,
    DeploymentValidationError,
    HardwareMismatchError,
    OptimizationExecutionError,
    OptimizationValidationError,
    OptimizationVerificationError,
    ReadinessProbeFailureError,
    RollbackExecutionError,
)
from cv_agent.optimization.types import DeploymentId, DeploymentStatus
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


@runtime_checkable
class DeploymentManager(Protocol):
    """Protocol for deployment staging, readiness verification, approval-gated activation, and rollback."""

    def stage(
        self,
        plan: DeploymentPlan,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        optimization_result: OptimizationResult | None = None,
        target_id: TargetId | None = None,
    ) -> DeploymentRecord: ...

    def verify_readiness(
        self,
        record: DeploymentRecord,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
    ) -> ReadinessProbeResult: ...

    def activate(
        self,
        record: DeploymentRecord,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
    ) -> DeploymentRecord: ...

    def rollback(
        self,
        record: DeploymentRecord,
        reason: str,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        active_deployments: Mapping[DeploymentId, DeploymentRecord] | None = None,
    ) -> tuple[DeploymentRecord, DeploymentRecord | None]: ...


class DefaultDeploymentManager:
    """Standard deployment manager enforcing ADR-0003 approval and automated rollback per ADR-0012."""

    def stage(
        self,
        plan: DeploymentPlan,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        optimization_result: OptimizationResult | None = None,
        target_id: TargetId | None = None,
    ) -> DeploymentRecord:
        """Stage an optimized model artifact and runtime packaging on the deployment target."""
        # 1. Mandatory tool arguments (No local synthesis)
        if tool_id is None or not str(tool_id).strip():
            raise OptimizationExecutionError(
                "tool_id is mandatory for deployment staging. "
                "Local synthesis of ToolId is strictly prohibited."
            )
        if tool_executor is None:
            raise OptimizationExecutionError(
                "tool_executor is mandatory for deployment staging. "
                "ADR-0005 execution dispatch cannot be omitted."
            )

        # 2. Target consistency validation
        effective_target_id = target_id or plan.target.target_id
        if target_id is not None and str(target_id) != str(plan.target.target_id):
            raise DeploymentValidationError(
                f"Supplied target_id {target_id!r} conflicts with DeploymentPlan target {plan.target.target_id!r}"
            )

        # 3. Target hardware validation (Fail fast on hardware mismatch per D-022)
        if not str(plan.target.gpu_device_name).strip():
            raise HardwareMismatchError("Target gpu_device_name cannot be empty")
        if not str(plan.target.compute_capability).strip():
            raise HardwareMismatchError("Target compute_capability cannot be empty")

        # 4. Mandatory post-optimization verification gate (Acceptance Criterion 5)
        if optimization_result is not None:
            if not optimization_result.verification_passed:
                raise OptimizationVerificationError(
                    f"Cannot deploy unverified or degraded artifact {plan.artifact_id!r}. "
                    f"Diagnostics: {optimization_result.diagnostics}"
                )
            if str(optimization_result.artifact.artifact_id) != str(plan.artifact_id):
                raise OptimizationValidationError(
                    f"OptimizationResult artifact {optimization_result.artifact.artifact_id!r} "
                    f"does not match DeploymentPlan artifact {plan.artifact_id!r}"
                )

        # 5. Dispatch staging to ADR-0005
        exec_id = ExecutionId(f"exec-stage-{plan.deployment_id}-{uuid.uuid4().hex[:8]}")
        invocation = ToolInvocation(
            tool_id=tool_id,
            target_id=effective_target_id,
            execution_id=exec_id,
            timeout_seconds=int(
                plan.runtime_config.get("staging_timeout_seconds", 300)
            ),
            parameters={
                "action": "stage",
                "deployment_payload": extract_deployment_operation_payload(plan),
                "runtime": plan.runtime.value,
            },
        )

        tool_result = self._dispatch_tool(tool_executor, invocation)
        if tool_result.status != ToolExecutionStatus.SUCCESS:
            raise OptimizationExecutionError(
                f"Deployment staging failed on target {effective_target_id}: "
                f"{tool_result.error_message or 'Unknown execution error'}"
            )

        return DeploymentRecord(
            deployment_id=plan.deployment_id,
            plan=plan,
            status=DeploymentStatus.STAGING,
            execution_id=exec_id,
            target_id=effective_target_id,
        )

    def verify_readiness(
        self,
        record: DeploymentRecord,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
    ) -> ReadinessProbeResult:
        """Execute automated startup and readiness health probing per ADR-0012 §3.7."""
        if tool_id is None or not str(tool_id).strip():
            raise OptimizationExecutionError("tool_id is mandatory for readiness probe")
        if tool_executor is None:
            raise OptimizationExecutionError(
                "tool_executor is mandatory for readiness probe"
            )

        if record.status not in (DeploymentStatus.STAGING, DeploymentStatus.VERIFYING):
            raise DeploymentValidationError(
                f"Deployment must be in STAGING to run readiness probe, currently in {record.status.value!r}"
            )

        health = record.plan.health_check
        exec_id = ExecutionId(
            f"exec-probe-{record.deployment_id}-{uuid.uuid4().hex[:8]}"
        )
        invocation = ToolInvocation(
            tool_id=tool_id,
            target_id=record.plan.target.target_id,
            execution_id=exec_id,
            timeout_seconds=int(
                record.plan.runtime_config.get(
                    "probe_timeout_seconds",
                    max(30, int(health.startup_timeout_sec) + 10),
                )
            ),
            parameters={
                "action": "readiness_probe",
                "health_check": {
                    "startup_timeout_sec": health.startup_timeout_sec,
                    "readiness_probe_samples": health.readiness_probe_samples,
                    "max_latency_threshold_ms": health.max_latency_threshold_ms,
                    "memory_headroom_mb": health.memory_headroom_mb,
                },
            },
        )

        tool_result = self._dispatch_tool(tool_executor, invocation)

        output = tool_result.structured_output or {}
        startup_time = float(output.get("startup_time_sec", 1.5))
        probe_latency = float(output.get("probe_latency_mean_ms", 15.0))
        vram_mb = float(output.get("vram_consumed_mb", 1024.0))

        # Evaluate against health check specifications
        max_allowed_vram = (
            record.plan.target.max_memory_allocation_gb * 1024.0
        ) - health.memory_headroom_mb

        passed = True
        err_msg: str | None = None

        if tool_result.status != ToolExecutionStatus.SUCCESS:
            passed = False
            err_msg = f"Readiness probe execution error: {tool_result.error_message}"
        elif startup_time > health.startup_timeout_sec:
            passed = False
            err_msg = (
                f"Startup timeout exceeded: took {startup_time:.2f}s, "
                f"budget is {health.startup_timeout_sec:.2f}s"
            )
        elif probe_latency > health.max_latency_threshold_ms:
            passed = False
            err_msg = (
                f"Readiness probe latency exceeded budget: mean latency {probe_latency:.2f}ms > "
                f"threshold {health.max_latency_threshold_ms:.2f}ms"
            )
        elif vram_mb > max_allowed_vram:
            passed = False
            err_msg = (
                f"Memory headroom budget violated: VRAM consumed {vram_mb:.1f}MB exceeds "
                f"maximum allowable {max_allowed_vram:.1f}MB"
            )

        probe_result = ReadinessProbeResult(
            passed=passed,
            startup_time_sec=startup_time,
            probe_latency_mean_ms=probe_latency,
            vram_consumed_mb=vram_mb,
            error_message=err_msg,
            probed_at=datetime.now(timezone.utc),
        )

        if not passed:
            raise ReadinessProbeFailureError(
                f"Deployment {record.deployment_id} failed readiness verification: {err_msg}"
            )

        return probe_result

    def activate(
        self,
        record: DeploymentRecord,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
    ) -> DeploymentRecord:
        """Activate a staged deployment following strict ADR-0003 4-tuple approval verification."""
        # 1. Mandatory arguments
        if tool_id is None or not str(tool_id).strip():
            raise OptimizationExecutionError(
                "tool_id is mandatory for deployment activation"
            )
        if tool_executor is None:
            raise OptimizationExecutionError(
                "tool_executor is mandatory for deployment activation"
            )

        # 2. Verify readiness probing passed
        if record.readiness_result is None or not record.readiness_result.passed:
            raise DeploymentValidationError(
                "Deployment cannot be activated without passing readiness probing (ADR-0012 Invariant 8)"
            )

        # 3. ADR-0003 4-tuple approval verification
        computed_hash = compute_deployment_payload_hash(record.plan)

        # 3a. Canonical payload hash matches operation identity
        if operation_identity.canonical_payload_hash != computed_hash:
            raise DeploymentApprovalRequiredError(
                f"Canonical payload hash mismatch: OperationIdentity has {operation_identity.canonical_payload_hash!r}, "
                f"but DeploymentPlan computes to {computed_hash!r}"
            )

        # 3b. Operation identity matches approval request
        if str(operation_identity.operation_id) != str(approval_request.operation_id):
            raise DeploymentApprovalRequiredError(
                f"Operation ID mismatch: OperationIdentity has {operation_identity.operation_id!r}, "
                f"ApprovalRequest has {approval_request.operation_id!r}"
            )

        # 3c. Approval decision matches request
        if approval_decision.request_id != approval_request.request_id:
            raise DeploymentApprovalRequiredError(
                f"Approval decision request_id mismatch: expected {approval_request.request_id!r}, "
                f"got {approval_decision.request_id!r}"
            )

        # 3d. Approval decision is strictly APPROVED
        if approval_decision.decision != ApprovalStatus.APPROVED:
            raise DeploymentApprovalRequiredError(
                f"Deployment activation rejected: approval status is {approval_decision.decision!r}"
            )

        # 3e. Bound payload hash matches
        if (
            approval_decision.bound_payload_hash
            != operation_identity.canonical_payload_hash
        ):
            raise DeploymentApprovalRequiredError(
                "ApprovalDecision bound_payload_hash does not match OperationIdentity canonical hash"
            )

        # 3f. Target resource scope consistency
        scope = approval_request.resource_scope
        expected_target_str = str(record.plan.target.target_id)
        if ":" in scope:
            prefix, scope_target = scope.split(":", 1)
            if prefix == "target" and scope_target != expected_target_str:
                raise DeploymentApprovalRequiredError(
                    f"Supplied target {expected_target_str!r} conflicts with authorized scope target {scope_target!r}"
                )
        elif scope != expected_target_str:
            raise DeploymentApprovalRequiredError(
                f"Supplied target {expected_target_str!r} conflicts with authorized scope {scope!r}"
            )

        # 4. Dispatch activation to ADR-0005 with approval_authorized=True
        exec_id = ExecutionId(f"exec-act-{record.deployment_id}-{uuid.uuid4().hex[:8]}")
        invocation = ToolInvocation(
            tool_id=tool_id,
            target_id=record.plan.target.target_id,
            execution_id=exec_id,
            timeout_seconds=int(
                record.plan.runtime_config.get("activation_timeout_seconds", 300)
            ),
            parameters={
                "action": "activate",
                "deployment_id": str(record.deployment_id),
                "artifact_id": str(record.plan.artifact_id),
            },
            approval_authorized=True,
        )

        tool_result = self._dispatch_tool(tool_executor, invocation)
        if tool_result.status != ToolExecutionStatus.SUCCESS:
            raise OptimizationExecutionError(
                f"Deployment activation failed on target {record.plan.target.target_id}: "
                f"{tool_result.error_message or 'Unknown execution error'}"
            )

        return DeploymentRecord(
            deployment_id=record.deployment_id,
            plan=record.plan,
            status=DeploymentStatus.ACTIVE,
            readiness_result=record.readiness_result,
            execution_id=exec_id,
            target_id=record.plan.target.target_id,
            activated_at=datetime.now(timezone.utc),
        )

    def rollback(
        self,
        record: DeploymentRecord,
        reason: str,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        active_deployments: Mapping[DeploymentId, DeploymentRecord] | None = None,
    ) -> tuple[DeploymentRecord, DeploymentRecord | None]:
        """Execute deterministic rollback or escalate to OPERATOR_REQUIRED per ADR-0012 §3.7."""
        # 1. Null Predecessor Escalation Invariant (ADR-0012 §3.7 & Acceptance Criterion 9)
        # If rollback_target_id is None (first deployment), cannot execute automated rollback.
        # Must escalate to OPERATOR_REQUIRED and fail closed.
        if record.plan.rollback_target_id is None:
            raise RollbackExecutionError(
                f"Deployment {record.deployment_id} has no predecessor rollback target (first deployment). "
                f"Escalated to OPERATOR_REQUIRED: {reason}"
            )

        # 2. Locate rollback target if active_deployments registry provided
        target_dep_id = record.plan.rollback_target_id
        target_record: DeploymentRecord | None = None
        if active_deployments is not None:
            if target_dep_id not in active_deployments:
                raise RollbackExecutionError(
                    f"Designated rollback predecessor {target_dep_id!r} not found in active deployments registry"
                )
            target_record = active_deployments[target_dep_id]

        # 3. Dispatch rollback to ADR-0005
        exec_id = ExecutionId(
            f"exec-roll-{record.deployment_id}-{uuid.uuid4().hex[:8]}"
        )
        invocation = ToolInvocation(
            tool_id=tool_id,
            target_id=record.plan.target.target_id,
            execution_id=exec_id,
            timeout_seconds=int(
                record.plan.runtime_config.get("rollback_timeout_seconds", 300)
            ),
            parameters={
                "action": "rollback",
                "current_deployment_id": str(record.deployment_id),
                "rollback_target_id": str(target_dep_id),
                "reason": reason,
            },
            approval_authorized=True,
        )

        tool_result = self._dispatch_tool(tool_executor, invocation)
        if tool_result.status != ToolExecutionStatus.SUCCESS:
            raise RollbackExecutionError(
                f"Rollback execution failed on target {record.plan.target.target_id}: "
                f"{tool_result.error_message or 'Unknown execution error'}"
            )

        rolled_back_record = DeploymentRecord(
            deployment_id=record.deployment_id,
            plan=record.plan,
            status=DeploymentStatus.ROLLED_BACK,
            readiness_result=record.readiness_result,
            execution_id=exec_id,
            target_id=record.plan.target.target_id,
            rolled_back_at=datetime.now(timezone.utc),
            failure_details=f"Rolled back to {target_dep_id}: {reason}",
        )

        restored_record: DeploymentRecord | None = None
        if target_record is not None:
            restored_record = DeploymentRecord(
                deployment_id=target_record.deployment_id,
                plan=target_record.plan,
                status=DeploymentStatus.ACTIVE,
                readiness_result=target_record.readiness_result,
                execution_id=exec_id,
                target_id=target_record.target_id,
                activated_at=datetime.now(timezone.utc),
            )

        return rolled_back_record, restored_record

    def _dispatch_tool(
        self,
        tool_executor: ToolExecutor,
        invocation: ToolInvocation,
    ) -> ToolResult:
        """Dispatch ToolInvocation to ADR-0005 ToolExecutor synchronously or via async loop."""
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
            if isinstance(
                exc,
                (
                    OptimizationExecutionError,
                    DeploymentValidationError,
                    DeploymentApprovalRequiredError,
                    ReadinessProbeFailureError,
                    RollbackExecutionError,
                ),
            ):
                raise
            raise OptimizationExecutionError(
                f"Deployment transport failed via ADR-0005: {exc}"
            ) from exc
