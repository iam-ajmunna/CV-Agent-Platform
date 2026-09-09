# ADR-0010: Training Subsystem Architecture

- **Status:** Accepted — Frozen
- **Date:** 2026-09-08
- **Layer:** training / execution orchestration
- **Canon:** `[P§10]`, `[P§11]`, `[P§13]`, `[P§15]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§29.2]`, `[P§29.4]`, `[P§29.8]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #4
- **Preceded by Decisions:** D-029 (ADR-0009 Dataset Subsystem), D-026 (ADR-0008 Reasoning Subsystem), D-025 (ADR-0007 Skills), D-023 (ADR-0005 Tools & MCP Boundary), D-021 (ADR-0004 Experiment Ledger), D-020 (ADR-0003 Orchestration & Approval), D-014 (Out-of-process GPU compute)

## 1. Context

In empirical computer vision engineering, model training is among the most resource-intensive, consequential, and failure-prone activities in the entire lifecycle `[P§10]`, `[P§24]`. Modern CV training is not simply running a single CLI command: it encompasses multi-parameter optimization, learning rate schedules, loss combinations (focal, CIoU/DIoU, contrastive, label smoothing), data augmentation recipes, hardware-constrained batch sizing, mixed-precision configurations, continuous telemetry streaming, divergence/NaN detection, checkpoint persistence, and rigorous experiment reproducibility `[P§10]`, `[P§13]`, `[P§25]`.

Furthermore, autonomous training presents severe operational hazards:
1. **Unbounded Compute Expenditure (`[P§24]`, `docs/APPROVALS.md`):** An unconstrained agent must never silently spin up costly cloud clusters or consume hundreds of GPU-hours without explicit, pre-calculated cost estimation and human authorization.
2. **Layer Leakage (`[P§34]`):** Allowing cognitive reasoning agents (ADR-0008) or workflow nodes (ADR-0003) to directly spawn training subprocesses, format low-level shell flags, or manipulate local sockets tightly couples decision logic with execution mechanics.
3. **Execution Transport Conflation (`D-014`, `D-022`, ADR-0005):** Local agent workstations (e.g. Apple Silicon macOS) do not possess NVIDIA CUDA runtimes or GPUs. Training must execute out-of-process on external compute targets (`REMOTE` or `MCP_ENDPOINT`), but training job semantics (epochs, batches, loss curves, checkpoints) must not be conflated with generic process transport (SSH, subprocess, MCP protocol).
4. **Data and Lineage Severance (`[P§25]`, `[P§26]`, ADR-0009):** Training without immutable bindings to exact dataset versions (`DatasetId`, `DatasetVersion`, `split_membership` hash verification) invalidates scientific reproducibility and violates experiment-driven development.
5. **Divergence and Silent Failure (`[P§27]`):** Training runs that diverge (NaN loss, vanishing/exploding gradients, mode collapse) waste hours of GPU time if not caught early by automated diagnostic monitors.

This ADR establishes the architectural specification, interface contracts, lifecycle state machine, and operational invariants for the **Training Subsystem**.

## 2. Responsibility (`[P§34]`)

- **This owns:**
  - The structured, immutable training experiment specification (`TrainingPlan`).
  - Validation of training hyperparameter contracts (optimizers, learning rate schedules, loss functions, batch sizes, precision modes, augmentation configurations).
  - Explicit binding between a `TrainingPlan` and authoritative dataset versions (`DatasetId`, `DatasetVersion`) governed by ADR-0009.
  - Model input references (base model architecture, pretrained checkpoint hashes or URIs).
  - Compute-target requirement declarations (minimum VRAM, CUDA compute capability, device count, distributed topology).
  - Pre-execution cost estimation (GPU-hours, wall-clock time, monetary cost, artifact storage) per `docs/APPROVALS.md` and `[P§24]`.
  - Enforcement of human approval preconditions before training job dispatch.
  - The training job lifecycle state machine (`TrainingJobState`) from validation through launch, streaming execution, checkpointing, and completion or failure.
  - The training-specific dispatch abstraction (`TrainingLauncher`), translating a `TrainingPlan` into executable tool invocations and delegating transport to ADR-0005.
  - Real-time training telemetry capture and loss curve streaming (`TrainingTelemetryRecord`).
  - Checkpoint manifest creation, validation, retention policy enforcement, and content-addressed storage registration.
  - Online divergence and failure diagnostics (NaN/Inf detection, loss explosion, loss stagnation, GPU OOM detection) triggering early intervention or graceful abort.
  - Complete experiment reproducibility metadata packaging (`TrainingEnvironmentRecord`) per `[P§25]`.
- **This does NOT own:**
  - Cognitive hypothesis generation, training strategy formulation, or post-training failure analysis → Reasoning Subsystem (ADR-0008) `[P§4]`, `[P§27]`;
  - General agent workflow coordination, multi-step graph execution, checkpoint state persistence, or human approval interrupt handling → Orchestration Subsystem (ADR-0003) `[P§21]`, `[P§24]`;
  - Permanent experiment record persistence in SQLite (`experiments.sqlite`), metric querying, or baseline comparisons → Project Memory & Experiment Ledger (ADR-0004) `[P§25]`;
  - Low-level subprocess management, SSH transport, remote environment execution, or MCP socket handling → Tools & MCP Execution Boundary (ADR-0005) `[P§22]`;
  - Procedural training recipes, vendor-specific framework scripts (e.g. YOLOv8, PyTorch Lightning, Detectron2), or augmentation implementations → Skills Subsystem (ADR-0007) `[P§15]`;
  - Authoritative dataset manifests, payload storage, and split leakage auditing → Dataset Subsystem (ADR-0009) `[P§26]`;
  - Post-training evaluation benchmarks, metric calculation on held-out test splits, or deployment suitability verification → future Evaluation Subsystem (ADR-0011) `[P§12]`;
  - Capability taxonomy definitions or registry authority → Capability Model (ADR-0001) `[P§23]`.
- **Why this responsibility does not belong elsewhere:**
  Conflating training job semantics with ADR-0005 makes generic tool execution responsible for deep learning concepts (learning rates, loss curves, early stopping, checkpoint manifests). Placing training dispatch directly inside Reasoning (ADR-0008) contaminates cognitive decision-making with execution lifecycle states. Leaving training to ad-hoc scripts inside Skills (ADR-0007) eliminates cross-framework validation, cost guardrails, and telemetry standardization. The Training Subsystem isolates training-specific engineering semantics into an authoritative, reproducible layer.

## 3. Decision

We establish an explicit **Training Subsystem Architecture** centered on typed training plans, mandatory cost estimation, approval gates, training-specific dispatch delegation to ADR-0005, telemetry streaming, checkpoint manifests, and online failure diagnostics.

```text
               ADR-0008 Reasoning Subsystem
              (Hypothesis & Strategy Formulation)
                            │
                            │ proposes experiment parameters
                            ▼
             ┌─────────────────────────────┐
             │        TrainingPlan         │
             │ (Hyperparameters, Datasets, │
             │  Compute Requirements, etc.)│
             └──────────────┬──────────────┘
                            │
                            │ 1. validate & estimate cost
                            ▼
             ┌─────────────────────────────┐
             │       Cost Estimator        │
             │   (GPU-hrs, $, Wall-clock)  │
             └──────────────┬──────────────┘
                            │
                            │ 2. request approval (docs/APPROVALS.md)
                            ▼
             ┌─────────────────────────────┐
             │    ADR-0003 Orchestration   │
             │    Approval Gate Interrupt  │
             └──────────────┬──────────────┘
                            │
                            │ 3. authorized
                            ▼
             ┌─────────────────────────────┐
             │      TrainingLauncher       │
             │ (Training Dispatch Contract)│
             └──────────────┬──────────────┘
                            │
                            │ 4. delegates execution transport
                            ▼
             ┌─────────────────────────────┐
             │    ADR-0005 Tool / MCP      │
             │     Execution Boundary      │
             └──────────────┬──────────────┘
                            │
       ┌────────────────────┼────────────────────┐
       ▼                    ▼                    ▼
   LOCAL Target       REMOTE Target        MCP Target
  (Inspection/Mock)  (External GPU/DDP)   (Managed Cloud)
       │                    │                    │
       └────────────────────┼────────────────────┘
                            │
                            │ telemetry & checkpoints
                            ▼
             ┌─────────────────────────────┐
             │     Telemetry & Monitors    │
             │  (NaN, Divergence, OOM)     │
             └──────────────┬──────────────┘
                            │
                            │ terminal handoff
                            ▼
             ┌─────────────────────────────┐
             │  ADR-0004 Experiment Ledger │
             │   (experiments.sqlite)      │
             └──────────────┬──────────────┘
                            │
                            ▼
             future Evaluation Subsystem (ADR-0011)
