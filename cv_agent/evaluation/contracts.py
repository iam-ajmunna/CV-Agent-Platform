"""cv_agent.evaluation.contracts — Immutable Pydantic v2 domain models for ADR-0011."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cv_agent.dataset.types import ContentHash, DatasetId, DatasetVersion
from cv_agent.domain import CapabilityId
from cv_agent.evaluation.types import (
    BenchmarkId,
    EvaluationId,
    EvaluationVerdict,
    ExperimentId,
    HardwareClass,
    MetricId,
    StatisticalTestType,
    TaskType,
    validate_benchmark_id,
    validate_evaluation_id,
    validate_experiment_id,
    validate_metric_id,
)
from cv_agent.tools import TargetId
from cv_agent.training.types import (
    CheckpointId,
    TrainingPrecision,
    validate_checkpoint_id,
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
    if not isinstance(val, (int, float)) or math.isnan(val) or math.isinf(val):
        raise ValueError(f"Field {field_name!r} must be a finite float, got {val}")
    return float(val)


class EvaluationDatasetBinding(BaseModel):
    """Immutable binding to an authoritative dataset split per ADR-0009."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    dataset_id: DatasetId
    dataset_version: DatasetVersion
    dataset_manifest_hash: ContentHash
    split_name: str = "test"
    slice_filters: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("split_name")
    @classmethod
    def _validate_split_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("split_name cannot be empty")
        return v.strip()

    @field_validator("slice_filters")
    @classmethod
    def _freeze_filters(cls, v: Mapping[str, Any]) -> Any:
        return deep_freeze(v)


class ModelArtifactReference(BaseModel):
    """Immutable reference to the evaluated model artifact per ADR-0010."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    checkpoint_id: CheckpointId
    architecture_name: str
    weights_hash: ContentHash
    weights_uri: str
    precision: TrainingPrecision = TrainingPrecision.FP32
    input_resolution: tuple[int, int]  # (height, width)

    @field_validator("checkpoint_id")
    @classmethod
    def _validate_ckpt(cls, v: CheckpointId) -> CheckpointId:
        return validate_checkpoint_id(str(v))

    @field_validator("input_resolution")
    @classmethod
    def _validate_resolution(cls, v: tuple[int, int]) -> tuple[int, int]:
        if len(v) != 2 or v[0] <= 0 or v[1] <= 0:
            raise ValueError(
                f"input_resolution must be a tuple of 2 positive integers (height, width), got {v}"
            )
        return (int(v[0]), int(v[1]))

    @field_validator("weights_uri")
    @classmethod
    def _validate_uri(cls, v: str) -> str:
        if "://" not in v:
            raise ValueError(
                f"weights_uri must be an absolute URI with scheme, got {v!r}"
            )
        scheme = v.split("://", 1)[0]
        if scheme not in {"file", "s3", "gcs", "minio", "http", "https"}:
            raise ValueError(f"Unsupported weights_uri scheme {scheme!r}")
        return v


class EvaluationTargetRequirements(BaseModel):
    """Declarative constraints on target compute hardware."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    required_hardware_class: HardwareClass | None = None
    min_cuda_compute_capability: tuple[int, int] | None = None


class MetricDefinition(BaseModel):
    """Declarative, versioned specification of an evaluation metric."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    metric_id: MetricId
    name: str
    task_type: TaskType
    version: str = "v1"
    higher_is_better: bool = True
    parameters: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("metric_id")
    @classmethod
    def _validate_id(cls, v: MetricId) -> MetricId:
        return validate_metric_id(str(v))

    @field_validator("parameters")
    @classmethod
    def _freeze_params(cls, v: Mapping[str, Any]) -> Any:
        return deep_freeze(v)


class MetricResult(BaseModel):
    """Quantitative outcome of an evaluated model quality metric."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    metric: MetricDefinition
    primary_value: float
    per_class_values: Mapping[str, float] = Field(default_factory=dict)
    confidence_interval: tuple[float, float] | None = None
    sample_count: int = Field(ge=0, default=0)
    curve_data: Mapping[str, tuple[float, ...]] = Field(default_factory=dict)
    metadata: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("primary_value")
    @classmethod
    def _check_primary_value(cls, v: float) -> float:
        return _ensure_finite_float(v, "primary_value")

    @field_validator("per_class_values")
    @classmethod
    def _check_per_class(cls, v: Mapping[str, float]) -> Any:
        for k, val in v.items():
            _ensure_finite_float(val, f"per_class_values[{k}]")
        return deep_freeze(v)

    @field_validator("confidence_interval")
    @classmethod
    def _check_ci(cls, v: tuple[float, float] | None) -> tuple[float, float] | None:
        if v is None:
            return None
        if len(v) != 2:
            raise ValueError(
                f"confidence_interval must be a 2-tuple (lower, upper), got {v}"
            )
        lower = _ensure_finite_float(v[0], "confidence_interval[0]")
        upper = _ensure_finite_float(v[1], "confidence_interval[1]")
        if lower > upper:
            raise ValueError(
                f"confidence_interval lower bound {lower} exceeds upper bound {upper}"
            )
        return (lower, upper)

    @field_validator("curve_data", "metadata")
    @classmethod
    def _freeze_maps(cls, v: Mapping[str, Any]) -> Any:
        return deep_freeze(v)


