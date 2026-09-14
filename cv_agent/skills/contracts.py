"""cv_agent.skills.contracts — Canonical typed contracts for ADR-0007 Skills Architecture.

Implements:
- Authoritative identifier types and provenance enumerations.
- Hardware classification and numeric CUDA compute capability validation.
- TargetRequirements with hardware class subsumption.
- SkillDescriptor declarative procedural expertise contract.
- SkillResolution delegating availability state to ADR-0001.
- Closed SkillExecutionStatus with explicit DISPATCHED and ActiveJobHandoff semantics.
- Immutable SkillExecutionResult with deep freeze.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from cv_agent.domain.contracts import AvailabilityState
from cv_agent.domain.identifiers import CapabilityId, SkillId, ToolId
from cv_agent.tools.contracts import (
    ActiveJobHandoff,
    ExecutionTargetDescriptor,
    TargetType,
    ToolResult,
    deep_freeze,
)

_ENV_VAR_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CUDA_COMPUTE_PATTERN = re.compile(r"^[0-9]+\.[0-9]+$")


class SkillProvenance(str, Enum):
    """Provenance origin of skill procedural instructions."""

    INTERNAL = "internal"  # Packaged or workspace procedural recipe
    EXTERNAL = "external"  # Declarative vendor adapter (e.g. NVIDIA per [P§15])
    DISCOVERED = "discovered"  # Ephemeral runtime candidate (e.g. via MCP)


class HardwareClass(str, Enum):
    """Authoritative hardware category vocabulary."""

    ANY = "any"
    CPU = "cpu"
    NVIDIA_GPU = "nvidia_gpu"
    JETSON = "jetson"
    JETSON_ORIN = "jetson_orin"
    JETSON_XAVIER = "jetson_xavier"


def parse_cuda_compute(val: str) -> tuple[int, int]:
    """Parse and validate CUDA compute capability string into a numeric (major, minor) tuple.

    Canonical syntax: ^[0-9]+\\.[0-9]+$ (e.g. '7.0', '8.0', '8.7', '9.0').
    Comparison semantics: numeric tuple comparison, preventing lexical string errors.
    """
    if not isinstance(val, str) or not _CUDA_COMPUTE_PATTERN.fullmatch(val):
        raise ValueError(
            f"min_cuda_compute must match regex '^[0-9]+\\.[0-9]+$', got {val!r}"
        )
    major_str, minor_str = val.split(".", 1)
    return int(major_str), int(minor_str)


def is_hardware_compatible(required: HardwareClass, target_hw: str) -> bool:
    """Evaluate hardware class compatibility using strict subsumption hierarchy.

    Hierarchy:
      - ANY: Satisfied by any target hardware.
      - CPU: Satisfied by any target.
      - NVIDIA_GPU: Satisfied by discrete NVIDIA GPUs (dGPU) and Jetson targets.
      - JETSON: Satisfied by any Jetson platform (Orin, Xavier).
      - JETSON_ORIN: Satisfied strictly by Jetson Orin platforms.
      - JETSON_XAVIER: Satisfied strictly by Jetson Xavier platforms.

    Note: Local workstation macOS arm64 ('apple_silicon' / 'cpu') strictly fails
    NVIDIA_GPU, JETSON, JETSON_ORIN, and JETSON_XAVIER (D-022).
    """
    norm = target_hw.strip().lower()
    if required in (HardwareClass.ANY, HardwareClass.CPU):
        return True

    # Host-local macOS / Apple Silicon check (D-022)
    if norm in ("apple_silicon", "mac", "darwin", "cpu", "host", "generic_cpu"):
        return False

    is_jetson_orin = "orin" in norm
    is_jetson_xavier = "xavier" in norm
    is_jetson = "jetson" in norm or is_jetson_orin or is_jetson_xavier
    is_nvidia = "nvidia" in norm or "cuda" in norm or "dgpu" in norm or is_jetson

    if required == HardwareClass.NVIDIA_GPU:
        return is_nvidia or is_jetson

    if required == HardwareClass.JETSON:
        return is_jetson

    if required == HardwareClass.JETSON_ORIN:
        return is_jetson_orin

    if required == HardwareClass.JETSON_XAVIER:
        return is_jetson_xavier

    return False


def validate_env_var_name(name: str) -> str:
    """Validate that an environment variable name is a valid non-empty C identifier."""
    if not isinstance(name, str) or not _ENV_VAR_PATTERN.fullmatch(name):
        raise ValueError(
            f"Environment variable name must match regex '^[A-Za-z_][A-Za-z0-9_]*$', got {name!r}"
        )
    return name


@dataclass(frozen=True)
class TargetRequirements:
    """Hardware and environment prerequisites required by a skill."""

    allowed_targets: tuple[TargetType, ...] = (TargetType.REMOTE,)
    required_hardware: HardwareClass = HardwareClass.ANY
    min_cuda_compute: str | None = None
    required_env_vars: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.allowed_targets:
            raise ValueError("TargetRequirements.allowed_targets cannot be empty")
        object.__setattr__(self, "allowed_targets", tuple(self.allowed_targets))

        if not isinstance(self.required_hardware, HardwareClass):
            try:
                hw = HardwareClass(str(self.required_hardware).lower())
                object.__setattr__(self, "required_hardware", hw)
            except ValueError:
                raise ValueError(
                    f"Invalid required_hardware: {self.required_hardware!r}. "
                    f"Must be one of {[h.value for h in HardwareClass]}"
                ) from None

        if self.min_cuda_compute is not None:
            # Semantic validation: verify numeric format
            parse_cuda_compute(self.min_cuda_compute)

        validated_vars = tuple(validate_env_var_name(v) for v in self.required_env_vars)
        object.__setattr__(self, "required_env_vars", validated_vars)


@dataclass(frozen=True)
class SkillDescriptor:
    """Architectural definition and procedural contract of a skill."""

    skill_id: SkillId
    name: str
    description: str
    version: str
    provenance: SkillProvenance
    satisfies_capabilities: tuple[CapabilityId, ...]
    required_tools: tuple[ToolId, ...]
    target_requirements: TargetRequirements
    parameter_schema: Mapping[str, Any]
    procedural_recipe: str
    vendor: str | None = None
    prerequisites: tuple[str, ...] = ()
    timeout_seconds: int = 600
    deprecated_in_favor_of: SkillId | None = None
    requires_approval: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("SkillDescriptor.name must not be empty")
        if not self.description or not self.description.strip():
            raise ValueError("SkillDescriptor.description must not be empty")
        if not self.version or not self.version.strip():
            raise ValueError("SkillDescriptor.version must not be empty")
        if not self.satisfies_capabilities:
            raise ValueError(
                "SkillDescriptor.satisfies_capabilities must declare at least one CapabilityId"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("SkillDescriptor.timeout_seconds must be positive")

        object.__setattr__(
            self, "satisfies_capabilities", tuple(self.satisfies_capabilities)
        )
        object.__setattr__(self, "required_tools", tuple(self.required_tools))
        object.__setattr__(self, "prerequisites", tuple(self.prerequisites))
        object.__setattr__(self, "parameter_schema", deep_freeze(self.parameter_schema))
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))


@dataclass(frozen=True)
class SkillResolution:
    """Result of resolving a CapabilityId against candidate skills and compute targets."""

    capability_id: CapabilityId
    candidate_skills: tuple[SkillDescriptor, ...]
    selected_skill: SkillDescriptor | None
    availability_state: AvailabilityState
    target_descriptor: ExecutionTargetDescriptor | None = None
    missing_prerequisites: tuple[str, ...] = ()
    unavailability_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_skills", tuple(self.candidate_skills))
        object.__setattr__(
            self, "missing_prerequisites", tuple(self.missing_prerequisites)
        )

    @property
    def is_executable(self) -> bool:
        return (
            self.selected_skill is not None
            and self.availability_state == AvailabilityState.EXECUTABLE
            and self.target_descriptor is not None
            and self.target_descriptor.is_available
        )


class SkillExecutionStatus(str, Enum):
    """Terminal execution or dispatch outcome of a skill invocation."""

    SUCCESS = "success"  # Synchronous procedure completed successfully
    DISPATCHED = "dispatched"  # Asynchronous procedure dispatched to external compute target; returned ActiveJobHandoff
    EXECUTION_FAILED = "execution_failed"  # Tool invocation or recipe command failed
    PREREQUISITE_FAILED = "prerequisite_failed"  # Input validation, artifact missing, or prerequisite check failed
    POLICY_VIOLATION = (
        "policy_violation"  # Approval precondition failed or safety constraint violated
    )
    TIMEOUT = "timeout"  # Execution exceeded skill timeout_seconds


@dataclass(frozen=True)
class SkillExecutionResult:
    """Normalized, transport-independent output from a skill execution turn."""

    skill_id: SkillId
    execution_id: str
    status: SkillExecutionStatus
    outputs: Mapping[str, Any] = field(default_factory=dict)
    tool_results: tuple[ToolResult, ...] = ()
    active_job_ref: ActiveJobHandoff | None = None
    duration_ms: int = 0
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.execution_id or not self.execution_id.strip():
            raise ValueError("SkillExecutionResult.execution_id must not be empty")

        # Invariant I1: On policy violation, tool_results must be empty and active_job_ref must be None
        if self.status == SkillExecutionStatus.POLICY_VIOLATION:
            if self.tool_results:
                raise ValueError(
                    "SkillExecutionResult with POLICY_VIOLATION status cannot contain tool_results"
                )
            if self.active_job_ref is not None:
                raise ValueError(
                    "SkillExecutionResult with POLICY_VIOLATION status cannot contain active_job_ref"
                )

        # Invariant I1: On DISPATCHED, active_job_ref must be present
        if (
            self.status == SkillExecutionStatus.DISPATCHED
            and self.active_job_ref is None
        ):
            raise ValueError(
                "SkillExecutionResult with DISPATCHED status must provide an active_job_ref"
            )

        object.__setattr__(self, "outputs", deep_freeze(self.outputs))
        object.__setattr__(self, "tool_results", tuple(self.tool_results))