```

### 3.1. Reasoning vs. Training Boundary (Proposal vs. Materialization)

Consistent with ADR-0008, the boundary between cognitive reasoning and the training subsystem is strictly defined:
- **Reasoning Subsystem (ADR-0008) owns strategy formulation:** It analyzes project goals, evaluates previous experiment baselines, diagnoses past failures, and hypothesizes what change to make (e.g. "Add focal loss to combat foreground-background imbalance; decrease learning rate by 2x; train for 50 epochs").
- **Training Subsystem owns materialization and lifecycle:** It validates that the proposed parameters form a structurally sound, hardware-viable `TrainingPlan`; computes required GPU-hours and monetary cost; enforces dataset-version binding; manages the job execution state machine; captures streaming telemetry; and verifies checkpoint integrity.
- **Reasoning proposes; Training validates and materializes.** Reasoning never directly executes commands, configures sockets, or parses raw progress logs.

### 3.2. Training-Specific Semantics vs. ADR-0005 Generic Execution

The `TrainingLauncher` is a training-specific dispatch contract, **not** a generic execution engine:
- `TrainingLauncher` transforms a validated, approved `TrainingPlan` into the required execution payload (environment variables, invocation arguments, config files, container specifications).
- It dispatches the workload exclusively through the **ADR-0005 Tool and MCP Execution Boundary** using declared `ToolId` tokens and `ExecutionTarget` bindings (`REMOTE` or `MCP_ENDPOINT`).
- ADR-0005 owns the physical transport (SSH channels, host subprocesses, JSON-RPC envelopes over MCP).
- `TrainingLauncher` receives back an `ActiveJobHandoff` from ADR-0005 and tracks training-specific semantics (step telemetry, epoch boundaries, loss curves, validation intervals, checkpoint manifests).

```text
TrainingPlan
    ↓
