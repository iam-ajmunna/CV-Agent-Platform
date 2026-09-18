"""cv_agent.optimization.contracts — Immutable Pydantic v2 domain models for ADR-0012."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cv_agent.dataset import ContentHash, DatasetId, DatasetVersion
from cv_agent.evaluation.contracts import (
    BaselineComparisonResult,
    EvaluationDatasetBinding,
    EvaluationResult,
)
from cv_agent.evaluation.types import EvaluationId
from cv_agent.optimization.errors import (
    CalibrationLeakageError,
    DeploymentValidationError,
    OptimizationValidationError,
)
from cv_agent.optimization.types import (
    CalibrationAlgorithm,
    DeploymentId,
    DeploymentRuntime,
    DeploymentStatus,
    OptimizationPlanId,
    OptimizationPrecision,
    OptimizationTechnique,
    OptimizedArtifactId,
)
from cv_agent.tools import ExecutionId, TargetId
from cv_agent.training import CheckpointId


def _ensure_finite_float(val: float, name: str) -> float:
    """Validate float metric is strictly finite (not NaN, not Inf)."""
    if math.isnan(val) or math.isinf(val):
        raise ValueError(f"Field {name!r} must be a finite float, got {val}")
    return val


class DynamicShapeProfile(BaseModel):
    """Explicit minimum, optimal, and maximum dynamic dimension profile per ADR-0012 §3.1."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    tensor_name: str
    min_shape: tuple[int, ...]
    opt_shape: tuple[int, ...]
    max_shape: tuple[int, ...]

    @model_validator(mode="after")
    def _validate_shape_monotonicity(self) -> DynamicShapeProfile:
        if not self.tensor_name or not str(self.tensor_name).strip():
            raise ValueError("tensor_name cannot be empty")

        if not (len(self.min_shape) == len(self.opt_shape) == len(self.max_shape)):
            raise ValueError(
                f"Dynamic shape dimensions must have equal rank: "
                f"min={len(self.min_shape)}, opt={len(self.opt_shape)}, max={len(self.max_shape)}"
            )

        if len(self.min_shape) == 0:
            raise ValueError("Dynamic shape rank must be at least 1")

        for idx, (min_dim, opt_dim, max_dim) in enumerate(
            zip(self.min_shape, self.opt_shape, self.max_shape)
        ):
            if min_dim <= 0:
                raise ValueError(
                    f"Dimension {idx} min_shape must be strictly positive, got {min_dim}"
                )
            if not (min_dim <= opt_dim <= max_dim):
                raise ValueError(
                    f"Dynamic shape monotonicity violated at dimension {idx}: "
                    f"expected min ({min_dim}) <= opt ({opt_dim}) <= max ({max_dim})"
                )

        return self


