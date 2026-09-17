"""cv_agent.optimization.types — Canonical types, enums, and identifiers for ADR-0012.

Identifier syntax rules conform to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated

from pydantic import AfterValidator

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$")


def validate_optimization_plan_id(value: str) -> str:
    """Validate OptimizationPlanId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"OptimizationPlanId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return value


def validate_optimized_artifact_id(value: str) -> str:
    """Validate OptimizedArtifactId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"OptimizedArtifactId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return value


def validate_deployment_id(value: str) -> str:
    """Validate DeploymentId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"DeploymentId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return value


OptimizationPlanId = Annotated[str, AfterValidator(validate_optimization_plan_id)]
OptimizedArtifactId = Annotated[str, AfterValidator(validate_optimized_artifact_id)]
DeploymentId = Annotated[str, AfterValidator(validate_deployment_id)]


class OptimizationTechnique(str, Enum):
    """Supported model transformation and graph optimization techniques."""

    ONNX_EXPORT = "onnx_export"
    TENSORRT_COMPILATION = "tensorrt_compilation"
    PTQ_QUANTIZATION = "ptq_quantization"
    QAT_PREPARATION = "qat_preparation"
    STRUCTURED_PRUNING = "structured_pruning"
    KNOWLEDGE_DISTILLATION = "knowledge_distillation"
    LAYER_FUSION = "layer_fusion"


class OptimizationPrecision(str, Enum):
    """Target execution precisions for compiled inference engines."""

    FP32 = "fp32"
    FP16 = "fp16"
    BF16 = "bf16"
    FP8 = "fp8"
    INT8 = "int8"


class CalibrationAlgorithm(str, Enum):
    """Algorithms for INT8 Post-Training Quantization (PTQ) activation calibration."""

    ENTROPY = "entropy"
    MINMAX = "minmax"
    PERCENTILE = "percentile"


class DeploymentRuntime(str, Enum):
    """Supported production serving and inference runtime engines."""

    STANDALONE_TENSORRT = "standalone_tensorrt"
    DEEPSTREAM_PIPELINE = "deepstream_pipeline"
    TRITON_SERVER = "triton_server"
    ONNX_RUNTIME = "onnx_runtime"
    EMBEDDED_DAEMON = "embedded_daemon"


class DeploymentStatus(str, Enum):
    """Domain deployment readiness and lifecycle status per ADR-0012 §3.6.

    Orthogonal to ADR-0003 orchestration session LifecycleStatus.
    """

    DRAFT = "draft"
    VALIDATED = "validated"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    STAGING = "staging"
    VERIFYING = "verifying"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ROLLBACK_INITIATED = "rollback_initiated"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    REJECTED = "rejected"
    OPERATOR_REQUIRED = "operator_required"