TrainingLauncher (training semantics, arg formatting, monitor hooks)
    ↓
ADR-0005 Tool / Execution Boundary (transport adapter, security, target validation)
    ↓
LOCAL / REMOTE / MCP_ENDPOINT (physical compute environment)
```

### 3.3. The `TrainingPlan` Schema

A `TrainingPlan` is an immutable, Pydantic v2 frozen specification containing all parameters necessary to reproduce a training run deterministically:
1. **Plan Metadata:** `plan_id`, `experiment_id` (foreign key to ADR-0004), `created_at`.
2. **Dataset Binding (`[P§26]`, ADR-0009):** `dataset_id`, `dataset_version`, `dataset_manifest_hash`, `train_split`, `val_split`. Random splitting at runtime is strictly prohibited.
3. **Model Configuration:** `architecture_name`, `num_classes`, `pretrained_weights_ref` (optional `ContentHash` or URI), `model_hyperparameters`.
4. **Optimization & Schedule (`[P§10]`):**
   - `optimizer`: Algorithm (`adamw`, `sgd`, `lion`), base learning rate, momentum/betas, weight decay, gradient clipping max norm.
   - `lr_scheduler`: Policy (`cosine_annealing`, `linear_warmup`, `step`, `exponential`), warmup epochs, min learning rate, restart periods.
5. **Loss Specification (`[P§10]`):**
   - Primary and auxiliary loss formulations (e.g. `cross_entropy`, `focal_loss`, `ciou`, `diou`, `giou`, `bce_with_logits`).
   - Component weights, label smoothing factor, focal loss gamma/alpha parameters.
6. **Execution Hyperparameters:**
   - `batch_size`: Per-device batch size.
   - `gradient_accumulation_steps`: Number of steps before optimizer update (effective batch size = per-device × devices × accumulation).
   - `epochs`: Total target epochs.
   - `precision`: Computation mode (`fp32`, `fp16_amp`, `bf16_amp`, `int8_ptq_aware`).
7. **Augmentation Recipe (`[P§10]`):**
   - Training pipeline transforms (spatial transforms, photometric distortion, mosaic, mixup, copy-paste) with explicit parameters.
   - Deterministic validation transform pipeline (resize, normalization mean/std).
8. **Compute Requirements (`[P§13]`, `[P§29.4]`):**
   - `min_vram_gb`: Minimum GPU memory required per worker to prevent OOM.
   - `min_cuda_compute_capability`: Target architecture constraint (e.g. `8.0` for Ampere, `8.9` for Ada, `9.0` for Hopper).
   - `num_devices`: Requested accelerator count.
   - `distributed_strategy`: Single-GPU, `ddp`, `fsdp`, or `deepspeed`.

### 3.4. Cost Estimation & Approval Preconditions (`docs/APPROVALS.md`, `[P§24]`)

Per `docs/APPROVALS.md` and `[P§24]`, training runs are **strictly gated actions** requiring explicit human approval:
- **Estimation Precondition:** The agent must never submit a vague "May I train?" request. It must present a structured `CostEstimate`:
  $$\text{Steps Per Epoch} = \left\lceil \frac{N_{\text{train items}}}{\text{Effective Batch Size}} \right\rceil$$
  $$\text{Total Steps} = \text{Steps Per Epoch} \times N_{\text{epochs}}$$
  $$\text{Estimated Wall Time (hours)} = \frac{\text{Total Steps} \times \text{Step Latency (sec)}}{3600}$$
  $$\text{Estimated GPU-Hours} = \text{Estimated Wall Time} \times N_{\text{devices}}$$
  $$\text{Estimated Cost (\$)} = \text{Estimated GPU-Hours} \times \text{Target Hourly Rate}$$
  $$\text{Estimated Artifact Storage (GB)} = N_{\text{checkpoints}} \times \text{Model Size (GB)}$$
- **Approval Precondition:** Training job dispatch will refuse execution unless an authorized approval record (`approval_id`, approver, scope, timestamp) is attached to the plan context, verified by the orchestration approval interrupt (ADR-0003).

### 3.5. Training Job Lifecycle State Machine

A training job progresses through an explicit, auditable state machine:

```text
  ┌───────────┐
  │   DRAFT   │
  └─────┬─────┘
        │ validate schema & dataset binding
        ▼
  ┌───────────┐
  │ VALIDATED │
  └─────┬─────┘
        │ calculate cost
        ▼
  ┌───────────┐
  │ ESTIMATED │
  └─────┬─────┘
        │ submit approval request to Orchestration (ADR-0003)
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
        │ dispatch via TrainingLauncher -> ADR-0005
        ▼
  ┌───────────┐
  │ LAUNCHING │
  └─────┬─────┘
        │ process active & first telemetry received
        ▼
  ┌───────────┐
  │  RUNNING  │◄─────────────────────────────┐
  └─────┬─────┘                              │
        │                                    │
        ├──► checkpoint saved ───────────────┘
        │
        ├──► NaN / Divergence / OOM detected
        │       ▼
        │  ┌──────────┐
        │  │  FAILED  │ (terminal)
        │  └──────────┘
        │
        ├──► user / orchestrator cancel
        │       ▼
        │  ┌──────────┐
        │  │ ABORTED  │ (terminal)
        │  └──────────┘
        │
        │ all epochs completed
        ▼
  ┌───────────┐
  │ COMPLETED │ (terminal)
  └───────────┘
