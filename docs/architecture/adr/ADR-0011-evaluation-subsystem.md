# ADR-0011: Evaluation Subsystem Architecture

- **Status:** Proposed
- **Date:** 2026-09-09
- **Layer:** evaluation / empirical measurement
- **Canon:** `[P§7]`, `[P§12]`, `[P§13]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§27]`, `[P§29.2]`, `[P§29.3]`, `[P§29.4]`, `[P§29.10]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #6
- **Preceded by Decisions:** D-030 (ADR-0010 Training Subsystem), D-029 (ADR-0009 Dataset Subsystem), D-026 (ADR-0008 Reasoning Subsystem), D-025 (ADR-0007 Skills), D-023 (ADR-0005 Tools & MCP Boundary), D-021 (ADR-0004 Experiment Ledger), D-020 (ADR-0003 Orchestration & Approval), D-014 (Out-of-process GPU compute), D-022 (NVIDIA hardware inventory)

## 1. Context

In empirical computer vision engineering, "the agent must be measurement-driven" `[P§12]`. Modern computer vision systems succeed or fail on the rigor of their evaluation. Flawed, shallow, or ad-hoc evaluation produces false confidence, deploys brittle models, and wastes engineering cycles:
1. **The Single-Metric Fallacy (`[P§12]`, `[P§29.4]`):** Evaluating only model accuracy (e.g. mAP or top-1 accuracy) without measuring systems performance creates deployability blindness. As established in `[P§12]`, "a model with 99% mAP is useless if the target Jetson can only process 2 FPS when the application requires 20 FPS." Model quality and runtime efficiency are co-equal dimensions of CV engineering.
2. **Subjective Judgment vs. Deterministic Measurement (`[P§29.3]`):** An LLM asserting that a model "looks good" or predicting performance through conversational narrative is strictly prohibited. Claims of model performance must be backed by quantitative, reproducible measurement against explicit datasets.
3. **The Baseline-First Invariant (`[P§29.2]`):** An evaluation result is scientifically meaningless in isolation. The agent must never claim that a model, architecture, or hyperparameter change improved performance without comparing against a quantitatively established baseline on an identical, immutable test split.
4. **Diagnostic Depth Beyond Aggregate Metrics (`[P§27]`):** Reporting that a model achieved $72\%$ mAP is incomplete without understanding why $28\%$ failed. The system requires structured error analysis decomposing failures into physical phenomena: small objects, severe occlusion, optical blur, low illumination, class confusion, localization error, false positives, and dataset slice degradation `[P§27]`.
5. **Execution and Lineage Integrity (`[P§25]`, `[P§26]`, ADR-0009, ADR-0010):** Evaluating unversioned checkpoints against volatile dataset directories voids experiment reproducibility. Evaluation must bind deterministically to exact `DatasetManifest` versions and model checkpoint content hashes.
6. **Hardware Separation (`D-014`, `D-022`, ADR-0005):** Accurate latency, throughput, VRAM, and power benchmarking require running on representative physical hardware targets (e.g. NVIDIA Jetson, remote Linux GPU worker), not the local developer workstation.

This ADR establishes the architectural specification, interface contracts, lifecycle state machine, and operational invariants for the **Evaluation Subsystem**.

## 2. Responsibility (`[P§34]`)

- **This owns:**
  - The structured, immutable evaluation specification (`EvaluationPlan`).
  - Strict binding to immutable dataset versions (`DatasetId`, `DatasetVersion`, `split_membership` partition name) governed by ADR-0009.
  - Strict binding to evaluated model artifacts (`CheckpointId`, `weights_hash`, model architecture) governed by ADR-0010.
  - The formal decoupling and co-equal measurement of **Model Quality Metrics** (accuracy, precision, recall, F1, mAP, IoU, MOTA) and **Systems Performance Metrics** (latency p50/p90/p95/p99, throughput FPS, VRAM allocation, CPU/GPU utilization, thermal and power behavior) `[P§12]`.
  - Extensible, versioned metric definitions (`MetricDefinition`) across CV tasks without hard-coding task-specific logic into the core agent loop.
  - Benchmark configuration contracts (`BenchmarkConfiguration`) defining warmup cycles, measurement iterations, batch sizes, input dimensions, and precision modes.
  - The evaluation execution dispatch abstraction (`EvaluationRunner`), translating an `EvaluationPlan` into tool invocations and delegating transport to ADR-0005.
  - The mandatory baseline comparison engine (`BaselineComparisonResult`), calculating metric deltas, detecting regressions, and validating statistical significance `[P§29.2]`.
  - Structured diagnostic error analysis (`ErrorAnalysisRecord`), cataloguing false positives, false negatives, localization failures, class confusion matrices, and dataset slice performance `[P§27]`.
  - Comprehensive evaluation report generation (`EvaluationReport`).
  - Immutable evaluation reproducibility environment packaging (`EvaluationEnvironmentRecord`) per `[P§25]`.
  - Delegation of compute-intensive benchmarking approval checks to `docs/APPROVALS.md`.
- **This does NOT own:**
  - Cognitive hypothesis formulation, causal diagnosis of failures, or proposing next experiment strategies → Reasoning Subsystem (ADR-0008) `[P§4]`, `[P§27]`;
  - Workflow graph orchestration, human approval interrupt handling, or checkpoint persistence → Orchestration Subsystem (ADR-0003) `[P§21]`, `[P§24]`;
  - Authoritative persistence of historical experiment ledger rows in SQLite (`experiments.sqlite`) → Project Memory & Experiment Ledger (ADR-0004) `[P§25]`;
  - Low-level subprocess spawning, SSH transport, remote environment execution, or MCP socket handling → Tools & MCP Execution Boundary (ADR-0005) `[P§22]`;
  - Dataset payload fetching, manifest schema authority, or split leakage verification → Dataset Subsystem (ADR-0009) `[P§26]`;
  - Model training, loss optimization, learning rate schedules, or checkpoint creation → Training Subsystem (ADR-0010) `[P§10]`;
  - Model optimization (TensorRT compilation, pruning, quantization, calibration) → future Optimization / Deployment Subsystem (ADR-0012) `[P§14]`;
  - Post-deployment live telemetry, data drift detection, or production monitoring → future Monitoring Subsystem (ADR-0013) `[P§28]`;
  - Capability taxonomy authority or tool catalog registration → Capability Model (ADR-0001) `[P§23]`.
- **Why this responsibility does not belong elsewhere:**
  Conflating evaluation with training (ADR-0010) creates conflict of interest, encourages optimistic evaluation on validation splits, and obscures independent test benchmarking. Conflating evaluation with Reasoning (ADR-0008) tempts the agent to substitute qualitative LLM opinion for deterministic empirical measurement. Conflating evaluation with generic tools (ADR-0005) loses structured CV semantics (confusion matrices, mAP thresholds, slice breakdowns, baseline deltas). The Evaluation Subsystem isolates objective empirical measurement into an authoritative, reproducible layer.

## 3. Decision

We establish an explicit **Evaluation Subsystem Architecture** centered on typed evaluation plans, strict dataset and model bindings, co-equal model quality and systems benchmarking, mandatory baseline comparison, structured error analysis, and clean delegation to ADR-0005 and ADR-0004.

```text
               ADR-0008 Reasoning Subsystem
               (Evaluation Intent & Strategy)
                            │
                            │ specifies target hypothesis
                            ▼
             ┌─────────────────────────────┐
             │       EvaluationPlan        │
             │ (Model Artifact, Dataset,   │
             │  Metrics, Benchmarks, Base) │
             └──────────────┬──────────────┘
                            │
                            │ 1. validate bindings & cost
                            ▼
             ┌─────────────────────────────┐
             │      EvaluationRunner       │
             │  (Dispatch & Coordination)  │
             └──────────────┬──────────────┘
                            │
                            │ 2. delegates physical execution
                            ▼
             ┌─────────────────────────────┐
             │    ADR-0005 Tool / MCP      │
             │     Execution Boundary      │
             └──────────────┬──────────────┘
                            │
       ┌────────────────────┼────────────────────┐
       ▼                    ▼                    ▼
   LOCAL Target       REMOTE Target        MCP Target
  (Inspection/Fast)  (NVIDIA GPU Worker)  (Edge / Jetson)
       │                    │                    │
       └────────────────────┼────────────────────┘
                            │
                            │ raw metrics, traces & error cases
                            ▼
             ┌─────────────────────────────┐
             │   Analysis & Calculation    │
             │ ┌─────────────────────────┐ │
             │ │ MetricCalculator        │ │
             │ │ BenchmarkRunner         │ │
             │ │ ErrorAnalyzer           │ │
             │ └─────────────────────────┘ │
             └──────────────┬──────────────┘
                            │
                            │ 3. produces unified result
                            ▼
             ┌─────────────────────────────┐
             │      EvaluationResult       │
             │  ├── MetricResults          │
             │  ├── BenchmarkResults       │
             │  ├── BaselineComparison     │
             │  └── ErrorAnalysisRecord    │
             └──────────────┬──────────────┘
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
┌───────────────────────────┐ ┌───────────────────────────┐
│ ADR-0004 Project Memory   │ │ ADR-0008 Reasoning        │
│ (Immutable SQLite Ledger) │ │ (ExperimentDiagnosisNode) │
└───────────────────────────┘ └───────────────────────────┘
```

### 3.1. Reasoning vs. Evaluation Boundary (Hypothesis vs. Measurement)

Consistent with ADR-0008, the boundary between cognitive reasoning and the evaluation subsystem is strictly maintained:
- **Reasoning Subsystem (ADR-0008):** Proposes the evaluation intent, selects which baseline to measure against, and hypothesizes potential weaknesses (e.g. "Evaluate whether fine-tuning improved recall on night-time cameras without increasing inference latency beyond the 30ms budget").
- **Evaluation Subsystem (ADR-0011):** Formulates the rigorous `EvaluationPlan`, fetches test split items via ADR-0009, runs deterministic inference via ADR-0005, calculates mathematical metric values, executes hardware benchmarks, performs slice-level error analysis, and compiles an objective `EvaluationResult`.
- **Reasoning analyzes the results:** After evaluation completes, Reasoning ingests the structured `ErrorAnalysisRecord` and `BaselineComparisonResult` to diagnose root causes and formulate the next iteration `[P§27]`. Reasoning never overrides, invents, or subjectively alters empirical metric outputs.

### 3.2. Decoupling Model Quality from Systems Performance (`[P§12]`)

Evaluation explicitly separates and treats as co-equal two distinct measurement dimensions:
1. **Model Quality Metrics (`MetricResult`):** Measures task-specific mathematical fidelity against ground truth annotations on a frozen dataset split:
   - Object Detection: $mAP_{50}$, $mAP_{50:95}$, $AP_{\text{small}}$, $AP_{\text{medium}}$, $AP_{\text{large}}$, Precision, Recall, F1 score.
   - Classification: Top-1 accuracy, Top-5 accuracy, Precision, Recall, F1, AUROC, confusion matrix.
   - Semantic / Instance Segmentation: Mean IoU (mIoU), per-class IoU, Dice coefficient, boundary F1.
   - Multi-Object Tracking: MOTA, MOTP, IDF1, HOTA, ID switches, track fragmentation.
2. **Systems Performance Metrics (`BenchmarkResult`):** Measures runtime efficiency and hardware resource consumption on a target accelerator (`[P§13]`):
   - Latency: End-to-end and pre/infer/post latency percentiles ($p_{50}, p_{90}, p_{95}, p_{99}$, min, max).
   - Throughput: Sustained frames per second (FPS) at specified batch sizes.
   - Memory: Peak VRAM allocated (GB), system RAM usage.
   - Compute: GPU engine utilization %, CPU utilization %.
   - Power & Thermal: Average watts consumed, thermal throttling flags (where supported on Jetson/NVIDIA targets).

A candidate model is only viable if it simultaneously satisfies the project's quality thresholds **and** hardware operational budgets `[P§12]`, `[P§29.4]`.

### 3.3. Deterministic Dataset and Model Artifact Binding

To guarantee total reproducibility and prevent evaluation leakage `[P§25]`, `[P§26]`:
- **Dataset Binding (`EvaluationDatasetBinding`):** Evaluation strictly binds to an authoritative `DatasetId` and `DatasetVersion` governed by ADR-0009. The binding records the `dataset_manifest_hash` and the specific split partition (defaulting to `test`, or a documented `val` partition). Evaluation on unversioned or ad-hoc data directories is rejected.
- **Model Binding (`ModelArtifactReference`):** The evaluated model must be explicitly referenced by its `CheckpointId`, architecture name, weights content hash (`ContentHash` via SHA-256), and precision format. Evaluating arbitrary in-memory models without a persisted checkpoint is disallowed.

### 3.4. Extensible Task-Agnostic Metric System

Rather than hard-coding CV metrics into a rigid conditional tree, the subsystem defines a declarative `MetricDefinition` contract:
- Each metric specifies: `metric_id`, `name`, `task_type` (`detection`, `classification`, `segmentation`, `tracking`), `version`, parameters (e.g. IoU thresholds `0.5:0.95:0.05`), and whether higher values are better.
- Metrics are calculated by specialized, stateless `MetricCalculator` implementations dispatched based on `task_type`.
- Standardized `MetricResult` records store aggregate scalar values, per-class breakdowns, threshold sweeps (PR curves, ROC curves), and metadata.

### 3.5. Systems Benchmarking Contract (`BenchmarkConfiguration`, `BenchmarkResult`)

Systems benchmarking is conducted under controlled, reproducible conditions to eliminate measurement noise:
1. **Warmup Protocol:** Benchmarking mandates a configurable number of warmup iterations (default $\ge 10$ iterations) to initialize CUDA contexts, stabilize dynamic clocks, and eliminate cold-start transients.
2. **Repetition Count:** Timing measurements must be collected over $N \ge 100$ timed runs using hardware-synchronized timers (e.g. `cudaEventRecord` / CUDA event synchronization, not host wall-clock for asynchronous CUDA queues).
3. **Execution Target Binding (`[P§13]`, `D-014`, `D-022`):** Benchmarks must execute on the declared target environment (e.g. remote Linux workstation with RTX 4090, or NVIDIA Jetson AGX Orin). Benchmarking Jetson latency on a macOS workstation is strictly prohibited.
4. **Configuration Sweeps:** Supports benchmarking across varying batch sizes ($1, 2, 4, 8, 16$) and precision modes (`fp32`, `fp16_amp`, `int8`).

### 3.6. Mandatory Baseline Comparison Invariant (`[P§29.2]`, `[P§29.3]`)

Per the core project philosophy:
- **Baseline Requirement:** Every evaluation plan should link to a recognized `baseline_experiment_id`.
- **`BaselineComparisonResult`:** The subsystem automatically computes:
  $$\Delta_{\text{metric}} = \text{Metric}_{\text{candidate}} - \text{Metric}_{\text{baseline}}$$
  $$\%_{\text{change}} = \frac{\Delta_{\text{metric}}}{\text{Metric}_{\text{baseline}}} \times 100$$
- **Regression Detection:** The comparison flags regressions across both quality metrics and systems metrics. If quality increases by $+1.2\%$ mAP but latency degrades by $+45\%$, the comparison records an explicit Pareto tradeoff notice for downstream reasoning.
- **Unsubstantiated Claim Prevention:** The agent core will reject any automated claim of improvement that lacks an empirical `BaselineComparisonResult`.

### 3.7. Diagnostic Error Analysis Architecture (`[P§27]`)

Evaluation produces structured, empirical error diagnostic records (`ErrorAnalysisRecord`) rather than leaving error inspection to manual human effort:
- **False Positive & False Negative Indices:** Content hashes or item indices of false positives and missed ground-truth instances.
- **Localization Error vs. Classification Error:** Distinguishing bounding box / mask misalignment from miscategorization.
- **Class Confusion Matrix:** Normalized class-to-class confusion mapping.
- **Dataset Slice Performance:** Metric breakdowns across predefined dataset slices (e.g. camera location, time-of-day, object scale, occlusion level) identified in the ADR-0009 manifest.
- **Confidence Calibration:** Expected Calibration Error (ECE) and confidence histogram distribution to detect overconfident misclassifications.
- **Handoff to Reasoning:** The `ErrorAnalysisRecord` is directly consumed by ADR-0008 (`ExperimentDiagnosisNode`), enabling evidence-grounded hypotheses for targeted remediation.

### 3.8. Evaluation Execution Delegation via ADR-0005

The evaluation subsystem manages evaluation semantics, not process management or transport protocols:
- `EvaluationRunner` coordinates the preparation of test data slices, model checkpoints, and evaluation configuration.
- It dispatches evaluation jobs through the **ADR-0005 Tool and MCP Execution Boundary** using appropriate `ToolId` tokens and `ExecutionTarget` bindings (`REMOTE` or `MCP_ENDPOINT`).
- For long-running benchmarks or large-scale evaluation sweeps, ADR-0005 returns an `ActiveJobHandoff`, allowing ADR-0003 (Orchestration) to monitor progress asynchronously.

```text
EvaluationPlan
    ↓
