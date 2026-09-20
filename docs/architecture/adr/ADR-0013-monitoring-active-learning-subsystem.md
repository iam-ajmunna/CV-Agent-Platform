# ADR-0013: Monitoring and Active Learning Subsystem Architecture

- **Status:** Accepted — Frozen
- **Date:** 2026-09-09 (Frozen: 2026-09-10)
- **Last Reviewed:** 2026-09-10
- **Layer:** monitoring / active_learning
- **Canon:** `[P§8]`, `[P§9]`, `[P§16]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§26]`, `[P§27]`, `[P§28]`, `[P§29.6]`, `[P§29.7]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #10
- **Preceded by Decisions:** D-032 (ADR-0012 Optimization & Deployment Subsystem), D-031 (ADR-0011 Evaluation Subsystem), D-030 (ADR-0010 Training Subsystem), D-029 (ADR-0009 Dataset Subsystem), D-026 (ADR-0008 Reasoning Subsystem), D-025 (ADR-0007 Skills), D-023 (ADR-0005 Tools & MCP Boundary), D-021 (ADR-0004 Experiment Ledger), D-020 (ADR-0003 Orchestration & Approval), D-014 (Out-of-process GPU compute), D-022 (NVIDIA hardware inventory)

## 1. Context

Deploying a computer vision model to production (ADR-0012) is not the terminus of the engineering lifecycle `[P§28]`. In real-world visual operations—such as multi-camera facility surveillance, industrial inspection, or robotics—deployed models confront an environment of inevitable entropy:
1. **Covariate Shift / Data Distribution Shift (`[P§28]`, `[P§29.6]`):** Optical sensors degrade over time; lens occlusion, dust, lighting changes (day/night, seasonal shadows, glare), focus drift, and weather anomalies alter input distributions relative to training baselines.
2. **Concept Drift (`[P§28]`, `[P§29.6]`):** Visual classes evolve dynamically (e.g. changes in worker uniforms, novel vehicle models, unexpected posture variations, unseen edge cases).
3. **Hardware & Edge Streaming Degradation (`[P§13]`, `[P§28]`):** Edge devices suffer from thermal throttling, packet drops in RTSP feeds, decoder buffer overruns, and latency spikes that degrade real-time inference throughput (FPS) and cause dropped frames.
4. **Silent Failure Mode (`[P§27]`, `[P§29.3]`):** Deep neural networks frequently emit high-confidence predictions on novel out-of-distribution (OOD) samples, masking degradation until physical failures occur.
5. **The Uncurated Stream Dilemma (`[P§26]`, `[P§29.7]`):** A continuous CCTV stream produces tens of thousands of frames per hour. Ingesting and labeling all production video is economically and computationally impossible. A principled data flywheel requires an **Active Learning Subsystem** that intelligently samples high-information frames (uncertainty, diversity, OOD anomalies) to drive targeted model iteration.

Crucially, **active learning must not become an autonomous, unsupervised retraining loop** `[P§24]`, `[P§31]`. Unbounded autonomous retraining risks catastrophic forgetting, runaway compute expenditure, data poisoning, and model regression. ADR-0013 establishes an **observe $\rightarrow$ detect $\rightarrow$ sample $\rightarrow$ propose** architecture, where retraining proposals are submitted to the Reasoning Subsystem (ADR-0008) and gated by mandatory Human Approval (`docs/APPROVALS.md`).

This ADR establishes the architectural specification, interface contracts, telemetry schemas, statistical drift engines, active learning candidate selectors, and operational invariants for the **Monitoring & Active Learning Subsystem**.

## 2. Responsibility (`[P§34]`)

- **This owns:**
  - Continuous ingestion and buffering of tri-dimensional operational telemetry per `[P§28]`: Model metrics, System/Hardware metrics, and Data/Stream metrics.
  - The structured, immutable telemetry schemas: `ModelTelemetryRecord`, `SystemTelemetryRecord`, and `DataTelemetryRecord`.
  - Baseline distribution binding: linking live operational monitoring to the authoritative baseline established by the Evaluation Subsystem (ADR-0011 `EvaluationReport`).
  - Statistical baseline deviation and drift detection algorithms: Kolmogorov-Smirnov (KS) tests, Population Stability Index (PSI), Wasserstein / Earth Mover's Distance, Chi-Square divergence, and prediction entropy shifts.
  - The immutable drift diagnosis specification: `DriftAnalysisRecord` and `DriftMetricResult`.
  - Active learning candidate selection strategies (`ActiveLearningSelector`): uncertainty/entropy sampling, margin sampling, feature-space diversity/clustering, out-of-distribution density estimation, and environmental shift triggers.
  - Candidate sample extraction and packaging: `CandidateSample` and `ActiveLearningCandidateBatch` with cryptographic frame checksums and stream metadata.
  - Structured proposal formulation: emitting `RetrainingTriggerProposal` and `MonitoringAlertRecord` documenting empirical drift evidence and candidate batch references.
  - Explicit handoff boundaries to downstream subsystems: candidate payloads to ADR-0009 (Dataset) for storage/annotation, and proposals to ADR-0008 (Reasoning) for diagnosis and approval review.
  - Codification of operational alerting thresholds, windowing rules, and rollback trigger signals.
- **This does NOT own:**
  - Cognitive retraining decision-making, hypothesis generation, or strategy selection → Reasoning Subsystem (ADR-0008) `[P§4]`;
  - Human approval policy enforcement and workflow state persistence → Orchestration & Approvals (ADR-0003, `docs/APPROVALS.md`) `[P§24]`;
  - Dataset storage, content-addressed payload hashing, annotation versioning, or train/val split isolation → Dataset Subsystem (ADR-0009) `[P§26]`;
  - Neural network retraining execution, hyperparameter tuning, or checkpoint generation → Training Subsystem (ADR-0010) `[P§10]`;
  - Gold-standard offline model benchmarking, metric evaluation, or hardware profiling → Evaluation Subsystem (ADR-0011) `[P§12]`;
  - Deployment runtime lifecycle, engine instantiation, process restarts, or traffic rollback → Optimization & Deployment Subsystem (ADR-0012) `[P§8]`, `[P§9]`;
  - Physical network transport protocols, streaming brokers (Kafka, MQTT, gRPC, Prometheus), or socket polling → Tools & MCP Execution Boundary (ADR-0005) `[P§22]`;
  - External human labeling UI or manual annotation workflows → External Annotation Tools (interfaced via ADR-0009 manifests);
  - Permanent experiment or deployment history logging → Project Memory & Experiment Ledger (ADR-0004) `[P§25]`.
- **Why this responsibility does not belong elsewhere:**
  Conflating monitoring with deployment (ADR-0012) couples operational serving runtimes with heavyweight statistical distribution math and active learning heuristics. Placing monitoring inside evaluation (ADR-0011) confuses offline, gold-standard ground-truth benchmark measurement with online, unlabelled streaming telemetry analysis. Merging active learning into training (ADR-0010) risks creating an unconstrained autonomous feedback loop that bypasses data governance and human oversight. The Monitoring & Active Learning Subsystem isolates continuous observation, drift analysis, and intelligent candidate curation into a dedicated, verifiable layer.

## 3. Decision

We establish an explicit **Monitoring and Active Learning Subsystem Architecture** centered on tri-dimensional telemetry streaming, baseline drift detection, algorithmic candidate curation, and human-gated retraining triggers.

```text
              Production Deployment (ADR-0012)
       [Standalone TensorRT / DeepStream / Triton / ORT]
                              │
                              ▼ (Live Telemetry Stream)
       ┌────────────────────────────────────────────────────────┐
       │   ADR-0013 Monitoring & Active Learning Subsystem      │
       │                                                        │
       │  1. Tri-Dimensional Telemetry Ingestion [P§28]         │
       │     ├── Model: Confidence, Class Distribution, Entropy │
       │     ├── System: FPS, Latency, VRAM, Power, Dropped     │
       │     └── Data: Luminance, Blur, Occlusion, Packet Loss  │
       │                                                        │
       │  2. Statistical Drift & Anomaly Engine [P§28, P§29.6]  │
       │     └── Compare against ADR-0011 Validated Baseline    │
       │         (KS Test, PSI, Wasserstein, Entropy Divergence)│
       │                                                        │
       │  3. Active Learning Candidate Selector [P§26, P§29.7]  │
       │     ├── Uncertainty / Margin / Entropy Sampling        │
       │     ├── Feature-Space Diversity / Core-Set Clustering  │
       │     └── Out-of-Distribution & Environmental Anomaly    │
       │                                                        │
       │  4. Proposal & Alert Emission [P§24, P§27]             │
       │     ├── MonitoringAlertRecord (Immediate Alert)        │
       │     └── RetrainingTriggerProposal + Candidate Batch    │
       └──────────────────────────────┬─────────────────────────┘
                                      │
               ┌──────────────────────┴───────────────────────┐
               ▼                                              ▼
   ActiveLearningCandidateBatch                 RetrainingTriggerProposal
               │                                              │
               ▼                                              ▼
   ADR-0009 Dataset Subsystem                     ADR-0008 Reasoning Subsystem
   - Content-addressed staging                    - Experiment strategy formulation
   - Annotation manifest binding                  - Problem framing & diagnosis
               │                                              │
               │                                              ▼
               │                                   docs/APPROVALS.md [P§24]
               │                                    Human Approval Gate
               │                                              │
               └──────────────────────┬───────────────────────┘
                                      ▼
                        ADR-0010 Training Subsystem
                        (Gated Retraining Execution)
