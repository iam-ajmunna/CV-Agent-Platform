"""cv_agent.skills — Skills Architecture and NVIDIA Capability Discovery.

Implements ADR-0007:
- Declarative procedural skill contracts (SkillDescriptor, TargetRequirements, HardwareClass).
- Authoritative SkillRegistry enforcing ADR-0001 capability authority (B1).
- Ephemeral DiscoveryCache isolating non-authoritative MCP/remote candidates.
- Deterministic SkillResolver delegating to ADR-0001 availability semantics (B2).
- Hardware-aware known-but-unavailable enforcement on local macOS workstations (D-022).
- Procedural SkillExecutor coordinating tool execution via ADR-0005 with typed async handoffs.
- Declarative external NVIDIA skill catalog (10 skills per [P§15]).
"""

from __future__ import annotations

from cv_agent.skills.catalog import (
    ALL_AUTHORITATIVE_SKILLS,
    AUTHORITATIVE_NVIDIA_SKILLS,
    CUDA_PROFILING_SKILL,
    DALI_DATA_PIPELINE_SKILL,
    DEEPSTREAM_PIPELINE_SKILL,
    INTERNAL_ONNX_CHECK_SKILL,
    JETSON_PROFILING_SKILL,
    MODEL_INSPECTION_VISUALIZE_SKILL,
    MODEL_OPTIMIZER_PTQ_SKILL,
    MODEL_OPTIMIZER_QAT_SKILL,
    TAO_TRANSFER_LEARNING_SKILL,
    TENSORRT_OPTIMIZE_SKILL,
    TRITON_SERVING_SKILL,
    register_all_authoritative_skills,
)
from cv_agent.skills.contracts import (
    HardwareClass,
    SkillDescriptor,
    SkillExecutionResult,
    SkillExecutionStatus,
    SkillProvenance,
    SkillResolution,
    TargetRequirements,
    is_hardware_compatible,
    parse_cuda_compute,
    validate_env_var_name,
)
from cv_agent.skills.errors import (
    ApprovalRequiredError,
    KnownButUnavailableError,
    MissingToolDependencyError,
    NoSatisfyingSkillError,
    ParameterValidationError,
    PrerequisiteFailedError,
    SkillError,
    SkillTimeoutError,
    TargetMismatchError,
    UnknownCapabilityError,
)
from cv_agent.skills.executor import (
    DefaultSkillExecutor,
    SkillExecutor,
    validate_parameters_against_schema,
)
from cv_agent.skills.registry import (
    DefaultSkillRegistry,
    DiscoveryCache,
    SkillRegistry,
)
from cv_agent.skills.resolver import (
    DefaultSkillResolver,
    SkillResolver,
)

__all__ = [
    "ALL_AUTHORITATIVE_SKILLS",
    "AUTHORITATIVE_NVIDIA_SKILLS",
    "CUDA_PROFILING_SKILL",
    "DALI_DATA_PIPELINE_SKILL",
    "DEEPSTREAM_PIPELINE_SKILL",
    "INTERNAL_ONNX_CHECK_SKILL",
    "JETSON_PROFILING_SKILL",
    "MODEL_INSPECTION_VISUALIZE_SKILL",
    "MODEL_OPTIMIZER_PTQ_SKILL",
    "MODEL_OPTIMIZER_QAT_SKILL",
    "TAO_TRANSFER_LEARNING_SKILL",
    "TENSORRT_OPTIMIZE_SKILL",
    "TRITON_SERVING_SKILL",
    "ApprovalRequiredError",
    "DefaultSkillExecutor",
    "DefaultSkillRegistry",
    "DefaultSkillResolver",
    "DiscoveryCache",
    "HardwareClass",
    "KnownButUnavailableError",
    "MissingToolDependencyError",
    "NoSatisfyingSkillError",
    "ParameterValidationError",
    "PrerequisiteFailedError",
    "SkillDescriptor",
    "SkillError",
    "SkillExecutionResult",
    "SkillExecutionStatus",
    "SkillExecutor",
    "SkillProvenance",
    "SkillRegistry",
    "SkillResolution",
    "SkillResolver",
    "SkillTimeoutError",
    "TargetMismatchError",
    "TargetRequirements",
    "UnknownCapabilityError",
    "is_hardware_compatible",
    "parse_cuda_compute",
    "register_all_authoritative_skills",
    "validate_env_var_name",
    "validate_parameters_against_schema",
]
