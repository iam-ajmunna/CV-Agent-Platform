"""cv_agent.monitoring.contracts — Immutable Pydantic v2 domain models for ADR-0013."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cv_agent.dataset import ContentHash, DatasetId, validate_content_hash
from cv_agent.evaluation.types import EvaluationId, validate_evaluation_id
from cv_agent.monitoring.types import (
    ActiveLearningStrategy,
    AlertId,
    CandidateBatchId,
    CandidateSampleId,
    DriftAnalysisId,
    DriftMetricType,
    DriftSeverity,
    SnapshotId,
    TelemetryDimension,
    TriggerProposalId,
    TriggerReason,
)
from cv_agent.optimization.types import DeploymentId
from cv_agent.training.types import ApprovalId


def _ensure_finite_float(val: float, name: str) -> float:
    """Validate float metric is strictly finite (not NaN, not Inf)."""
    if math.isnan(val) or math.isinf(val):
        raise ValueError(f"Field {name!r} must be a finite float, got {val}")
    return val


def deep_freeze(value: Any) -> Any:
    """Recursively freeze dictionaries into MappingProxyType and lists into tuples."""
    if isinstance(value, dict):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    return value


class ModelTelemetryRecord(BaseModel):
    """Rolling window observations of model prediction behavior [P§28]."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    deployment_id: DeploymentId = Field(
        ..., description="ID of the deployed runtime (ADR-0012)."
    )
    window_start: datetime = Field(
        ..., description="Start of the observation window (UTC)."
    )
    window_end: datetime = Field(
        ..., description="End of the observation window (UTC)."
    )
    window_seconds: float = Field(
        ..., gt=0.0, description="Duration of observation window."
    )
    total_inferences: int = Field(
        ..., ge=0, description="Total frames processed in window."
    )

    # Confidence distribution
    confidence_histogram: Mapping[str, int] = Field(
        ...,
        description="Binned prediction confidence histogram (e.g. '0.0-0.1': 12, '0.9-1.0': 850).",
    )
    confidence_mean: float = Field(
        ..., ge=0.0, le=1.0, description="Mean confidence across predictions."
    )
    confidence_p10: float = Field(
        ..., ge=0.0, le=1.0, description="10th percentile confidence."
    )
    confidence_p50: float = Field(..., ge=0.0, le=1.0, description="Median confidence.")
    confidence_p90: float = Field(
        ..., ge=0.0, le=1.0, description="90th percentile confidence."
    )

    # Class distribution and detection rates
    class_counts: Mapping[str, int] = Field(
        ..., description="Total detections per predicted class."
    )
    class_proportions: Mapping[str, float] = Field(
        ...,
        description="Proportion of total detections per class (sums to 1.0).",
    )
    mean_detections_per_frame: float = Field(
        ..., ge=0.0, description="Average objects detected per frame."
    )
    zero_detection_frame_ratio: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Fraction of frames with zero detections.",
    )

    # Information-theoretic & distribution metrics
    prediction_entropy_mean: float = Field(
        ...,
        ge=0.0,
        description="Mean Shannon entropy across output probability distributions.",
    )
    out_of_distribution_score_mean: float | None = Field(
        default=None,
        ge=0.0,
        description="Mean OOD/reconstruction error score if an OOD detector is active.",
    )

    # Optional feedback metrics
    empirical_false_positive_count: int | None = Field(default=None, ge=0)
    empirical_false_negative_count: int | None = Field(default=None, ge=0)

    @field_validator(
        "window_seconds",
        "confidence_mean",
        "confidence_p10",
        "confidence_p50",
        "confidence_p90",
        "mean_detections_per_frame",
        "zero_detection_frame_ratio",
        "prediction_entropy_mean",
    )
    @classmethod
    def _validate_floats(cls, v: float) -> float:
        return _ensure_finite_float(v, "ModelTelemetryRecord_float")

    @field_validator("out_of_distribution_score_mean")
    @classmethod
    def _validate_ood_float(cls, v: float | None) -> float | None:
        if v is not None:
            return _ensure_finite_float(v, "out_of_distribution_score_mean")
        return None

    @field_validator("confidence_histogram", "class_counts")
    @classmethod
    def _validate_counts(cls, v: Mapping[str, int]) -> Any:
        for k, count in v.items():
            if count < 0:
                raise ValueError(
                    f"Counts for key {k!r} must be non-negative, got {count}"
                )
        return deep_freeze(v)

    @field_validator("class_proportions")
    @classmethod
    def _validate_proportions(cls, v: Mapping[str, float]) -> Any:
        for k, prop in v.items():
            _ensure_finite_float(prop, f"class_proportions[{k}]")
            if prop < 0.0 or prop > 1.0:
                raise ValueError(
                    f"Class proportion for {k!r} must be in [0.0, 1.0], got {prop}"
                )
        return deep_freeze(v)

    @model_validator(mode="after")
    def _validate_model_telemetry(self) -> ModelTelemetryRecord:
        if self.window_end <= self.window_start:
            raise ValueError(
                f"window_end ({self.window_end}) must be strictly after window_start ({self.window_start})"
            )

        # Monotonic confidence percentiles: p10 <= p50 <= p90
        if not (self.confidence_p10 <= self.confidence_p50 <= self.confidence_p90):
            raise ValueError(
                f"Confidence percentiles must satisfy p10 <= p50 <= p90, got "
                f"p10={self.confidence_p10}, p50={self.confidence_p50}, p90={self.confidence_p90}"
            )

        # If there are class proportions, check sum is ~1.0 within tolerance
        if self.class_proportions:
            total_prop = sum(self.class_proportions.values())
            if not math.isclose(total_prop, 1.0, abs_tol=1e-3):
                raise ValueError(
                    f"class_proportions must sum to 1.0 within tolerance, got {total_prop}"
                )

        return self