class BenchmarkConfiguration(BaseModel):
    """Configuration for hardware-aware systems performance measurement."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    benchmark_id: BenchmarkId
    batch_sizes: tuple[int, ...] = (1,)
    warmup_iterations: int = Field(ge=5, default=10)
    measured_iterations: int = Field(ge=20, default=100)
    precision: TrainingPrecision = TrainingPrecision.FP16_AMP
    measure_power: bool = False
    measure_memory: bool = True
    target_requirements: EvaluationTargetRequirements = Field(
        default_factory=EvaluationTargetRequirements
    )

    @field_validator("benchmark_id")
    @classmethod
    def _validate_id(cls, v: BenchmarkId) -> BenchmarkId:
        return validate_benchmark_id(str(v))

    @field_validator("batch_sizes")
    @classmethod
    def _validate_batch_sizes(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if not v:
            raise ValueError("batch_sizes tuple cannot be empty")
        for b in v:
            if b <= 0:
                raise ValueError(f"Batch size must be strictly positive, got {b}")
        return tuple(v)


class BenchmarkResult(BaseModel):
    """Quantitative systems performance measurements on target accelerator."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    benchmark_id: BenchmarkId
    batch_size: int = Field(ge=1)
    precision: TrainingPrecision
    latency_min_ms: float = Field(ge=0.0)
    latency_mean_ms: float = Field(ge=0.0)
    latency_p50_ms: float = Field(ge=0.0)
    latency_p90_ms: float = Field(ge=0.0)
    latency_p95_ms: float = Field(ge=0.0)
    latency_p99_ms: float = Field(ge=0.0)
    latency_max_ms: float = Field(ge=0.0)
    throughput_fps: float = Field(gt=0.0)
    peak_vram_gb: float | None = None
    cpu_utilization_pct: float | None = None
    gpu_utilization_pct: float | None = None
    power_watts_avg: float | None = None
    device_name: str
    target_id: TargetId
    timing_synchronized: bool
    timing_source: str

    @model_validator(mode="after")
    def _validate_percentiles_and_finiteness(self) -> BenchmarkResult:
        # Check finite floats for mandatory metrics
        for field_name in [
            "latency_min_ms",
            "latency_mean_ms",
            "latency_p50_ms",
            "latency_p90_ms",
            "latency_p95_ms",
            "latency_p99_ms",
            "latency_max_ms",
            "throughput_fps",
        ]:
            _ensure_finite_float(getattr(self, field_name), field_name)

        if self.peak_vram_gb is not None:
            _ensure_finite_float(self.peak_vram_gb, "peak_vram_gb")
            if self.peak_vram_gb < 0.0:
                raise ValueError("peak_vram_gb must be non-negative")

        if self.cpu_utilization_pct is not None:
            _ensure_finite_float(self.cpu_utilization_pct, "cpu_utilization_pct")
            if not (0.0 <= self.cpu_utilization_pct <= 100.0):
                raise ValueError(
                    f"cpu_utilization_pct must be between 0.0 and 100.0, got {self.cpu_utilization_pct}"
                )

        if self.gpu_utilization_pct is not None:
            _ensure_finite_float(self.gpu_utilization_pct, "gpu_utilization_pct")
            if not (0.0 <= self.gpu_utilization_pct <= 100.0):
                raise ValueError(
                    f"gpu_utilization_pct must be between 0.0 and 100.0, got {self.gpu_utilization_pct}"
                )

        if self.power_watts_avg is not None:
            _ensure_finite_float(self.power_watts_avg, "power_watts_avg")
            if self.power_watts_avg < 0.0:
                raise ValueError("power_watts_avg must be non-negative")

        if not self.device_name or not str(self.device_name).strip():
            raise ValueError("device_name cannot be empty")

        if not self.timing_source or not str(self.timing_source).strip():
            raise ValueError("timing_source cannot be empty")

        source_normalized = str(self.timing_source).strip().lower()
        if source_normalized in {
            "unsynchronized",
            "wall_clock_async",
            "none",
            "unknown",
            "async_wall_clock",
            "async",
            "wall_clock",
        }:
            raise ValueError(
                f"timing_source {self.timing_source!r} is unsynchronized. "
                "Hardware-synchronized timing is required per ADR-0011 §3.13."
            )

        dev_lower = str(self.device_name).strip().lower()
        gpu_keywords = (
            "nvidia",
            "cuda",
            "geforce",
            "rtx",
            "gtx",
            "tesla",
            "a100",
            "h100",
            "v100",
            "t4",
            "l4",
            "jetson",
            "radeon",
            "rocm",
            "metal",
            "apple m",
            "accelerator",
            "tpu",
            "npu",
        )
        is_accelerator = any(k in dev_lower for k in gpu_keywords)
        if is_accelerator and self.timing_synchronized is not True:
            raise ValueError(
                f"Hardware-synchronized timing (timing_synchronized=True) is mandatory for "
                f"accelerator measurements ({self.device_name!r}) per ADR-0011 §3.13"
            )

        # Monotonic percentiles invariant:
        # 0 <= min <= p50 <= p90 <= p95 <= p99 <= max
        if not (
            self.latency_min_ms
            <= self.latency_p50_ms
            <= self.latency_p90_ms
            <= self.latency_p95_ms
            <= self.latency_p99_ms
            <= self.latency_max_ms
        ):
            raise ValueError(
                f"Latency percentiles violate monotonic ordering "
                f"(min={self.latency_min_ms}, p50={self.latency_p50_ms}, "
                f"p90={self.latency_p90_ms}, p95={self.latency_p95_ms}, "
                f"p99={self.latency_p99_ms}, max={self.latency_max_ms})"
            )

        return self


