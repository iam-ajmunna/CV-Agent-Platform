"""cv_agent.training.contracts — Immutable Pydantic v2 domain models for ADR-0010."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cv_agent.dataset import ContentHash, DatasetId, DatasetVersion
from cv_agent.domain import CapabilityId
from cv_agent.tools import ExecutionId, TargetId
from cv_agent.training.types import (
    ApprovalId,
    CheckpointId,
    CompletionReason,
    DistributedStrategy,
    FailureReason,
    PlanId,
    TrainingJobId,
    TrainingJobStatus,
    TrainingPrecision,
    validate_approval_id,
    validate_checkpoint_id,
    validate_plan_id,
    validate_training_job_id,
)


def deep_freeze(val: Any) -> Any:
    """Recursively freeze dictionaries, lists, and sets into immutable types."""
    if isinstance(val, Mapping):
        return MappingProxyType({k: deep_freeze(v) for k, v in val.items()})
    if isinstance(val, (list, tuple)):
        return tuple(deep_freeze(x) for x in val)
    if isinstance(val, set):
        return frozenset(deep_freeze(x) for x in val)
    return val


def _ensure_finite_float(val: float, field_name: str) -> float:
    if math.isnan(val) or math.isinf(val):
        raise ValueError(f"Field {field_name!r} must be a finite float, got {val}")
    return val


class OptimizerConfig(BaseModel):
    """Optimizer algorithm configuration and parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    lr: float = Field(gt=0.0)
    weight_decay: float = Field(ge=0.0, default=0.01)
    momentum: float | None = Field(ge=0.0, le=1.0, default=None)
    betas: tuple[float, float] | None = (0.9, 0.999)
    gradient_clip_norm: float | None = Field(gt=0.0, default=None)

    @field_validator("lr", "weight_decay", mode="after")
    @classmethod
    def check_finite_floats(cls, v: float) -> float:
        return _ensure_finite_float(v, "optimizer_float")


class SchedulerConfig(BaseModel):
    """Learning rate schedule policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: str
    warmup_epochs: int = Field(ge=0, default=3)
    min_lr: float = Field(ge=0.0, default=1e-6)
    step_size: int | None = None
    gamma: float | None = None

    @field_validator("min_lr", mode="after")
    @classmethod
    def check_finite_min_lr(cls, v: float) -> float:
        return _ensure_finite_float(v, "min_lr")


class LossComponentConfig(BaseModel):
    """Individual loss function component."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    loss_type: str
    weight: float = Field(gt=0.0, default=1.0)
    params: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("weight", mode="after")
    @classmethod
    def check_finite_weight(cls, v: float) -> float:
        return _ensure_finite_float(v, "weight")

    @field_validator("params", mode="after")
    @classmethod
    def freeze_params(cls, v: Mapping[str, Any]) -> Mapping[str, Any]:
        return deep_freeze(v)


class AugmentationConfig(BaseModel):
    """Augmentation pipeline specification."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    pipeline_name: str
    image_size: tuple[int, int]
    horizontal_flip_prob: float = Field(ge=0.0, le=1.0, default=0.5)
    mosaic_prob: float = Field(ge=0.0, le=1.0, default=0.0)
    mixup_prob: float = Field(ge=0.0, le=1.0, default=0.0)
    color_jitter: float = Field(ge=0.0, default=0.0)
    custom_params: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator(
        "horizontal_flip_prob",
        "mosaic_prob",
        "mixup_prob",
        "color_jitter",
        mode="after",
    )
    @classmethod
    def check_finite_probs(cls, v: float) -> float:
        return _ensure_finite_float(v, "augmentation_prob")

    @field_validator("custom_params", mode="after")
    @classmethod
    def freeze_custom_params(cls, v: Mapping[str, Any]) -> Mapping[str, Any]:
        return deep_freeze(v)


class ComputeRequirements(BaseModel):
    """Hardware and distributed training topology requirements."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_vram_gb: float = Field(gt=0.0)
    min_cuda_compute_capability: str | None = None
    num_devices: int = Field(ge=1, default=1)
    distributed_strategy: DistributedStrategy = DistributedStrategy.NONE

    @field_validator("min_vram_gb", mode="after")
    @classmethod
    def check_finite_vram(cls, v: float) -> float:
        return _ensure_finite_float(v, "min_vram_gb")