class SystemTelemetryRecord(BaseModel):
    """Hardware, edge execution, and pipeline throughput metrics [P§13], [P§28]."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    deployment_id: DeploymentId = Field(
        ..., description="ID of the deployed runtime (ADR-0012)."
    )
    timestamp: datetime = Field(
        ..., description="Timestamp of telemetry capture (UTC)."
    )
    hardware_target: str = Field(
        ..., description="Target hardware identifier (e.g. 'jetson_orin_nano')."
    )

    # Throughput and latency
    throughput_fps: float = Field(
        ..., ge=0.0, description="Instantaneous or windowed frames per second."
    )
    latency_p50_ms: float = Field(
        ..., ge=0.0, description="50th percentile end-to-end latency in ms."
    )
    latency_p95_ms: float = Field(
        ..., ge=0.0, description="95th percentile end-to-end latency in ms."
    )
    latency_p99_ms: float = Field(
        ..., ge=0.0, description="99th percentile end-to-end latency in ms."
    )
    dropped_frames_total: int = Field(
        ..., ge=0, description="Dropped frame count in window."
    )
    dropped_frame_ratio: float = Field(
        ..., ge=0.0, le=1.0, description="Ratio of dropped to ingested frames."
    )

    # Compute utilization
    gpu_utilization_pct: float = Field(
        ..., ge=0.0, le=100.0, description="GPU engine utilization percentage."
    )
    cpu_utilization_pct: float = Field(
        ..., ge=0.0, le=100.0, description="Host CPU utilization percentage."
    )
    vram_used_mb: float = Field(
        ..., ge=0.0, description="Active GPU VRAM in megabytes."
    )
    vram_total_mb: float = Field(
        ..., ge=0.0, description="Total available GPU VRAM in megabytes."
    )
    ram_used_mb: float = Field(..., ge=0.0, description="Host RAM used in megabytes.")

    # Thermal and power conditions
    gpu_temperature_celsius: float = Field(..., description="GPU core temperature.")
    power_draw_watts: float | None = Field(
        default=None, ge=0.0, description="Current power consumption in Watts."
    )
    thermal_throttling_active: bool = Field(
        default=False,
        description="True if hardware is actively throttling clock frequencies due to temperature.",
    )

    @field_validator(
        "throughput_fps",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "dropped_frame_ratio",
        "gpu_utilization_pct",
        "cpu_utilization_pct",
        "vram_used_mb",
        "vram_total_mb",
        "ram_used_mb",
        "gpu_temperature_celsius",
    )
    @classmethod
    def _validate_floats(cls, v: float) -> float:
        return _ensure_finite_float(v, "SystemTelemetryRecord_float")

    @field_validator("power_draw_watts")
    @classmethod
    def _validate_power(cls, v: float | None) -> float | None:
        if v is not None:
            return _ensure_finite_float(v, "power_draw_watts")
        return None

    @model_validator(mode="after")
    def _validate_system_telemetry(self) -> SystemTelemetryRecord:
        if not (self.latency_p50_ms <= self.latency_p95_ms <= self.latency_p99_ms):
            raise ValueError(
                f"Latency percentiles must satisfy p50 <= p95 <= p99, got "
                f"p50={self.latency_p50_ms}, p95={self.latency_p95_ms}, p99={self.latency_p99_ms}"
            )
        if self.vram_used_mb > self.vram_total_mb:
            raise ValueError(
                f"vram_used_mb ({self.vram_used_mb}) cannot exceed vram_total_mb ({self.vram_total_mb})"
            )
        if not self.hardware_target or not self.hardware_target.strip():
            raise ValueError("hardware_target cannot be empty")
        return self


class DataTelemetryRecord(BaseModel):
    """Input stream characteristics, image quality, and optical degradation metrics [P§28]."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    stream_id: str = Field(..., description="Identifier of the video or sensor stream.")
    camera_id: str = Field(..., description="Logical or physical camera identifier.")
    timestamp: datetime = Field(
        ..., description="Timestamp of stream telemetry capture (UTC)."
    )

    # Image quality and optical properties
    mean_luminance: float = Field(
        ..., ge=0.0, le=255.0, description="Mean pixel intensity (brightness)."
    )
    contrast_std: float = Field(
        ..., ge=0.0, description="Standard deviation of pixel intensities (contrast)."
    )
    blur_laplacian_variance: float = Field(
        ...,
        ge=0.0,
        description="Variance of the Laplacian; low values indicate defocus, blur, or occlusion.",
    )
    occlusion_detected: bool = Field(
        default=False, description="True if camera is occluded or lens is blocked."
    )
    overexposure_ratio: float = Field(
        ..., ge=0.0, le=1.0, description="Fraction of saturated white pixels."
    )
    underexposure_ratio: float = Field(
        ..., ge=0.0, le=1.0, description="Fraction of near-black pixels."
    )

    # Video stream health
    resolution_width: int = Field(..., gt=0, description="Stream frame width.")
    resolution_height: int = Field(..., gt=0, description="Stream frame height.")
    stream_bitrate_kbps: float | None = Field(
        default=None, ge=0.0, description="Network stream bitrate."
    )
    packet_loss_ratio: float | None = Field(
        default=None, ge=0.0, le=1.0, description="RTSP/network packet loss ratio."
    )
    scene_change_detected: bool = Field(
        default=False, description="True if structural scene shift occurred."
    )

    @field_validator(
        "mean_luminance",
        "contrast_std",
        "blur_laplacian_variance",
        "overexposure_ratio",
        "underexposure_ratio",
    )
    @classmethod
    def _validate_floats(cls, v: float) -> float:
        return _ensure_finite_float(v, "DataTelemetryRecord_float")

    @field_validator("stream_bitrate_kbps", "packet_loss_ratio")
    @classmethod
    def _validate_optional_floats(cls, v: float | None) -> float | None:
        if v is not None:
            return _ensure_finite_float(v, "DataTelemetryRecord_opt_float")
        return None

    @model_validator(mode="after")
    def _validate_data_telemetry(self) -> DataTelemetryRecord:
        if not self.stream_id or not self.stream_id.strip():
            raise ValueError("stream_id cannot be empty")
        if not self.camera_id or not self.camera_id.strip():
            raise ValueError("camera_id cannot be empty")
        if (self.overexposure_ratio + self.underexposure_ratio) > 1.0 + 1e-4:
            raise ValueError(
                f"Sum of overexposure ({self.overexposure_ratio}) and underexposure ({self.underexposure_ratio}) cannot exceed 1.0"
            )
        return self