```

## 4. Tri-Dimensional Telemetry Ingestion Contract (`[P§28]`)

Per `[P§28]`, monitoring a deployed computer vision system requires concurrent observation across three distinct dimensions: **Model**, **System**, and **Data**. Observing one dimension in isolation yields false conclusions (e.g. an apparent drop in model confidence may be caused by a dirty camera lens, not model decay; an FPS drop may be caused by thermal throttling, not algorithmic complexity).

```python
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, NewType, Protocol, Sequence, runtime_checkable
from pydantic import BaseModel, ConfigDict, Field

from cv_agent.dataset.types import ContentHash, DatasetId, DatasetVersion
from cv_agent.evaluation.types import EvaluationId
from cv_agent.optimization.types import DeploymentId
from cv_agent.training.types import ApprovalId

# Canonical identifier format conforming to [a-z0-9_.-]+(/[a-z0-9_.-]+)* per ADR-0001 §3.1.
DriftAnalysisId = NewType("DriftAnalysisId", str)
CandidateBatchId = NewType("CandidateBatchId", str)
CandidateSampleId = NewType("CandidateSampleId", str)
AlertId = NewType("AlertId", str)
TriggerProposalId = NewType("TriggerProposalId", str)
SnapshotId = NewType("SnapshotId", str)