EvaluationRunner (evaluation semantics, test slice prep, metric aggregation)
    ↓
ADR-0005 Tool / Execution Boundary (SSH transport, container execution, process security)
    ↓
LOCAL / REMOTE / MCP_ENDPOINT (physical hardware target)
```

### 3.9. Approval and Compute Cost Governance (`docs/APPROVALS.md`, `[P§24]`)

Evaluation operations are governed by `docs/APPROVALS.md`:
- Standard evaluation on frozen validation or test splits is typically an analytical inspection action.
- However, **large-scale profiling sweeps** (e.g. extensive multi-hour latency profiling across multiple hardware targets or shared cluster resources) fall under the `notify + proceed` or explicit approval gate per `docs/APPROVALS.md`.
- ADR-0011 checks whether a benchmark plan exceeds configured duration/cost thresholds and delegates policy enforcement to Orchestration (ADR-0003). It does not create a parallel approval system.

### 3.10. Full Reproducibility Contract (`EvaluationEnvironmentRecord`, `[P§25]`)

Per `[P§25]`, every evaluation outcome compiles an immutable `EvaluationEnvironmentRecord`:
- Git commit SHA and workspace cleanliness status.
- Exact dataset manifest hash (`dataset_manifest_hash`).
- Model checkpoint content hash (`weights_hash`).
- Evaluation configuration parameters and metric definitions.
- Random seeds (if stochastic test-time augmentation or sampling is applied).
- Python, PyTorch, CUDA, and driver versions.
- Target device identity (GPU model, compute capability, driver version, memory capacity).

### 3.11. Boundaries with Future Subsystems

- **Future Optimization / Deployment Subsystem (ADR-0012):** ADR-0012 will own the transformation of models (TensorRT INT8 quantization, pruning, engine export). ADR-0011 will be invoked by ADR-0012 to evaluate the resulting compiled engine against the original unquantized baseline to verify that quantization did not cause unacceptable accuracy degradation.
- **Future Monitoring Subsystem (ADR-0013):** ADR-0013 will monitor streaming production camera data for data drift and operational health. ADR-0011 provides the offline gold-standard benchmark against which production behavior is verified.

### 3.12. Required Architectural Invariants

The Evaluation Subsystem strictly enforces twelve non-negotiable architectural invariants:
1. **Immutable Dataset Versioning (`[P§26]`, ADR-0009):** Evaluation references an authoritative, immutable `DatasetManifest` version and content hash. Evaluating implicit "current" or unversioned data directories is strictly prohibited.
2. **Immutable Model Artifacts (`[P§25]`, ADR-0010):** Evaluation binds to a specific model checkpoint identified by `CheckpointId` and SHA-256 `ContentHash`. Evaluating transient in-memory weights is barred.
3. **Mandatory Baseline Reference (`[P§29.2]`, `[P§29.3]`):** Every evaluation plan declares a baseline reference. Claims such as "model improved" without quantitative empirical comparison are architecturally barred.
4. **Versioned Metric Definitions:** Metric definitions are declarative, versioned, and parameter-explicit (`MetricDefinition`), avoiding monolithic conditional evaluation scripts.
5. **Separation of Quality and Systems Metrics (`[P§12]`):** Model quality metrics (`MetricResult`) and systems performance metrics (`BenchmarkResult`) remain distinct, co-equal measurement dimensions.
6. **Hardware Execution Identification (`[P§13]`, `[P§29.4]`, `D-014`, `D-022`):** All systems benchmarks explicitly record their physical hardware target, device name, and execution environment.
7. **Complete Reproducibility (`[P§25]`):** Every evaluation result bundles an immutable `EvaluationEnvironmentRecord` (git commit SHA, dataset manifest hash, weights hash, metric versions, random seeds, runtime/CUDA environment).
8. **Structured Error Analysis (`[P§27]`):** Error analysis (`ErrorAnalysisRecord`) must be quantitative, structured, and evidence-based (confusion matrices, false positive/negative hashes, slice breakdowns), not subjective narrative.
9. **No Physical Transport Ownership:** Evaluation defines evaluation semantics; physical process transport is delegated entirely to ADR-0005 (`LOCAL`, `REMOTE`, `MCP_ENDPOINT`).
10. **No Experiment Persistence Ownership:** Evaluation produces immutable evaluation records; authoritative database persistence is owned exclusively by ADR-0004 (`experiments.sqlite`).
11. **Anti-LLM Narrative Invariant (`[P§29.3]`):** Subjective LLM judgment or conversational narrative cannot substitute for deterministic quantitative metrics.
12. **Decoupled Reasoning Consumption:** Evaluation results are structured for ingestion by Reasoning (ADR-0008 `ExperimentDiagnosisNode`) without coupling evaluation execution to reasoning prompts or cognitive graphs.

## 4. Proposed Interface Contracts (Phase 2 Implementation Target)

*(Note: These interfaces represent the proposed type contracts for Phase 2 implementation. They do not exist in the repository today).*

```python
# module: cv_agent.evaluation.types (Proposed — Phase 2 implementation target)

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, NewType, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from cv_agent.capabilities import CapabilityId, ToolId
from cv_agent.dataset.types import ContentHash, DatasetId, DatasetVersion
from cv_agent.training.types import CheckpointId, TrainingPrecision