class UnifiedTelemetrySnapshot(BaseModel):
    """Synchronized tri-dimensional snapshot combining model, system, and data metrics [P§28]."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    snapshot_id: SnapshotId = Field(..., description="Unique snapshot identifier.")
    deployment_id: DeploymentId = Field(..., description="Deployment runtime ID.")
    stream_id: str = Field(..., description="Stream identifier.")
    timestamp: datetime = Field(..., description="Synchronization timestamp (UTC).")
    model_telemetry: ModelTelemetryRecord
    system_telemetry: SystemTelemetryRecord
    data_telemetry: DataTelemetryRecord

    @model_validator(mode="after")
    def _validate_snapshot_consistency(self) -> UnifiedTelemetrySnapshot:
        if self.deployment_id != self.model_telemetry.deployment_id:
            raise ValueError(
                f"Snapshot deployment_id ({self.deployment_id}) does not match model_telemetry.deployment_id ({self.model_telemetry.deployment_id})"
            )
        if self.deployment_id != self.system_telemetry.deployment_id:
            raise ValueError(
                f"Snapshot deployment_id ({self.deployment_id}) does not match system_telemetry.deployment_id ({self.system_telemetry.deployment_id})"
            )
        if self.stream_id != self.data_telemetry.stream_id:
            raise ValueError(
                f"Snapshot stream_id ({self.stream_id}) does not match data_telemetry.stream_id ({self.data_telemetry.stream_id})"
            )
        return self


class BaselineDistributionProfile(BaseModel):
    """Authoritative baseline distribution profile extracted from ADR-0011 evaluation evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    distribution_ref: str = Field(
        ...,
        description="Authoritative URI or reference to ADR-0011 distribution evidence artifact (e.g. 'eval-evidence://eval-20260920/distributions').",
    )
    confidence_histogram: Mapping[str, int] = Field(
        ...,
        description="Authoritative binned confidence histogram from offline evaluation.",
    )
    confidence_quantiles: tuple[float, ...] = Field(
        ...,
        description="Quantiles of baseline confidence (monotonic in [0.0, 1.0]) for continuous KS and Wasserstein tests.",
    )
    class_distribution: Mapping[str, float] = Field(
        ...,
        description="Normalized class proportions from the validated evaluation dataset split.",
    )
    prediction_entropy_mean: float = Field(
        ...,
        ge=0.0,
        description="Mean Shannon entropy across baseline evaluation predictions.",
    )
    sample_count: int = Field(
        ...,
        gt=0,
        description="Number of evaluation samples constituting this baseline distribution.",
    )
    feature_embedding_centroid: tuple[float, ...] | None = Field(
        default=None,
        description="Optional centroid vector in feature space for baseline representation distance.",
    )
    evidence_hash: ContentHash | None = Field(
        default=None,
        description="SHA-256 hash of the authoritative distribution artifact payload.",
    )

    @field_validator("prediction_entropy_mean")
    @classmethod
    def _validate_entropy(cls, v: float) -> float:
        return _ensure_finite_float(v, "prediction_entropy_mean")

    @field_validator("confidence_histogram")
    @classmethod
    def _freeze_hist(cls, v: Mapping[str, int]) -> Any:
        for k, count in v.items():
            if count < 0:
                raise ValueError(
                    f"Histogram count for {k!r} must be non-negative, got {count}"
                )
        return deep_freeze(v)

    @field_validator("confidence_quantiles")
    @classmethod
    def _validate_quantiles(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if not v:
            raise ValueError("confidence_quantiles cannot be empty")
        for i, q in enumerate(v):
            _ensure_finite_float(q, f"confidence_quantiles[{i}]")
            if not (0.0 <= q <= 1.0):
                raise ValueError(f"Quantile value must be in [0.0, 1.0], got {q}")
            if i > 0 and q < v[i - 1]:
                raise ValueError(
                    f"confidence_quantiles must be monotonically non-decreasing: {v[i - 1]} > {q}"
                )
        return tuple(v)

    @field_validator("class_distribution")
    @classmethod
    def _validate_class_dist(cls, v: Mapping[str, float]) -> Any:
        if not v:
            raise ValueError("class_distribution cannot be empty")
        for k, prop in v.items():
            _ensure_finite_float(prop, f"class_distribution[{k}]")
            if prop < 0.0 or prop > 1.0:
                raise ValueError(
                    f"Class proportion for {k!r} must be in [0.0, 1.0], got {prop}"
                )
        total = sum(v.values())
        if not math.isclose(total, 1.0, abs_tol=1e-3):
            raise ValueError(
                f"class_distribution must sum to 1.0 within tolerance, got {total}"
            )
        return deep_freeze(v)

    @field_validator("feature_embedding_centroid")
    @classmethod
    def _validate_centroid(
        cls, v: tuple[float, ...] | None
    ) -> tuple[float, ...] | None:
        if v is not None:
            for i, val in enumerate(v):
                _ensure_finite_float(val, f"feature_embedding_centroid[{i}]")
            return tuple(v)
        return None

    @field_validator("evidence_hash")
    @classmethod
    def _validate_evidence_hash(cls, v: ContentHash | None) -> ContentHash | None:
        if v is not None:
            return validate_content_hash(str(v))
        return None


class BaselineReferenceBinding(BaseModel):
    """Immutable binding to the validated offline baseline from ADR-0011."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    evaluation_id: EvaluationId = Field(
        ...,
        description="ID of the authoritative ADR-0011 EvaluationReport.",
    )
    dataset_manifest_hash: ContentHash = Field(
        ...,
        description="SHA-256 hash of the ADR-0009 evaluation split manifest.",
    )
    model_checkpoint_hash: ContentHash = Field(
        ...,
        description="SHA-256 hash of the unquantized or deployed checkpoint.",
    )
    baseline_distribution_ref: str = Field(
        ...,
        description="Authoritative reference URI to ADR-0011 distribution artifact.",
    )
    distribution_profile: BaselineDistributionProfile = Field(
        ...,
        description="Authoritative statistical distribution profile from ADR-0011 evaluation.",
    )
    evidence_hash: ContentHash | None = Field(
        default=None,
        description="Optional cryptographic content hash of the authoritative distribution evidence artifact.",
    )
    baseline_fps_p50: float = Field(
        ...,
        gt=0.0,
        description="50th-percentile validated baseline throughput (FPS).",
    )
    baseline_fps_p05: float = Field(
        ...,
        gt=0.0,
        description="5th-percentile baseline throughput; defines the lower operational bound.",
    )
    baseline_latency_p95_ms: float = Field(
        ...,
        gt=0.0,
        description="95th-percentile validated baseline latency (ms); alerts fire when online p95 exceeds this.",
    )
    baseline_latency_p99_ms: float = Field(
        ...,
        gt=0.0,
        description="99th-percentile validated baseline latency (ms); defines the worst-case SLA ceiling.",
    )
    baseline_confidence_mean: float = Field(..., ge=0.0, le=1.0)
    baseline_class_distribution: Mapping[str, float] = Field(
        ..., description="Baseline class balance."
    )
    deployed_artifact_id: str | None = Field(
        default=None,
        description="Optional deployed optimized artifact ID per ADR-0012 [B24].",
    )
    source_checkpoint_id: str | None = Field(
        default=None,
        description="Optional source checkpoint ID per ADR-0010 [B24].",
    )

    @field_validator("evaluation_id")
    @classmethod
    def _validate_eval_id(cls, v: EvaluationId) -> EvaluationId:
        return validate_evaluation_id(str(v))

    @field_validator("dataset_manifest_hash", "model_checkpoint_hash")
    @classmethod
    def _validate_content_hashes(cls, v: ContentHash) -> ContentHash:
        return validate_content_hash(str(v))

    @field_validator("evidence_hash")
    @classmethod
    def _validate_binding_evidence_hash(
        cls, v: ContentHash | None
    ) -> ContentHash | None:
        if v is not None:
            return validate_content_hash(str(v))
        return None

    @field_validator(
        "baseline_fps_p50",
        "baseline_fps_p05",
        "baseline_latency_p95_ms",
        "baseline_latency_p99_ms",
        "baseline_confidence_mean",
    )
    @classmethod
    def _validate_floats(cls, v: float) -> float:
        return _ensure_finite_float(v, "BaselineReferenceBinding_float")

    @field_validator("baseline_class_distribution")
    @classmethod
    def _validate_class_dist(cls, v: Mapping[str, float]) -> Any:
        for k, prop in v.items():
            _ensure_finite_float(prop, f"baseline_class_distribution[{k}]")
            if prop < 0.0 or prop > 1.0:
                raise ValueError(
                    f"Class proportion for {k!r} must be in [0.0, 1.0], got {prop}"
                )
        total = sum(v.values())
        if not math.isclose(total, 1.0, abs_tol=1e-3):
            raise ValueError(
                f"baseline_class_distribution must sum to 1.0 within tolerance, got {total}"
            )
        return deep_freeze(v)

    @model_validator(mode="after")
    def _validate_baseline_binding(self) -> BaselineReferenceBinding:
        if self.baseline_fps_p05 > self.baseline_fps_p50:
            raise ValueError(
                f"baseline_fps_p05 ({self.baseline_fps_p05}) cannot exceed baseline_fps_p50 ({self.baseline_fps_p50})"
            )
        if self.baseline_latency_p95_ms > self.baseline_latency_p99_ms:
            raise ValueError(
                f"baseline_latency_p95_ms ({self.baseline_latency_p95_ms}) cannot exceed baseline_latency_p99_ms ({self.baseline_latency_p99_ms})"
            )
        if self.baseline_distribution_ref != self.distribution_profile.distribution_ref:
            raise ValueError(
                f"baseline_distribution_ref ({self.baseline_distribution_ref}) does not match "
                f"distribution_profile.distribution_ref ({self.distribution_profile.distribution_ref})"
            )
        if (
            self.evidence_hash is not None
            and self.distribution_profile.evidence_hash is not None
            and self.evidence_hash != self.distribution_profile.evidence_hash
        ):
            raise ValueError(
                f"evidence_hash ({self.evidence_hash}) does not match "
                f"distribution_profile.evidence_hash ({self.distribution_profile.evidence_hash})"
            )
        return self


class DeploymentBaselineBinding(BaseModel):
    """Explicitly immutable join binding an active DeploymentRecord to its authoritative BaselineReferenceBinding per B24."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    deployment_id: DeploymentId
    deployed_artifact_id: str
    deployed_artifact_hash: ContentHash
    evaluation_id: EvaluationId
    evaluation_evidence_hash: ContentHash
    baseline_binding: BaselineReferenceBinding
    source_checkpoint_id: str | None = None

    @field_validator("deployed_artifact_hash", "evaluation_evidence_hash")
    @classmethod
    def _validate_join_hashes(cls, v: ContentHash) -> ContentHash:
        return validate_content_hash(str(v))

    @model_validator(mode="after")
    def _validate_binding_integrity(self) -> DeploymentBaselineBinding:
        if str(self.baseline_binding.evaluation_id) != str(self.evaluation_id):
            raise ValueError(
                f"evaluation_id mismatch: baseline_binding has {self.baseline_binding.evaluation_id} "
                f"but join declares {self.evaluation_id}"
            )
        if self.baseline_binding.evidence_hash is not None and str(
            self.baseline_binding.evidence_hash
        ) != str(self.evaluation_evidence_hash):
            raise ValueError(
                f"evidence_hash mismatch: baseline_binding has {self.baseline_binding.evidence_hash} "
                f"but join declares {self.evaluation_evidence_hash}"
            )
        if self.baseline_binding.deployed_artifact_id is not None and str(
            self.baseline_binding.deployed_artifact_id
        ) != str(self.deployed_artifact_id):
            raise ValueError(
                f"deployed_artifact_id mismatch: baseline_binding has {self.baseline_binding.deployed_artifact_id} "
                f"but join declares {self.deployed_artifact_id}"
            )
        if str(self.baseline_binding.model_checkpoint_hash) != str(
            self.deployed_artifact_hash
        ):
            raise ValueError(
                f"deployed_artifact_hash mismatch: baseline_binding model_checkpoint_hash "
                f"has {self.baseline_binding.model_checkpoint_hash} but join declares {self.deployed_artifact_hash}"
            )
        return self


def compute_distribution_profile_hash(
    profile: BaselineDistributionProfile,
) -> ContentHash:
    """Compute deterministic SHA-256 content hash of BaselineDistributionProfile evidence.

    Canonicalizes all distribution profile fields into a stable JSON representation per [P§25],
    guaranteeing cryptographic content integrity independent of in-memory formatting.
    """
    payload = {
        "class_distribution": dict(sorted(profile.class_distribution.items())),
        "confidence_histogram": dict(sorted(profile.confidence_histogram.items())),
        "confidence_quantiles": [
            round(float(q), 6) for q in profile.confidence_quantiles
        ],
        "distribution_ref": profile.distribution_ref,
        "feature_embedding_centroid": (
            [round(float(c), 6) for c in profile.feature_embedding_centroid]
            if profile.feature_embedding_centroid is not None
            else None
        ),
        "prediction_entropy_mean": round(float(profile.prediction_entropy_mean), 6),
        "sample_count": int(profile.sample_count),
    }
    canonical_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(canonical_bytes).hexdigest()
    return ContentHash(digest)


class DriftMetricResult(BaseModel):
    """Result of a specific statistical drift test across a monitored metric."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    dimension: TelemetryDimension
    metric_name: str = Field(
        ...,
        description="Name of the evaluated metric (e.g. 'confidence', 'class_distribution').",
    )
    drift_type: DriftMetricType
    statistic_value: float = Field(
        ...,
        description="Computed test statistic (e.g. KS statistic, PSI value, Wasserstein distance).",
    )
    p_value: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="P-value if the test is a hypothesis test (KS, Chi-Square).",
    )
    statistic_threshold: float = Field(
        ...,
        ge=0.0,
        description=(
            "Pre-configured critical threshold for statistic_value. "
            "Drift is detected when statistic_value >= statistic_threshold. "
            "Examples: KS threshold = 0.1, PSI threshold = 0.25, Wasserstein threshold = 0.05."
        ),
    )
    p_value_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Pre-configured significance level for p_value (e.g. 0.05). "
            "Only set when the test produces a p_value. "
            "Drift is detected when p_value < p_value_threshold."
        ),
    )
    drift_detected: bool = Field(
        ...,
        description="True if statistic exceeds statistic_threshold or p_value < p_value_threshold.",
    )
    severity: DriftSeverity
    details: Mapping[str, Any] = Field(
        default_factory=dict, description="Diagnostic parameters."
    )

    @field_validator("statistic_value", "statistic_threshold")
    @classmethod
    def _validate_stats(cls, v: float) -> float:
        return _ensure_finite_float(v, "DriftMetricResult_statistic")

    @field_validator("p_value", "p_value_threshold")
    @classmethod
    def _validate_p_values(cls, v: float | None) -> float | None:
        if v is not None:
            return _ensure_finite_float(v, "DriftMetricResult_p_value")
        return None

    @field_validator("details")
    @classmethod
    def _freeze_details(cls, v: Mapping[str, Any]) -> Any:
        return deep_freeze(v)


class DriftAnalysisRecord(BaseModel):
    """Comprehensive drift diagnosis generated over an observation window."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    analysis_id: DriftAnalysisId = Field(
        ..., description="Unique analysis record identifier."
    )
    deployment_id: DeploymentId = Field(
        ..., description="Evaluated deployment runtime ID."
    )
    baseline_binding: BaselineReferenceBinding
    timestamp: datetime = Field(
        ..., description="Timestamp of analysis generation (UTC)."
    )
    window_start: datetime
    window_end: datetime
    sample_count: int = Field(
        ..., gt=0, description="Number of observed frames in window."
    )

    # Detailed test results
    metric_results: tuple[DriftMetricResult, ...] = Field(
        ...,
        description="Individual drift test evaluations across model, system, and data.",
    )
    overall_drift_detected: bool = Field(
        ..., description="True if any critical or breaching metric was detected."
    )
    highest_severity: DriftSeverity
    diagnostic_summary: str = Field(
        ..., description="Human-readable synthesis of detected anomalies."
    )

    @model_validator(mode="after")
    def _validate_drift_analysis(self) -> DriftAnalysisRecord:
        if self.window_end <= self.window_start:
            raise ValueError(
                f"window_end ({self.window_end}) must be strictly after window_start ({self.window_start})"
            )
        if not self.diagnostic_summary or not self.diagnostic_summary.strip():
            raise ValueError("diagnostic_summary cannot be empty")
        return self