class TelemetryDimension(str, Enum):
    """The three canonical observation dimensions defined in [P§28]."""

    MODEL = "model"
    SYSTEM = "system"
    DATA = "data"


class ModelTelemetryRecord(BaseModel):
    """Rolling window observations of model prediction behavior [P§28]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        None,
        ge=0.0,
        description="Mean OOD/reconstruction error score if an OOD detector is active.",
    )

    # Optional feedback metrics (when operator ground-truth or human feedback exists)
    empirical_false_positive_count: int | None = Field(None, ge=0)
    empirical_false_negative_count: int | None = Field(None, ge=0)


class SystemTelemetryRecord(BaseModel):
    """Hardware, edge execution, and pipeline throughput metrics [P§13], [P§28]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
    power_draw_watts: Optional[float] = Field(
        None, ge=0.0, description="Current power consumption in Watts."
    )
    thermal_throttling_active: bool = Field(
        False,
        description="True if hardware is actively throttling clock frequencies due to temperature.",
    )


class DataTelemetryRecord(BaseModel):
    """Input stream characteristics, image quality, and optical degradation metrics [P§28]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        False, description="True if camera is occluded or lens is blocked."
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
    stream_bitrate_kbps: Optional[float] = Field(
        None, ge=0.0, description="Network stream bitrate."
    )
    packet_loss_ratio: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="RTSP/network packet loss ratio."
    )
    scene_change_detected: bool = Field(
        False, description="True if structural scene shift occurred."
    )


class UnifiedTelemetrySnapshot(BaseModel):
    """Synchronized tri-dimensional snapshot combining model, system, and data metrics [P§28]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: SnapshotId = Field(..., description="Unique snapshot identifier.")
    deployment_id: DeploymentId = Field(..., description="Deployment runtime ID.")
    stream_id: str = Field(..., description="Stream identifier.")
    timestamp: datetime = Field(..., description="Synchronization timestamp (UTC).")
    model_telemetry: ModelTelemetryRecord
    system_telemetry: SystemTelemetryRecord
    data_telemetry: DataTelemetryRecord
