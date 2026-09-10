# ADR-0012: Optimization and Deployment Subsystem Architecture

- **Status:** Accepted — Frozen
- **Date:** 2026-09-09 (Frozen: 2026-09-10)
- **Last Reviewed:** 2026-09-10
- **Layer:** optimization / deployment
- **Canon:** `[P§8]`, `[P§9]`, `[P§13]`, `[P§14]`, `[P§15]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§28]`, `[P§29.4]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #8
- **Preceded by Decisions:** D-031 (ADR-0011 Evaluation Subsystem), D-030 (ADR-0010 Training Subsystem), D-029 (ADR-0009 Dataset Subsystem), D-026 (ADR-0008 Reasoning Subsystem), D-025 (ADR-0007 Skills), D-023 (ADR-0005 Tools & MCP Boundary), D-021 (ADR-0004 Experiment Ledger), D-020 (ADR-0003 Orchestration & Approval), D-014 (Out-of-process GPU compute), D-022 (NVIDIA hardware inventory)

## 1. Context

In production computer vision engineering, training a high-performing neural network in PyTorch is only half the battle `[P§14]`. Raw research models are computationally heavy, memory-inefficient, and unsuitable for latency-critical edge or real-time server deployment `[P§9]`, `[P§13]`. To achieve deployability, models must undergo structured compilation, precision reduction, and runtime integration:
$$\text{PyTorch Checkpoint} \longrightarrow \text{ONNX Exchange} \longrightarrow \text{Compiled Engine (e.g. TensorRT)} \longrightarrow \text{Precision (FP32} \rightarrow \text{FP16} \rightarrow \text{INT8/FP8)}$$

However, optimization and deployment present substantial architectural hazards:
1. **The Optimization Accuracy Cliff (`[P§14]`, ADR-0011):** Aggressive quantization (such as INT8 Post-Training Quantization) or unstructured pruning can degrade detection recall or boundary precision. Optimization cannot operate in a vacuum; it must be quantitatively verified against the unquantized baseline on an identical, frozen dataset split `[P§29.2]`, `[P§29.3]`.
2. **Hardware-Specific Compilation (`[P§13]`, `[P§29.4]`, `D-014`, `D-022`):** Modern inference engines (such as NVIDIA TensorRT) build hardware-locked serialized binaries tailored to the specific GPU microarchitecture (e.g. Jetson Orin SM 8.7 vs RTX 4090 SM 8.9 vs Datacenter H100 SM 9.0). A TensorRT engine built on an x86 host will fail on an ARM64 Jetson target. Optimization must be hardware-aware and compiled on or targeted specifically for the verified target profile.
3. **Multi-Runtime Extensibility (`[P§8]`, `[P§9]`, `[P§15]`):** While NVIDIA TensorRT and DeepStream represent premier acceleration pipelines for real-time CCTV `[P§9]`, `[P§15]`, the architecture must avoid proprietary lock-in. It must flexibly accommodate alternative runtimes (ONNX Runtime, Triton Inference Server, OpenVINO, embedded C++ daemons) behind a uniform deployment contract.
4. **Production Consequence & Blast Radius (`docs/APPROVALS.md`, `[P§24]`):** Production deployment alters live systems, routes camera streams, and consumes physical hardware resources. Per `docs/APPROVALS.md`, deployment is an **always-gated action** requiring explicit human approval with defined rollback paths and blast-radius analysis.
5. **Runtime Readiness & Failure Recovery (`[P§28]`):** Deploying a model without automated readiness probing risks black-screen camera ingestion, out-of-memory crashes, or pipeline stalling. Automated health checks and instantaneous rollback to previously active deployment manifests are mandatory.

This ADR establishes the architectural specification, interface contracts, lifecycle state machine, and operational invariants for the **Optimization & Deployment Subsystem**.

## 2. Responsibility (`[P§34]`)

- **This owns:**
  - The structured, immutable optimization plan specification (`OptimizationPlan`).
  - Model transformation strategies: graph extraction, ONNX conversion, layer fusion, kernel auto-tuning, structured/unstructured pruning, and knowledge distillation coordination.
  - Precision calibration and quantization configurations: FP32 $\rightarrow$ FP16, BF16, FP8, INT8 Post-Training Quantization (PTQ) with explicit calibration datasets, and Quantization-Aware Training (QAT) specifications `[P§14]`.
  - Calibration dataset binding (`CalibrationDatasetBinding`), linking calibration batches to immutable ADR-0009 dataset versions.
  - Dynamic tensor shape profile declarations (minimum, optimal, and maximum batch/resolution dimensions) preventing runtime allocation failures.
  - The immutable optimized model artifact specification (`OptimizedModelArtifact`) and transformation lineage DAG (parent checkpoint $\rightarrow$ optimization recipe $\rightarrow$ compiled engine).
  - Mandatory pre-deployment optimization verification: invoking the Evaluation Subsystem (ADR-0011) to measure accuracy retention and systems speedup against the baseline before permitting deployment.
  - Target deployment environment declarations (`DeploymentTarget`, `TargetProfile`) capturing hardware architecture, GPU SM capability, OS, driver, runtime version, and memory budgets.
  - Deployment runtime packaging specifications (`DeploymentPlan`, `DeploymentProfile`) supporting TensorRT standalone engines, DeepStream GStreamer pipelines, Triton Inference Server configurations, and ONNX Runtime endpoints.
  - The deployment lifecycle state machine (`DeploymentStatus`, `DeploymentRecord`) from validation through staging, readiness verification, promotion to active, and rollback.
  - Automated health check specifications (`HealthCheckSpec`) and readiness probe contracts (`ReadinessProbeResult`).
  - Rollback orchestration and deterministic fallback version selection upon health check failure or operator intervention.
  - Deployment approval precondition enforcement per `docs/APPROVALS.md` and `[P§24]`.
- **This does NOT own:**
  - Cognitive optimization strategy selection, model architecture recommendations, or hypothesis generation → Reasoning Subsystem (ADR-0008) `[P§4]`, `[P§14]`;
  - Neural network weights training, loss minimization, or backpropagation → Training Subsystem (ADR-0010) `[P§10]`;
  - Objective accuracy metric calculation, benchmark timing, or error analysis → Evaluation Subsystem (ADR-0011) `[P§12]`;
  - Procedural vendor scripts, compilation CLI command syntax (e.g. `trtexec`, `tao-converter`), or DeepStream pipeline generation recipes → Skills Subsystem (ADR-0007) `[P§15]`;
  - Physical process invocation, SSH execution, remote daemon communication, or container transport → Tools & MCP Execution Boundary (ADR-0005) `[P§22]`;
  - Authoritative dataset storage, payload hashing, or split verification → Dataset Subsystem (ADR-0009) `[P§26]`;
  - Permanent experiment or deployment history database persistence in SQLite → Project Memory & Experiment Ledger (ADR-0004) `[P§25]`;
  - Live production camera stream ingestion, data drift detection, or ongoing monitoring → future Monitoring Subsystem (ADR-0013) `[P§28]`;
  - Capability taxonomy definition or registration → Capability Model (ADR-0001) `[P§23]`.
- **Why this responsibility does not belong elsewhere:**
  Treating model compilation and deployment as an ad-hoc step in Tools (ADR-0005) loses transformation lineage, shape profiles, and calibration provenance. Placing deployment directly in Orchestration (ADR-0003) couples workflow state with edge hardware profiles and runtime health probing. Merging optimization into Training (ADR-0010) conflates model parameter convergence with target hardware compilation. The Optimization & Deployment Subsystem isolates target hardware acceleration, artifact transformation, and production release management into a coherent, verifiable layer.

## 3. Decision

We establish an explicit **Optimization & Deployment Subsystem Architecture** centered on typed optimization plans, calibration bindings, transformation lineage, mandatory evaluation verification, target deployment profiles, approval-gated lifecycle transitions, and automated rollback semantics.

```text
               ADR-0008 Reasoning Subsystem
             (Optimization & Deployment Strategy)
                            │
                            │ specifies target goals & constraints
                            ▼
             ┌─────────────────────────────┐
             │      OptimizationPlan       │
             │ (Source Checkpoint, Precision│
             │  Calibration, Target Device)│
             └──────────────┬──────────────┘
                            │
                            │ 1. dispatch compilation via Skills & Tools
                            ▼
             ┌─────────────────────────────┐
             │       ModelOptimizer        │
             │  ├── ONNX Export / Graph    │
             │  ├── TensorRT / OpenVINO    │
             │  └── PTQ Calibration        │
             └──────────────┬──────────────┘
                            │
                            │ delegates execution transport
                            ▼
             ┌─────────────────────────────┐
             │    ADR-0005 Tool / MCP      │
             │     Execution Boundary      │
             └──────────────┬──────────────┘
                            │
                            ▼
             ┌─────────────────────────────┐
             │   OptimizedModelArtifact    │
             │ (Engine Bytes, Lineage DAG, │
             │  Dynamic Shapes, Metadata)  │
             └──────────────┬──────────────┘
                            │
                            │ 2. mandatory verification invocation
                            ▼
             ┌─────────────────────────────┐
             │  ADR-0011 Evaluation Engine │
             │   (Accuracy Retention vs    │
             │    Baseline Speedup Check)  │
             └──────────────┬──────────────┘
                            │
                            │ verified retention >= threshold
                            ▼
             ┌─────────────────────────────┐
             │       DeploymentPlan        │
             │ (Target Profile, Runtime,   │
             │  Rollback Plan, HealthSpec) │
             └──────────────┬──────────────┘
                            │
                            │ 3. request human approval (docs/APPROVALS.md)
                            ▼
             ┌─────────────────────────────┐
             │    ADR-0003 Orchestration   │
             │    Approval Gate Interrupt  │
             └──────────────┬──────────────┘
                            │
                            │ authorized
                            ▼
             ┌─────────────────────────────┐
             │      DeploymentManager      │
             │  ├── Staging & Activation   │
             │  ├── Readiness Probing      │
             │  └── Automated Rollback     │
             └──────────────┬──────────────┘
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
┌───────────────────────────┐ ┌───────────────────────────┐
│ ADR-0004 Project Memory   │ │ future Monitoring (ADR-13)│
│ (Active Deployment Record)│ │ (Live Stream Telemetry)   │
└───────────────────────────┘ └───────────────────────────┘
```

### 3.1. The Optimization Pipeline & Lineage DAG

Every optimization begins with an authoritative `OptimizationPlan` and produces an immutable `OptimizedModelArtifact`:
1. **Source Model Binding (`[P§25]`, ADR-0010):** The plan binds to a specific, verified PyTorch model checkpoint (`CheckpointId`, `weights_hash`).
2. **Intermediate Graph Representation:** When targeting specialized inference runtimes, the subsystem coordinates export to standardized intermediate representations (e.g. ONNX) before compilation, capturing operator set version and symbolic graph transforms.
3. **Compilation & Precision Reduction (`[P§14]`):**
   - Precision: Supports `FP32`, `FP16` (half-precision), `BF16`, `FP8` (for Ada/Hopper microarchitectures), and `INT8` (PTQ or QAT).
   - PTQ Calibration: For INT8 PTQ, the plan binds to an authoritative calibration dataset split (`CalibrationDatasetBinding`) via ADR-0009. The calibration algorithm (e.g. Entropy, MinMax, Percentile) and sample count are recorded in the manifest.
4. **Dynamic Shape Profiles:** For variable-input models (e.g. dynamic batch size or multi-scale video frames), the plan defines strict dimension profiles:
   $$\text{min\_shape} \le \text{opt\_shape} \le \text{max\_shape}$$
   Engines compiled with dynamic shapes reject inputs outside these validated boundaries, preventing out-of-memory runtime crashes.
5. **Transformation Lineage DAG:** The resulting `OptimizedModelArtifact` records complete provenance:
   $$\text{Parent Checkpoint Hash} + \text{Optimization Config Hash} + \text{Calibration Manifest Hash} \longrightarrow \text{Artifact Hash}$$
   Compiled engine binaries are registered in accordance with content-addressed storage principles.

### 3.2. Mandatory Evaluation Verification (`[P§14]`, `[P§29.2]`, ADR-0011)

An optimized artifact is **provisional** until verified. The subsystem enforces a mandatory post-optimization verification gate:
1. **Automated Verification Run:** The optimization pipeline automatically invokes the Evaluation Subsystem (ADR-0011) to evaluate the candidate optimized artifact against the identical test split used for the unoptimized baseline model.
2. **Accuracy Retention Ratio:** The subsystem computes the accuracy retention score:
   $$\text{Retention Ratio} = \frac{\text{Metric}_{\text{optimized}}}{\text{Metric}_{\text{baseline}}}$$
   For example, in an object detection task:
   $$\Delta mAP = mAP_{\text{baseline}} - mAP_{\text{optimized}}$$
3. **Tolerance Guardrails:** The `OptimizationPlan` declares a maximum tolerable accuracy drop (e.g. $\Delta mAP \le 0.015$). If accuracy degradation exceeds this threshold, the optimization artifact is flagged as degraded and barred from automated production promotion.
4. **Systems Efficiency Validation:** Simultaneously, the evaluation verifies that target throughput (FPS) and latency budgets on the target accelerator are satisfied per `[P§12]`, `[P§13]`.

### 3.3. Runtime Agnostic Deployment Architecture (`[P§8]`, `[P§9]`, `[P§15]`)

The subsystem decouples deployment specifications from underlying runtime engines. It supports multiple deployment runtimes through a common `DeploymentProfile` abstraction:
- **Standalone TensorRT (`DeploymentRuntime.STANDALONE_TENSORRT`):** C++/Python runtime loading serialized `.engine` files directly into CUDA streams for minimal-overhead inference.
- **NVIDIA DeepStream (`DeploymentRuntime.DEEPSTREAM_PIPELINE`):** Accelerated multi-camera GStreamer pipeline integrating decoding (NVDEC), pre-processing (nvstreammux), TensorRT inference (nvinfer/nvinferserver), tracking (nvtracker), and message brokering `[P§9]`, `[P§15]`.
- **Triton Inference Server (`DeploymentRuntime.TRITON_SERVER`):** Dynamic batching, concurrent model execution, and gRPC/REST endpoint serving for datacenter clusters.
- **ONNX Runtime (`DeploymentRuntime.ONNX_RUNTIME`):** Cross-platform execution fallback for edge x86/ARM devices lacking NVIDIA GPUs.
- **Embedded C++ Daemon (`DeploymentRuntime.EMBEDDED_DAEMON`):** Lightweight custom binaries for resource-constrained edge endpoints.

The architecture evaluates and supports these runtimes as deployment adapters, preventing the agent core from being rigidly hardcoded to a single framework.

### 3.4. Target Environment Specification & Hardware Awareness (`[P§13]`, `[P§29.4]`, `D-014`, `D-022`)

Deployment configurations explicitly declare their target execution environment (`DeploymentTarget`):
- `platform_type`: e.g. `nvidia_jetson_orin`, `nvidia_discrete_gpu`, `generic_x86_cpu`, `generic_arm_cpu`.
- `compute_capability`: GPU SM version (e.g. `8.7` for Orin, `8.9` for Ada Lovelace, `9.0` for Hopper).
- `driver_version` & `cuda_version`: Required runtime environment.
- `memory_limit_gb`: Maximum allowable host and device memory consumption.
- `power_mode`: Target power profile (e.g. `MAXN`, `15W`, `25W` on Jetson platforms).

Compiling or deploying an engine on an unconfigured or mismatched hardware target fails fast with an explicit error, adhering to `D-022`.

### 3.5. Human Approval & Safety Gates (`docs/APPROVALS.md`, `[P§24]`)

Production deployment and model promotion are **strictly gated actions**:
- **Approval Precondition:** The `DeploymentManager` will refuse activation unless an authorized `ApprovalId` is attached to the deployment context, verified through the orchestration approval interrupt (ADR-0003).
- **Required Approval Metadata:** Before requesting approval, the agent must present:
  - Target environment and endpoints affected.
  - Verification summary: accuracy retention table vs unoptimized baseline and previous production model.
  - Systems metrics: FPS, latency percentiles, VRAM usage.
  - Blast radius and impacted video streams / cameras.
  - Explicit rollback plan identifying the previous active deployment manifest.

### 3.6. Deployment Lifecycle State Machine

A deployment progresses through an auditable, recoverable state machine:

```text
  ┌───────────┐
  │   DRAFT   │
  └─────┬─────┘
        │ validate target compatibility & artifact hashes
        ▼
  ┌───────────┐
  │ VALIDATED │
  └─────┬─────┘
        │ submit approval request with blast radius & rollback plan
        ▼
  ┌───────────────────┐
  │ AWAITING_APPROVAL │
  └─────┬───────┬─────┘
        │       │ approval denied
        │       ▼
        │  ┌──────────┐
        │  │ REJECTED │ (terminal)
        │  └──────────┘
        │ approval granted
        ▼
  ┌───────────┐
  │ APPROVED  │
  └─────┬─────┘
        │ stage engine and runtime config on target
        ▼
  ┌───────────┐
  │  STAGING  │
  └─────┬─────┘
        │ execute startup & readiness probes
        ▼
  ┌───────────┐
  │ VERIFYING │
  └─────┬─────┴─────────────────────────┐
        │                               │ probe failed / timeout
        │ probe passed                  ▼
        │                         ┌───────────┐
        │                         │  FAILED   │ (terminal)
        │                         └─────┬─────┘
        ▼                               │ trigger rollback
  ┌───────────┐                         ▼
  │  ACTIVE   │                  ┌────────────────────┐
  └─────┬─────┴────────────┐     │ ROLLBACK_INITIATED │
        │                  │     └──────────┬─────────┘
        │ new version      │ manual         │
        │ promoted         │ rollback       ▼
        ▼                  │           ┌─────────────┐
  ┌────────────┐           └──────────►│ ROLLED_BACK │ (terminal)
  │ SUPERSEDED │ (terminal)            └─────────────┘
  └────────────┘
