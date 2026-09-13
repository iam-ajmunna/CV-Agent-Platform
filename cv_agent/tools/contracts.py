"""cv_agent.tools.contracts — Canonical typed contracts for ADR-0005 Tools and MCP.

Defines the transport-independent tool execution boundary, target descriptors,
invocation contracts, normalized results, and asynchronous job handoff references.
All contracts are immutable and frozen.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from cv_agent.domain.identifiers import ToolId, _Identifier


class TargetId(_Identifier):
    """Identity of a declared execution target environment."""


class ExecutionId(_Identifier):
    """Identity of a concrete tool execution invocation."""


class MCPServerId(_Identifier):
    """Identity of an external Model Context Protocol server connection."""


def deep_freeze(val: Any) -> Any:
    """Recursively freeze mappings, lists, tuples, and sets for defensive immutability."""
    if isinstance(val, Mapping):
        return MappingProxyType({k: deep_freeze(v) for k, v in val.items()})
    if isinstance(val, (list, tuple)):
        return tuple(deep_freeze(v) for v in val)
    if isinstance(val, (set, frozenset)):
        return frozenset(deep_freeze(v) for v in val)
    return val


class TargetType(str, Enum):
    """Class of compute or invocation environment."""

    LOCAL = "local"
    REMOTE = "remote"
    MCP_ENDPOINT = "mcp_endpoint"


class ToolRiskLevel(str, Enum):
    """Operational risk and resource intensity classification."""

    READ_ONLY = "read_only"  # Non-mutating inspection, query, or local check
    RESOURCE_INTENSIVE = "resource_intensive"  # Heavy GPU/CPU compute, profiling
    DESTRUCTIVE = (
        "destructive"  # Data mutations, deletions, system configuration changes
    )


class ToolExecutionStatus(str, Enum):
    """Unified status vocabulary representing tool resolution, validation, and execution/dispatch outcomes."""

    SUCCESS = "success"
    TOOL_NOT_FOUND = "tool_not_found"
    TARGET_UNAVAILABLE = "target_unavailable"
    TOOL_UNAVAILABLE = "tool_unavailable"
    INVALID_PARAMETERS = "invalid_parameters"
    PERMISSION_DENIED = "permission_denied"
    POLICY_VIOLATION = "policy_violation"
    EXECUTION_TIMEOUT = "execution_timeout"
    EXECUTION_CANCELLED = "execution_cancelled"
    EXECUTION_FAILED = "execution_failed"
    SCHEMA_VIOLATION = "schema_violation"
    TRANSPORT_ERROR = "transport_error"


@dataclass(frozen=True)
class ExecutionTargetDescriptor:
    """Represents a declared compute or invocation environment (ADR-0005 §3.4)."""

    target_id: TargetId
    target_type: TargetType
    is_available: bool
    description: str = ""
    unavailable_reason: str | None = None
    target_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_metadata", deep_freeze(self.target_metadata))


@dataclass(frozen=True)
class ToolDescriptor:
    """Logical architectural specification of an executable tool (ADR-0005 §3.3)."""

    tool_id: ToolId  # Authoritative identity defined in ADR-0001 Capability Registry
    name: str
    description: str
    compatible_target_types: tuple[TargetType, ...]
    input_schema: Mapping[str, Any]  # JSON Schema for parameters
    output_schema: Mapping[str, Any]  # JSON Schema for structured output
    risk_level: ToolRiskLevel
    requires_approval: bool  # Declares an approval precondition; does not replace authoritative orchestration policy
    default_timeout_seconds: int = 300
    is_idempotent: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("ToolDescriptor.name must not be empty")
        if not self.compatible_target_types:
            raise ValueError(
                "ToolDescriptor must declare at least one compatible target type"
            )
        object.__setattr__(self, "input_schema", deep_freeze(self.input_schema))
        object.__setattr__(self, "output_schema", deep_freeze(self.output_schema))
        object.__setattr__(
            self, "compatible_target_types", tuple(self.compatible_target_types)
        )


@dataclass(frozen=True)
class ActiveJobHandoff:
    """Transport-neutral handoff contract mirroring ADR-0003's ActiveJobRef (ADR-0005 §3.8).

    ADR-0003 converts and records this as the authoritative ActiveJobRef in AgentState
    and owns all subsequent lifecycle monitoring, restart recovery, and checkpointing.
    """

    job_id: str
    job_type: Literal["training", "profiling", "evaluation", "export"]
    target: Literal["local_gpu", "remote_gpu", "cloud"]
    submitted_at: str
    status: Literal["submitted", "running", "completed", "failed"]
    heartbeat_timeout_seconds: int = 3600
    expires_at: str | None = (
        None  # ISO-8601 UTC; if set, handoff is invalid after this instant
    )
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.job_id or not self.job_id.strip():
            raise ValueError("ActiveJobHandoff.job_id must not be empty")
        if self.expires_at is not None:
            try:
                dt = datetime.fromisoformat(self.expires_at)
                if dt.tzinfo is None:
                    raise ValueError(
                        f"ActiveJobHandoff.expires_at must be timezone-aware ISO-8601 string, got: {self.expires_at!r}"
                    )
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"ActiveJobHandoff.expires_at must be valid ISO-8601 string: {exc}"
                ) from None
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))

    def is_expired(self, now: datetime | None = None) -> bool:
        """Evaluate whether this handoff tracking TTL has lapsed.

        Note per ADR-0005 §3.8: Expiry indicates the handoff is invalid for tracking,
        but does NOT indicate that the remote execution failed (expired != failed).
        """
        if self.expires_at is None:
            return False
        reference = now if now is not None else datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        try:
            expires_dt = datetime.fromisoformat(self.expires_at)
            return reference >= expires_dt
        except (ValueError, TypeError):
            return False


@dataclass(frozen=True)
class ToolInvocation:
    """A concrete request to invoke a tool on a target (ADR-0005 §4)."""

    tool_id: ToolId
    parameters: Mapping[str, Any]
    target_id: TargetId
    execution_id: ExecutionId
    timeout_seconds: int
    approval_authorized: bool = False  # Verified against ADR-0003 approval state
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("ToolInvocation.timeout_seconds must be positive")
        object.__setattr__(self, "parameters", deep_freeze(self.parameters))


@dataclass(frozen=True)
class ToolResult:
    """Normalized, transport-independent output from a tool invocation (ADR-0005 §3.9)."""

    tool_id: ToolId
    execution_id: ExecutionId
    target_id: TargetId
    status: ToolExecutionStatus  # Terminal outcome for sync tools; dispatch outcome for async tools
    structured_output: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)  # stdout, stderr, logs
    artifacts: tuple[str, ...] = ()
    duration_ms: int = 0
    error_message: str | None = None
    active_job_ref: ActiveJobHandoff | None = (
        None  # Handoff to ADR-0003 ActiveJobRef when invocation is asynchronous
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "structured_output", deep_freeze(self.structured_output)
        )
        object.__setattr__(self, "diagnostics", deep_freeze(self.diagnostics))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))


@dataclass(frozen=True)
class ToolAvailabilityInfo:
    """Structured availability evaluation returned by tool probing (ADR-0005 §3.5)."""

    is_available: bool
    status: ToolExecutionStatus
    reason: str | None = None
    target_reachable: bool = False
    runtime_verified: bool = False


@runtime_checkable
class ToolExecutor(Protocol):
    """Protocol for executing tool invocations across transport adapters (ADR-0005 §4)."""

    async def execute(self, invocation: ToolInvocation) -> ToolResult: ...

    async def probe(
        self, tool: ToolDescriptor, target: ExecutionTargetDescriptor
    ) -> ToolAvailabilityInfo:
        """Evaluate operational availability against a target."""
        ...


@runtime_checkable
class ToolRegistry(Protocol):
    """Runtime operational registry/view for authoritative executable tool descriptors (ADR-0005 §4).

    Acts as an operational cache and resolver; does NOT create or redefine authoritative
    ToolId identities, which remain strictly owned by ADR-0001.
    """

    def register(self, tool: ToolDescriptor) -> None:
        """Bind an operational descriptor to an existing authoritative ADR-0001 ToolId."""
        ...

    def get(self, tool_id: ToolId) -> ToolDescriptor | None: ...

    def list_tools(
        self, *, target_type: TargetType | None = None
    ) -> tuple[ToolDescriptor, ...]: ...

    def check_availability(
        self, tool_id: ToolId, target_id: TargetId | None = None
    ) -> ToolAvailabilityInfo: ...