```

## 5. Baseline Drift Detection Engine (`[P§28]`, `[P§29.6]`)

A core invariant of the platform is **Baseline First** `[P§29.2]`: *never declare a model degraded or drifting without quantitative comparison against a validated baseline*. 

Live telemetry is continuously tested against the immutable baseline distribution recorded in the ADR-0011 `EvaluationReport`.

```python
class DriftMetricType(str, Enum):
    """Statistical distribution distance and hypothesis test algorithms."""

    KOLMOGOROV_SMIRNOV = (
        "kolmogorov_smirnov"  # Continuous distributions (confidence, latency)
    )
    POPULATION_STABILITY_INDEX = (
        "population_stability_index"  # Binned feature/confidence drift
    )
    WASSERSTEIN_DISTANCE = "wasserstein_distance"  # Earth Mover's Distance
    CHI_SQUARE_INDEPENDENCE = (
        "chi_square_independence"  # 2 x K contingency table independence test
    )
    CHI_SQUARE_GOODNESS_OF_FIT = "chi_square_goodness_of_fit"  # One-sample goodness-of-fit against baseline proportions
    ENTROPY_DIVERGENCE = (
        "entropy_divergence"  # KL/JS divergence of prediction uncertainty
    )
    Z_SCORE_DEVIATION = "z_score_deviation"  # Scalar system metrics (FPS, temperature)


class DriftSeverity(str, Enum):
    """Operational alert severity tiers."""

    INFO = "info"  # Detectable variation within acceptable operational tolerance
    WARNING = (
        "warning"  # Statistically significant drift approaching operational SLA limit
    )
    CRITICAL = "critical"  # Severe distribution shift violating performance invariants


class BaselineReferenceBinding(BaseModel):
    """Immutable binding to the validated offline baseline from ADR-0011."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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


class DriftMetricResult(BaseModel):
    """Result of a specific statistical drift test across a monitored metric."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        None,
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
            "Examples: KS threshold = 0.1, PSI threshold = 0.25, Wasserstein threshold = 0.05. "
            "MUST NOT be interpreted as a p-value."
        ),
    )
    p_value_threshold: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description=(
            "Pre-configured significance level for p_value (e.g. 0.05). "
            "Only set when the test produces a p_value. "
            "Drift is detected when p_value < p_value_threshold. "
            "MUST NOT be interpreted as a statistic threshold."
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


class DriftAnalysisRecord(BaseModel):
    """Comprehensive drift diagnosis generated over an observation window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        ..., description="True if any critical metric breached threshold."
    )
    highest_severity: DriftSeverity
    diagnostic_summary: str = Field(
        ..., description="Human-readable synthesis of detected anomalies."
    )
```

## 6. Active Learning Candidate Selection Engine (`[P§26]`, `[P§29.7]`)

Rather than streaming all raw CCTV video to storage, the **Active Learning Engine** filters and curates high-information candidate frames from edge or server streams. It operationalizes the data flywheel by selecting samples that maximize expected model improvement while minimizing labeling overhead.

```python
class ActiveLearningStrategy(str, Enum):
    """Canonical active learning sampling heuristics [P§26], [P§29.7]."""

    UNCERTAINTY_LEAST_CONFIDENT = (
        "uncertainty_least_confident"  # Lowest max-class confidence
    )
    UNCERTAINTY_MARGIN = "uncertainty_margin"  # Smallest difference between top-1 and top-2 probabilities
    UNCERTAINTY_ENTROPY = (
        "uncertainty_entropy"  # Highest output probability distribution entropy
    )
    FEATURE_DIVERSITY_CLUSTER = (
        "feature_diversity_cluster"  # Core-set / k-means in visual embedding space
    )
    OUT_OF_DISTRIBUTION_ANOMALY = (
        "out_of_distribution_anomaly"  # High reconstruction or density error
    )
    ENVIRONMENTAL_SHIFT = (
        "environmental_shift"  # Low-light, blur, weather, or scene change trigger
    )
    EMPIRICAL_FAILURE_FEEDBACK = (
        "empirical_failure_feedback"  # Operator-flagged false alarms / misses
    )


class CandidateSample(BaseModel):
    """An individual video frame or sensor item curated by active learning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        None,
        description=(
            "UTC expiry time of the staging payload at temporary_payload_uri. "
            "Candidate buffers and pre-signed storage URIs expire; if this field "
            "is set, the ActiveLearningSelector MUST evict or escalate this sample "
            "before the expiry instant rather than assembling it into a batch after "
            "the payload has become irrecoverable."
        ),
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
        None,
        description="Feature embedding for diversity / core-set clustering.",
    )
    optical_metadata: Mapping[str, float] = Field(
        default_factory=dict,
        description="Luminance, contrast, and blur metrics at frame capture.",
    )