class CandidateSample(BaseModel):
    """An individual video frame or sensor item curated by active learning."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    sample_id: CandidateSampleId = Field(..., description="Unique sample identifier.")
    stream_id: str = Field(..., description="Source camera or stream identifier.")
    frame_index: int = Field(..., ge=0, description="Frame sequence index in stream.")
    timestamp: datetime = Field(..., description="Timestamp of frame capture (UTC).")
    frame_content_hash: ContentHash = Field(
        ..., description="SHA-256 hash of the uncompressed raw frame payload."
    )
    temporary_payload_uri: str = Field(
        ..., description="Staging URI (local scratch or object storage)."
    )
    expires_at: datetime | None = Field(
        default=None,
        description="UTC expiry time of the staging payload at temporary_payload_uri.",
    )

    # Sampling justification
    strategy_applied: ActiveLearningStrategy
    priority_score: float = Field(
        ...,
        ge=0.0,
        description="Priority score; higher means higher curation priority.",
    )
    model_predictions: tuple[Mapping[str, Any], ...] = Field(
        default_factory=tuple,
        description="Inference detections at capture time (classes, boxes, confidences).",
    )
    embedding_vector: tuple[float, ...] | None = Field(
        default=None,
        description="Feature embedding for diversity / core-set clustering.",
    )
    optical_metadata: Mapping[str, float] = Field(
        default_factory=dict,
        description="Luminance, contrast, and blur metrics at frame capture.",
    )

    @field_validator("priority_score")
    @classmethod
    def _validate_priority(cls, v: float) -> float:
        return _ensure_finite_float(v, "priority_score")

    @field_validator("frame_content_hash")
    @classmethod
    def _validate_frame_hash(cls, v: ContentHash) -> ContentHash:
        return validate_content_hash(str(v))

    @field_validator("model_predictions")
    @classmethod
    def _freeze_predictions(cls, v: tuple[Mapping[str, Any], ...]) -> Any:
        return deep_freeze(v)

    @field_validator("embedding_vector")
    @classmethod
    def _validate_embedding(
        cls, v: tuple[float, ...] | None
    ) -> tuple[float, ...] | None:
        if v is not None:
            for i, val in enumerate(v):
                _ensure_finite_float(val, f"embedding_vector[{i}]")
            return tuple(v)
        return None

    @field_validator("optical_metadata")
    @classmethod
    def _freeze_optical(cls, v: Mapping[str, float]) -> Any:
        for k, val in v.items():
            _ensure_finite_float(val, f"optical_metadata[{k}]")
        return deep_freeze(v)

    @model_validator(mode="after")
    def _validate_candidate_sample(self) -> CandidateSample:
        if not self.stream_id or not self.stream_id.strip():
            raise ValueError("stream_id cannot be empty")
        if not self.temporary_payload_uri or not self.temporary_payload_uri.strip():
            raise ValueError("temporary_payload_uri cannot be empty")
        return self


class ActiveLearningCandidateBatch(BaseModel):
    """A bounded collection of curated candidate samples packaged for dataset ingestion."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    batch_id: CandidateBatchId = Field(
        ..., description="Unique candidate batch identifier."
    )
    deployment_id: DeploymentId = Field(
        ..., description="Deployment ID producing the stream."
    )
    created_at: datetime = Field(..., description="Batch creation timestamp (UTC).")
    strategies_used: tuple[ActiveLearningStrategy, ...]
    total_candidates: int = Field(..., gt=0, description="Number of samples in batch.")
    sample_manifest: tuple[CandidateSample, ...] = Field(
        ..., description="Curated candidate samples."
    )
    selection_rationale: str = Field(
        ..., description="Algorithmic rationale for batch curation."
    )
    target_dataset_id: DatasetId = Field(
        ...,
        description="Target ADR-0009 dataset designated to receive this candidate batch.",
    )

    @model_validator(mode="after")
    def _validate_batch(self) -> ActiveLearningCandidateBatch:
        if len(self.sample_manifest) != self.total_candidates:
            raise ValueError(
                f"Length of sample_manifest ({len(self.sample_manifest)}) does not match total_candidates ({self.total_candidates})"
            )
        if not self.selection_rationale or not self.selection_rationale.strip():
            raise ValueError("selection_rationale cannot be empty")
        return self


