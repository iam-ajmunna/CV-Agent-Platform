"""cv_agent.tools — Tools and Model Context Protocol (MCP) execution boundary.

Implements ADR-0005:
- Typed tool and execution target descriptors.
- Operational tool registry.
- Security constraints: shell prohibition, workspace containment, secret isolation, approval gating.
- Pluggable transport adapters for Local, Remote, and MCP execution.
- Asynchronous dispatch boundary returning ActiveJobHandoff.
"""

from __future__ import annotations

from cv_agent.tools.adapters import (
    LocalProcessAdapter,
    MockMCPEndpointAdapter,
    MockRemoteTargetAdapter,
)
from cv_agent.tools.contracts import (
    ActiveJobHandoff,
    ExecutionId,
    ExecutionTargetDescriptor,
    MCPServerId,
    TargetId,
    TargetType,
    ToolAvailabilityInfo,
    ToolDescriptor,
    ToolExecutionStatus,
    ToolExecutor,
    ToolId,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    ToolRiskLevel,
    deep_freeze,
)
from cv_agent.tools.executor import DefaultToolExecutor
from cv_agent.tools.registry import DefaultToolRegistry, detect_host_execution_target
from cv_agent.tools.security import (
    ToolSecurityError,
    check_approval_precondition,
    validate_argv_safety,
    validate_secret_isolation,
    validate_workspace_containment,
)

__all__ = [
    "ActiveJobHandoff",
    "DefaultToolExecutor",
    "DefaultToolRegistry",
    "ExecutionId",
    "ExecutionTargetDescriptor",
    "LocalProcessAdapter",
    "MCPServerId",
    "MockMCPEndpointAdapter",
    "MockRemoteTargetAdapter",
    "TargetId",
    "TargetType",
    "ToolAvailabilityInfo",
    "ToolDescriptor",
    "ToolExecutionStatus",
    "ToolExecutor",
    "ToolId",
    "ToolInvocation",
    "ToolRegistry",
    "ToolResult",
    "ToolRiskLevel",
    "ToolSecurityError",
    "check_approval_precondition",
    "deep_freeze",
    "detect_host_execution_target",
    "validate_argv_safety",
    "validate_secret_isolation",
    "validate_workspace_containment",
]