class CalibrationDatasetBinding(BaseModel):
    """Binding to an immutable dataset split for quantization calibration per Invariant 13."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    dataset_id: DatasetId
    dataset_version: DatasetVersion
    dataset_manifest_hash: ContentHash
    split_name: str = "calibration"
    num_calibration_samples: int = Field(ge=10, default=512)
    calibration_algorithm: CalibrationAlgorithm = CalibrationAlgorithm.ENTROPY

    @model_validator(mode="after")
    def _validate_calibration_leakage(self) -> CalibrationDatasetBinding:
        split_lower = str(self.split_name).strip().lower()
        if not split_lower:
            raise ValueError("split_name cannot be empty")

        if split_lower != "calibration":
            raise CalibrationLeakageError(
                f"Calibration dataset split must strictly be 'calibration', got {self.split_name!r}. "
                "Per ADR-0009 Invariant 13 and ADR-0012 §3.1, INT8 PTQ calibration must strictly use "
                "a dedicated 'calibration' split to prevent data leakage."
            )

        return self


class OptimizationPlan(BaseModel):
    """Authoritative, immutable model optimization specification per ADR-0012 §4."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    plan_id: OptimizationPlanId
    source_checkpoint_id: CheckpointId
    source_weights_hash: ContentHash
    technique: OptimizationTechnique
    target_precision: OptimizationPrecision
    target_platform: str
    target_compute_capability: str
    shape_profiles: tuple[DynamicShapeProfile, ...] = Field(default_factory=tuple)
    calibration_binding: CalibrationDatasetBinding | None = None
    verification_dataset_binding: EvaluationDatasetBinding
    max_accuracy_drop_tolerance: float = Field(ge=0.0, default=0.015)
    parameters: Mapping[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("max_accuracy_drop_tolerance", mode="before")
    @classmethod
    def _validate_tolerance_finite(cls, v: Any) -> Any:
        if isinstance(v, (int, float)):
            return _ensure_finite_float(float(v), "max_accuracy_drop_tolerance")
        return v

    @model_validator(mode="after")
    def _validate_plan_constraints(self) -> OptimizationPlan:
        if not str(self.target_platform).strip():
            raise OptimizationValidationError("target_platform cannot be empty")

        if not str(self.target_compute_capability).strip():
            raise OptimizationValidationError(
                "target_compute_capability cannot be empty"
            )

        _ensure_finite_float(
            self.max_accuracy_drop_tolerance, "max_accuracy_drop_tolerance"
        )

        # Unique shape profile tensor names
        seen_tensors: set[str] = set()
        for sp in self.shape_profiles:
            if sp.tensor_name in seen_tensors:
                raise OptimizationValidationError(
                    f"Duplicate shape profile declared for tensor {sp.tensor_name!r}"
                )
            seen_tensors.add(sp.tensor_name)

        # Invariant 2: Calibration binding mandatory for PTQ or INT8 precision
        needs_calibration = (
            self.technique == OptimizationTechnique.PTQ_QUANTIZATION
            or self.target_precision == OptimizationPrecision.INT8
        )
        if needs_calibration and self.calibration_binding is None:
            raise OptimizationValidationError(
                f"CalibrationDatasetBinding is mandatory for technique={self.technique.value} "
                f"and precision={self.target_precision.value} per ADR-0012 §3.1"
            )

        # Invariant 13: verification dataset binding cannot be "calibration"
        if (
            str(self.verification_dataset_binding.split_name).strip().lower()
            == "calibration"
        ):
            raise OptimizationValidationError(
                "verification_dataset_binding split cannot be 'calibration'. "
                "Calibration split cannot be evaluated as an evaluation split (ADR-0009 Invariant 13)."
            )

        # Wrap parameters in immutable MappingProxyType
        if not isinstance(self.parameters, MappingProxyType):
            object.__setattr__(
                self, "parameters", MappingProxyType(dict(self.parameters))
            )

        return self


class OptimizedModelArtifact(BaseModel):
    """Compiled, hardware-locked optimized model artifact per ADR-0012 §4."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    artifact_id: OptimizedArtifactId
    plan_id: OptimizationPlanId
    source_checkpoint_id: CheckpointId
    technique: OptimizationTechnique
    precision: OptimizationPrecision
    runtime_format: str
    target_device: str
    target_compute_capability: str
    artifact_hash: ContentHash
    artifact_uri: str
    artifact_size_bytes: int = Field(gt=0)
    shape_profiles: tuple[DynamicShapeProfile, ...] = Field(default_factory=tuple)
    execution_id: ExecutionId | None = None
    target_id: TargetId | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _validate_artifact(self) -> OptimizedModelArtifact:
        if not str(self.target_device).strip():
            raise ValueError("target_device cannot be empty")
        if not str(self.target_compute_capability).strip():
            raise ValueError("target_compute_capability cannot be empty")
        if not str(self.artifact_hash).strip():
            raise ValueError("artifact_hash cannot be empty")
        if not str(self.artifact_uri).strip():
            raise ValueError("artifact_uri cannot be empty")
        return self


class OptimizedArtifactEvaluationBinding(BaseModel):
    """Explicit Phase 2M binding between an optimized model artifact and candidate evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    candidate_artifact_id: OptimizedArtifactId
    candidate_artifact_hash: ContentHash
    evaluation_id: EvaluationId | None = None

    @model_validator(mode="after")
    def _validate_non_empty(self) -> OptimizedArtifactEvaluationBinding:
        if not str(self.candidate_artifact_id).strip():
            raise ValueError("candidate_artifact_id cannot be empty")
        if not str(self.candidate_artifact_hash).strip():
            raise ValueError("candidate_artifact_hash cannot be empty")
        return self


class OptimizationResult(BaseModel):
    """Outcome of model optimization including empirical verification against baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    plan: OptimizationPlan
    artifact: OptimizedModelArtifact
    evaluation_result: EvaluationResult
    baseline_comparison: BaselineComparisonResult
    accuracy_retention_ratio: float = Field(ge=0.0)
    speedup_ratio: float = Field(gt=0.0)
    memory_reduction_ratio: float = Field(ge=0.0)
    verification_passed: bool
    diagnostics: str
    candidate_artifact_binding: OptimizedArtifactEvaluationBinding | None = None

    @model_validator(mode="after")
    def _validate_metrics(self) -> OptimizationResult:
        _ensure_finite_float(self.accuracy_retention_ratio, "accuracy_retention_ratio")
        _ensure_finite_float(self.speedup_ratio, "speedup_ratio")
        _ensure_finite_float(self.memory_reduction_ratio, "memory_reduction_ratio")
        return self


class DeploymentTarget(BaseModel):
    """Physical or virtual edge/server endpoint where model is deployed."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    target_id: TargetId
    hostname_or_ip: str
    platform_type: str
    gpu_device_name: str
    compute_capability: str
    cuda_runtime_version: str
    driver_version: str
    max_memory_allocation_gb: float = Field(gt=0.0)
    power_mode: str | None = None

    @model_validator(mode="after")
    def _validate_target(self) -> DeploymentTarget:
        if not str(self.hostname_or_ip).strip():
            raise ValueError("hostname_or_ip cannot be empty")
        if not str(self.platform_type).strip():
            raise ValueError("platform_type cannot be empty")
        if not str(self.gpu_device_name).strip():
            raise ValueError("gpu_device_name cannot be empty")
        if not str(self.compute_capability).strip():
            raise ValueError("compute_capability cannot be empty")
        return self


class HealthCheckSpec(BaseModel):
    """Specification for deployment startup and readiness verification per ADR-0012 §3.7."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    startup_timeout_sec: float = Field(gt=0.0, default=60.0)
    readiness_probe_samples: int = Field(ge=1, default=5)
    max_latency_threshold_ms: float = Field(gt=0.0, default=50.0)
    memory_headroom_mb: float = Field(ge=0.0, default=512.0)
    expected_output_shape: tuple[int, ...] | None = None


class ReadinessProbeResult(BaseModel):
    """Result of active readiness probing before traffic routing."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    passed: bool
    startup_time_sec: float = Field(ge=0.0)
    probe_latency_mean_ms: float = Field(ge=0.0)
    vram_consumed_mb: float = Field(ge=0.0)
    error_message: str | None = None
    probed_at: datetime

    @model_validator(mode="after")
    def _validate_floats(self) -> ReadinessProbeResult:
        _ensure_finite_float(self.startup_time_sec, "startup_time_sec")
        _ensure_finite_float(self.probe_latency_mean_ms, "probe_latency_mean_ms")
        _ensure_finite_float(self.vram_consumed_mb, "vram_consumed_mb")
        return self


class DeploymentPlan(BaseModel):
    """Immutable production deployment specification per ADR-0012 §4."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    deployment_id: DeploymentId
    artifact_id: OptimizedArtifactId
    target: DeploymentTarget
    runtime: DeploymentRuntime
    runtime_config: Mapping[str, Any] = Field(default_factory=dict)
    health_check: HealthCheckSpec
    rollback_target_id: DeploymentId | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _validate_config(self) -> DeploymentPlan:
        if not isinstance(self.runtime_config, MappingProxyType):
            object.__setattr__(
                self, "runtime_config", MappingProxyType(dict(self.runtime_config))
            )
        return self


VALID_DEPLOYMENT_TRANSITIONS: dict[DeploymentStatus, set[DeploymentStatus]] = {
    DeploymentStatus.DRAFT: {DeploymentStatus.VALIDATED, DeploymentStatus.STAGING},
    DeploymentStatus.VALIDATED: {
        DeploymentStatus.AWAITING_APPROVAL,
        DeploymentStatus.STAGING,
    },
    DeploymentStatus.AWAITING_APPROVAL: {
        DeploymentStatus.APPROVED,
        DeploymentStatus.REJECTED,
    },
    DeploymentStatus.APPROVED: {DeploymentStatus.STAGING},
    DeploymentStatus.STAGING: {DeploymentStatus.VERIFYING, DeploymentStatus.FAILED},
    DeploymentStatus.VERIFYING: {DeploymentStatus.ACTIVE, DeploymentStatus.FAILED},
    DeploymentStatus.ACTIVE: {
        DeploymentStatus.SUPERSEDED,
        DeploymentStatus.ROLLBACK_INITIATED,
        DeploymentStatus.ROLLED_BACK,
        DeploymentStatus.OPERATOR_REQUIRED,
    },
    DeploymentStatus.ROLLBACK_INITIATED: {
        DeploymentStatus.ROLLED_BACK,
        DeploymentStatus.OPERATOR_REQUIRED,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.FAILED: {
        DeploymentStatus.ROLLBACK_INITIATED,
        DeploymentStatus.OPERATOR_REQUIRED,
    },
    DeploymentStatus.ROLLED_BACK: set(),
    DeploymentStatus.SUPERSEDED: {
        DeploymentStatus.ACTIVE,
    },
    DeploymentStatus.REJECTED: set(),
    DeploymentStatus.OPERATOR_REQUIRED: set(),
}


def validate_deployment_transition(
    current: DeploymentStatus, target: DeploymentStatus
) -> None:
    """Validate that a deployment status transition conforms to the ADR-0012 state machine."""
    allowed = VALID_DEPLOYMENT_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise DeploymentValidationError(
            f"Invalid deployment lifecycle transition from {current.value!r} to {target.value!r}. "
            f"Allowed transitions from {current.value!r}: {sorted(s.value for s in allowed)}"
        )


class DeploymentRecord(BaseModel):
    """Authoritative lifecycle and status record of a deployment per ADR-0012 §3.6."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    deployment_id: DeploymentId
    plan: DeploymentPlan
    status: DeploymentStatus
    readiness_result: ReadinessProbeResult | None = None
    execution_id: ExecutionId | None = None
    target_id: TargetId | None = None
    activated_at: datetime | None = None
    superseded_at: datetime | None = None
    rolled_back_at: datetime | None = None
    failure_details: str | None = None

    @model_validator(mode="after")
    def _populate_target_id(self) -> DeploymentRecord:
        if (
            self.target_id is None
            and self.plan is not None
            and getattr(self.plan, "target", None) is not None
        ):
            object.__setattr__(self, "target_id", self.plan.target.target_id)
        return self

    def with_transition(
        self,
        new_status: DeploymentStatus,
        *,
        readiness_result: ReadinessProbeResult | None = None,
        execution_id: ExecutionId | None = None,
        target_id: TargetId | None = None,
        activated_at: datetime | None = None,
        superseded_at: datetime | None = None,
        rolled_back_at: datetime | None = None,
        failure_details: str | None = None,
    ) -> DeploymentRecord:
        """Return a new DeploymentRecord with validated status transition per ADR-0012 §3.6."""
        validate_deployment_transition(self.status, new_status)
        return DeploymentRecord(
            deployment_id=self.deployment_id,
            plan=self.plan,
            status=new_status,
            readiness_result=(
                readiness_result
                if readiness_result is not None
                else self.readiness_result
            ),
            execution_id=(
                execution_id if execution_id is not None else self.execution_id
            ),
            target_id=target_id if target_id is not None else self.target_id,
            activated_at=(
                activated_at if activated_at is not None else self.activated_at
            ),
            superseded_at=(
                superseded_at if superseded_at is not None else self.superseded_at
            ),
            rolled_back_at=(
                rolled_back_at if rolled_back_at is not None else self.rolled_back_at
            ),
            failure_details=(
                failure_details if failure_details is not None else self.failure_details
            ),
        )
