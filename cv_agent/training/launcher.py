"""cv_agent.training.launcher — Training-specific dispatch delegation to ADR-0005."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timezone
from typing import Protocol, cast, runtime_checkable

from cv_agent.dataset import DatasetManifest, OperationIdentity
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
from cv_agent.training.canonicalization import (
    compute_training_payload_hash,
    extract_training_operation_payload,
)
from cv_agent.training.contracts import TrainingJobRecord, TrainingPlan
from cv_agent.training.errors import (
    PlanValidationError,
    TrainingApprovalRequiredError,
    TrainingExecutionError,
)
from cv_agent.training.state_machine import validate_training_job_transition
from cv_agent.training.types import (
    TrainingJobId,
    TrainingJobStatus,
    TrainingPrecision,
    validate_training_job_id,
)


@runtime_checkable
class TrainingLauncher(Protocol):
    """Training-specific dispatch contract delegating physical transport strictly to ADR-0005."""

    def validate_plan(
        self,
        plan: TrainingPlan,
        dataset_manifest: DatasetManifest | None = None,
    ) -> bool:
        """Validate plan syntax, dataset manifest bindings, and split disjointness."""
        ...

    def prepare_invocation(
        self,
        plan: TrainingPlan,
        tool_id: ToolId,
        target_id: TargetId,
        *,
        execution_id: ExecutionId | None = None,
        timeout_seconds: int = 86400,
    ) -> ToolInvocation:
        """Format training execution payload into a typed ADR-0005 ToolInvocation."""
        ...

    def verify_approval(
        self,
        plan: TrainingPlan,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        target_id: TargetId | None = None,
    ) -> None:
        """Verify complete ADR-0003 approval binding against plan canonical payload."""
        ...

    def launch(
        self,
        plan: TrainingPlan,
        tool_id: ToolId,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        target_id: TargetId | None = None,
        job_id: TrainingJobId | None = None,
        tool_executor: ToolExecutor | None = None,
    ) -> TrainingJobRecord:
        """Dispatch an approved training plan to ADR-0005."""
        ...

    def abort_request(
        self,
        job_id: TrainingJobId,
        target_id: TargetId,
        tool_executor: ToolExecutor,
        *,
        cancel_tool_id: ToolId | None = None,
        execution_id: ExecutionId | None = None,
        timeout_seconds: int = 60,
    ) -> bool:
        """Emit cancellation signal to execution target via ADR-0005."""
        ...


class DefaultTrainingLauncher:
    """Authoritative training launcher delegating execution transport to ADR-0005."""

    def validate_plan(
        self,
        plan: TrainingPlan,
        dataset_manifest: DatasetManifest | None = None,
    ) -> bool:
        """Validate structural and architectural constraints on a TrainingPlan.

        Invariants:
            1. train_split != val_split.
            2. Neither train_split nor val_split may be "calibration" (even with INT8_PTQ_AWARE).
            3. When dataset_manifest is supplied, dataset_id, version, and manifest hash must match.
            4. Declared splits must exist in dataset manifest and their payload hashes must be disjoint.
        """
        if plan.train_split == plan.val_split:
            raise PlanValidationError(
                f"train_split and val_split must be distinct, got {plan.train_split!r}"
            )

        if plan.train_split == "calibration" or plan.val_split == "calibration":
            raise PlanValidationError(
                "Calibration split 'calibration' is strictly reserved for post-training quantization "
                "and cannot be used as a training or validation split."
            )

        if plan.precision == TrainingPrecision.INT8_PTQ_AWARE and (
            plan.train_split == "calibration" or plan.val_split == "calibration"
        ):
            raise PlanValidationError(
                "INT8_PTQ_AWARE training cannot ingest calibration split data."
            )

        if dataset_manifest is not None:
            if str(plan.dataset_id) != str(dataset_manifest.dataset_id):
                raise PlanValidationError(
                    f"Dataset ID mismatch: plan specifies {plan.dataset_id!r}, "
                    f"manifest specifies {dataset_manifest.dataset_id!r}"
                )
            if str(plan.dataset_version) != str(dataset_manifest.version):
                raise PlanValidationError(
                    f"Dataset version mismatch: plan specifies {plan.dataset_version!r}, "
                    f"manifest specifies {dataset_manifest.version!r}"
                )
            if str(plan.dataset_manifest_hash) != str(
                dataset_manifest.payload_root_checksum
            ):
                raise PlanValidationError(
                    f"Dataset manifest hash mismatch: plan specifies {plan.dataset_manifest_hash!r}, "
                    f"manifest specifies {dataset_manifest.payload_root_checksum!r}"
                )

            available_splits = {"train", "val", "test", "calibration"}
            if plan.train_split not in available_splits:
                raise PlanValidationError(
                    f"train_split {plan.train_split!r} does not exist in dataset manifest splits {sorted(available_splits)}"
                )
            if plan.val_split not in available_splits:
                raise PlanValidationError(
                    f"val_split {plan.val_split!r} does not exist in dataset manifest splits {sorted(available_splits)}"
                )

            # Check split partition disjointness
            membership = dataset_manifest.split_membership
            train_hashes = (
                set(membership.train_hashes)
                if plan.train_split == "train"
                else (
                    set(membership.val_hashes)
                    if plan.train_split == "val"
                    else (
                        set(membership.test_hashes)
                        if plan.train_split == "test"
                        else set(membership.calibration_hashes)
                    )
                )
            )
            val_hashes = (
                set(membership.val_hashes)
                if plan.val_split == "val"
                else (
                    set(membership.train_hashes)
                    if plan.val_split == "train"
                    else (
                        set(membership.test_hashes)
                        if plan.val_split == "test"
                        else set(membership.calibration_hashes)
                    )
                )
            )
            overlap = train_hashes & val_hashes
            if overlap:
                raise PlanValidationError(
                    f"Dataset split contamination: train_split and val_split share {len(overlap)} overlapping payload hashes"
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
            if isinstance(exc, TrainingExecutionError):
                raise
            raise TrainingExecutionError(
                f"Dispatch to tool executor failed: {exc}"
            ) from exc

    def prepare_invocation(
        self,
        plan: TrainingPlan,
        tool_id: ToolId,
        target_id: TargetId,
        *,
        execution_id: ExecutionId | None = None,
        timeout_seconds: int = 86400,
    ) -> ToolInvocation:
        """Format training execution payload into a typed ADR-0005 ToolInvocation.

        Zero shell interpretation, zero subprocess calls, zero local target synthesis.
        """
        if target_id is None or not str(target_id).strip():
            raise TrainingApprovalRequiredError(
                "Cannot prepare invocation without an authorized TargetId. "
                "Local target synthesis is strictly prohibited."
            )
        payload = extract_training_operation_payload(plan)
        exec_id = execution_id or ExecutionId(f"exec-train-{uuid.uuid4().hex[:12]}")
        return ToolInvocation(
            tool_id=tool_id,
            parameters=payload,
            target_id=target_id,
            execution_id=exec_id,
            timeout_seconds=timeout_seconds,
            approval_authorized=True,
        )

    def verify_approval(
        self,
        plan: TrainingPlan,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        target_id: TargetId | None = None,
    ) -> None:
        """Verify complete ADR-0003 approval security binding.

        Enforces that:
        1. operation_identity.canonical_payload_hash matches canonical plan hash.
        2. operation_identity matches approval_request (operation_id, version, scope).
        3. approval_decision matches approval_request (request_id, status APPROVED, bound hash, scope, policy).
        4. target_id (if provided) does not conflict with authorized target identity / resource scope.
        """
        if operation_identity is None:
            raise TrainingApprovalRequiredError(
                "Missing authoritative ADR-0003 OperationIdentity"
            )
        if approval_request is None:
            raise TrainingApprovalRequiredError(
                "Missing authoritative ADR-0003 ApprovalRequest"
            )
        if approval_decision is None:
            raise TrainingApprovalRequiredError(
                "Missing authoritative ADR-0003 ApprovalDecision"
            )

        computed_hash = compute_training_payload_hash(plan)

        # 1. Verify computed payload hash matches operation identity
        if operation_identity.canonical_payload_hash != computed_hash:
            raise TrainingApprovalRequiredError(
                f"Canonical payload hash mismatch: OperationIdentity has {operation_identity.canonical_payload_hash!r}, "
                f"but TrainingPlan computes to {computed_hash!r}"
            )

        # 2. Verify operation identity matches approval request
        if str(operation_identity.operation_id) != str(approval_request.operation_id):
            raise TrainingApprovalRequiredError(
                f"Operation ID mismatch: OperationIdentity has {operation_identity.operation_id!r}, "
                f"ApprovalRequest has {approval_request.operation_id!r}"
            )
        if str(operation_identity.operation_version) != str(
            approval_request.operation_version
        ):
            raise TrainingApprovalRequiredError(
                f"Operation version mismatch: OperationIdentity has {operation_identity.operation_version!r}, "
                f"ApprovalRequest has {approval_request.operation_version!r}"
            )
        if (
            operation_identity.canonical_payload_hash
            != approval_request.canonical_payload_hash
        ):
            raise TrainingApprovalRequiredError(
                f"ApprovalRequest payload hash mismatch: expected {operation_identity.canonical_payload_hash!r}, "
                f"got {approval_request.canonical_payload_hash!r}"
            )
        if operation_identity.target_identity != approval_request.resource_scope:
            raise TrainingApprovalRequiredError(
                f"Target identity / resource scope mismatch: OperationIdentity has {operation_identity.target_identity!r}, "
                f"ApprovalRequest has {approval_request.resource_scope!r}"
            )

        # 3. Verify approval decision matches approval request & operation identity
        if approval_decision.request_id != approval_request.request_id:
            raise TrainingApprovalRequiredError(
                f"Approval decision request_id mismatch: expected {approval_request.request_id!r}, "
                f"got {approval_decision.request_id!r}"
            )
        if approval_decision.decision != ApprovalStatus.APPROVED:
            raise TrainingApprovalRequiredError(
                f"Approval status is not APPROVED: got {approval_decision.decision.value!r}"
            )
        if (
            approval_decision.bound_payload_hash
            != operation_identity.canonical_payload_hash
        ):
            raise TrainingApprovalRequiredError(
                f"ApprovalDecision bound_payload_hash mismatch: expected {operation_identity.canonical_payload_hash!r}, "
                f"got {approval_decision.bound_payload_hash!r}"
            )
        if approval_decision.bound_resource_scope != approval_request.resource_scope:
            raise TrainingApprovalRequiredError(
                f"ApprovalDecision bound_resource_scope mismatch: expected {approval_request.resource_scope!r}, "
                f"got {approval_decision.bound_resource_scope!r}"
            )
        if approval_decision.bound_policy_version != approval_request.policy_version:
            raise TrainingApprovalRequiredError(
                f"ApprovalDecision bound_policy_version mismatch: expected {approval_request.policy_version!r}, "
                f"got {approval_decision.bound_policy_version!r}"
            )

        # 4. Target ID consistency check (PM hard invariant)
        if target_id is not None:
            # If the authoritative resource scope contains an explicit target specification,
            # ensure target_id does not disagree with it.
            scope = approval_request.resource_scope
            if ":" in scope:
                prefix, scope_target = scope.split(":", 1)
                if prefix == "target" and scope_target != str(target_id):
                    raise TrainingApprovalRequiredError(
                        f"Supplied target_id {target_id!r} conflicts with authorized scope target {scope_target!r}"
                    )
            elif str(operation_identity.target_identity).startswith("target:"):
                _, op_target = str(operation_identity.target_identity).split(":", 1)
                if op_target != str(target_id):
                    raise TrainingApprovalRequiredError(
                        f"Supplied target_id {target_id!r} conflicts with authorized operation target {op_target!r}"
                    )

    def launch(
        self,
        plan: TrainingPlan,
        tool_id: ToolId,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        target_id: TargetId | None = None,
        job_id: TrainingJobId | None = None,
        tool_executor: ToolExecutor | None = None,
    ) -> TrainingJobRecord:
        """Launch training workload by verifying approval and delegating execution transport to ADR-0005.

        Invariants:
        1. Approval verification is performed strictly against authoritative ADR-0003 credentials.
        2. Target identity is derived from the authoritative approval context if not explicitly passed.
           Local synthesis (such as inventing 'default') is strictly prohibited.
        3. A unique TrainingJobId is generated for each execution launch, preserving plan_id for lineage.
        """
        self.validate_plan(plan)
        self.verify_approval(
            plan=plan,
            operation_identity=operation_identity,
            approval_request=approval_request,
            approval_decision=approval_decision,
            target_id=target_id,
        )

        # Authoritative target resolution (zero local synthesis):
        # Derive target from approval context if not explicitly provided
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
            raise TrainingApprovalRequiredError(
                "No authorized execution target_id supplied or derivable from authoritative approval context. "
                "Local target synthesis is strictly prohibited."
            )

        validate_training_job_transition(
            TrainingJobStatus.APPROVED, TrainingJobStatus.LAUNCHING
        )

        invocation = self.prepare_invocation(
            plan, tool_id, target_id=resolved_target_id
        )
        execution_id: ExecutionId | None = None

        if tool_executor is not None:
            result = self._dispatch_invocation(tool_executor, invocation)
            execution_id = result.execution_id
            if result.status != ToolExecutionStatus.SUCCESS:
                validate_training_job_transition(
                    TrainingJobStatus.LAUNCHING, TrainingJobStatus.FAILED
                )
                raise TrainingExecutionError(
                    f"Tool execution failed for tool {tool_id!r}: {result.error_message}"
                )

        validate_training_job_transition(
            TrainingJobStatus.LAUNCHING, TrainingJobStatus.RUNNING
        )

        # Unique execution identity per launch (preserves plan_id as lineage reference)
        if job_id is not None:
            resolved_job_id = validate_training_job_id(str(job_id))
        else:
            resolved_job_id = validate_training_job_id(
                f"job-{plan.plan_id}-{uuid.uuid4().hex[:12]}"
            )

        return TrainingJobRecord(
            job_id=resolved_job_id,
            plan_id=plan.plan_id,
            status=TrainingJobStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            execution_id=execution_id,
            target_id=resolved_target_id,
        )

    def abort_request(
        self,
        job_id: TrainingJobId,
        target_id: TargetId,
        tool_executor: ToolExecutor,
        *,
        cancel_tool_id: ToolId | None = None,
        execution_id: ExecutionId | None = None,
        timeout_seconds: int = 60,
    ) -> bool:
        """Emit cancellation signal via ADR-0005 ToolExecutor without mutating orchestrator session state."""
        if target_id is None or not str(target_id).strip():
            raise TrainingExecutionError(
                "TargetId is required to emit cancellation signal via ADR-0005"
            )
        if tool_executor is None:
            raise TrainingExecutionError(
                "ToolExecutor is required to emit cancellation signal via ADR-0005"
            )

        tool = cancel_tool_id or ToolId("cv.train.cancel")
        exec_id = execution_id or ExecutionId(f"exec-abort-{uuid.uuid4().hex[:12]}")
        invocation = ToolInvocation(
            tool_id=tool,
            parameters={"job_id": str(job_id)},
            target_id=target_id,
            execution_id=exec_id,
            timeout_seconds=timeout_seconds,
            approval_authorized=True,
        )

        result = self._dispatch_invocation(tool_executor, invocation)
        return result.status == ToolExecutionStatus.SUCCESS
