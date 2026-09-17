"""cv_agent.optimization.optimizer — Model compilation and optimization dispatch for ADR-0012."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Protocol, cast, runtime_checkable

from cv_agent.dataset import DatasetManifest
from cv_agent.optimization.canonicalization import (
    compute_artifact_lineage_hash,
    extract_optimization_operation_payload,
)
from cv_agent.optimization.contracts import (
    OptimizationPlan,
    OptimizedArtifactId,
    OptimizedModelArtifact,
)
from cv_agent.optimization.errors import (
    CalibrationLeakageError,
    OptimizationExecutionError,
    OptimizationValidationError,
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
from cv_agent.training import CheckpointManifest


@runtime_checkable
class ModelOptimizer(Protocol):
    """Protocol for executing model compilation and precision transformations via ADR-0005."""

    def compile(
        self,
        plan: OptimizationPlan,
        tool_id: ToolId,
        target_id: TargetId,
        tool_executor: ToolExecutor,
        checkpoint_manifest: CheckpointManifest,
        *,
        dataset_manifest: DatasetManifest | None = None,
        approval_authorized: bool = False,
    ) -> OptimizedModelArtifact: ...


class DefaultModelOptimizer:
    """Standard model optimizer delegating compilation execution to ADR-0005."""

    def compile(
        self,
        plan: OptimizationPlan,
        tool_id: ToolId,
        target_id: TargetId,
        tool_executor: ToolExecutor,
        checkpoint_manifest: CheckpointManifest,
        *,
        dataset_manifest: DatasetManifest | None = None,
        approval_authorized: bool = False,
    ) -> OptimizedModelArtifact:
        """Validate optimization plan, verify lineage bindings, and dispatch compilation."""
        # 1. Mandatory execution arguments (Zero local synthesis)
        if tool_id is None or not str(tool_id).strip():
            raise OptimizationExecutionError(
                "tool_id is mandatory for optimization compilation. "
                "Local synthesis of ToolId is strictly prohibited."
            )
        if target_id is None or not str(target_id).strip():
            raise OptimizationExecutionError(
                "target_id is mandatory for optimization compilation. "
                "Local synthesis of TargetId is strictly prohibited."
            )
        if tool_executor is None:
            raise OptimizationExecutionError(
                "tool_executor is mandatory for optimization compilation. "
                "ADR-0005 execution dispatch cannot be omitted."
            )

        # 2. Lineage verification against CheckpointManifest (B1: Mandatory, fail-closed)
        if checkpoint_manifest is None:
            raise OptimizationValidationError(
                "checkpoint_manifest is mandatory for optimization compilation per ADR-0010 lineage requirements. "
                "Compilation without authoritative checkpoint manifest is strictly prohibited."
            )
        if str(plan.source_checkpoint_id) != str(checkpoint_manifest.checkpoint_id):
            raise OptimizationValidationError(
                f"Checkpoint ID mismatch: plan specifies {plan.source_checkpoint_id!r}, "
                f"but manifest has {checkpoint_manifest.checkpoint_id!r}"
            )
        if str(plan.source_weights_hash) != str(checkpoint_manifest.weights_hash):
            raise OptimizationValidationError(
                f"Weights hash mismatch: plan specifies {plan.source_weights_hash!r}, "
                f"but manifest has {checkpoint_manifest.weights_hash!r}"
            )

        # 3. Lineage & leakage verification against DatasetManifest (B2: Mandatory for PTQ/calibration)
        if plan.calibration_binding is not None:
            if dataset_manifest is None:
                raise CalibrationLeakageError(
                    "dataset_manifest is mandatory when calibration_binding is specified for compilation. "
                    "PTQ calibration without authoritative dataset manifest is strictly prohibited."
                )
            calib = plan.calibration_binding
            if str(calib.dataset_id) != str(dataset_manifest.dataset_id):
                raise CalibrationLeakageError(
                    f"Calibration dataset ID mismatch: plan specifies {calib.dataset_id!r}, "
                    f"but manifest has {dataset_manifest.dataset_id!r}"
                )
            if str(calib.dataset_version) != str(dataset_manifest.version):
                raise CalibrationLeakageError(
                    f"Calibration dataset version mismatch: plan specifies {calib.dataset_version!r}, "
                    f"but manifest has {dataset_manifest.version!r}"
                )
            if str(calib.dataset_manifest_hash) != str(
                dataset_manifest.payload_root_checksum
            ):
                raise CalibrationLeakageError(
                    f"Calibration manifest hash mismatch: plan specifies {calib.dataset_manifest_hash!r}, "
                    f"but manifest has {dataset_manifest.payload_root_checksum!r}"
                )
            if not dataset_manifest.split_membership.calibration_hashes:
                raise CalibrationLeakageError(
                    f"Calibration split 'calibration' in dataset {dataset_manifest.dataset_id} "
                    "has no items (empty calibration split in manifest)."
                )

        # 4. Prepare ADR-0005 ToolInvocation
        exec_id = ExecutionId(f"exec-opt-{plan.plan_id}-{uuid.uuid4().hex[:8]}")
        payload = extract_optimization_operation_payload(plan)

        invocation = ToolInvocation(
            tool_id=tool_id,
            target_id=target_id,
            execution_id=exec_id,
            timeout_seconds=int(plan.parameters.get("timeout_seconds", 3600)),
            parameters={
                "optimization_payload": payload,
                "technique": plan.technique.value,
                "precision": plan.target_precision.value,
                "target_platform": plan.target_platform,
                "target_compute_capability": plan.target_compute_capability,
            },
            approval_authorized=approval_authorized,
        )

        # 5. Dispatch invocation to ADR-0005 ToolExecutor
        tool_result = self._dispatch_tool(tool_executor, invocation)

        if tool_result.status != ToolExecutionStatus.SUCCESS:
            raise OptimizationExecutionError(
                f"Model optimization compilation failed on target {target_id}: "
                f"{tool_result.error_message or 'Unknown error'}"
            )

        # 6. Parse compilation telemetry & construct OptimizedModelArtifact (B3: Zero synthetic metadata)
        output = tool_result.structured_output
        if not output or not isinstance(output, Mapping):
            raise OptimizationExecutionError(
                "Compilation completed tool dispatch without structured completion output/evidence."
            )

        if "artifact_uri" not in output or not str(output["artifact_uri"]).strip():
            raise OptimizationExecutionError(
                "Compilation tool execution output missing mandatory non-empty 'artifact_uri'"
            )
        artifact_uri = str(output["artifact_uri"]).strip()

        if "artifact_size_bytes" not in output:
            raise OptimizationExecutionError(
                "Compilation tool execution output missing mandatory 'artifact_size_bytes'"
            )
        try:
            artifact_size = int(output["artifact_size_bytes"])
            if artifact_size <= 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise OptimizationExecutionError(
                f"Compilation output 'artifact_size_bytes' must be a positive integer, got {output.get('artifact_size_bytes')!r}"
            )

        if "runtime_format" not in output or not str(output["runtime_format"]).strip():
            raise OptimizationExecutionError(
                "Compilation tool execution output missing mandatory non-empty 'runtime_format'"
            )
        runtime_format = str(output["runtime_format"]).strip()

        lineage_hash = compute_artifact_lineage_hash(
            plan,
            {"runtime_format": runtime_format, "target_device": str(target_id)},
        )

        return OptimizedModelArtifact(
            artifact_id=OptimizedArtifactId(f"art-{plan.plan_id}"),
            plan_id=plan.plan_id,
            source_checkpoint_id=plan.source_checkpoint_id,
            technique=plan.technique,
            precision=plan.target_precision,
            runtime_format=runtime_format,
            target_device=str(target_id),
            target_compute_capability=plan.target_compute_capability,
            artifact_hash=lineage_hash,
            artifact_uri=artifact_uri,
            artifact_size_bytes=artifact_size,
            shape_profiles=plan.shape_profiles,
            execution_id=exec_id,
            target_id=target_id,
            created_at=datetime.now(timezone.utc),
        )

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
                exc, (OptimizationExecutionError, OptimizationValidationError)
            ):
                raise
            raise OptimizationExecutionError(
                f"Compilation transport failed via ADR-0005: {exc}"
            ) from exc
