"""cv_agent.optimization.optimizer — Model compilation and optimization dispatch for ADR-0012."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol, cast, runtime_checkable

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
        *,
        checkpoint_manifest: CheckpointManifest | None = None,
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
        *,
        checkpoint_manifest: CheckpointManifest | None = None,
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

        # 2. Lineage verification against CheckpointManifest
        if checkpoint_manifest is not None:
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

        # 3. Lineage & leakage verification against DatasetManifest
        if dataset_manifest is not None and plan.calibration_binding is not None:
            calib = plan.calibration_binding
            if str(calib.dataset_id) != str(dataset_manifest.dataset_id):
                raise CalibrationLeakageError(
                    f"Calibration dataset ID mismatch: plan specifies {calib.dataset_id!r}, "
                    f"but manifest has {dataset_manifest.dataset_id!r}"
                )
            if str(calib.dataset_manifest_hash) != str(
                dataset_manifest.payload_root_checksum
            ):
                raise CalibrationLeakageError(
                    f"Calibration manifest hash mismatch: plan specifies {calib.dataset_manifest_hash!r}, "
                    f"but manifest has {dataset_manifest.payload_root_checksum!r}"
                )
            # Verify calibration split exists in manifest
            available_splits: set[str] = set()
            if dataset_manifest.split_membership.train_hashes:
                available_splits.add("train")
            if dataset_manifest.split_membership.val_hashes:
                available_splits.add("val")
            if dataset_manifest.split_membership.test_hashes:
                available_splits.add("test")
            if dataset_manifest.split_membership.calibration_hashes:
                available_splits.add("calibration")

            if calib.split_name not in available_splits:
                raise CalibrationLeakageError(
                    f"Calibration split {calib.split_name!r} not declared in dataset manifest. "
                    f"Available splits: {sorted(available_splits)}"
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

        # 6. Parse compilation telemetry & construct OptimizedModelArtifact
        output = tool_result.structured_output or {}
        artifact_uri = str(
            output.get(
                "artifact_uri",
                f"artifact://models/{plan.plan_id}/model.{self._default_ext(plan.technique)}",
            )
        )
        artifact_size = int(
            output.get("artifact_size_bytes", 104857600)
        )  # Default 100MB
        runtime_format = str(
            output.get(
                "runtime_format",
                "tensorrt_engine"
                if plan.technique == plan.technique.TENSORRT_COMPILATION
                else "onnx",
            )
        )

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

    @staticmethod
    def _default_ext(technique: Any) -> str:
        tech_val = getattr(technique, "value", str(technique))
        if tech_val == "onnx_export":
            return "onnx"
        return "engine"