class ErrorAnalysisRecord(BaseModel):
    """Structured diagnostic failure breakdown per [P§27]."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    total_samples: int = Field(ge=0)
    failed_samples_count: int = Field(ge=0)
    false_positive_count: int = Field(ge=0)
    false_negative_count: int = Field(ge=0)
    confusion_matrix: tuple[tuple[int, ...], ...] = Field(default_factory=tuple)
    class_labels: tuple[str, ...] = Field(default_factory=tuple)
    localization_errors: int = Field(ge=0, default=0)
    classification_errors: int = Field(ge=0, default=0)
    slice_performance: Mapping[str, float] = Field(default_factory=dict)
    representative_failure_hashes: tuple[ContentHash, ...] = Field(
        default_factory=tuple
    )
    diagnostics_summary: str = ""

    @field_validator("slice_performance")
    @classmethod
    def _freeze_slices(cls, v: Mapping[str, float]) -> Any:
        for k, val in v.items():
            _ensure_finite_float(val, f"slice_performance[{k}]")
        return deep_freeze(v)

    @model_validator(mode="after")
    def _validate_confusion_matrix(self) -> ErrorAnalysisRecord:
        k = len(self.class_labels)
        matrix = self.confusion_matrix
        if matrix:
            if len(matrix) != k:
                raise ValueError(
                    f"Confusion matrix row count {len(matrix)} does not match class_labels count {k}"
                )
            for i, row in enumerate(matrix):
                if len(row) != k:
                    raise ValueError(
                        f"Confusion matrix row {i} length {len(row)} does not match class_labels count {k}"
                    )
                for j, count in enumerate(row):
                    if count < 0:
                        raise ValueError(
                            f"Confusion matrix cell [{i}][{j}] has negative count {count}"
                        )
        return self


class BaselineComparisonResult(BaseModel):
    """Explicit comparative analysis against an authoritative baseline [P§29.2]."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    baseline_experiment_id: ExperimentId
    candidate_experiment_id: ExperimentId
    comparable_evidence_ref: str
    test_type: StatisticalTestType
    test_version: str
    sample_count: int = Field(ge=1)
    confidence_level: float
    p_value: float
    statistically_significant: bool
    quality_metric_deltas: Mapping[MetricId, float]
    system_metric_deltas: Mapping[str, float] = Field(default_factory=dict)
    quality_improved: bool
    pareto_dominant: bool
    summary: str = ""

    @field_validator("baseline_experiment_id", "candidate_experiment_id")
    @classmethod
    def _validate_exp_id(cls, v: ExperimentId) -> ExperimentId:
        return validate_experiment_id(str(v))

    @field_validator("confidence_level")
    @classmethod
    def _check_confidence(cls, v: float) -> float:
        _ensure_finite_float(v, "confidence_level")
        if not (0.0 < v < 1.0):
            raise ValueError(f"confidence_level must be in (0.0, 1.0), got {v}")
        return v

    @field_validator("p_value")
    @classmethod
    def _check_p_value(cls, v: float) -> float:
        _ensure_finite_float(v, "p_value")
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"p_value must be in [0.0, 1.0], got {v}")
        return v

    @field_validator("quality_metric_deltas", "system_metric_deltas")
    @classmethod
    def _freeze_deltas(cls, v: Mapping[Any, float]) -> Any:
        for k, val in v.items():
            _ensure_finite_float(val, f"metric_delta[{k}]")
        return deep_freeze(v)


