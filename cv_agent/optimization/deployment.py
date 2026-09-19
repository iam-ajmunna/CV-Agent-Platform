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
    _ensure_finite_float,
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
        target_id: TargetId,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        optimization_result: OptimizationResult,
        active_deployments: Mapping[DeploymentId, DeploymentRecord] | None = None,
    ) -> DeploymentRecord: ...

    def verify_readiness(
        self,
        record: DeploymentRecord,
        target_id: TargetId,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
    ) -> tuple[ReadinessProbeResult, DeploymentRecord]: ...

    def activate(
        self,
        record: DeploymentRecord,
        target_id: TargetId,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        active_deployments: Mapping[DeploymentId, DeploymentRecord] | None = None,
    ) -> DeploymentRecord: ...

    def rollback(
        self,
        record: DeploymentRecord,
        target_id: TargetId,
        reason: str,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        approval_authorized: bool = False,
        active_deployments: dict[DeploymentId, DeploymentRecord] | None = None,
    ) -> tuple[DeploymentRecord, DeploymentRecord | None]: ...


class DefaultDeploymentManager:
    """Standard deployment manager enforcing ADR-0003 approval and automated rollback per ADR-0012."""

    def __init__(
        self,
        safety_index: dict[DeploymentId, DeploymentRecord] | None = None,
    ) -> None:
        """Initialize DefaultDeploymentManager with an optional process-local operational safety index (B13)."""
        self._safety_index: dict[DeploymentId, DeploymentRecord] = (
            dict(safety_index) if safety_index is not None else {}
        )

    def _check_target_lockout(
        self,
        target_id: TargetId,
        caller_deployments: Mapping[DeploymentId, DeploymentRecord] | None = None,
    ) -> None:
        """Enforce process-local target-scoped lockout when target is in OPERATOR_REQUIRED state (B13)."""
        target_str = str(target_id)
        records_to_check: list[DeploymentRecord] = list(self._safety_index.values())
        if caller_deployments:
            records_to_check.extend(caller_deployments.values())

        for existing in records_to_check:
            if existing.status == DeploymentStatus.OPERATOR_REQUIRED:
                existing_target_str = (
                    str(existing.target_id)
                    if existing.target_id is not None
                    else str(existing.plan.target.target_id)
                )
                if existing_target_str == target_str:
                    raise DeploymentValidationError(
                        f"Deployment operation rejected: target {target_id!r} is locked out by deployment "
                        f"{existing.deployment_id!r} in OPERATOR_REQUIRED state per ADR-0012 §3.7."
                    )

    def stage(
        self,
        plan: DeploymentPlan,
        target_id: TargetId,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        optimization_result: OptimizationResult,
        active_deployments: Mapping[DeploymentId, DeploymentRecord] | None = None,
    ) -> DeploymentRecord:
        """Stage an optimized model artifact and runtime packaging on the deployment target."""
        # 1. Mandatory tool arguments (B5: External TargetId mandatory, zero local synthesis)
        if target_id is None or not str(target_id).strip():
            raise DeploymentValidationError(
                "target_id is mandatory for deployment staging. "
                "Local synthesis of TargetId is strictly prohibited."
            )
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

        # 2. Target-scoped lockout state check (B13: OPERATOR_REQUIRED blocks affected target)
        self._check_target_lockout(target_id, active_deployments)

        # 3. Target consistency validation (B5)
        if str(target_id) != str(plan.target.target_id):
            raise DeploymentValidationError(
                f"Supplied target_id {target_id!r} conflicts with DeploymentPlan target {plan.target.target_id!r}"
            )

        # 4. Target hardware validation (Fail fast on hardware mismatch per D-022)
        if not str(plan.target.gpu_device_name).strip():
            raise HardwareMismatchError("Target gpu_device_name cannot be empty")
        if not str(plan.target.compute_capability).strip():
            raise HardwareMismatchError("Target compute_capability cannot be empty")

        # 5. Mandatory post-optimization verification gate (B12: Acceptance Criterion 5)
        if optimization_result is None:
            raise OptimizationVerificationError(
                "Mandatory OptimizationResult missing for deployment staging. "
                "Optimized artifacts remain strictly provisional until verified."
            )
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

        # B16: Mandatory candidate-artifact evaluation binding verification
        if getattr(optimization_result, "candidate_artifact_binding", None) is None:
            raise OptimizationVerificationError(
                "OptimizationResult missing mandatory candidate_artifact_binding. "
                "Deployment staging strictly requires verified candidate-artifact evaluation binding per ADR-0012."
            )
        binding = optimization_result.candidate_artifact_binding
        if str(binding.candidate_artifact_id) != str(plan.artifact_id):
            raise OptimizationVerificationError(
                f"candidate_artifact_binding candidate_artifact_id {binding.candidate_artifact_id!r} "
                f"does not match DeploymentPlan artifact {plan.artifact_id!r}"
            )
        if str(binding.candidate_artifact_id) != str(
            optimization_result.artifact.artifact_id
        ):
            raise OptimizationVerificationError(
                f"candidate_artifact_binding candidate_artifact_id {binding.candidate_artifact_id!r} "
                f"does not match OptimizationResult artifact {optimization_result.artifact.artifact_id!r}"
            )
        if str(binding.candidate_artifact_hash) != str(
            optimization_result.artifact.artifact_hash
        ):
            raise OptimizationVerificationError(
                f"candidate_artifact_binding candidate_artifact_hash {binding.candidate_artifact_hash!r} "
                f"does not match OptimizationResult artifact hash {optimization_result.artifact.artifact_hash!r}"
            )
        if str(binding.evaluation_id) != str(
            optimization_result.evaluation_result.evaluation_id
        ):
            raise OptimizationVerificationError(
                f"candidate_artifact_binding evaluation_id {binding.evaluation_id!r} "
                f"does not match OptimizationResult evaluation_id {optimization_result.evaluation_result.evaluation_id!r}"
            )

        # 6. Dispatch staging to ADR-0005
        exec_id = ExecutionId(f"exec-stage-{plan.deployment_id}-{uuid.uuid4().hex[:8]}")
        invocation = ToolInvocation(
            tool_id=tool_id,
            target_id=target_id,
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
                f"Deployment staging failed on target {target_id}: "
                f"{tool_result.error_message or 'Unknown execution error'}"
            )

        staged_record = DeploymentRecord(
            deployment_id=plan.deployment_id,
            plan=plan,
            status=DeploymentStatus.STAGING,
            execution_id=exec_id,
            target_id=target_id,
        )
        # Update process-local operational safety index (B13)
        self._safety_index[staged_record.deployment_id] = staged_record
        if active_deployments is not None and isinstance(active_deployments, dict):
            active_deployments[staged_record.deployment_id] = staged_record

        return staged_record

    def verify_readiness(
        self,
        record: DeploymentRecord,
        target_id: TargetId,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
    ) -> tuple[ReadinessProbeResult, DeploymentRecord]:
        """Execute automated startup and readiness health probing per ADR-0012 §3.7."""
        # 1. Mandatory arguments (B5)
        if target_id is None or not str(target_id).strip():
            raise DeploymentValidationError(
                "target_id is mandatory for readiness probe. "
                "Local synthesis of TargetId is strictly prohibited."
            )
        if str(target_id) != str(record.target_id) or str(target_id) != str(
            record.plan.target.target_id
        ):
            raise DeploymentValidationError(
                f"Supplied target_id {target_id!r} conflicts with record target {record.target_id!r}"
            )
        if tool_id is None or not str(tool_id).strip():
            raise OptimizationExecutionError("tool_id is mandatory for readiness probe")
        if tool_executor is None:
            raise OptimizationExecutionError(
                "tool_executor is mandatory for readiness probe"
            )

        # 2. Lifecycle state check (B7: Must strictly be STAGING)
        if record.status != DeploymentStatus.STAGING:
            raise DeploymentValidationError(
                f"Deployment must be in STAGING to run readiness probe, currently in {record.status.value!r}"
            )

        health = record.plan.health_check
        exec_id = ExecutionId(
            f"exec-probe-{record.deployment_id}-{uuid.uuid4().hex[:8]}"
        )
        invocation = ToolInvocation(
            tool_id=tool_id,
            target_id=target_id,
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

        # 3. Telemetry extraction (B4: Fail-closed on missing/malformed telemetry, zero synthetic defaults)
        output = tool_result.structured_output
        if not output or not isinstance(output, Mapping):
            failed_record = record.with_transition(
                DeploymentStatus.FAILED,
                failure_details="Readiness probe tool execution output missing structured telemetry",
            )
            raise ReadinessProbeFailureError(
                f"Deployment {record.deployment_id} failed readiness verification: "
                "missing structured output/telemetry from probe tool execution",
                failed_record=failed_record,
            )

        for req_metric in (
            "startup_time_sec",
            "probe_latency_mean_ms",
            "vram_consumed_mb",
        ):
            if req_metric not in output or output[req_metric] is None:
                failed_record = record.with_transition(
                    DeploymentStatus.FAILED,
                    failure_details=f"Readiness probe telemetry missing required metric {req_metric!r}",
                )
                raise ReadinessProbeFailureError(
                    f"Deployment {record.deployment_id} failed readiness verification: "
                    f"probe telemetry missing required metric {req_metric!r}. "
                    "Synthetic fail-open defaults are prohibited per ADR-0012 §3.7.",
                    failed_record=failed_record,
                )

        try:
            startup_time = _ensure_finite_float(
                float(output["startup_time_sec"]), "startup_time_sec"
            )
            probe_latency = _ensure_finite_float(
                float(output["probe_latency_mean_ms"]), "probe_latency_mean_ms"
            )
            vram_mb = _ensure_finite_float(
                float(output["vram_consumed_mb"]), "vram_consumed_mb"
            )
        except (TypeError, ValueError) as exc:
            failed_record = record.with_transition(
                DeploymentStatus.FAILED,
                failure_details=f"Readiness probe telemetry numeric parse failure: {exc}",
            )
            raise ReadinessProbeFailureError(
                f"Deployment {record.deployment_id} failed readiness verification: {exc}",
                failed_record=failed_record,
            ) from exc

        if startup_time < 0.0 or probe_latency < 0.0 or vram_mb < 0.0:
            failed_record = record.with_transition(
                DeploymentStatus.FAILED,
                failure_details="Readiness probe telemetry metrics must be non-negative",
            )
            raise ReadinessProbeFailureError(
                f"Deployment {record.deployment_id} failed readiness verification: "
                "telemetry metrics must be non-negative",
                failed_record=failed_record,
            )

        # 4. Evaluate against health check specifications
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
            failed_record = record.with_transition(
                DeploymentStatus.FAILED,
                readiness_result=probe_result,
                failure_details=err_msg,
            )
            self._safety_index[failed_record.deployment_id] = failed_record
            raise ReadinessProbeFailureError(
                f"Deployment {record.deployment_id} failed readiness verification: {err_msg}",
                failed_record=failed_record,
            )

        # B7: Return updated record transitioned from STAGING to VERIFYING
        verifying_record = record.with_transition(
            DeploymentStatus.VERIFYING,
            readiness_result=probe_result,
        )
        self._safety_index[verifying_record.deployment_id] = verifying_record
        return probe_result, verifying_record

    def activate(
        self,
        record: DeploymentRecord,
        target_id: TargetId,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        active_deployments: Mapping[DeploymentId, DeploymentRecord] | None = None,
    ) -> DeploymentRecord:
        """Activate a staged deployment following strict ADR-0003 4-tuple approval verification."""
        # 1. Mandatory arguments (B5)
        if target_id is None or not str(target_id).strip():
            raise DeploymentValidationError(
                "target_id is mandatory for activation. "
                "Local synthesis of TargetId is strictly prohibited."
            )
        if str(target_id) != str(record.target_id) or str(target_id) != str(
            record.plan.target.target_id
        ):
            raise DeploymentValidationError(
                f"Supplied target_id {target_id!r} conflicts with record target {record.target_id!r}"
            )
        if tool_id is None or not str(tool_id).strip():
            raise OptimizationExecutionError(
                "tool_id is mandatory for deployment activation"
            )
        if tool_executor is None:
            raise OptimizationExecutionError(
                "tool_executor is mandatory for deployment activation"
            )

        # 2. Target-scoped lockout state check (B13: OPERATOR_REQUIRED blocks affected target)
        self._check_target_lockout(target_id, active_deployments)

        # 3. Lifecycle state check (B7: Must be in VERIFYING state to transition to ACTIVE)
        if record.status != DeploymentStatus.VERIFYING:
            raise DeploymentValidationError(
                f"Deployment must be in VERIFYING state to be activated, currently in {record.status.value!r}. "
                "Lifecycle must strictly follow STAGING -> VERIFYING -> ACTIVE."
            )

        # 4. Verify readiness probing passed
        if record.readiness_result is None or not record.readiness_result.passed:
            raise DeploymentValidationError(
                "Deployment cannot be activated without passing readiness probing (ADR-0012 Invariant 8)"
            )

        # 5. Strict ADR-0003 4-tuple approval verification (B6 & B15)
        # 5a. Exact operation ID validation for deployment activation (B15)
        if str(operation_identity.operation_id) != "deployment.activate":
            raise DeploymentApprovalRequiredError(
                f"Operation ID {operation_identity.operation_id!r} is not authorized for deployment activation. "
                "Activation strictly requires operation_id 'deployment.activate'."
            )
        if str(approval_request.operation_id) != "deployment.activate":
            raise DeploymentApprovalRequiredError(
                f"ApprovalRequest operation_id {approval_request.operation_id!r} is not authorized for deployment activation. "
                "Activation strictly requires operation_id 'deployment.activate'."
            )
        if str(operation_identity.operation_id) != str(approval_request.operation_id):
            raise DeploymentApprovalRequiredError(
                f"Operation ID mismatch across 4-tuple: OperationIdentity={operation_identity.operation_id!r}, "
                f"ApprovalRequest={approval_request.operation_id!r}"
            )

        # 5b. Exact operation version validation using actual contract types (B15)
        if operation_identity.operation_version != "1.0.0":
            raise DeploymentApprovalRequiredError(
                f"OperationIdentity operation_version {operation_identity.operation_version!r} is not authorized for deployment activation. "
                "Activation strictly requires OperationIdentity.operation_version == '1.0.0'."
            )
        if approval_request.operation_version != 1:
            raise DeploymentApprovalRequiredError(
                f"ApprovalRequest operation_version {approval_request.operation_version!r} is not authorized for deployment activation. "
                "Activation strictly requires ApprovalRequest.operation_version == 1."
            )
        # Authoritative cross-representation check: version 1.0.0 maps to major version 1
        expected_major_version = int(operation_identity.operation_version.split(".")[0])
        if approval_request.operation_version != expected_major_version:
            raise DeploymentApprovalRequiredError(
                f"Operation version mismatch across 4-tuple: OperationIdentity={operation_identity.operation_version}, "
                f"ApprovalRequest={approval_request.operation_version}"
            )

        # 5c. Target identity cross-check
        if not (
            str(operation_identity.target_identity)
            == str(target_id)
            == str(record.target_id)
        ):
            raise DeploymentApprovalRequiredError(
                f"Target identity mismatch: OperationIdentity target={operation_identity.target_identity!r}, "
                f"external target={target_id!r}, record target={record.target_id!r}"
            )

        # 5d. Approval decision matches request
        if approval_decision.request_id != approval_request.request_id:
            raise DeploymentApprovalRequiredError(
                f"Approval decision request_id mismatch: expected {approval_request.request_id!r}, "
                f"got {approval_decision.request_id!r}"
            )

        # 5e. Policy version cross-check
        if approval_decision.bound_policy_version != approval_request.policy_version:
            raise DeploymentApprovalRequiredError(
                f"Policy version mismatch: ApprovalRequest policy_version={approval_request.policy_version!r}, "
                f"ApprovalDecision bound_policy_version={approval_decision.bound_policy_version!r}"
            )

        # 5f. Approval decision is strictly APPROVED
        if approval_decision.decision != ApprovalStatus.APPROVED:
            raise DeploymentApprovalRequiredError(
                f"Deployment activation rejected: approval status is {approval_decision.decision!r}"
            )

        # 5g. Canonical payload hash matches across operation identity, approval decision, and plan
        computed_hash = compute_deployment_payload_hash(record.plan)
        if operation_identity.canonical_payload_hash != computed_hash:
            raise DeploymentApprovalRequiredError(
                f"Canonical payload hash mismatch: OperationIdentity has {operation_identity.canonical_payload_hash!r}, "
                f"but DeploymentPlan computes to {computed_hash!r}"
            )
        if approval_decision.bound_payload_hash != computed_hash:
            raise DeploymentApprovalRequiredError(
                f"ApprovalDecision bound_payload_hash mismatch: expected {computed_hash!r}, "
                f"got {approval_decision.bound_payload_hash!r}"
            )

        # 5h. Bound resource scope cross-check
        if approval_decision.bound_resource_scope != approval_request.resource_scope:
            raise DeploymentApprovalRequiredError(
                f"Bound resource scope mismatch: ApprovalRequest={approval_request.resource_scope!r}, "
                f"ApprovalDecision={approval_decision.bound_resource_scope!r}"
            )

        scope = approval_request.resource_scope
        expected_target_str = str(target_id)
        expected_dep_str = str(record.deployment_id)
        valid_scopes = {
            f"deployment:{expected_dep_str}",
            f"target:{expected_target_str}",
            expected_target_str,
            expected_dep_str,
        }
        if scope not in valid_scopes:
            raise DeploymentApprovalRequiredError(
                f"ApprovalRequest resource_scope {scope!r} does not match deployment {expected_dep_str!r} "
                f"or target {expected_target_str!r}"
            )

        # 6. Dispatch activation to ADR-0005 with approval_authorized=True
        exec_id = ExecutionId(f"exec-act-{record.deployment_id}-{uuid.uuid4().hex[:8]}")
        invocation = ToolInvocation(
            tool_id=tool_id,
            target_id=target_id,
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
            record.with_transition(
                DeploymentStatus.FAILED,
                failure_details=tool_result.error_message or "Unknown execution error",
            )
            raise OptimizationExecutionError(
                f"Deployment activation failed on target {target_id}: "
                f"{tool_result.error_message or 'Unknown execution error'}"
            )

        # 7. Transition to ACTIVE per ADR-0012 §3.6 (B7)
        activated_record = record.with_transition(
            DeploymentStatus.ACTIVE,
            execution_id=exec_id,
            target_id=target_id,
            activated_at=datetime.now(timezone.utc),
        )
        self._safety_index[activated_record.deployment_id] = activated_record
        if active_deployments is not None and isinstance(active_deployments, dict):
            active_deployments[activated_record.deployment_id] = activated_record
        return activated_record

    def rollback(
        self,
        record: DeploymentRecord,
        target_id: TargetId,
        reason: str,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        *,
        approval_authorized: bool = False,
        active_deployments: dict[DeploymentId, DeploymentRecord] | None = None,
    ) -> tuple[DeploymentRecord, DeploymentRecord | None]:
        """Execute deterministic rollback or escalate to OPERATOR_REQUIRED per ADR-0012 §3.7."""
        # 1. Mandatory arguments (B5)
        if target_id is None or not str(target_id).strip():
            raise DeploymentValidationError(
                "target_id is mandatory for rollback. "
                "Local synthesis of TargetId is strictly prohibited."
            )
        if str(target_id) != str(record.target_id) and str(target_id) != str(
            record.plan.target.target_id
        ):
            raise DeploymentValidationError(
                f"Supplied target_id {target_id!r} conflicts with record target {record.target_id!r}"
            )

        # 2. Null Predecessor Escalation Invariant (B8: ADR-0012 §3.7 & Acceptance Criterion 9)
        # If rollback_target_id is None (first deployment), cannot execute automated rollback.
        # Must escalate to OPERATOR_REQUIRED domain state and lock out target.
        if record.plan.rollback_target_id is None:
            escalated_record = record.with_transition(
                DeploymentStatus.OPERATOR_REQUIRED,
                target_id=target_id,
                failure_details=(
                    f"Null predecessor rollback target (first deployment). "
                    f"Escalated to OPERATOR_REQUIRED: {reason}"
                ),
            )
            self._safety_index[record.deployment_id] = escalated_record
            if active_deployments is not None and isinstance(active_deployments, dict):
                active_deployments[record.deployment_id] = escalated_record
            return escalated_record, None

        # 3. Locate rollback target if active_deployments registry provided
        target_dep_id = record.plan.rollback_target_id
        target_record: DeploymentRecord | None = None
        if active_deployments is not None:
            if target_dep_id not in active_deployments:
                raise RollbackExecutionError(
                    f"Designated rollback predecessor {target_dep_id!r} not found in active deployments registry"
                )
            target_record = active_deployments[target_dep_id]

        # 4. Dispatch rollback to ADR-0005
        exec_id = ExecutionId(
            f"exec-roll-{record.deployment_id}-{uuid.uuid4().hex[:8]}"
        )
        invocation = ToolInvocation(
            tool_id=tool_id,
            target_id=target_id,
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
            approval_authorized=approval_authorized,
        )

        tool_result = self._dispatch_tool(tool_executor, invocation)
        if tool_result.status != ToolExecutionStatus.SUCCESS:
            raise RollbackExecutionError(
                f"Rollback execution failed on target {target_id}: "
                f"{tool_result.error_message or 'Unknown execution error'}"
            )

        rolled_back_record = record.with_transition(
            DeploymentStatus.ROLLED_BACK,
            execution_id=exec_id,
            target_id=target_id,
            rolled_back_at=datetime.now(timezone.utc),
            failure_details=f"Rolled back to {target_dep_id}: {reason}",
        )

        restored_record: DeploymentRecord | None = None
        if target_record is not None:
            restored_record = target_record.with_transition(
                DeploymentStatus.ACTIVE,
                execution_id=exec_id,
                target_id=target_id,
                activated_at=datetime.now(timezone.utc),
            )
            self._safety_index[target_dep_id] = restored_record
            if active_deployments is not None and isinstance(active_deployments, dict):
                active_deployments[target_dep_id] = restored_record

        self._safety_index[record.deployment_id] = rolled_back_record
        if active_deployments is not None and isinstance(active_deployments, dict):
            active_deployments[record.deployment_id] = rolled_back_record

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