```

### 3.6. Telemetry & Streaming Loss Monitoring Contract

The training subsystem continuously ingests, validates, and normalizes telemetry emitted during execution:
- **`TrainingTelemetryRecord`:** Represents a point-in-time progress event:
  - `step`: Global training step.
  - `epoch`: Current epoch index and total target epochs.
  - `train_loss`: Aggregate loss value and named loss components (e.g. `loss_cls`, `loss_box`, `loss_dfl`).
  - `val_loss`: Validation loss (if evaluated at this step).
  - `learning_rate`: Current effective learning rate.
  - `throughput_fps`: Processed images/items per second.
  - `vram_allocated_gb`: Accelerator memory currently allocated.
  - `timestamp`: UTC ISO 8601 timestamp.
- **Decoupled Transport:** Telemetry streams from the remote target via ADR-0005 streaming adapters (or standard log tails), preventing blocking of the agent's main event loop.

### 3.7. Checkpoint Contract & Storage Invariants

Checkpoints are managed as immutable engineering artifacts:
1. **`CheckpointManifest`:** Each saved checkpoint is accompanied by a manifest recording:
   - `checkpoint_id`: Unique identifier (e.g. `ckpt-exp042-ep025`).
   - `plan_id` & `experiment_id`: Provenance links.
   - `epoch` & `step`: Exact temporal coordinates.
   - `metrics`: Evaluation metrics snapshot achieved at this checkpoint.
   - `weights_hash`: SHA-256 content hash of the model weights payload.
   - `weights_uri`: Storage location URI (local path or remote blob store).
   - `optimizer_state_included`: Boolean indicating if optimizer/scheduler states are bundled for resumption.
2. **Content-Addressed Registration:** When checkpoints are persisted, their binary payloads are registered in accordance with ADR-0009 content-addressed storage patterns.
3. **Retention Policies:** To avoid exhausting disk space, the subsystem enforces explicit retention rules:
   - `keep_best`: Retain top $K$ checkpoints ranked by validation metric.
   - `keep_last`: Retain latest $N$ checkpoints for crash recovery.
   - `keep_final`: Always retain final epoch weights.
   - Intermediate checkpoints outside retention policies are purged automatically.

### 3.8. Convergence, Failure Diagnostics & Early Intervention

Rather than passively waiting for multi-hour jobs to complete, the Training Subsystem executes online diagnostic monitors on every telemetry event:
1. **NaN / Inf Detection:** Immediate trigger if loss, gradient norm, or validation score evaluates to NaN or Infinity. The run is halted immediately to conserve GPU compute.
2. **Divergence Detection:** If training loss exceeds $M \times$ the initial loss or baseline moving average over $K$ consecutive steps, the monitor flags loss divergence and aborts execution.
3. **Loss Stagnation & Early Stopping:** If validation loss fails to improve by at least $\Delta_{\text{min}}$ over a configured patience window ($P$ epochs), the subsystem issues an early stop signal, preserving the best checkpoint.
4. **Out-of-Memory (OOM) Recovery:** If an uncaught CUDA OOM occurs, the launcher captures the memory allocation footprint and reports structured failure metadata to Reasoning (ADR-0008), enabling intelligent batch-size backoff hypotheses.

### 3.9. Experiment Reproducibility Invariant (`[P§25]`)

Per `[P§25]`, an experiment lacking full metadata is void. Every completed or failed training job compiles an immutable `TrainingEnvironmentRecord`:
- Exact random seeds (`seed_torch`, `seed_numpy`, `seed_python`, `seed_cuda`).
- Code commit SHA and git status clean/dirty flag.
- Dataset manifest content hash (`dataset_manifest_hash`).
- Training plan configuration hash.
- Python runtime, PyTorch version, CUDA runtime version, NVIDIA driver version.
- Hardware topology: GPU device name(s), count, VRAM capacity, interconnect type.

### 3.10. Boundary with Future Evaluation Subsystem (ADR-0011)

The training subsystem concludes its lifecycle when training terminates and final checkpoints are catalogued. It does **not** evaluate test splits or produce official deployment verification benchmarks:
- Training validation during training is strictly for monitoring convergence and checkpoint selection.
- Comprehensive benchmarking on held-out test splits, latency/throughput profiling across target devices, slicing/fairness evaluations, and deployment gates are reserved for the **future Evaluation Subsystem (ADR-0011)**.
- Training hands off the `CheckpointManifest` and `TrainingEnvironmentRecord` to Project Memory (ADR-0004); the future Evaluation Subsystem reads from that record.

## 4. Proposed Interface Contracts (Phase 2 Implementation Target)

*(Note: These interfaces represent the proposed type contracts for Phase 2 implementation. They do not exist in the repository today).*

```python
# module: cv_agent.training.types (Proposed — Phase 2 implementation target)

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, NewType, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from cv_agent.capabilities import CapabilityId, ToolId
from cv_agent.dataset.types import ContentHash, DatasetId, DatasetVersion
from cv_agent.tools.types import ExecutionId, TargetId