class EvaluationEnvironmentRecord(BaseModel):
    """Complete environment and reproducibility metadata per [P§25]."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    git_commit_sha: str
    git_is_dirty: bool
    dataset_manifest_hash: ContentHash
    weights_hash: ContentHash
    metric_definition_versions: tuple[str, ...]
    python_version: str
    torch_version: str
    cuda_version: str | None = None
    driver_version: str | None = None
    target_hardware: str
    seed: int | None = None


class EvaluationPlan(BaseModel):
    """Authoritative, immutable evaluation experiment plan."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    evaluation_id: EvaluationId
    experiment_id: ExperimentId
    created_at: datetime
    capability_id: CapabilityId
    dataset_binding: EvaluationDatasetBinding
    model_artifact: ModelArtifactReference
    task_type: TaskType
    metrics: tuple[MetricDefinition, ...]
    benchmark_config: BenchmarkConfiguration | None = None
    baseline_experiment_id: ExperimentId | None = None
    target_requirements: EvaluationTargetRequirements = Field(
        default_factory=EvaluationTargetRequirements
    )
    evaluation_requirements: Mapping[str, Any] = Field(default_factory=dict)
    reproducibility_requirements: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("evaluation_id")
    @classmethod
    def _validate_eval_id(cls, v: EvaluationId) -> EvaluationId:
        return validate_evaluation_id(str(v))

    @field_validator("experiment_id")
    @classmethod
    def _validate_exp_id(cls, v: ExperimentId) -> ExperimentId:
        return validate_experiment_id(str(v))

    @field_validator("baseline_experiment_id")
    @classmethod
    def _validate_base_id(cls, v: ExperimentId | None) -> ExperimentId | None:
        if v is None:
            return None
        return validate_experiment_id(str(v))

    @field_validator("metrics")
    @classmethod
    def _validate_metrics(
        cls, v: tuple[MetricDefinition, ...]
    ) -> tuple[MetricDefinition, ...]:
        if not v:
            raise ValueError(
                "EvaluationPlan must declare at least one MetricDefinition"
            )
        return tuple(v)

    @field_validator("evaluation_requirements", "reproducibility_requirements")
    @classmethod
    def _freeze_plan_maps(cls, v: Mapping[str, Any]) -> Any:
        return deep_freeze(v)


class EvaluationResult(BaseModel):
    """Comprehensive, immutable evaluation outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    evaluation_id: EvaluationId
    plan: EvaluationPlan
    verdict: EvaluationVerdict
    quality_metrics: tuple[MetricResult, ...]
    system_benchmarks: tuple[BenchmarkResult, ...] = Field(default_factory=tuple)
    baseline_comparison: BaselineComparisonResult | None = None
    error_analysis: ErrorAnalysisRecord | None = None
    environment: EvaluationEnvironmentRecord
    completed_at: datetime

    @field_validator("evaluation_id")
    @classmethod
    def _validate_id(cls, v: EvaluationId) -> EvaluationId:
        return validate_evaluation_id(str(v))


class EvaluationReport(BaseModel):
    """Human-readable and structured summary artifact for project memory."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    report_id: str
    evaluation_id: EvaluationId
    title: str
    markdown_content: str
    primary_quality_summary: str
    primary_systems_summary: str
    baseline_verdict: str
    generated_at: datetime

    @field_validator("evaluation_id")
    @classmethod
    def _validate_id(cls, v: EvaluationId) -> EvaluationId:
        return validate_evaluation_id(str(v))