class MonitoringAlertRecord(BaseModel):
    """Immediate operational alert dispatched to operators or orchestration."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    alert_id: AlertId = Field(..., description="Unique alert identifier.")
    deployment_id: DeploymentId = Field(..., description="Source deployment ID.")
    severity: DriftSeverity
    timestamp: datetime = Field(..., description="Alert timestamp (UTC).")
    dimension: TelemetryDimension
    message: str = Field(..., description="Concise human-readable alert message.")
    observed_value: float
    threshold_value: float
    recommended_mitigation: str = Field(
        ...,
        description="Mitigation action (e.g. 'Investigate camera 3 lens', 'Roll back to deployment d-011').",
    )

    @field_validator("observed_value", "threshold_value")
    @classmethod
    def _validate_floats(cls, v: float) -> float:
        return _ensure_finite_float(v, "MonitoringAlertRecord_float")

    @model_validator(mode="after")
    def _validate_alert(self) -> MonitoringAlertRecord:
        if not self.message or not self.message.strip():
            raise ValueError("message cannot be empty")
        if not self.recommended_mitigation or not self.recommended_mitigation.strip():
            raise ValueError("recommended_mitigation cannot be empty")
        return self


class RetrainingTriggerProposal(BaseModel):
    """Structured proposal requesting model iteration, submitted to Reasoning (ADR-0008)."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    proposal_id: TriggerProposalId = Field(
        ..., description="Unique trigger proposal identifier."
    )
    deployment_id: DeploymentId = Field(
        ..., description="Active deployment runtime ID."
    )
    created_at: datetime = Field(
        ..., description="Timestamp of proposal emission (UTC)."
    )
    trigger_reason: TriggerReason
    severity: DriftSeverity

    # Evidence bindings
    drift_analysis_id: DriftAnalysisId | None = Field(
        default=None,
        description="Reference to the originating DriftAnalysisRecord.",
    )
    candidate_batch_id: CandidateBatchId | None = Field(
        default=None,
        description="Reference to the curated ActiveLearningCandidateBatch.",
    )
    evidence_summary: str = Field(
        ...,
        description="Grounded quantitative evidence detailing why retraining is proposed.",
    )
    estimated_new_samples: int = Field(
        ..., ge=0, description="Number of new labeled samples to be added."
    )

    # Governance & Approval preconditions [P§24], docs/APPROVALS.md
    requires_human_approval: bool = Field(
        default=True,
        description="Invariant: must always be True. Autonomous retraining is strictly forbidden.",
    )
    approval_id: ApprovalId | None = Field(
        default=None,
        description="Identifier of the formal approval record once authorized per docs/APPROVALS.md.",
    )
    approved: bool = Field(
        default=False, description="True only after explicit PM/human approval."
    )
    approved_by: str | None = Field(
        default=None, description="Identity of approving human operator."
    )
    approval_timestamp: datetime | None = Field(default=None)

    @field_validator("requires_human_approval")
    @classmethod
    def _enforce_human_approval_invariant(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError(
                "requires_human_approval must always be True. Autonomous retraining is strictly forbidden per [P§24]."
            )
        return True

    @model_validator(mode="after")
    def _validate_approval_state(self) -> RetrainingTriggerProposal:
        if not self.evidence_summary or not self.evidence_summary.strip():
            raise ValueError("evidence_summary cannot be empty")

        if self.approved:
            if not self.approval_id:
                raise ValueError("Approved proposal must specify approval_id")
            if not self.approved_by or not self.approved_by.strip():
                raise ValueError("Approved proposal must specify approved_by identity")
            if self.approval_timestamp is None:
                raise ValueError("Approved proposal must specify approval_timestamp")
        else:
            if self.approved_by is not None:
                raise ValueError("Unapproved proposal cannot specify approved_by")
            if self.approval_timestamp is not None:
                raise ValueError(
                    "Unapproved proposal cannot specify approval_timestamp"
                )

        return self
