"""cv_agent.tools.registry — Operational ToolRegistry and Target catalog.

Implements the ADR-0005 §3.5 and §4 runtime operational view for tools and execution targets.
Does NOT invent authoritative ToolId tokens; binds executable descriptors to
authoritative ToolIds declared by ADR-0001.
"""

from __future__ import annotations

import platform
from collections.abc import Mapping, Sequence

from cv_agent.domain.identifiers import ToolId
from cv_agent.tools.contracts import (
    ExecutionTargetDescriptor,
    TargetId,
    TargetType,
    ToolAvailabilityInfo,
    ToolDescriptor,
    ToolExecutionStatus,
    ToolRegistry,
)


class DefaultToolRegistry(ToolRegistry):
    """Operational registry for executable ToolDescriptors and ExecutionTargetDescriptors."""

    def __init__(
        self,
        tools: Sequence[ToolDescriptor] | Mapping[ToolId, ToolDescriptor] | None = None,
        targets: (
            Sequence[ExecutionTargetDescriptor]
            | Mapping[TargetId, ExecutionTargetDescriptor]
            | None
        ) = None,
    ) -> None:
        self._tools: dict[ToolId, ToolDescriptor] = {}
        self._targets: dict[TargetId, ExecutionTargetDescriptor] = {}

        if tools:
            tool_seq = tools.values() if isinstance(tools, Mapping) else tools
            for t in tool_seq:
                self.register(t)

        if targets:
            target_seq = targets.values() if isinstance(targets, Mapping) else targets
            for tgt in target_seq:
                self.register_target(tgt)

    def register(self, tool: ToolDescriptor) -> None:
        """Bind an operational descriptor to an existing authoritative ToolId."""
        if tool.tool_id in self._tools:
            raise ValueError(
                f"Duplicate tool registration for {tool.tool_id!r}: descriptor already registered"
            )
        self._tools[tool.tool_id] = tool

    def register_target(self, target: ExecutionTargetDescriptor) -> None:
        """Register a declared execution target environment."""
        if target.target_id in self._targets:
            raise ValueError(
                f"Duplicate target registration for {target.target_id!r}: target already registered"
            )
        self._targets[target.target_id] = target

    def get(self, tool_id: ToolId) -> ToolDescriptor | None:
        """Retrieve an operational tool descriptor by ToolId."""
        return self._tools.get(tool_id)

    def get_target(self, target_id: TargetId) -> ExecutionTargetDescriptor | None:
        """Retrieve a declared execution target by TargetId."""
        return self._targets.get(target_id)

    def list_tools(
        self, *, target_type: TargetType | None = None
    ) -> tuple[ToolDescriptor, ...]:
        """List registered tools, optionally filtered by compatible target type."""
        tools = sorted(self._tools.values(), key=lambda t: str(t.tool_id))
        if target_type is not None:
            return tuple(t for t in tools if target_type in t.compatible_target_types)
        return tuple(tools)

    def list_targets(self) -> tuple[ExecutionTargetDescriptor, ...]:
        """List registered execution targets."""
        return tuple(sorted(self._targets.values(), key=lambda tgt: str(tgt.target_id)))

    def check_availability(
        self, tool_id: ToolId, target_id: TargetId | None = None
    ) -> ToolAvailabilityInfo:
        """Evaluate operational availability against a target environment (ADR-0005 §3.5).

        Implements honest "known but unavailable" reporting without throwing exceptions.
        """
        tool = self._tools.get(tool_id)
        if tool is None:
            return ToolAvailabilityInfo(
                is_available=False,
                status=ToolExecutionStatus.TOOL_NOT_FOUND,
                reason=f"Tool {str(tool_id)!r} is not registered in operational tool catalog",
            )

        # Resolve execution target
        resolved_target: ExecutionTargetDescriptor
        if target_id is None:
            compatible = [
                tgt
                for tgt in self._targets.values()
                if tgt.target_type in tool.compatible_target_types and tgt.is_available
            ]
            if not compatible:
                return ToolAvailabilityInfo(
                    is_available=False,
                    status=ToolExecutionStatus.TARGET_UNAVAILABLE,
                    reason=(
                        f"No available execution target configured for tool {str(tool_id)!r} "
                        f"(requires one of {[t.value for t in tool.compatible_target_types]})"
                    ),
                )
            resolved_target = compatible[0]
        else:
            maybe_target = self._targets.get(target_id)
            if maybe_target is None:
                return ToolAvailabilityInfo(
                    is_available=False,
                    status=ToolExecutionStatus.TARGET_UNAVAILABLE,
                    reason=f"Execution target {str(target_id)!r} is not configured",
                )
            resolved_target = maybe_target

        # Hardware-aware constraint (D-014, D-022, ADR-0005 §3.4):
        # Tools requiring NVIDIA / CUDA runtimes cannot run on host-local macOS workstations.
        if resolved_target.target_type == TargetType.LOCAL and any(
            n in str(tool_id).lower()
            for n in ("nvidia", "tensorrt", "trt", "cuda", "tao", "deepstream")
        ):
            return ToolAvailabilityInfo(
                is_available=False,
                status=ToolExecutionStatus.TOOL_UNAVAILABLE,
                reason=(
                    f"NVIDIA tool {str(tool_id)!r} cannot execute on host-local macOS workstation (D-022); "
                    f"requires REMOTE or MCP_ENDPOINT target."
                ),
                target_reachable=True,
                runtime_verified=False,
            )

        if resolved_target.target_type not in tool.compatible_target_types:
            return ToolAvailabilityInfo(
                is_available=False,
                status=ToolExecutionStatus.TARGET_UNAVAILABLE,
                reason=(
                    f"Tool {str(tool_id)!r} is not compatible with target type {resolved_target.target_type.value!r} "
                    f"(requires one of {[t.value for t in tool.compatible_target_types]})"
                ),
                target_reachable=True,
                runtime_verified=False,
            )

        if not resolved_target.is_available:
            return ToolAvailabilityInfo(
                is_available=False,
                status=ToolExecutionStatus.TARGET_UNAVAILABLE,
                reason=f"Execution target {str(resolved_target.target_id)!r} is currently unavailable",
                target_reachable=False,
                runtime_verified=False,
            )

        return ToolAvailabilityInfo(
            is_available=True,
            status=ToolExecutionStatus.SUCCESS,
            reason=None,
            target_reachable=True,
            runtime_verified=True,
        )


def detect_host_execution_target() -> ExecutionTargetDescriptor:
    """Detect and return canonical ExecutionTargetDescriptor for current host machine (ADR-0005 §3.4).

    Identifies hardware architecture (Apple Silicon vs CPU), operating system, and returns
    a canonical LOCAL target descriptor for operational use.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()

    hardware = "cpu"
    if system == "darwin" and ("arm" in machine or "aarch64" in machine):
        hardware = "apple_silicon"

    return ExecutionTargetDescriptor(
        target_id=TargetId("target.local.host"),
        target_type=TargetType.LOCAL,
        is_available=True,
        description=f"Host Local Workstation ({system} {machine})",
        target_metadata={
            "hardware": hardware,
            "os": system,
            "arch": machine,
        },
    )