class CostEstimate(BaseModel):
    """Pre-execution resource and monetary cost estimate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    estimated_gpu_hours: float = Field(ge=0.0)
    estimated_wall_clock_hours: float = Field(ge=0.0)
    estimated_cost_usd: Decimal = Field(default=Decimal("0.00"))
    estimated_storage_gb: float = Field(ge=0.0)
    basis_description: str

    @field_validator(
        "estimated_gpu_hours",
        "estimated_wall_clock_hours",
        "estimated_storage_gb",
        mode="after",
    )
    @classmethod
    def check_finite_costs(cls, v: float) -> float:
        return _ensure_finite_float(v, "cost_float")

    @field_validator("estimated_cost_usd", mode="after")
    @classmethod
    def check_cost_decimal(cls, v: Decimal) -> Decimal:
        if v.is_nan() or v.is_infinite() or v < Decimal("0.00"):
            raise ValueError(
                f"estimated_cost_usd must be finite and non-negative, got {v}"
            )
        return v.quantize(Decimal("0.01"))


class TrainingPlan(BaseModel):
    """Authoritative, immutable training experiment specification."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    plan_id: PlanId
    experiment_id: str
    created_at: datetime
    capability_id: CapabilityId

    # Dataset binding (ADR-0009)
    dataset_id: DatasetId
    dataset_version: DatasetVersion
    dataset_manifest_hash: ContentHash
    train_split: str = "train"
    val_split: str = "val"

    # Model & Optimization
    architecture_name: str
    num_classes: int = Field(gt=0)
    pretrained_weights_ref: str | None = None
    optimizer: OptimizerConfig
    scheduler: SchedulerConfig
    losses: tuple[LossComponentConfig, ...]
    augmentation: AugmentationConfig

    # Execution hyperparameters
    batch_size_per_device: int = Field(gt=0)
    gradient_accumulation_steps: int = Field(ge=1, default=1)
    epochs: int = Field(gt=0)
    precision: TrainingPrecision = TrainingPrecision.FP16_AMP
    compute_requirements: ComputeRequirements

    # Gating & Traceability Reference (strictly non-authoritative provenance reference)
    cost_estimate: CostEstimate | None = None
    approval_id: ApprovalId | None = None

    @field_validator("plan_id", mode="before")
    @classmethod
    def validate_plan_id_syntax(cls, v: str) -> PlanId:
        return validate_plan_id(v)

    @field_validator("approval_id", mode="before")
    @classmethod
    def validate_approval_id_syntax(cls, v: str | None) -> ApprovalId | None:
        if v is None:
            return None
        return validate_approval_id(v)

    @field_validator("losses", mode="after")
    @classmethod
    def freeze_losses(
        cls, v: tuple[LossComponentConfig, ...] | list[LossComponentConfig]
    ) -> tuple[LossComponentConfig, ...]:
        return tuple(v)


