"""cv_agent.skills.executor — Procedural skill executor delegating to ADR-0005 tools.

Implements ADR-0007 §3.1, §3.2, §3.8:
- Validates input parameters against declared JSON parameter_schema.
- Gating enforcement: fails closed with POLICY_VIOLATION and empty tool_results on unapproved execution.
- Dispatches tool invocations strictly via ADR-0005 ToolExecutor.
- Detects ActiveJobHandoff and returns SkillExecutionStatus.DISPATCHED.
- Enforces declared timeout_seconds.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from cv_agent.skills.contracts import (
    SkillDescriptor,
    SkillExecutionResult,
    SkillExecutionStatus,
)
from cv_agent.skills.errors import (
    ParameterValidationError,
)
from cv_agent.tools.contracts import (
    ExecutionId,
    ExecutionTargetDescriptor,
    ToolExecutionStatus,
    ToolExecutor,
    ToolInvocation,
    ToolResult,
)


def validate_parameters_against_schema(
    parameters: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> None:
    """Validate parameter dictionary against basic JSON schema requirements."""
    if not schema:
        return

    required = schema.get("required", ())
    for req_key in required:
        if req_key not in parameters:
            raise ParameterValidationError(
                f"Missing required parameter {req_key!r} declared in parameter_schema"
            )

    properties = schema.get("properties", {})
    if isinstance(properties, Mapping):
        for key, prop_schema in properties.items():
            if key in parameters and isinstance(prop_schema, Mapping):
                expected_type = prop_schema.get("type")
                val = parameters[key]
                if expected_type == "string" and not isinstance(val, str):
                    raise ParameterValidationError(
                        f"Parameter {key!r} must be string, got {type(val).__name__}"
                    )
                elif expected_type == "integer" and (
                    isinstance(val, bool) or not isinstance(val, int)
                ):
                    raise ParameterValidationError(
                        f"Parameter {key!r} must be integer, got {type(val).__name__}"
                    )
                elif expected_type == "number" and not isinstance(val, (int, float)):
                    raise ParameterValidationError(
                        f"Parameter {key!r} must be number, got {type(val).__name__}"
                    )
                elif expected_type == "boolean" and not isinstance(val, bool):
                    raise ParameterValidationError(
                        f"Parameter {key!r} must be boolean, got {type(val).__name__}"
                    )


@runtime_checkable
class SkillExecutor(Protocol):
    """Coordinates procedural execution of a bound skill via ADR-0005 tools."""

    async def execute(
        self,
        skill: SkillDescriptor,
        parameters: Mapping[str, Any],
        target: ExecutionTargetDescriptor,
        tool_executor: ToolExecutor,
        *,
        approval_authorized: bool = False,
        execution_id: str | None = None,
    ) -> SkillExecutionResult:
        """Validate parameters, enforce approvals, and invoke dependent tools."""
        ...


class DefaultSkillExecutor:
    """Production skill executor coordinating ADR-0005 tool execution and async handoffs."""

    async def execute(
        self,
        skill: SkillDescriptor,
        parameters: Mapping[str, Any],
        target: ExecutionTargetDescriptor,
        tool_executor: ToolExecutor,
        *,
        approval_authorized: bool = False,
        execution_id: str | None = None,
    ) -> SkillExecutionResult:
        """Execute or dispatch a procedural skill against a validated target."""
        exec_id = execution_id or f"skill-exec-{uuid.uuid4().hex[:12]}"
        start_time = time.monotonic()

        # 1. Parameter schema validation
        try:
            validate_parameters_against_schema(parameters, skill.parameter_schema)
        except ParameterValidationError as exc:
            return SkillExecutionResult(
                skill_id=skill.skill_id,
                execution_id=exec_id,
                status=SkillExecutionStatus.PREREQUISITE_FAILED,
                error_message=str(exc),
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        # 2. Approval Gating (Gate 4, Invariant I1)
        # Gated skills fail closed before any tool invocation; tool_results MUST be empty
        if skill.requires_approval and not approval_authorized:
            return SkillExecutionResult(
                skill_id=skill.skill_id,
                execution_id=exec_id,
                status=SkillExecutionStatus.POLICY_VIOLATION,
                tool_results=(),
                active_job_ref=None,
                error_message=(
                    f"Skill {str(skill.skill_id)!r} requires human approval, but "
                    f"approval_authorized is False"
                ),
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        # 3. Target type validation
        if target.target_type not in skill.target_requirements.allowed_targets:
            return SkillExecutionResult(
                skill_id=skill.skill_id,
                execution_id=exec_id,
                status=SkillExecutionStatus.PREREQUISITE_FAILED,
                error_message=(
                    f"Target {str(target.target_id)!r} has type {target.target_type.value!r}, "
                    f"which is not permitted by skill target requirements: "
                    f"{[t.value for t in skill.target_requirements.allowed_targets]}"
                ),
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        # 4. Tool Invocation Dispatch (Gate 3)
        collected_results: list[ToolResult] = []
        merged_outputs: dict[str, Any] = {}

        try:
            # Wrap execution in declared skill timeout
            async with asyncio.timeout(skill.timeout_seconds):
                for tool_id in skill.required_tools:
                    tool_inv_id = ExecutionId(f"tool-{uuid.uuid4().hex[:12]}")
                    invocation = ToolInvocation(
                        tool_id=tool_id,
                        parameters=parameters,
                        target_id=target.target_id,
                        execution_id=tool_inv_id,
                        timeout_seconds=skill.timeout_seconds,
                        approval_authorized=approval_authorized,
                    )

                    result = await tool_executor.execute(invocation)
                    collected_results.append(result)

                    # If tool execution failed, stop and return failed result
                    if result.status != ToolExecutionStatus.SUCCESS:
                        return SkillExecutionResult(
                            skill_id=skill.skill_id,
                            execution_id=exec_id,
                            status=SkillExecutionStatus.EXECUTION_FAILED,
                            outputs=result.structured_output,
                            tool_results=tuple(collected_results),
                            error_message=result.error_message
                            or f"Tool {str(tool_id)!r} failed with status {result.status.value}",
                            duration_ms=int((time.monotonic() - start_time) * 1000),
                        )

                    # Asynchronous handoff detection (Invariant I1)
                    if result.active_job_ref is not None:
                        return SkillExecutionResult(
                            skill_id=skill.skill_id,
                            execution_id=exec_id,
                            status=SkillExecutionStatus.DISPATCHED,
                            outputs=result.structured_output,
                            tool_results=tuple(collected_results),
                            active_job_ref=result.active_job_ref,
                            duration_ms=int((time.monotonic() - start_time) * 1000),
                        )

                    merged_outputs.update(result.structured_output)

        except (asyncio.TimeoutError, TimeoutError):
            return SkillExecutionResult(
                skill_id=skill.skill_id,
                execution_id=exec_id,
                status=SkillExecutionStatus.TIMEOUT,
                tool_results=tuple(collected_results),
                error_message=f"Skill execution exceeded declared timeout of {skill.timeout_seconds}s",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        duration_ms = int((time.monotonic() - start_time) * 1000)
        return SkillExecutionResult(
            skill_id=skill.skill_id,
            execution_id=exec_id,
            status=SkillExecutionStatus.SUCCESS,
            outputs=merged_outputs,
            tool_results=tuple(collected_results),
            duration_ms=duration_ms,
        )
