"""cv_agent.tools.adapters — Transport adapters for Local, Remote, and MCP execution.

Implements the transport decoupling invariant (ADR-0005 §3.3).
Mediate execution across:
1. Local subprocess adapter (structured argv, zero shell).
2. Remote compute target adapter (asynchronous job dispatch returning ActiveJobHandoff).
3. MCP invocation endpoint adapter (untrusted protocol boundary with output validation).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cv_agent.domain.identifiers import ToolId
from cv_agent.llm.schema import SchemaValidationError, validate_json_schema
from cv_agent.tools.contracts import (
    ActiveJobHandoff,
    ExecutionTargetDescriptor,
    ToolDescriptor,
    ToolExecutionStatus,
    ToolInvocation,
    ToolResult,
)
from cv_agent.tools.security import (
    ToolSecurityError,
    validate_argv_safety,
    validate_workspace_containment,
)


class LocalProcessAdapter:
    """Dispatches execution to local host subprocess using structured argv (ADR-0005 §3.7).

    Guarantees zero shell interpretation (shell=False strictly enforced).
    """

    def __init__(
        self,
        workspace_root: Path | None = None,
        command_bindings: Mapping[ToolId, Sequence[str]] | None = None,
    ) -> None:
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.command_bindings: dict[ToolId, Sequence[str]] = dict(
            command_bindings or {}
        )

    async def execute(
        self,
        tool: ToolDescriptor,
        target: ExecutionTargetDescriptor,
        invocation: ToolInvocation,
        argv_builder: (
            Callable[[ToolDescriptor, Mapping[str, Any]], Sequence[str]] | None
        ) = None,
    ) -> ToolResult:
        """Execute local process with argv safety, workspace containment, and timeout."""
        start_time = time.monotonic()

        # Build command argv
        if argv_builder is not None:
            argv = list(argv_builder(tool, invocation.parameters))
        elif "argv" in invocation.parameters:
            raw_argv = invocation.parameters["argv"]
            if not isinstance(raw_argv, (list, tuple)):
                return ToolResult(
                    tool_id=invocation.tool_id,
                    execution_id=invocation.execution_id,
                    target_id=invocation.target_id,
                    status=ToolExecutionStatus.INVALID_PARAMETERS,
                    error_message="'argv' parameter must be a sequence of strings",
                )
            argv = [str(a) for a in raw_argv]
        elif tool.tool_id in self.command_bindings:
            argv = list(self.command_bindings[tool.tool_id])
        else:
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.POLICY_VIOLATION,
                error_message=(
                    f"No executable command binding or argv_builder configured for local execution of tool {tool.tool_id!r}. "
                    "Automatic conversion of logical ToolId to executable process name is strictly prohibited (ADR-0005 §3.3)."
                ),
            )

        # Enforce structured argv safety (no shell concatenation or metacharacters)
        try:
            validate_argv_safety(argv)
        except ToolSecurityError as exc:
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=(
                    ToolExecutionStatus.INVALID_PARAMETERS
                    if exc.code == "INVALID_PARAMETERS"
                    else ToolExecutionStatus.POLICY_VIOLATION
                ),
                error_message=str(exc),
            )

        # Validate path containment for any file/path parameters
        for k, v in invocation.parameters.items():
            if (
                "path" in k.lower() or "file" in k.lower() or "dir" in k.lower()
            ) and isinstance(v, str):
                try:
                    validate_workspace_containment(v, self.workspace_root)
                except ToolSecurityError as exc:
                    return ToolResult(
                        tool_id=invocation.tool_id,
                        execution_id=invocation.execution_id,
                        target_id=invocation.target_id,
                        status=ToolExecutionStatus.POLICY_VIOLATION,
                        error_message=str(exc),
                    )

        try:
            # Strictly shell=False — structured argv execution
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace_root),
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=float(invocation.timeout_seconds)
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            if proc.returncode == 0:
                status = ToolExecutionStatus.SUCCESS
                error_msg = None
            else:
                status = ToolExecutionStatus.EXECUTION_FAILED
                error_msg = f"Process exited with non-zero code {proc.returncode}"

            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=status,
                structured_output={"exit_code": proc.returncode},
                diagnostics={"stdout": stdout_text, "stderr": stderr_text},
                duration_ms=duration_ms,
                error_message=error_msg,
            )

        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            with contextlib.suppress(OSError, ProcessLookupError):
                proc.kill()
                await proc.wait()
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.EXECUTION_TIMEOUT,
                duration_ms=duration_ms,
                error_message=f"Execution timed out after {invocation.timeout_seconds} seconds",
            )
        except (OSError, RuntimeError) as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.TRANSPORT_ERROR,
                duration_ms=duration_ms,
                error_message=f"Subprocess launch failed: {exc}",
            )


class MockRemoteTargetAdapter:
    """Dispatches execution to remote compute target (ADR-0005 §3.4, §3.8).

    For asynchronous workloads, returns ActiveJobHandoff where ToolResult.status == SUCCESS
    denotes successful dispatch/acceptance, leaving lifecycle monitoring to orchestration.
    """

    def __init__(
        self,
        is_reachable: bool = True,
        simulate_async: bool = False,
        fixed_result: ToolResult | None = None,
    ) -> None:
        self.is_reachable = is_reachable
        self.simulate_async = simulate_async
        self.fixed_result = fixed_result
        self.dispatched_invocations: list[ToolInvocation] = []

    async def execute(
        self,
        tool: ToolDescriptor,
        target: ExecutionTargetDescriptor,
        invocation: ToolInvocation,
    ) -> ToolResult:
        self.dispatched_invocations.append(invocation)

        if not self.is_reachable or not target.is_available:
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.TARGET_UNAVAILABLE,
                error_message=f"Remote target {str(target.target_id)!r} is unreachable",
            )

        if self.fixed_result is not None:
            return self.fixed_result

        now_utc = datetime.now(timezone.utc).isoformat()

        if self.simulate_async:
            # Asynchronous dispatch: return ActiveJobHandoff (ADR-0005 §3.8)
            handoff = ActiveJobHandoff(
                job_id=f"job-{invocation.execution_id!s}",
                job_type="training" if "train" in str(tool.tool_id) else "profiling",
                target="remote_gpu",
                submitted_at=now_utc,
                status="submitted",
                heartbeat_timeout_seconds=3600,
                metadata={"remote_queue": "priority-gpu"},
            )
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.SUCCESS,  # Denotes successful dispatch, NOT completion!
                structured_output={
                    "dispatch_acknowledged": True,
                    "job_id": handoff.job_id,
                },
                active_job_ref=handoff,
            )

        # Synchronous execution simulation
        return ToolResult(
            tool_id=invocation.tool_id,
            execution_id=invocation.execution_id,
            target_id=invocation.target_id,
            status=ToolExecutionStatus.SUCCESS,
            structured_output={"remote_status": "completed", "output": "ok"},
            duration_ms=150,
        )


class MockMCPEndpointAdapter:
    """Dispatches execution over Model Context Protocol boundary (ADR-0005 §3.6).

    MCP endpoints are untrusted transport boundaries:
    1. Discovery produces runtime candidates; external tool names must resolve to an authoritative ToolId.
    2. MCP servers cannot synthesize or invent ToolIds; unmapped candidates are rejected.
    3. Outputs must be validated against declared output_schema before acceptance, failing with SCHEMA_VIOLATION.
    """

    def __init__(
        self,
        server_id: str = "mock-mcp-server",
        discovered_tool_candidates: Mapping[str, Mapping[str, Any]] | None = None,
        tool_mapping: Mapping[str, ToolId] | None = None,
        simulated_response: Mapping[str, Any] | None = None,
        transport_error: bool = False,
    ) -> None:
        self.server_id = server_id
        self.discovered_tool_candidates = dict(discovered_tool_candidates or {})
        self.tool_mapping: dict[str, ToolId] = {}
        if tool_mapping:
            for external_candidate_name, auth_tool_id in tool_mapping.items():
                self.register_tool_mapping(external_candidate_name, auth_tool_id)
        self.simulated_response = simulated_response
        self.transport_error = transport_error
        self.dispatched_invocations: list[ToolInvocation] = []

    def discover_candidates(self) -> tuple[str, ...]:
        """Query MCP endpoint to discover external candidate tool names.

        Produces runtime metadata and candidate descriptors without creating authoritative ToolIds.
        """
        return tuple(self.discovered_tool_candidates.keys())

    def register_tool_mapping(
        self, external_candidate_name: str, authoritative_tool_id: ToolId
    ) -> None:
        """Explicitly map an external MCP candidate name to an authoritative project ToolId.

        Per ADR-0005 §3.6: Rejects candidates that were never discovered.
        """
        if external_candidate_name not in self.discovered_tool_candidates:
            raise ToolSecurityError(
                "UNDISCOVERED_MCP_TOOL",
                f"Cannot map external candidate {external_candidate_name!r}: tool was never discovered on MCP server {self.server_id!r} (ADR-0005 §3.6).",
            )
        self.tool_mapping[external_candidate_name] = authoritative_tool_id

    def resolve_candidate(self, external_candidate_name: str) -> ToolId:
        """Resolve external candidate tool name to an authoritative ToolId.

        Per ADR-0005 §3.6: An MCP-discovered tool becomes invocable through the agent only when
        it resolves to an authoritative ToolId defined by ADR-0001. Unmapped candidates remain
        discovered but unavailable, preventing external endpoints from silently extending or
        mutating the project's capability/tool catalogue.
        """
        if external_candidate_name not in self.tool_mapping:
            raise ToolSecurityError(
                "UNMAPPED_MCP_TOOL",
                f"MCP tool candidate {external_candidate_name!r} is not mapped to an authoritative ToolId (ADR-0005 §3.6). "
                "MCP endpoints cannot synthesize new ToolIds.",
            )
        return self.tool_mapping[external_candidate_name]

    async def execute(
        self,
        tool: ToolDescriptor,
        target: ExecutionTargetDescriptor,
        invocation: ToolInvocation,
    ) -> ToolResult:
        self.dispatched_invocations.append(invocation)

        # Authority boundary check: verify the invoked tool is mapped from an authorized candidate.
        # Per ADR-0005 §3.6, execution authorization must fail closed: unmapped tools or empty
        # mappings cannot be executed.
        if invocation.tool_id not in self.tool_mapping.values():
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.PERMISSION_DENIED,
                error_message=(
                    f"ToolId {str(invocation.tool_id)!r} is not mapped from an authorized candidate on MCP server {self.server_id!r}. "
                    "Unmapped MCP tools cannot be executed (ADR-0005 §3.6)."
                ),
            )

        if self.transport_error or not target.is_available:
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.TRANSPORT_ERROR,
                error_message=f"MCP transport to server {self.server_id!r} disconnected",
            )

        raw_output = (
            self.simulated_response
            if self.simulated_response is not None
            else {"result": "mcp_ok"}
        )

        # Untrusted output boundary: validate external output against declared output_schema
        if tool.output_schema:
            try:
                validate_json_schema(raw_output, tool.output_schema)
            except SchemaValidationError as exc:
                return ToolResult(
                    tool_id=invocation.tool_id,
                    execution_id=invocation.execution_id,
                    target_id=invocation.target_id,
                    status=ToolExecutionStatus.SCHEMA_VIOLATION,
                    error_message=f"MCP output validation failed against declared schema: {exc}",
                    diagnostics={"raw_mcp_output": raw_output},
                )

        return ToolResult(
            tool_id=invocation.tool_id,
            execution_id=invocation.execution_id,
            target_id=invocation.target_id,
            status=ToolExecutionStatus.SUCCESS,
            structured_output=raw_output,
            duration_ms=45,
        )