class TrainingTelemetryRecord(BaseModel):
    """Point-in-time training progress and metric telemetry event."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    job_id: TrainingJobId
    step: int = Field(ge=0)
    epoch: int = Field(ge=0)
    total_epochs: int = Field(gt=0)
    train_loss: float
    loss_components: Mapping[str, float] = Field(default_factory=dict)
    val_loss: float | None = None
    learning_rate: float = Field(ge=0.0)
    throughput_fps: float = Field(ge=0.0)
    vram_allocated_gb: float = Field(ge=0.0)
    timestamp: datetime

    @field_validator(
        "train_loss",
        "learning_rate",
        "throughput_fps",
        "vram_allocated_gb",
        mode="after",
    )
    @classmethod
    def check_finite_telemetry(cls, v: float) -> float:
        return _ensure_finite_float(v, "telemetry_float")

    @field_validator("val_loss", mode="after")
    @classmethod
    def check_finite_val_loss(cls, v: float | None) -> float | None:
        if v is not None:
            return _ensure_finite_float(v, "val_loss")
        return None

    @field_validator("loss_components", mode="after")
    @classmethod
    def freeze_loss_components(cls, v: Mapping[str, float]) -> Mapping[str, float]:
        for k, comp in v.items():
            _ensure_finite_float(comp, f"loss_components[{k}]")
        return deep_freeze(v)


class CheckpointManifest(BaseModel):
    """Immutable metadata record for a saved model checkpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    checkpoint_id: CheckpointId
    job_id: TrainingJobId
    plan_id: PlanId
    epoch: int = Field(ge=0)
    step: int = Field(ge=0)
    weights_hash: ContentHash
    weights_uri: str = Field(
        ...,
        description="Storage URI for weights payload. MUST begin with a recognized URI scheme.",
        pattern=r"^[a-zA-Z][a-zA-Z0-9+.-]*://.+",
    )
    optimizer_state_included: bool = False
    metrics: Mapping[str, float] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("checkpoint_id", mode="before")
    @classmethod
    def validate_ckpt_id(cls, v: str) -> CheckpointId:
        return validate_checkpoint_id(v)

    @field_validator("job_id", mode="before")
    @classmethod
    def validate_job_id_val(cls, v: str) -> TrainingJobId:
        return validate_training_job_id(v)

    @field_validator("plan_id", mode="before")
    @classmethod
    def validate_plan_id_val(cls, v: str) -> PlanId:
        return validate_plan_id(v)

    @field_validator("metrics", mode="after")
    @classmethod
    def freeze_metrics(cls, v: Mapping[str, float]) -> Mapping[str, float]:
        for k, metric in v.items():
            _ensure_finite_float(metric, f"metrics[{k}]")
        return deep_freeze(v)


class TrainingEnvironmentRecord(BaseModel):
    """Complete environment and reproducibility metadata per [P§25]."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    seed_torch: int
    seed_numpy: int
    seed_python: int
    seed_cuda: int
    git_commit_sha: str
    git_is_dirty: bool
    python_version: str
    torch_version: str
    cuda_version: str | None
    driver_version: str | None
    devices_info: tuple[Mapping[str, Any], ...]

    @field_validator("devices_info", mode="after")
    @classmethod
    def freeze_devices(
        cls, v: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]]
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(deep_freeze(d) for d in v)


class TrainingJobRecord(BaseModel):
    """Authoritative lifecycle state record of a training job."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    job_id: TrainingJobId
    plan_id: PlanId
    status: TrainingJobStatus
    failure_reason: FailureReason = FailureReason.NONE
    failure_details: str | None = None
    completion_reason: CompletionReason | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    best_checkpoint_id: CheckpointId | None = None
    environment: TrainingEnvironmentRecord | None = None
    execution_id: ExecutionId | None = None
    target_id: TargetId | None = None

    @field_validator("job_id", mode="before")
    @classmethod
    def validate_record_job_id(cls, v: str) -> TrainingJobId:
        return validate_training_job_id(v)

    @field_validator("plan_id", mode="before")
    @classmethod
    def validate_record_plan_id(cls, v: str) -> PlanId:
        return validate_plan_id(v)


class RetentionPolicy(BaseModel):
    """Checkpoint retention configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keep_best: int = Field(default=3, ge=1)
    keep_last: int = Field(default=2, ge=1)
    keep_final: bool = True
    primary_metric: str = "val_loss"
    higher_is_better: bool = False


class RetentionDecision(BaseModel):
    """Pure logical retention determination distinguishing retention from physical storage deletion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retained_checkpoint_ids: tuple[CheckpointId, ...]
    prunable_checkpoint_ids: tuple[CheckpointId, ...]
    best_checkpoint_id: CheckpointId
    final_checkpoint_id: CheckpointId