class ActiveLearningCandidateBatch(BaseModel):
    """A bounded collection of curated candidate samples packaged for dataset ingestion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
```

## 7. Retraining Trigger Proposals & Alerting (`[P§24]`, `[P§27]`)

The Monitoring Subsystem does **not** unilaterally initiate model training. When drift, performance collapse, or candidate quotas are reached, it synthesizes an evidence-backed proposal.

```python
class TriggerReason(str, Enum):
    """Root cause triggering the retraining proposal."""

    MODEL_ACCURACY_DRIFT = (
        "model_accuracy_drift"  # Statistical confidence/entropy shift
    )
    CLASS_IMBALANCE_SHIFT = (
        "class_imbalance_shift"  # Production class frequencies diverged from training
    )
    OUT_OF_DISTRIBUTION_SPIKE = (
        "out_of_distribution_spike"  # High density of novel visual concepts
    )
    CAMERA_ENVIRONMENTAL_SHIFT = (
        "camera_environmental_shift"  # Persistent optical or scene change
    )
    ACTIVE_LEARNING_QUOTA_REACHED = (
        "active_learning_quota_reached"  # Curated candidate batch threshold met
    )
    SYSTEM_SLA_VIOLATION = (
        "system_sla_violation"  # Latency / throughput degraded below SLA limit
    )


class MonitoringAlertRecord(BaseModel):
    """Immediate operational alert dispatched to operators or orchestration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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