EvaluationId = NewType("EvaluationId", str)
MetricId = NewType("MetricId", str)
BenchmarkId = NewType("BenchmarkId", str)


class TaskType(str, Enum):
    CLASSIFICATION = "classification"
    DETECTION = "detection"
    SEGMENTATION = "segmentation"
    TRACKING = "tracking"
    REPRESENTATION = "representation"


class EvaluationTargetType(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"
    MCP_ENDPOINT = "mcp_endpoint"


class EvaluationTarget(BaseModel):
    """Target compute environment for evaluation execution per ADR-0005."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: str
    target_type: EvaluationTargetType
    device_name: str | None = None
    cuda_compute_capability: str | None = None
    description: str = ""


class EvaluationStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class EvaluationDatasetBinding(BaseModel):
    """Immutable binding to an authoritative dataset split per ADR-0009."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: DatasetId
    dataset_version: DatasetVersion
    dataset_manifest_hash: ContentHash
    split_name: str = "test"
    slice_filters: Mapping[str, Any] = Field(default_factory=dict)


class ModelArtifactReference(BaseModel):
    """Immutable reference to the evaluated model artifact per ADR-0010."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_id: CheckpointId
    architecture_name: str
    weights_hash: ContentHash
    weights_uri: str
    precision: TrainingPrecision = TrainingPrecision.FP32
    input_resolution: tuple[int, int]  # (height, width)


class MetricDefinition(BaseModel):
    """Declarative, versioned specification of an evaluation metric."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_id: MetricId
    name: str  # e.g. "mAP@0.5:0.95", "top1_accuracy", "IDF1"
    task_type: TaskType
    version: str = "v1"
    higher_is_better: bool = True
    parameters: Mapping[str, Any] = Field(default_factory=dict)


class MetricResult(BaseModel):
    """Quantitative outcome of an evaluated model quality metric."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: MetricDefinition
    primary_value: float
    per_class_values: Mapping[str, float] = Field(default_factory=dict)
    confidence_interval: tuple[float, float] | None = None
    curve_data: Mapping[str, Sequence[float]] = Field(default_factory=dict)
    metadata: Mapping[str, Any] = Field(default_factory=dict)


class BenchmarkConfiguration(BaseModel):
    """Configuration for hardware-aware systems performance measurement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark_id: BenchmarkId
    batch_sizes: Sequence[int] = (1,)
    warmup_iterations: int = Field(ge=5, default=10)
    measured_iterations: int = Field(ge=20, default=100)
    precision: TrainingPrecision = TrainingPrecision.FP16_AMP
    measure_power: bool = False
    measure_memory: bool = True


class BenchmarkResult(BaseModel):
    """Quantitative systems performance measurements on target accelerator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark_id: BenchmarkId
    batch_size: int
    precision: TrainingPrecision
    latency_mean_ms: float
    latency_p50_ms: float
    latency_p90_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    throughput_fps: float
    peak_vram_gb: float
    cpu_utilization_pct: float
    gpu_utilization_pct: float
    power_watts_avg: float | None = None
    device_name: str
    target_id: str


class ErrorAnalysisRecord(BaseModel):
    """Structured diagnostic failure breakdown per [P§27]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_samples: int
    failed_samples_count: int
    false_positive_count: int
    false_negative_count: int
    confusion_matrix: Sequence[Sequence[int]] = Field(default_factory=list)
    class_labels: Sequence[str] = Field(default_factory=list)
    localization_errors: int = 0
    classification_errors: int = 0
    slice_performance: Mapping[str, float] = Field(default_factory=dict)
    representative_failure_hashes: Sequence[ContentHash] = Field(default_factory=list)
    diagnostics_summary: str


class BaselineComparisonResult(BaseModel):
    """Explicit comparative analysis against an authoritative baseline [P§29.2]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_experiment_id: str
    candidate_experiment_id: str
    quality_metric_deltas: Mapping[str, float]  # metric_name -> delta
    quality_improved: bool
    latency_delta_ms: float
    throughput_delta_fps: float
    memory_delta_gb: float
    pareto_dominant: bool
    summary: str


class EvaluationEnvironmentRecord(BaseModel):
    """Complete environment and reproducibility metadata per [P§25]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    git_commit_sha: str
    git_is_dirty: bool
    python_version: str
    torch_version: str
    cuda_version: str | None
    driver_version: str | None
    target_hardware: str
    seed: int | None = None


class EvaluationPlan(BaseModel):
    """Authoritative, immutable evaluation experiment plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: EvaluationId
    experiment_id: str  # Foreign key to ADR-0004
    created_at: datetime
    capability_id: CapabilityId

    dataset_binding: EvaluationDatasetBinding
    model_artifact: ModelArtifactReference
    task_type: TaskType
    metrics: Sequence[MetricDefinition]
    benchmark_config: BenchmarkConfiguration | None = None
    baseline_experiment_id: str | None = None
    compute_target_id: str = "default_evaluator"
    evaluation_requirements: Mapping[str, Any] = Field(default_factory=dict)
    reproducibility_requirements: Mapping[str, Any] = Field(default_factory=dict)


class EvaluationExecution(BaseModel):
    """Active execution handle and status record for an in-flight evaluation job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: EvaluationId
    plan: EvaluationPlan
    status: EvaluationStatus
    started_at: datetime
    target: EvaluationTarget
    active_job_ref: Mapping[str, Any] | None = (
        None  # Aligned with ADR-0005 ActiveJobHandoff
    )


class EvaluationResult(BaseModel):
    """Comprehensive, immutable evaluation outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: EvaluationId
    plan: EvaluationPlan
    status: EvaluationStatus
    quality_metrics: Sequence[MetricResult]
    system_benchmarks: Sequence[BenchmarkResult] = Field(default_factory=list)
    baseline_comparison: BaselineComparisonResult | None = None
    error_analysis: ErrorAnalysisRecord | None = None
    environment: EvaluationEnvironmentRecord
    completed_at: datetime


class EvaluationReport(BaseModel):
    """Human-readable and structured summary artifact for project memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: str
    evaluation_id: EvaluationId
    title: str
    markdown_content: str
    primary_quality_summary: str
    primary_systems_summary: str
    baseline_verdict: str
    generated_at: datetime


@runtime_checkable
class MetricCalculator(Protocol):
    """Stateless protocol for computing task-specific quality metrics."""

    def compute(
        self,
        predictions: Any,
        ground_truth: Any,
        metric_definitions: Sequence[MetricDefinition],
    ) -> Sequence[MetricResult]: ...


@runtime_checkable
class BenchmarkRunner(Protocol):
    """Protocol for executing hardware-aware latency and throughput benchmarks."""

    def run_benchmark(
        self,
        model_artifact: ModelArtifactReference,
        config: BenchmarkConfiguration,
        target_id: str,
    ) -> Sequence[BenchmarkResult]: ...


@runtime_checkable
class ErrorAnalyzer(Protocol):
    """Protocol for extracting diagnostic failure structures from predictions."""

    def analyze(
        self,
        predictions: Any,
        ground_truth: Any,
        dataset_binding: EvaluationDatasetBinding,
    ) -> ErrorAnalysisRecord: ...


@runtime_checkable
class EvaluationRunner(Protocol):
    """High-level dispatch protocol coordinating execution via ADR-0005."""

    def validate_plan(self, plan: EvaluationPlan) -> bool: ...

    def execute(
        self,
        plan: EvaluationPlan,
        tool_id: ToolId,
    ) -> EvaluationResult: ...
```

## 5. Consequences

### Positive
- **Guaranteed Deployability (`[P§12]`, `[P§29.4]`):** Co-equal evaluation of model quality and systems performance ensures high-accuracy models are never recommended if they exceed target edge hardware latency or VRAM budgets.
- **Objective Grounding over LLM Hype (`[P§29.3]`):** Pure quantitative evaluation eliminates speculative or narrative claims of model quality.
- **Strict Baseline Enforcement (`[P§29.2]`):** Every candidate run must justify claims against an explicit baseline, eliminating unsubstantiated assertions of improvement.
- **Actionable Diagnostic Depth (`[P§27]`):** Structured `ErrorAnalysisRecord` feeds directly into Reasoning (ADR-0008), providing concrete failure distributions (small objects, blur, occlusion, class confusion) for data-centric or architectural refinement.
- **Total Reproducibility (`[P§25]`):** Explicit binding to ADR-0009 dataset manifest hashes, ADR-0010 checkpoint hashes, metric versions, and hardware environments guarantees reproducible evaluation results.
- **Clean Subsystem Decoupling (`[P§34]`):** Evaluation owns measurement semantics; physical execution is delegated to ADR-0005, historical persistence to ADR-0004, and causal diagnosis to ADR-0008.

### Negative / Tradeoffs
- **Benchmarking Overhead:** Hardware benchmarking with multiple warmup iterations and repetitions adds latency to the evaluation phase.
- **Multi-Hardware Dependency:** Accurate systems benchmarking requires access to configured physical targets (e.g. Jetson, NVIDIA GPU servers); evaluation on unconfigured targets reports "known but unavailable" per `D-022`.

### Neutral
- Output records are formatted for seamless persistence into SQLite (`experiments.sqlite`) via ADR-0004 without ADR-0011 owning the database.
- Integration with future optimization (ADR-0012) and monitoring (ADR-0013) subsystems uses the contracts defined here.

## 6. References
- `docs/PROJECT.md`: §7 (CV knowledge scope), §12 (Evaluation philosophy), §13 (Hardware-aware engineering), §21 (Orchestration), §22 (MCP), §24 (Autonomous training), §25 (Experiment-driven development), §27 (Failure analysis), §29 (Philosophy), §34 (Modular architecture)
- `docs/APPROVALS.md`: Gated actions and profiling rules
- `docs/DATA.md`: Manifest-first content-addressed dataset specification
- ADR-0001: Capability Registry (`D-011`)
- ADR-0003: Orchestration State & Checkpointing (`D-020`)
- ADR-0004: Project Memory & Experiment Ledger (`D-021`)
- ADR-0005: Tools and MCP Execution Boundary (`D-023`)
- ADR-0007: Skills Architecture & NVIDIA Discovery (`D-025`)
- ADR-0008: Reasoning Subsystem (`D-026`)
- ADR-0009: Dataset Subsystem (`D-029`)
- ADR-0010: Training Subsystem (`D-030`)
