"""cv_agent.tools.barrier — Execution barrier for DISCOVER / DEFINE phases.

Enforces PM Gate G5, ADR-0005, and ADR-0008:
- During DISCOVER and DEFINE, tool executions, subprocesses, MCP calls, skill executions,
  and background jobs are strictly prohibited.
- Guards/denies dispatch by raising WorkflowBoundaryViolationError without transferring
  ADR-0005 execution authority into ADR-0008.
- Records all invocation attempts for boundary audit verification.
"""

from __future__ import annotations

from cv_agent.reasoning.errors import WorkflowBoundaryViolationError
from cv_agent.tools.contracts import (
    ExecutionTargetDescriptor,
    ToolAvailabilityInfo,
    ToolDescriptor,
    ToolExecutor,
    ToolInvocation,
    ToolResult,
)


class Phase4ExecutionBarrier(ToolExecutor):
    """Authoritative execution barrier guarding DISCOVER/DEFINE against accidental dispatch."""

    def __init__(self, delegate: ToolExecutor | None = None) -> None:
        self._delegate = delegate
        self._attempted_invocations: list[ToolInvocation] = []

    @property
    def attempted_invocations(self) -> tuple[ToolInvocation, ...]:
        """Audit log of all invocation attempts trapped by the barrier."""
        return tuple(self._attempted_invocations)

    @property
    def invocation_count(self) -> int:
        """Count of trapped execution attempts."""
        return len(self._attempted_invocations)

    async def probe(
        self, tool: ToolDescriptor, target: ExecutionTargetDescriptor
    ) -> ToolAvailabilityInfo:
        """Evaluate operational availability if delegate exists, or reject."""
        if self._delegate is not None:
            return await self._delegate.probe(tool, target)
        raise WorkflowBoundaryViolationError(
            f"Tool probing is prohibited during DISCOVER/DEFINE: attempted to probe {tool.tool_id}"
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        """Strictly denies any tool execution during DISCOVER/DEFINE phases."""
        self._attempted_invocations.append(invocation)
        raise WorkflowBoundaryViolationError(
            f"Workflow boundary violation: attempted execution of tool {str(invocation.tool_id)!r} "
            f"during DISCOVER/DEFINE. Operational characterization and decomposition must complete "
            f"before any physical execution (ADR-0008, [P§29.1], [P§34])."
        )