class RetrainingTriggerProposal(BaseModel):
    """Structured proposal requesting model iteration, submitted to Reasoning (ADR-0008)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        None,
        description="Reference to the originating DriftAnalysisRecord.",
    )
    candidate_batch_id: CandidateBatchId | None = Field(
        None,
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
        True,
        description="Invariant: must always be True. Autonomous retraining is strictly forbidden.",
    )
    approval_id: ApprovalId | None = Field(
        None,
        description="Identifier of the formal approval record once authorized per docs/APPROVALS.md.",
    )
    approved: bool = Field(
        False, description="True only after explicit PM/human approval."
    )
    approved_by: str | None = Field(
        None, description="Identity of approving human operator."
    )
    approval_timestamp: datetime | None = Field(None)
```

## 8. Subsystem Boundary Contracts & Interfaces

To maintain strict layer isolation `[P§19]`–`[P§22]`, the monitoring subsystem defines abstract interfaces for stream observation, statistical calculation, and active learning curation:

```python
@runtime_checkable
class TelemetryIngestionSink(Protocol):
    """Contract for consuming continuous runtime telemetry streams."""

    def ingest_model_telemetry(self, record: ModelTelemetryRecord) -> None:
        """Ingest a model telemetry window from a deployed runtime."""
        ...

    def ingest_system_telemetry(self, record: SystemTelemetryRecord) -> None:
        """Ingest hardware and throughput telemetry from an execution host."""
        ...

    def ingest_data_telemetry(self, record: DataTelemetryRecord) -> None:
        """Ingest optical stream health telemetry from an active camera feed."""
        ...


@runtime_checkable
class StatisticalDriftDetector(Protocol):
    """Contract for calculating distribution divergence against the validated baseline."""

    def evaluate_drift(
        self,
        telemetry_window: UnifiedTelemetrySnapshot,
        baseline: BaselineReferenceBinding,
    ) -> DriftAnalysisRecord:
        """Compute statistical drift metrics against the ADR-0011 baseline."""
        ...


@runtime_checkable
class ActiveLearningSelector(Protocol):
    """Contract for curating high-information frames from continuous streams."""

    def evaluate_candidate(
        self,
        frame_payload: bytes,
        predictions: Sequence[Mapping[str, Any]],
        optical_metrics: Mapping[str, float],
    ) -> CandidateSample | None:
        """Evaluate whether a stream frame qualifies as an active learning candidate."""
        ...

    def assemble_batch(
        self,
        min_candidates: int,
        target_dataset_id: DatasetId,
    ) -> ActiveLearningCandidateBatch | None:
        """Assemble buffered candidates into a bounded candidate batch for ADR-0009."""
        ...


@runtime_checkable
class MonitoringCoordinator(Protocol):
    """Top-level monitoring coordinator interface."""

    def emit_alert(self, alert: MonitoringAlertRecord) -> None:
        """Dispatch immediate operational alerts to logging or orchestration."""
        ...

    def submit_retraining_proposal(
        self,
        proposal: RetrainingTriggerProposal,
    ) -> None:
        """Submit an evidence-backed retraining proposal to ADR-0008 Reasoning."""
        ...
```

## 9. Interaction with Existing Subsystems

1. **Optimization & Deployment Subsystem (ADR-0012):**
   - ADR-0012 provisions the active inference runtime (`DeploymentRuntime`: TensorRT, DeepStream, Triton, ORT) and emits the initial `HealthCheckSpec`.
   - ADR-0013 continuously ingests the runtime's live inference outputs and hardware counters. If system latency or dropped frames violate operational invariants, ADR-0013 emits a critical alert recommending rollback to ADR-0012.
2. **Evaluation Subsystem (ADR-0011):**
   - ADR-0011 provides the immutable offline gold-standard benchmark (`BaselineReferenceBinding`).
   - ADR-0013 continuously contrasts online predictions against this baseline. Online monitoring never invents new baseline numbers; it strictly measures deviation from ADR-0011 results.
3. **Dataset Subsystem (ADR-0009):**
   - When an `ActiveLearningCandidateBatch` is assembled, candidate payloads are transferred to ADR-0009.
   - ADR-0009 content-addresses the raw media files via SHA-256 (`ContentHash`), stages them in `.cv_agent/datasets/`, and creates an unannotated `AnnotationManifest` draft for human labeling.
4. **Reasoning Subsystem (ADR-0008):**
   - ADR-0013 submits `RetrainingTriggerProposal` records to ADR-0008 (`ExperimentDiagnosisNode` / `StrategyFormulationNode`).
   - Reasoning formulates a cognitive retraining hypothesis (e.g. "Fine-tune backbone on low-light active learning batch with increased augmentations") without executing it directly.
5. **Orchestration & Approvals (ADR-0003 / `docs/APPROVALS.md` / `[P§24]`):**
   - Every retraining proposal requires explicit human approval. The platform never triggers retraining autonomously.
6. **Training Subsystem (ADR-0010):**
   - Only after human approval and ADR-0009 dataset manifest freezing does ADR-0010 receive an immutable `TrainingPlan` to execute the retraining run on external compute (`D-014`).
7. **Tools & MCP Execution Boundary (ADR-0005):**
   - Telemetry transport (subscribing to Kafka/Prometheus endpoints, polling edge daemons) is handled via ADR-0005 tool transports.

## 10. Codified Architectural Invariants

Every implementation of the Monitoring & Active Learning Subsystem must enforce these twelve non-negotiable invariants:

1. **Baseline Primacy Invariant (`[P§29.2]`):**
   No drift claim or degradation alert may be emitted without explicit mathematical comparison against a validated ADR-0011 `EvaluationReport`.
2. **Tri-Dimensional Completeness Invariant (`[P§28]`):**
   Operational telemetry must simultaneously capture Model, System, and Data metrics. Emitting model accuracy alerts without system throughput or optical data context is prohibited.
3. **Zero Autonomous Retraining Invariant (`[P§24]`, `[P§31]`):**
   The subsystem is strictly observation, detection, candidate selection, and proposal formulation. It possesses zero authority to start training jobs or modify production model weights autonomously.
4. **Mandatory Human Approval Invariant (`docs/APPROVALS.md`):**
   Every `RetrainingTriggerProposal` must enforce `requires_human_approval = True`. Retraining execution without explicit operator sign-off is an unrecoverable governance failure.
5. **Cryptographic Frame Lineage Invariant (`[P§25]`, `[P§26]`):**
   Every `CandidateSample` extracted by active learning must be content-addressed by its SHA-256 payload hash and bound to its source stream ID, frame index, and capture timestamp.
6. **Dataset Authority Delegation Invariant (`[P§26]`, ADR-0009):**
   The monitoring subsystem never mutates or directly commits dataset manifests. Candidate samples are handed off to ADR-0009, which retains sole authority over dataset versioning.
7. **Layer Decoupling Invariant (`[P§19]`–`[P§22]`):**
   Monitoring code must not import training frameworks (PyTorch, TensorFlow) or model execution engines directly; observation occurs strictly across typed telemetry boundaries.
8. **Statistically Grounded Drift Invariant (`[P§29.3]`):**
   Drift detection must utilize formal statistical tests (KS, PSI, Wasserstein, Chi-Square) with defined confidence intervals ($p < 0.05$ or validated distance thresholds), never arbitrary magic heuristics.
9. **Bounded Storage Buffer Invariant (`[P§28]`):**
   Active learning candidate buffers must enforce strict size and TTL limits. If buffers fill before candidate batch dispatch, least-informative samples must be evicted deterministically.
10. **Hardware-Aware Edge Viability Invariant (`[P§13]`, `[P§29.4]`):**
    On-device telemetry collection and candidate filtering must consume less than 5% of target CPU/GPU overhead so as not to degrade real-time inference SLAs.
11. **Immutability of Audit Records Invariant (`[P§25]`):**
    All telemetry summaries, drift analyses, and trigger proposals must be frozen Pydantic models (`ConfigDict(frozen=True)`).
12. **Transport Agnosticism Invariant (ADR-0005):**
    The monitoring contracts must remain transport-agnostic, supporting edge file buffers, MQTT, Kafka, and HTTP/gRPC telemetry streams without coupling domain logic to network libraries.

## 11. Consequences

### Positive
- **Outlives Deployment (`[P§28]`):** Transforms the platform from a static training framework into a living, production-hardened engineering agent that monitors edge realities.
- **Closed-Loop Data Flywheel (`[P§26]`):** High-value edge failure modes and rare events are algorithmically captured and staged for retraining, maximizing sample efficiency.
- **Absolute Safety from Runaway Autonomy (`[P§24]`):** Enforces human-in-the-loop control over all retraining compute allocations and model promotions.
- **Root-Cause Isolation:** Tri-dimensional metrics allow instant differentiation between sensor failure (dirty lens), network saturation (packet drops), thermal throttling (cooling failure), and true concept drift (new objects).

### Negative / Trade-offs
- **Telemetry Bandwidth:** Edge-to-cloud telemetry transmission requires bandwidth budget management; requires local edge summary aggregation rather than raw frame streaming.
- **Labeling Latency:** Active learning candidates require human annotation before retraining, meaning the flywheel speed is bounded by human labeling throughput.

### Neutral
- Telemetry ingestion can operate locally in single-machine workstation environments using local video files and mock streams without requiring enterprise Kafka clusters.

## 12. Alternatives Considered

1. **Autonomous Self-Retraining Loop:**
   - *Rejected:* Allowing the agent to automatically retrain and redeploy when drift is detected violates `[P§24]`, risks catastrophic model collapse, and can incur unlimited cloud compute expense.
2. **Unfiltered Frame Archival:**
   - *Rejected:* Streaming all CCTV camera frames to cloud storage is economically infeasible and floods datasets with redundant, uninformative background video.
3. **Purely Model-Centric Monitoring (Confidence Only):**
   - *Rejected:* High-confidence false positives on out-of-distribution data cannot be detected by confidence tracking alone; optical and environmental data tracking is mandatory per `[P§28]`.

## 13. Implementation Sequencing & Phasing

- **Phase 1 (Current):** ADR-0013 architectural specification and immutable Pydantic contracts.
- **Phase 2 (Substrate):** Integration of telemetry schemas into `cv_agent.monitoring.contracts` with zero external dependencies.
- **Phase 3 (Capabilities & Tools):** Development of statistical drift calculation tools (KS-test, PSI) and active learning sampling heuristics under ADR-0005/ADR-0007.
- **Phase 4 (Reasoning Integration):** Connecting `RetrainingTriggerProposal` handoffs into ADR-0008 reasoning nodes for cognitive diagnosis.
- **Phase 8 (Full Production Loop):** Complete end-to-end telemetry streaming against active TensorRT / DeepStream deployments per `ROADMAP.md` Phase 8.

## 14. Implementation Acceptance Criteria

When Phase 2 runtime implementation begins, the implementation of this subsystem MUST satisfy the following empirical acceptance tests before being considered complete:

1. **Tri-Dimensional Telemetry Immutability & Validation:** `ModelTelemetryRecord`, `SystemTelemetryRecord`, `DataTelemetryRecord`, and `UnifiedTelemetrySnapshot` instances are strictly validated and immutable. Telemetry windows with invalid ranges, out-of-order timestamps, or probabilities not summing to valid bounds are rejected at instantiation time.
2. **Mandatory Baseline Comparison Gate:** Any attempt by `StatisticalDriftDetector` to compute drift metrics without an authoritative `BaselineReferenceBinding` linked to a verified ADR-0011 `EvaluationReport` and ADR-0009 dataset manifest hash raises an explicit precondition error.
3. **Statistical Drift Algorithmic Verification:** Drift calculations against controlled synthetic distributions correctly detect known shifts: KS-test on confidence distributions ($p < 0.05$), PSI on binned distributions ($PSI \ge 0.25$ for critical drift), Wasserstein distance on latency, and Chi-Square on class balance.
4. **Zero Autonomous Retraining Invariant:** Every `RetrainingTriggerProposal` enforces `requires_human_approval = True`. Invoking training pipelines directly from monitoring without explicit human approval generated per `docs/APPROVALS.md` and ADR-0003 is mechanically prohibited.
5. **Cryptographic Frame Content Hashing:** Every `CandidateSample` extracted by active learning computes and records a SHA-256 payload hash (`ContentHash`) matching the raw frame bytes, bound to source stream ID and frame index.
6. **Active Learning Buffer Bounds & Deterministic Eviction:** Active learning candidate memory/storage buffers enforce configured maximum capacity limits. When capacity is exceeded, lower-priority samples are evicted deterministically according to strategy scores.
7. **Candidate Batch Handoff to Dataset Subsystem:** `ActiveLearningCandidateBatch` instances serialize cleanly and transfer to ADR-0009, generating unannotated draft manifests without mutating existing dataset versions or train/val splits.
8. **Structured Advisory Proposal Formulation:** Drift alerts and retraining proposals format quantitative evidence summaries linking causal telemetry dimensions (model vs. system vs. data) and hand off cleanly to ADR-0008 reasoning nodes.
9. **Persistence Protocol Independence:** All telemetry snapshots, drift analysis records, and trigger proposals serialize to immutable JSON/Pydantic representations for persistence in ADR-0004 without ADR-0013 managing direct database connections.
10. **Transport Agnosticism & Resource Overhead Guardrails:** Telemetry ingestion interfaces operate independently of physical transport protocols (file buffer, MQTT, Kafka, HTTP), consuming less than 5% target compute overhead during edge execution per `[P§13]`, `[P§29.4]`.