# Identifier syntax rules:
# - PlanId, TrainingJobId, CheckpointId, ApprovalId: conform to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1.
PlanId = NewType("PlanId", str)
TrainingJobId = NewType("TrainingJobId", str)
CheckpointId = NewType("CheckpointId", str)
ApprovalId = NewType("ApprovalId", str)


class TrainingPrecision(str, Enum):
    FP32 = "fp32"
    FP16_AMP = "fp16_amp"
    BF16_AMP = "bf16_amp"
    INT8_PTQ_AWARE = "int8_ptq_aware"


class DistributedStrategy(str, Enum):
    NONE = "none"
    DDP = "ddp"
    FSDP = "fsdp"
    DEEPSPEED = "deepspeed"


class TrainingJobStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    ESTIMATED = "estimated"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    LAUNCHING = "launching"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class FailureReason(str, Enum):
    NONE = "none"
    NAN_LOSS = "nan_loss"
    LOSS_DIVERGENCE = "loss_divergence"
    CUDA_OOM = "cuda_oom"
    HARDWARE_FAULT = "hardware_fault"
    EARLY_STOPPING = "early_stopping"
    USER_CANCELLED = "user_cancelled"
    TIMEOUT = "timeout"


class OptimizerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str  # e.g. "AdamW", "SGD"
    lr: float = Field(gt=0.0)
    weight_decay: float = Field(ge=0.0, default=0.01)
    momentum: float | None = Field(ge=0.0, le=1.0, default=None)
    betas: tuple[float, float] | None = (0.9, 0.999)
    gradient_clip_norm: float | None = Field(gt=0.0, default=None)


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: str  # e.g. "cosine_annealing", "linear_warmup", "step"
    warmup_epochs: int = Field(ge=0, default=3)
    min_lr: float = Field(ge=0.0, default=1e-6)
    step_size: int | None = None
    gamma: float | None = None


class LossComponentConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    loss_type: str  # e.g. "cross_entropy", "focal_loss", "ciou"
    weight: float = Field(gt=0.0, default=1.0)
    params: Mapping[str, Any] = Field(default_factory=dict)


class AugmentationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pipeline_name: str
    image_size: tuple[int, int]  # (height, width)
    horizontal_flip_prob: float = Field(ge=0.0, le=1.0, default=0.5)
    mosaic_prob: float = Field(ge=0.0, le=1.0, default=0.0)
    mixup_prob: float = Field(ge=0.0, le=1.0, default=0.0)
    color_jitter: float = Field(ge=0.0, default=0.0)
    custom_params: Mapping[str, Any] = Field(default_factory=dict)


class ComputeRequirements(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_vram_gb: float = Field(gt=0.0)
    min_cuda_compute_capability: str | None = None  # e.g. "8.0"
    num_devices: int = Field(ge=1, default=1)
    distributed_strategy: DistributedStrategy = DistributedStrategy.NONE


class CostEstimate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    estimated_gpu_hours: float = Field(ge=0.0)
    estimated_wall_clock_hours: float = Field(ge=0.0)
    estimated_cost_usd: float = Field(ge=0.0)
    estimated_storage_gb: float = Field(ge=0.0)
    basis_description: str


class TrainingPlan(BaseModel):
    """Authoritative, immutable training experiment specification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: PlanId
    experiment_id: str  # Foreign key to ADR-0004
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

    # Gating & Safety
    cost_estimate: CostEstimate | None = None
    approval_id: ApprovalId | None = None


class TrainingTelemetryRecord(BaseModel):
    """Point-in-time training progress and metric telemetry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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


class CheckpointManifest(BaseModel):
    """Immutable metadata record for a saved model checkpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_id: CheckpointId
    job_id: TrainingJobId
    plan_id: PlanId
    epoch: int = Field(ge=0)
    step: int = Field(ge=0)
    weights_hash: ContentHash
    weights_uri: str
    optimizer_state_included: bool = False
    metrics: Mapping[str, float] = Field(default_factory=dict)
    created_at: datetime


class TrainingEnvironmentRecord(BaseModel):
    """Complete environment and reproducibility metadata per [P§25]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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


class TrainingJobRecord(BaseModel):
    """Authoritative lifecycle state record of a training job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: TrainingJobId
    plan: TrainingPlan
    status: TrainingJobStatus
    failure_reason: FailureReason = FailureReason.NONE
    failure_details: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    best_checkpoint: CheckpointManifest | None = None
    environment: TrainingEnvironmentRecord | None = None
    execution_id: ExecutionId | None = None
    target_id: TargetId | None = None


@runtime_checkable
class TrainingEstimator(Protocol):
    """Estimates resource requirements and financial cost for a plan."""

    def estimate(
        self, plan: TrainingPlan, sample_step_latency_sec: float | None = None
    ) -> CostEstimate: ...


@runtime_checkable
class TrainingMonitor(Protocol):
    """Online diagnostic monitor analyzing streaming telemetry."""

    def on_step(
        self, telemetry: TrainingTelemetryRecord
    ) -> tuple[bool, FailureReason, str | None]:
        """Returns (should_abort, reason, details)."""
        ...


@runtime_checkable
class TrainingLauncher(Protocol):
    """Training-specific dispatch contract delegating transport to ADR-0005."""

    def validate_plan(self, plan: TrainingPlan) -> bool: ...

    def launch(
        self,
        plan: TrainingPlan,
        tool_id: ToolId,
        target_id: TargetId | None = None,
    ) -> TrainingJobRecord: ...

    def abort(self, job_id: TrainingJobId) -> bool: ...
```

## 5. Consequences

### Positive
- **Elimination of Silent GPU Spend:** Strict estimation and approval preconditions enforce `docs/APPROVALS.md` and `[P§24]`, ensuring no multi-GPU or cloud training job runs without explicit authorization.
- **Architectural Boundary Integrity (`[P§34]`):** Reasoning (ADR-0008) retains responsibility for hypothesis generation, Training manages job semantics and validation, and generic execution/transport remains strictly encapsulated in ADR-0005.
- **Flawless Scientific Reproducibility (`[P§25]`):** Every training run is immutably coupled with dataset version hashes (ADR-0009), complete hyperparameter plans, random seeds, and environment metadata.
- **Online Early Intervention:** Automated divergence, NaN, and stagnation monitors prevent wasting GPU-hours on broken or dead-end runs.
- **Pluggable Framework Support:** The separation between `TrainingPlan` and `Skills` (ADR-0007) allows supporting PyTorch Lightning, YOLOv8/v10/v11, Detectron2, and custom engines behind a unified contract.

### Negative / Tradeoffs
- **Overhead for Trivial Runs:** Compiling a full `TrainingPlan`, cost estimation, and approval record adds structured ceremony even for micro-experiments or smoke tests (mitigated by local dry-run / unit test exemptions).
- **Estimation Uncertainty:** Pre-training step latency estimation can vary based on IO bottlenecks, dynamic batch padding, and multi-tenant cluster contention.

### Neutral
- Checkpoint storage backends rely on the storage patterns established in ADR-0009.
- Evaluation and benchmark assertions are deferred to the future Evaluation Subsystem (ADR-0011).

## 6. References
- `docs/PROJECT.md`: §10 (Training intelligence), §11 (NAS), §13 (Hardware-aware), §15 (NVIDIA), §21 (Orchestration), §22 (MCP), §24 (Autonomous training), §25 (Experiment-driven), §26 (Dataset versioning), §29 (Philosophy), §34 (Modular architecture)
- `docs/APPROVALS.md`: Human approval gates and cost estimation rules
- `docs/DATA.md`: Manifest-first content-addressed dataset specification
- ADR-0001: Capability Registry (`D-011`)
- ADR-0003: Orchestration State & Checkpointing (`D-020`)
- ADR-0004: Project Memory & Experiment Ledger (`D-021`)
- ADR-0005: Tools and MCP Execution Boundary (`D-023`)
- ADR-0007: Skills Architecture & NVIDIA Discovery (`D-025`)
- ADR-0008: Reasoning Subsystem (`D-026`)
- ADR-0009: Dataset Subsystem (`D-029`)

## 7. Implementation Acceptance Criteria

1. **Plan Validation & Immutability:** A `TrainingPlan` enforces frozen validation (`frozen=True`, `extra='forbid'`), validates positive learning rates and class counts, and binds authoritatively to `DatasetId`, `DatasetVersion`, and `dataset_manifest_hash` per ADR-0009.
2. **Cost Estimation Precondition:** Every non-mock training job requires a pre-calculated `CostEstimate` detailing GPU-hours, wall-clock time, monetary cost, and artifact storage before state transition to `AWAITING_APPROVAL`.
3. **Approval Gate Enforcement:** Job dispatch transitions from `AWAITING_APPROVAL` to `LAUNCHING` strictly upon receipt of a valid `ApprovalId` routed through Orchestration (ADR-0003) per `docs/APPROVALS.md`. Any dispatch without approval raises an authorization defect.
4. **Transport Delegation:** `TrainingLauncher` formats execution arguments and environment variables, delegating physical execution strictly through ADR-0005 (`ToolId`, `ExecutionTarget`, `ActiveJobHandoff`) without managing raw SSH connections or local subprocess sockets.
5. **Traceability to Execution Tokens:** `TrainingJobRecord` preserves explicit linkages to ADR-0005 execution tokens via `execution_id: ExecutionId | None` and `target_id: TargetId | None`.
6. **Real-time Telemetry Normalization:** Point-in-time progress events are captured as immutable `TrainingTelemetryRecord` objects streaming step loss, loss components, throughput, learning rate, and VRAM utilization without blocking the orchestrator event loop.
7. **Online Failure Diagnostics:** Automated `TrainingMonitor` triggers immediate job abort on detecting NaN/Inf values, loss explosion, or unrecoverable CUDA OOM, recording structured `failure_reason` and `failure_details` in `TrainingJobRecord`.
8. **Deterministic Checkpoint Lineage:** Checkpoint manifests record `checkpoint_id`, `weights_hash` (SHA-256), training step, epoch, and metric snapshot, enforcing disk retention policies (`keep_best`, `keep_last`, `keep_final`).
9. **Reproducibility Metadata Packaging:** Completed runs package an immutable `TrainingEnvironmentRecord` (`seed_*`, git commit SHA, dependency versions, device topology) and hand off to ADR-0004 `experiments.sqlite` and ADR-0011 Evaluation.