```

### 3.7. Runtime Health Probes & Automated Rollback Semantics

To protect production camera feeds from bad deployments:
1. **`HealthCheckSpec`:** Specifies:
   - `startup_timeout_sec`: Time allowed for CUDA engine initialization and weight loading.
   - `readiness_probe_samples`: Synthetic warmup batch passed through the pipeline before live stream ingestion.
   - `max_latency_threshold_ms`: Latency upper bound; if warmup latency exceeds budget, readiness fails.
   - `memory_headroom_mb`: Verification that device memory usage does not exceed system limit.
2. **Zero-Downtime Routing:** Live camera streams or inference queues continue routing to the currently active deployment while the new candidate deployment is in `STAGING` and `VERIFYING`.
3. **Automated Rollback:** If the candidate fails readiness probing or crashes within the initial verification window, the subsystem automatically:
   - Aborts activation and flags status as `FAILED`.
   - Initiates rollback to the designated `rollback_target_id`. If `rollback_target_id is None` (first deployment with no predecessor), rollback cannot be executed automatically; the subsystem MUST escalate to `OPERATOR_REQUIRED` state, preserve all failure diagnostics, and block any further deployment attempts until an operator resolves the condition. Silent failure or ignoring the null target is prohibited.
   - Records failure diagnostics for Reasoning (ADR-0008).

### 3.8. Required Architectural Invariants

The Optimization & Deployment Subsystem strictly enforces twelve non-negotiable architectural invariants:
1. **Source Artifact Immutability:** Optimization strictly builds upon immutable, content-addressed model checkpoints governed by ADR-0010.
2. **Calibration Dataset Provenance:** Precision quantization calibration binds strictly to an immutable ADR-0009 `DatasetManifest` version; ad-hoc calibration directories are prohibited.
3. **Mandatory Post-Optimization Verification:** No optimized model artifact can be deployed without empirical evaluation (ADR-0011) verifying accuracy retention against the baseline model.
4. **Hardware-Locked Engine Integrity (`[P§13]`, `[P§29.4]`):** Compiled binary engines (e.g. TensorRT) are immutably tied to the target hardware architecture, CUDA version, and driver profile; cross-architecture execution without re-compilation is barred.
5. **Explicit Dynamic Shape Bounds:** Variable-input optimization requires explicit minimum, optimal, and maximum shape profiles preventing runtime memory faults.
6. **Complete Transformation Lineage:** Every optimized artifact maintains a cryptographic lineage DAG linking parent checkpoint, configuration, calibration dataset, and output hashes.
7. **Production Deployment Approval Gate (`docs/APPROVALS.md`, `[P§24]`):** Activating a deployment or promoting a model requires explicit human authorization with presented blast radius and rollback plan.
8. **Automated Pre-Activation Readiness Probes:** Live traffic is never routed to a newly staged model until startup and readiness probes pass successfully.
9. **Deterministic Rollback Guarantee:** Every deployment plan specifies an active predecessor deployment manifest for immediate zero-downtime fallback.
10. **Zero Physical Transport Ownership:** Optimization compilation and deployment transport are delegated entirely to the ADR-0005 execution boundary.
11. **Zero Database Ownership:** Deployment states and optimization records are formatted for immutable persistence in ADR-0004 (`experiments.sqlite`).
12. **Runtime Engine Plurality (`[P§15]`):** The subsystem supports diverse runtime adapters (TensorRT, DeepStream, Triton, ONNX Runtime) without hardcoding the agent core to a single proprietary runtime.

## 4. Proposed Interface Contracts (Phase 2 Implementation Target)

*(Note: These interfaces represent the proposed type contracts for Phase 2 implementation. They do not exist in the repository today).*

```python
# module: cv_agent.optimization.types (Proposed — Phase 2 implementation target)

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, NewType, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from cv_agent.capabilities import CapabilityId, ToolId
from cv_agent.dataset.types import ContentHash, DatasetId, DatasetVersion
from cv_agent.evaluation.types import BaselineComparisonResult, EvaluationResult
from cv_agent.tools.types import ExecutionId, TargetId
from cv_agent.training.types import ApprovalId, CheckpointId

# Canonical identifier format conforming to [a-z0-9_.-]+(/[a-z0-9_.-]+)* per ADR-0001 §3.1.
OptimizationPlanId = NewType("OptimizationPlanId", str)
OptimizedArtifactId = NewType("OptimizedArtifactId", str)
DeploymentId = NewType("DeploymentId", str)


class OptimizationTechnique(str, Enum):
    ONNX_EXPORT = "onnx_export"
    TENSORRT_COMPILATION = "tensorrt_compilation"
    PTQ_QUANTIZATION = "ptq_quantization"
    QAT_PREPARATION = "qat_preparation"
    STRUCTURED_PRUNING = "structured_pruning"
    KNOWLEDGE_DISTILLATION = "knowledge_distillation"
    LAYER_FUSION = "layer_fusion"


class OptimizationPrecision(str, Enum):
    FP32 = "fp32"
    FP16 = "fp16"
    BF16 = "bf16"
    FP8 = "fp8"
    INT8 = "int8"


class DynamicShapeProfile(BaseModel):
    """Explicit minimum, optimal, and maximum dynamic dimension profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tensor_name: str
    min_shape: tuple[int, ...]
    opt_shape: tuple[int, ...]
    max_shape: tuple[int, ...]


class CalibrationDatasetBinding(BaseModel):
    """Binding to an immutable dataset split for quantization calibration.

    Frames must be sampled strictly from the dedicated 'calibration' split
    (or verified validation split) per ADR-0009 Invariant 13 to prevent test-split leakage.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: DatasetId
    dataset_version: DatasetVersion
    dataset_manifest_hash: ContentHash
    split_name: str = "calibration"
    num_calibration_samples: int = Field(ge=10, default=512)
    calibration_algorithm: str = "entropy"  # e.g. entropy, minmax, percentile


class DeploymentRuntime(str, Enum):
    STANDALONE_TENSORRT = "standalone_tensorrt"
    DEEPSTREAM_PIPELINE = "deepstream_pipeline"
    TRITON_SERVER = "triton_server"
    ONNX_RUNTIME = "onnx_runtime"
    EMBEDDED_DAEMON = "embedded_daemon"


class DeploymentStatus(str, Enum):
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


class OptimizationPlan(BaseModel):
    """Authoritative, immutable model optimization specification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: OptimizationPlanId
    source_checkpoint_id: CheckpointId
    source_weights_hash: ContentHash
    technique: OptimizationTechnique
    target_precision: OptimizationPrecision
    target_platform: str  # e.g. "nvidia_jetson_orin", "rtx_4090"
    target_compute_capability: str  # e.g. "8.7", "8.9"
    shape_profiles: tuple[DynamicShapeProfile, ...] = Field(default_factory=tuple)
    calibration_binding: CalibrationDatasetBinding | None = None
    verification_dataset_binding: EvaluationDatasetBinding = Field(
        ...,
        description=(
            "Binding to the ADR-0009/ADR-0011 evaluation split used to verify that "
            "optimization did not exceed max_accuracy_drop_tolerance. MUST reference the "
            "identical test split used in the pre-optimization ADR-0011 baseline evaluation "
            "so that accuracy comparison is statistically valid. This binding is mandatory: "
            "an OptimizationPlan without it cannot pass the verification gate (\u00a73.2)."
        ),
    )
    max_accuracy_drop_tolerance: float = Field(ge=0.0, default=0.015)
    parameters: Mapping[str, Any] = Field(default_factory=dict)
    created_at: datetime


class OptimizedModelArtifact(BaseModel):
    """Compiled, hardware-locked optimized model artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: OptimizedArtifactId
    plan_id: OptimizationPlanId
    source_checkpoint_id: CheckpointId
    technique: OptimizationTechnique
    precision: OptimizationPrecision
    runtime_format: str  # e.g. "tensorrt_engine", "onnx"
    target_device: str
    target_compute_capability: str
    artifact_hash: ContentHash
    artifact_uri: str
    artifact_size_bytes: int = Field(gt=0)
    shape_profiles: tuple[DynamicShapeProfile, ...] = Field(default_factory=tuple)
    execution_id: ExecutionId | None = None
    target_id: TargetId | None = None
    created_at: datetime


class OptimizationResult(BaseModel):
    """Outcome of model optimization including verification against baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: OptimizationPlan
    artifact: OptimizedModelArtifact
    evaluation_result: EvaluationResult
    baseline_comparison: BaselineComparisonResult
    accuracy_retention_ratio: float = Field(ge=0.0)
    speedup_ratio: float = Field(gt=0.0)
    memory_reduction_ratio: float = Field(ge=0.0)
    verification_passed: bool
    diagnostics: str


class DeploymentTarget(BaseModel):
    """Physical or virtual edge/server endpoint where model is deployed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: TargetId
    hostname_or_ip: str
    platform_type: str  # e.g. "nvidia_jetson_agx_orin"
    gpu_device_name: str
    compute_capability: str
    cuda_runtime_version: str
    driver_version: str
    max_memory_allocation_gb: float = Field(gt=0.0)
    power_mode: str | None = None  # e.g. "MAXN"


class HealthCheckSpec(BaseModel):
    """Specification for deployment startup and readiness verification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    startup_timeout_sec: float = Field(gt=0.0, default=60.0)
    readiness_probe_samples: int = Field(ge=1, default=5)
    max_latency_threshold_ms: float = Field(gt=0.0, default=50.0)
    memory_headroom_mb: float = Field(ge=0.0, default=512.0)
    expected_output_shape: tuple[int, ...] | None = None


class ReadinessProbeResult(BaseModel):
    """Result of active readiness probing before traffic routing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    startup_time_sec: float
    probe_latency_mean_ms: float
    vram_consumed_mb: float
    error_message: str | None = None
    probed_at: datetime


class DeploymentPlan(BaseModel):
    """Immutable production deployment specification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deployment_id: DeploymentId
    artifact_id: OptimizedArtifactId
    target: DeploymentTarget
    runtime: DeploymentRuntime
    runtime_config: Mapping[str, Any] = Field(default_factory=dict)
    health_check: HealthCheckSpec
    rollback_target_id: DeploymentId | None = None
    approval_id: ApprovalId | None = None
    created_at: datetime


class DeploymentRecord(BaseModel):
    """Authoritative lifecycle and status record of a deployment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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


@runtime_checkable
class ModelOptimizer(Protocol):
    """Executes model compilation and precision transformations via ADR-0005."""

    def compile(
        self,
        plan: OptimizationPlan,
        tool_id: ToolId,
        target_id: TargetId | None = None,
    ) -> OptimizedModelArtifact: ...


@runtime_checkable
class OptimizationVerifier(Protocol):
    """Verifies optimization accuracy retention and speedup via ADR-0011."""

    def verify(
        self,
        artifact: OptimizedModelArtifact,
        plan: OptimizationPlan,
    ) -> OptimizationResult: ...


@runtime_checkable
class DeploymentManager(Protocol):
    """Coordinates deployment staging, readiness verification, and rollbacks."""

    def stage(
        self,
        plan: DeploymentPlan,
        target_id: TargetId | None = None,
    ) -> DeploymentRecord: ...

    def verify_readiness(
        self,
        deployment_id: DeploymentId,
    ) -> ReadinessProbeResult: ...

    def activate(
        self,
        deployment_id: DeploymentId,
        approval_id: ApprovalId,
    ) -> DeploymentRecord: ...

    def rollback(
        self,
        deployment_id: DeploymentId,
        reason: str,
    ) -> DeploymentRecord: ...
```

## 5. Consequences

### Positive
- **Guaranteed Production Safety (`docs/APPROVALS.md`, `[P§24]`):** Human approval gates with explicit blast radius and rollback plans prevent rogue or untested deployments from impacting active camera streams.
- **Elimination of Accuracy Cliffs (`[P§14]`, ADR-0011):** Mandatory evaluation verification stops degraded quantized models from reaching deployment.
- **Hardware-Aware Deployability (`[P§13]`, `[P§29.4]`):** Engines are strictly compiled and validated against verified target GPU microarchitectures and memory budgets, eliminating runtime hardware mismatch crashes.
- **Zero-Downtime Rollback:** Automated health and readiness probes combined with immutable rollback manifests ensure instant recovery from deployment failure.
- **Extensible Multi-Runtime Architecture (`[P§15]`):** Supports TensorRT, DeepStream, Triton, and ONNX Runtime without hardcoding vendor-specific syntax into the core agent loop.
- **Full Transformation Provenance (`[P§25]`):** Lineage DAG tracks the exact chain from raw training checkpoint to compiled edge engine.

### Negative / Tradeoffs
- **Compilation Overhead:** TensorRT engine compilation and INT8 PTQ calibration on physical targets can take 10–30 minutes depending on graph complexity.
- **Target Hardware Access Required:** Hardware-aware optimization requires an active target environment or cross-compilation container equipped with the target GPU architecture toolchain.

### Neutral
- Output records and active deployment manifests are persisted to SQLite (`experiments.sqlite`) via ADR-0004 without ADR-0012 managing database connections.
- Continuous streaming monitoring post-deployment is handed off to the future Monitoring Subsystem (ADR-0013).

## 6. References
- `docs/PROJECT.md`: §8 (Classical CV), §9 (Real-time CCTV), §13 (Hardware-aware), §14 (Model optimization), §15 (NVIDIA integration), §21 (Orchestration), §22 (MCP), §24 (Autonomous training/approvals), §25 (Experiment-driven), §28 (Production monitoring), §29 (Philosophy), §34 (Modular architecture)
- `docs/APPROVALS.md`: Production deployment and model promotion gates
- `docs/DATA.md`: Manifest-first content-addressed dataset specification
- ADR-0001: Capability Registry (`D-011`)
- ADR-0003: Orchestration State & Checkpointing (`D-020`)
- ADR-0004: Project Memory & Experiment Ledger (`D-021`)
- ADR-0005: Tools and MCP Execution Boundary (`D-023`)
- ADR-0007: Skills Architecture & NVIDIA Discovery (`D-025`)
- ADR-0008: Reasoning Subsystem (`D-026`)
- ADR-0009: Dataset Subsystem (`D-029`)
- ADR-0010: Training Subsystem (`D-030`)
- ADR-0011: Evaluation Subsystem (`D-031`)

## 7. Implementation Acceptance Criteria

When Phase 2 runtime implementation begins, the implementation of this subsystem MUST satisfy the following empirical acceptance tests before being considered complete:

1. **Optimization Plan Immutability & Validation:** `OptimizationPlan` instances are strictly validated and immutable. Plans referencing non-existent checkpoints, invalid precision levels, or out-of-order dynamic shape profiles ($\text{min\_shape} \le \text{opt\_shape} \le \text{max\_shape}$) are rejected at instantiation time.
2. **Calibration Dataset Leakage Gate:** INT8 PTQ quantization plans referencing calibration datasets that overlap with test splits or lack valid ADR-0009 manifest bindings are rejected before compilation begins.
3. **Dynamic Shape Bound Verification:** Engine compilation processes with dynamic shapes enforce minimum, optimal, and maximum shape bounds. Attempting inference with tensor dimensions outside validated shape profiles raises explicit runtime exceptions.
4. **Cryptographic Transformation Lineage DAG:** Every compiled `OptimizedModelArtifact` computes and records an immutable content hash derived deterministically from the parent checkpoint hash, optimization parameters, calibration dataset hash, and target architecture profile.
5. **Mandatory Post-Optimization Verification Gate:** Any attempt to promote or deploy an `OptimizedModelArtifact` without a verified `OptimizationResult` confirming that accuracy degradation does not exceed `max_accuracy_drop_tolerance` against the unoptimized baseline (via ADR-0011) is strictly rejected.
6. **Hardware-Locked Compilation Integrity:** Binary engines (e.g. TensorRT) compiled for a specific compute capability (e.g. SM 8.7 Jetson Orin) fail fast with a descriptive error when target profile verification detects a mismatched execution hardware environment per `D-022`.
7. **Production Deployment Human Approval Gate:** Invoking `DeploymentManager.activate` without an authorized `ApprovalId` generated per `docs/APPROVALS.md` and ADR-0003 raises an authorization failure and halts deployment execution.
8. **Automated Readiness Probe Gate:** Candidate deployments in `STAGING` undergo automated warmup and readiness probing via `HealthCheckSpec`. Deployments that fail latency budgets, memory headroom, or throw execution faults transition to `FAILED` without routing production traffic.
9. **Deterministic Zero-Downtime Rollback:** When an active deployment experiences failure or manual operator rollback is triggered, `DeploymentManager.rollback` immediately restores the verified predecessor `rollback_target_id` manifest without downtime or pipeline stalls.
10. **Execution Token Traceability:** Optimization compilation and deployment execution jobs track `execution_id` and `target_id` from ADR-0005, maintaining complete causal auditability to physical containers, processes, or remote daemon executions.
11. **Persistence Protocol Independence:** All optimization results, artifacts, and deployment lifecycle records serialize cleanly to immutable JSON/Pydantic representations for persistence in ADR-0004 (`experiments.sqlite`) without ADR-0012 directly owning database connections.
