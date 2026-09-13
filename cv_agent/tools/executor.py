"""cv_agent.tools.executor — Central tool invocation executor and dispatcher.

Implements the ADR-0005 ToolExecutor protocol, coordinating:
1. Tool descriptor & target resolution via ToolRegistry.
2. Operational availability verification.
3. Pre-invocation approval precondition enforcement.
4. Input parameter schema validation.
5. Secret isolation and security checks.
6. Transport adapter dispatch (LOCAL, REMOTE, MCP_ENDPOINT).
7. Normalization of execution results.
"""

from __future__ import annotations

import time
from pathlib import Path

from cv_agent.llm.schema import SchemaValidationError, validate_json_schema
from cv_agent.tools.adapters import (
    LocalProcessAdapter,
    MockMCPEndpointAdapter,
    MockRemoteTargetAdapter,
)
from cv_agent.tools.contracts import (
    ExecutionTargetDescriptor,
    TargetType,
    ToolAvailabilityInfo,
    ToolDescriptor,
    ToolExecutionStatus,
    ToolExecutor,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
)
from cv_agent.tools.security import (
    ToolSecurityError,
    check_approval_precondition,
    validate_secret_isolation,
)


class DefaultToolExecutor(ToolExecutor):
    """Authoritative ADR-0005 tool execution coordinator."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        workspace_root: Path | None = None,
        local_adapter: LocalProcessAdapter | None = None,
        remote_adapter: MockRemoteTargetAdapter | None = None,
        mcp_adapter: MockMCPEndpointAdapter | None = None,
    ) -> None:
        self._registry = registry
        self._workspace_root = (workspace_root or Path.cwd()).resolve()
        self._local_adapter = local_adapter or LocalProcessAdapter(self._workspace_root)
        self._remote_adapter = remote_adapter or MockRemoteTargetAdapter()
        self._mcp_adapter = mcp_adapter or MockMCPEndpointAdapter()

    async def probe(
        self, tool: ToolDescriptor, target: ExecutionTargetDescriptor
    ) -> ToolAvailabilityInfo:
        """Evaluate operational availability against a target environment."""
        return self._registry.check_availability(tool.tool_id, target.target_id)

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        """Execute a tool invocation with full pre-execution safety and validation."""
        start_time = time.monotonic()

        # 1. Resolve tool descriptor from registry
        tool = self._registry.get(invocation.tool_id)
        if tool is None:
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.TOOL_NOT_FOUND,
                error_message=f"Tool {str(invocation.tool_id)!r} is not registered in operational tool catalog",
            )

        # 2. Verify target availability & compatibility
        avail = self._registry.check_availability(
            invocation.tool_id, invocation.target_id
        )
        if not avail.is_available:
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=avail.status,
                error_message=avail.reason,
            )

        # 3. Retrieve target descriptor
        if hasattr(self._registry, "get_target"):
            target = self._registry.get_target(invocation.target_id)
        else:
            target = None

        if target is None:
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.TARGET_UNAVAILABLE,
                error_message=f"Target {str(invocation.target_id)!r} is not configured",
            )

        # 4. Enforce approval precondition (ADR-0005 §3.7, §3.8)
        try:
            check_approval_precondition(tool, invocation)
        except ToolSecurityError as exc:
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.PERMISSION_DENIED,
                error_message=exc.message,
            )

        # 5. Enforce secret isolation (ADR-0005 §3.7)
        try:
            validate_secret_isolation(invocation.parameters)
        except ToolSecurityError as exc:
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.POLICY_VIOLATION,
                error_message=exc.message,
            )

        # 6. Validate input parameters against declared input_schema
        if tool.input_schema:
            try:
                validate_json_schema(dict(invocation.parameters), tool.input_schema)
            except SchemaValidationError as exc:
                return ToolResult(
                    tool_id=invocation.tool_id,
                    execution_id=invocation.execution_id,
                    target_id=invocation.target_id,
                    status=ToolExecutionStatus.INVALID_PARAMETERS,
                    error_message=f"Parameter schema validation failed: {exc}",
                )

        # 7. Dispatch to appropriate transport adapter (ADR-0005 §3.3, §3.4)
        if target.target_type == TargetType.LOCAL:
            return await self._local_adapter.execute(tool, target, invocation)
        elif target.target_type == TargetType.REMOTE:
            return await self._remote_adapter.execute(tool, target, invocation)
        elif target.target_type == TargetType.MCP_ENDPOINT:
            return await self._mcp_adapter.execute(tool, target, invocation)
        else:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.TRANSPORT_ERROR,
                duration_ms=duration_ms,
                error_message=f"Unsupported target type {target.target_type!r}",
            )
