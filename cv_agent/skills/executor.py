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
import re
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
    """Validate parameter dictionary against comprehensive JSON schema requirements (I4)."""
    if not schema:
        return

    if not isinstance(parameters, Mapping):
        raise ParameterValidationError(
            f"Parameters must be a JSON object (mapping), got {type(parameters).__name__}"
        )

    _validate_object_against_schema(parameters, schema, path="")


def _validate_object_against_schema(
    data: Mapping[str, Any],
    schema: Mapping[str, Any],
    path: str,
) -> None:
    expected_type = schema.get("type")
    if expected_type and expected_type != "object":
        prefix = f"Parameter at {path!r}: " if path else ""
        raise ParameterValidationError(f"{prefix}Expected type 'object', got mapping")

    # 1. Required properties
    required = schema.get("required", ())
    for req_key in required:
        if req_key not in data:
            prop_path = f"{path}.{req_key}" if path else req_key
            raise ParameterValidationError(
                f"Missing required parameter {prop_path!r} declared in parameter_schema"
            )

    # 2. Additional properties check
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        for key in data:
            if key not in properties:
                prop_path = f"{path}.{key}" if path else key
                raise ParameterValidationError(
                    f"Additional property {prop_path!r} is not allowed by parameter_schema"
                )

    # 3. Property schema validation
    if isinstance(properties, Mapping):
        for key, prop_schema in properties.items():
            if key in data and isinstance(prop_schema, Mapping):
                val = data[key]
                prop_path = f"{path}.{key}" if path else key
                _validate_value_against_schema(val, prop_schema, prop_path)


def _validate_value_against_schema(
    val: Any,
    schema: Mapping[str, Any],
    path: str,
) -> None:
    expected_type = schema.get("type")

    # Type check
    if expected_type == "string":
        if not isinstance(val, str):
            raise ParameterValidationError(
                f"Parameter {path!r} must be string, got {type(val).__name__}"
            )
        min_len = schema.get("minLength")
        if min_len is not None and len(val) < min_len:
            raise ParameterValidationError(
                f"Parameter {path!r} length must be >= {min_len}, got {len(val)}"
            )
        max_len = schema.get("maxLength")
        if max_len is not None and len(val) > max_len:
            raise ParameterValidationError(
                f"Parameter {path!r} length must be <= {max_len}, got {len(val)}"
            )
        pattern = schema.get("pattern")
        if pattern is not None and not re.search(pattern, val):
            raise ParameterValidationError(
                f"Parameter {path!r} must match pattern {pattern!r}"
            )

    elif expected_type == "integer":
        if isinstance(val, bool) or not isinstance(val, int):
            raise ParameterValidationError(
                f"Parameter {path!r} must be integer, got {type(val).__name__}"
            )
        _validate_number_constraints(val, schema, path)

    elif expected_type == "number":
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ParameterValidationError(
                f"Parameter {path!r} must be number, got {type(val).__name__}"
            )
        _validate_number_constraints(val, schema, path)

    elif expected_type == "boolean":
        if not isinstance(val, bool):
            raise ParameterValidationError(
                f"Parameter {path!r} must be boolean, got {type(val).__name__}"
            )

    elif expected_type == "array":
        if not isinstance(val, (list, tuple)):
            raise ParameterValidationError(
                f"Parameter {path!r} must be array (list/tuple), got {type(val).__name__}"
            )
        min_items = schema.get("minItems")
        if min_items is not None and len(val) < min_items:
            raise ParameterValidationError(
                f"Parameter {path!r} items count must be >= {min_items}, got {len(val)}"
            )
        max_items = schema.get("maxItems")
        if max_items is not None and len(val) > max_items:
            raise ParameterValidationError(
                f"Parameter {path!r} items count must be <= {max_items}, got {len(val)}"
            )
        items_schema = schema.get("items")
        if isinstance(items_schema, Mapping):
            for i, item in enumerate(val):
                _validate_value_against_schema(item, items_schema, f"{path}[{i}]")

    elif expected_type == "object":
        if not isinstance(val, Mapping):
            raise ParameterValidationError(
                f"Parameter {path!r} must be object (mapping), got {type(val).__name__}"
            )
        _validate_object_against_schema(val, schema, path)

    # Enum check
    if "enum" in schema:
        allowed_enum = schema["enum"]
        if val not in allowed_enum:
            raise ParameterValidationError(
                f"Parameter {path!r} value {val!r} is not in allowed enum: {allowed_enum}"
            )


def _validate_number_constraints(
    val: float,
    schema: Mapping[str, Any],
    path: str,
) -> None:
    if "minimum" in schema and val < schema["minimum"]:
        raise ParameterValidationError(
            f"Parameter {path!r} must be >= {schema['minimum']}, got {val}"
        )
    if "maximum" in schema and val > schema["maximum"]:
        raise ParameterValidationError(
            f"Parameter {path!r} must be <= {schema['maximum']}, got {val}"
        )
    if "exclusiveMinimum" in schema and val <= schema["exclusiveMinimum"]:
        raise ParameterValidationError(
            f"Parameter {path!r} must be > {schema['exclusiveMinimum']}, got {val}"
        )
    if "exclusiveMaximum" in schema and val >= schema["exclusiveMaximum"]:
        raise ParameterValidationError(
            f"Parameter {path!r} must be < {schema['exclusiveMaximum']}, got {val}"
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

        # 1. Parameter schema validation (I4)
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

        # 3. Declarative Prerequisites Verification (Important I3)
        if skill.prerequisites:
            target_prereqs = target.target_metadata.get("prerequisites")
            target_features = target.target_metadata.get("features")
            provided: set[str] = set()
            if isinstance(target_prereqs, (list, tuple, set, frozenset)):
                provided.update(str(p).lower() for p in target_prereqs)
            if isinstance(target_features, (list, tuple, set, frozenset)):
                provided.update(str(f).lower() for f in target_features)

            if (
                "prerequisites" in target.target_metadata
                or "features" in target.target_metadata
            ):
                missing = [p for p in skill.prerequisites if p.lower() not in provided]
                if missing:
                    return SkillExecutionResult(
                        skill_id=skill.skill_id,
                        execution_id=exec_id,
                        status=SkillExecutionStatus.PREREQUISITE_FAILED,
                        error_message=(
                            f"Skill execution failed declarative prerequisites verification on target "
                            f"{str(target.target_id)!r}: {missing}"
                        ),
                        duration_ms=int((time.monotonic() - start_time) * 1000),
                    )

        # 4. Target type validation
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

        async def _run_tool_pipeline() -> SkillExecutionResult:
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

            duration_ms = int((time.monotonic() - start_time) * 1000)
            return SkillExecutionResult(
                skill_id=skill.skill_id,
                execution_id=exec_id,
                status=SkillExecutionStatus.SUCCESS,
                outputs=merged_outputs,
                tool_results=tuple(collected_results),
                duration_ms=duration_ms,
            )

        try:
            return await asyncio.wait_for(
                _run_tool_pipeline(),
                timeout=float(skill.timeout_seconds),
            )
        except (asyncio.TimeoutError, TimeoutError):
            return SkillExecutionResult(
                skill_id=skill.skill_id,
                execution_id=exec_id,
                status=SkillExecutionStatus.TIMEOUT,
                tool_results=tuple(collected_results),
                error_message=f"Skill execution exceeded declared timeout of {skill.timeout_seconds}s",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )
