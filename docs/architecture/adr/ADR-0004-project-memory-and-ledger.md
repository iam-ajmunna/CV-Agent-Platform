# ADR-0004: Project Memory and Experiment Ledger Persistence

- **Status:** Accepted
- **Amendment Status:** Frozen
- **Date:** 2026-09-02
- **Last Reviewed:** 2026-09-10
- **Layer:** memory
- **Canon:** `[P§10]`, `[P§12]`, `[P§24]`, `[P§25]`, `[P§26]`, `[P§29.2]`, `[P§29.4]`, `[P§29.5]`, `[P§33]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #19

## 1. Context

In complex computer-vision engineering workflows, accumulating state solely within an LLM conversation context or an ephemeral process heap is fragile and unsustainable `[P§25]`. Context windows are lossy, session-bound, and cannot reliably answer foundational engineering questions such as *"Why did we choose this architecture over the baseline three weeks ago?"* or *"What was the quantitative impact of FP16 TensorRT quantization on small-object recall at 30 FPS?"* `[P§25]`, `[P§34]`.

Furthermore, computer-vision projects exhibit two distinct persistence modalities with conflicting performance, durability, and storage characteristics:
1. **Qualitative, low-frequency domain knowledge:** Project goals, physical camera geometries, operational constraints, target hardware budgets, architectural choices, dataset lineage, and learned heuristics. This knowledge is human-readable, evolves in tandem with code, and naturally belongs in Git version control `[P§33]`.
2. **Quantitative, high-volume experiment rows:** Machine-readable records of training runs, evaluations, optimization sweeps, benchmark profiles, paired accuracy and system/hardware metrics, and artifact references `[P§25]`. Storing thousands of numerical metric vectors or per-step profiling records in flat Git-tracked markdown degrades repository performance and prevents relational querying, filtering, and automated baseline comparison `[P§29.2]`.

Decisions Q1 (`D-013`), Q2 (`D-014`), Q3 (`D-015`), and Q8 (`D-018`) established the foundational parameters for repository-scoped isolation, external asynchronous execution, persistent approvals, and a dual-layer persistence model. In ADR-0003 (`D-020`), orchestration state persistence was established in `.cv_agent/state/checkpoints.sqlite` with configurable retention, explicitly reserving permanent experiment tracking to ADR-0004.

This ADR formalizes the persistence architecture, structural contracts, ownership boundaries, and interface stubs for Project Memory and the Experiment Ledger.

## 2. Decision

We establish a dual-layer persistence architecture for the CV Engineering Agent:
1. **Git-Tracked Structured Project Memory** located under `.cv_agent/memory/` within the project repository root for qualitative domain state and architectural history.
2. **Permanent Experiment Ledger** stored in a dedicated local SQLite database at `.cv_agent/state/experiments.sqlite` for structured, append-oriented, high-volume experiment records, reproducibility metadata, and paired performance metrics.

### 2.1. Dual-Layer Persistence Architecture (Q8 / D-018, `[P§34]`)

The two layers operate side-by-side with strict separation of responsibilities:

```
<project_root>/
├── .cv_agent/
│   ├── memory/                     ← Git-tracked structured project memory
│   │   ├── project_spec.md         ← Domain problem, operational requirements, camera setups
│   │   ├── hardware_constraints.yaml ← Deployment target budgets (VRAM, RAM, power, latency)
│   │   ├── architectural_choices.md← High-level model decisions, rationale, design evolution
│   │   ├── dataset_lineage.md      ← Dataset manifests, versions, annotations, split rules [P§26]
│   │   └── learned_heuristics.md   ← Validated empirical heuristics across experiments
│   └── state/
│       ├── checkpoints.sqlite      ← ADR-0003: Runtime orchestration state (configurable retention)
│       └── experiments.sqlite      ← ADR-0004: Permanent experiment ledger (immutable completed results)
```

### 2.2. Persistence & Ownership Boundaries (`[P§34]`)

To prevent layer leakage and storage anti-patterns, the ownership boundaries are defined as follows:

| State Domain | Storage Medium | Owner Subsystem | Lifecycle & Retention | Must NOT Own |
|---|---|---|---|---|
| **Project Memory** | Git-tracked markdown & YAML (`.cv_agent/memory/`) | Knowledge & Reasoning (`[P§33]`, `[P§34]`) | Permanent, versioned with git commits | Raw conversation transcripts, numerical experiment sweeps |
| **Experiment Ledger** | SQLite database (`.cv_agent/state/experiments.sqlite`) | Experiment Management (`[P§25]`) | Permanent, append-oriented, non-pruned | Running training jobs, orchestration checkpointing |
| **Orchestration Checkpoints** | SQLite database (`.cv_agent/state/checkpoints.sqlite`) | LangGraph Orchestration (`[P§21]`, ADR-0003) | Runtime persistence, configurable retention, pruneable | Permanent experiment records, domain memory |
| **Conversation Transcripts** | Environment logs / host session state | Host Agent Environment | Session-bound / audit logs | Project memory or experiment records |

**Non-Negotiable Boundary Rules:**
1. **Append-Oriented Immutability & Operational State:** Experiment specification, reproducibility metadata, measurements, artifact lineage, and terminal diagnostics are **immutable once persisted as facts** `[P§25]`. Lifecycle fields required to represent in-flight execution (`status`, `job_id`, and timestamps) may transition only according to the formal state transition matrix (§2.4.B) until the experiment reaches a terminal state (`completed`, `failed`, `cancelled`). Once an experiment reaches a terminal state, the entire record is completely frozen and cannot be updated or overwritten. Corrective or follow-up trials require appending a new experiment row referencing the parent `[P§25]`.
2. **Ownership Separation between SQLite Backends:** `experiments.sqlite` exclusively owns experiment metadata, metrics, and artifact pointers. `checkpoints.sqlite` is owned entirely by the orchestration engine (ADR-0003). Checkpoint data must never silently become the experiment ledger.
3. **Approval Ownership Boundary:** Human approval tickets and records are owned by the orchestration/approval subsystem per `docs/APPROVALS.md` and ADR-0003 (`Q3 / D-015`). The experiment ledger stores `approval_ref` strictly as a foreign reference string, not as the authoritative storage of approval logic or audit trails.
4. **Transcript Boundary:** Conversation and interactive prompt transcript storage is strictly outside ADR-0004's ownership boundary. Project memory must never ingest or store uncurated conversation transcripts `[P§34]`. Insights extracted from user interactions must be synthesized into structured domain records before persistence.
5. **Orchestration Decoupling:** LangGraph `AgentState` holds only string identifiers (`memory_ref: str | None`, `experiment_ref: str | None`) referencing project memory sections or ledger rows, preventing runtime channel bloat.

### 2.3. Project Memory Contract (`.cv_agent/memory/`)

Project memory captures persistent domain knowledge scoped to the current project/workspace root (`Q1 / D-013`). The proposed structure defines five canonical documents:

1. **`project_spec.md` (Domain Problem & Operational Boundaries):**
   - Problem characterization (e.g. security analytics, perimeter breach, industrial defect classification) `[P§1]`, `[P§29.1]`.
   - Physical camera setup: Camera count, mounted positions, angles, field of view, resolutions, stream FPS, indoor/outdoor environmental exposure, lighting variability `[P§1]`.
   - Operational requirements: Required latency ceiling, target recall, false-positive tolerance, multi-task pipelines (detection, tracking, pose, classification).
2. **`hardware_constraints.yaml` (Deployment & Execution Budgets — `[P§29.4]`):**
   - Deployment target platform (e.g. NVIDIA Jetson Orin Nano, AGX Orin, server-grade A100/T4, edge CPU).
   - Resource ceilings: Maximum VRAM allocation, RAM ceiling, thermal/power budget (Watts).
   - Target precision support: FP32, FP16, INT8 (PTQ/QAT).
   - Canonical schema contract (maps directly to `HardwareConstraints` in §5):
     ```yaml
     target_device: "nvidia_jetson_orin_nano_8gb"
     max_vram_mb: 6144
     max_ram_mb: 7168
     power_budget_watts: 15.0
     latency_ceiling_ms: 33.3
     supported_precisions:
       - "FP32"
       - "FP16"
       - "INT8"
     ```
3. **`architectural_choices.md` (Model Selection & Evolution Rationale):**
   - High-level architectural paradigms selected (e.g. YOLO detector family, RT-DETR, ViT backbone).
   - Traceable justifications for architectural transitions, referencing quantitative results in the Experiment Ledger.
4. **`dataset_lineage.md` (Data Governance & Provenance — `[P§26]`):**
   - Dataset manifests, version tags, annotation specifications, train/validation/test split logic, and leakage guards `[P§26]`.
5. **`learned_heuristics.md` (Empirical Engineering Rules of Thumb):**
   - Generalizable project findings distilled from experiments (e.g., *"Augmentation technique X degrades edge detection under nighttime infrared cameras"*, *"Model architecture Y exhibits latency spikes above batch size 4 on target Jetson"*).

*(Note: Existing repository documentation such as `docs/PROJECT.md` remains frozen canon `[P§35]`. The `.cv_agent/memory/` tree is the designated workspace location for active project memory and will be created upon first memory initialization).*

### 2.4. Experiment Ledger Contract (`.cv_agent/state/experiments.sqlite`)

The experiment ledger captures all experimental runs across the CV engineering lifecycle `[P§25]`.

#### A. Experiment Identity, Lineage & Baseline Relational Design
- `exp_id`: Project-scoped primary identifier (e.g., `EXP-YYYYMMDD-NN`) `[D-013]`.
- `parent_exp_id`: Lineage pointer to the parent experiment from which this trial was derived (`str | None`), where `None` signifies a root trial `[P§25]`. Enforced as a self-referencing foreign key (`REFERENCES experiments(exp_id)`).
- `baseline_id`: Lineage reference to the benchmark run against which this experiment's metrics are comparatively evaluated (`str | None`) `[P§29.2]`. Enforced as a self-referencing foreign key (`REFERENCES experiments(exp_id)`).
  - A value of `NULL` explicitly indicates **no predecessor baseline reference** (e.g. for initial exploratory trials or root baseline runs).
  - Non-baseline experiments reference a valid prior baseline experiment via `baseline_id`.
- **Baseline Selection Invariant (`[P§29.2]`):** At most one experiment may be designated as the active baseline for a given workload class. Baseline designation is logically separate from immutable experiment results; designating or promoting a new active baseline updates project-level baseline selection metadata rather than modifying historical, immutable experiment records. Precondition: `set_active_baseline()` may designate only an existing, terminal `completed` experiment whose `workload_type` matches the designated workload class.

#### B. Lifecycle State Transition Matrix
To ensure deterministic execution and prevent illegal state mutations, experiment lifecycle transitions are governed strictly by the following transition matrix:

| Current State | Target State | Allowed? | Preconditions / Side Effects |
|---|---|---|---|
| `proposed` | `queued` | **Yes** | Run accepted by orchestrator; parameters validated. |
| `proposed` | `running` | **Yes** | Direct execution initiation without queuing. |
| `proposed` | `cancelled` | **Yes** | Cancelled before submission; reason recorded. |
| `proposed` | `failed` | **Yes** | Pre-flight validation or configuration error. |
| `queued` | `running` | **Yes** | Job picked up by execution target (`job_id` bound). |
| `queued` | `cancelled` | **Yes** | Cancelled while in queue; reason recorded. |
| `queued` | `failed` | **Yes** | Dispatch or cluster submission failure. |
| `running` | `completed` | **Yes** | Execution succeeded; metrics & artifacts atomically written; **terminal & immutable**. |
| `running` | `failed` | **Yes** | Execution crashed; `error_message` written; **terminal & immutable**. |
| `running` | `cancelled` | **Yes** | Operator/agent abort; cancellation reason written; **terminal & immutable**. |
| `completed` | *Any* | **No** | **Illegal:** Completed experiments are strictly immutable `[P§25]`. |
| `failed` | *Any* | **No** | **Illegal:** Failed experiments are strictly immutable. |
| `cancelled` | *Any* | **No** | **Illegal:** Cancelled experiments are strictly immutable. |

#### C. Workload-Specific Metric Completeness (`[P§12]`, `[P§29.4]`)
Metric completeness requirements are consistently defined across all four workload types, preserving `[P§29.4]` ("Hardware-aware: accuracy without deployability is incomplete") as the governing standard:

| Workload Type | Description | Required Completion Evidence |
|---|---|---|
| `training` | Model training & fine-tuning | Validation accuracy metrics (`map_50`, `map_50_95`, precision, recall) + training resource metrics (`gpu_hours`, `training_time_seconds`, `peak_vram_mb`). |
| `evaluation` | Standalone task evaluation | Task-specific accuracy metrics (precision, recall, mAP, per-class metrics); on-target system metrics when evaluating deployability `[P§12]`. |
| `optimization` | Quantization (PTQ/QAT), pruning, TensorRT compilation | Post-optimization accuracy/tolerance metrics + on-target system metrics (`latency_inference_ms`, `latency_e2e_ms`, `throughput_fps`, `peak_vram_mb`, `power_draw_watts`). |
| `benchmark` | Target hardware profiling | On-target benchmark/system metrics under production stream conditions (`throughput_fps`, `latency_inference_ms`, `latency_e2e_ms`, `peak_vram_mb`, `power_draw_watts`) + relevant accuracy verification. |

**Completeness Rule:** Any model selection claim, deployment recommendation, or optimization sign-off that lacks verified on-target system metrics under production stream conditions is **void and inadmissible** `[P§29.4]`.

#### D. Execution & Governance Metadata
- `status`: Execution state (`proposed`, `queued`, `running`, `completed`, `failed`, `cancelled`).
- `workload_type`: Type of experiment (`training`, `evaluation`, `optimization`, `benchmark`).
- `target`: Execution environment (`local_gpu`, `remote_gpu`, `cloud`) per `Q2 / D-014`.
- `job_id`: External out-of-process execution job handle.
- `approval_ref`: Persistent approval record ID if the experiment exceeded cost/resource thresholds in `docs/APPROVALS.md` (`Q3 / D-015`).
- `created_at`, `started_at`, `completed_at`: Standard ISO-8601 UTC timestamps.
- `error_message`: Diagnostic failure reason or cancellation explanation (`str | None`).

#### E. Reproducibility Metadata (`[P§25]`, `[P§29.5]`)
- `commit_sha`: Git commit hash of the codebase at execution time.
- `config_uri` / `config_hash`: Path and content hash of the training/evaluation configuration.
- `dataset_version`: Manifest ID or version hash from dataset governance `[P§26]`.
- `seed`: Random seed for reproducible weight initialization and batch sampling.

#### F. Hyperparameters & Architecture Specification
- Model family, architecture variant, and initial weights origin.
- Input image resolution (width, height), training batch size.
- Optimizer, initial learning rate, weight decay, learning rate scheduler.
- Augmentations pipeline configuration reference.
- Target precision: `FP32`, `FP16`, `INT8` (PTQ or QAT).

#### G. Artifact Lineage & Diagnostics
- `weights_uri`: Path or content-addressed storage URI of trained weights.
- `engine_uri`: Path or content-addressed storage URI of exported TensorRT/ONNX deployment engine.
- `failure_analysis_ref`: Reference or URI to diagnostic error analysis `[P§27]`.
- `notes`: Engineering rationale and follow-up directives.

## 3. SQLite Schema Architectural Invariants

ADR-0004 defines architectural schema invariants while explicitly deferring implementation choices (such as ORM selection, migration tooling, and physical index tuning) to Phase 2.

### Mandatory Schema Requirements:
1. **Isolation & Scoping:** Database file resides at `.cv_agent/state/experiments.sqlite` relative to workspace root (`D-013`, `D-018`).
2. **Relational Table Division:**
   - `experiments`: Core execution record (identity, status, workload type, timestamps, commit, dataset version, seed, approval ref, lineage, error message).
   - `hyperparameters`: Structured model configuration and training hyperparameter parameters.
   - `accuracy_metrics`: Validation and test accuracy measurements.
   - `system_metrics`: Hardware performance, throughput, resource consumption, and power measurements.
   - `artifacts`: Output weights, exported deployment engines, and diagnostic reports with URIs and integrity hashes.
   - `active_baselines`: Workload-to-experiment active baseline mapping table preserving experiment immutability while allowing project baseline selection to evolve. `active_baselines` contains at most one row per `workload_type`; its referenced `exp_id` must resolve to an existing, terminal `completed` experiment in `experiments(exp_id)` whose `workload_type` matches.
3. **Foreign Key Integrity:** Enforce foreign key constraints linking child metric, hyperparameter, and artifact tables to `experiments(exp_id)`. Enforce self-referential lineage:
   - `parent_exp_id REFERENCES experiments(exp_id) ON DELETE RESTRICT` (nullable).
   - `baseline_id REFERENCES experiments(exp_id) ON DELETE RESTRICT` (nullable).
4. **Concurrency & Atomicity:** SQLite Write-Ahead Logging (`PRAGMA journal_mode=WAL;`) enabled. Terminal completion of an experiment (writing metrics, updating status to `completed`) must execute within a single atomic database transaction.

### Explicitly Deferred Implementation Choices (Phase 2):
- Database access layer: Python standard library `sqlite3`, `aiosqlite`, or lightweight ORM/Query builder.
- Migration management: Schema version pragma vs Alembic.
- Specific database index configurations beyond primary/foreign keys.

## 4. Alternatives Considered

| Alternative | Evidence For | Evidence Against | Why Not Chosen |
|---|---|---|---|
| **1. Flat Markdown/Git Only for All Persistence** | Completely transparent in Git; human-readable diffs; zero database dependencies. | Fails completely at scale (`[P§25]`, `D-018`): large-scale sweeps generate thousands of rows, inflating Git tree; querying, sorting, and automated baseline comparison require custom text parsing. | Violates `D-018` dual-layer persistence decision. |
| **2. Single Consolidated SQLite Database (`checkpoints.sqlite` + `experiments.sqlite`)** | Single database file under `.cv_agent/state/`. | Violates separation of concerns (`[P§34]`); couples ephemeral LangGraph serialization (pickle/msgpack channels) with long-term immutable experiment schemas; makes pruning runtime checkpoints risky for permanent experiment retention. | Violates ADR-0003 §3.1 boundary reconciliation. |
| **3. External Hosted Experiment Tracking Service Only (e.g. W&B, MLflow, Comet)** | Rich web UI; real-time dashboarding; collaborative sharing. | Requires external network connectivity and accounts; breaks local-first, self-contained reproducibility requirement `[P§25]`; adds vendor lock-in. | Violates local-first workstation agent architecture (`D-014`). |

## 5. Interface Specifications (Architecture Stubs)

The following type contracts define the interaction boundary for `cv_agent.memory` and `cv_agent.experiments`. Implementation bodies are deferred to Phase 2.

```python
# module: cv_agent.memory.interfaces

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HardwareConstraints:
    """Hardware resource budgets and deployment target specifications [P§29.4]."""

    target_device: str
    max_vram_mb: int
    max_ram_mb: int
    power_budget_watts: float | None
    latency_ceiling_ms: float | None
    supported_precisions: tuple[str, ...]


@dataclass(frozen=True)
class ArchitecturalDecision:
    """Documented high-level model or pipeline architecture decision."""

    decision_id: str
    timestamp: str
    title: str
    rationale: str
    linked_experiment_ids: tuple[str, ...]


@dataclass(frozen=True)
class LearnedHeuristic:
    """Empirical engineering rule of thumb discovered during project execution."""

    heuristic_id: str
    context: str
    observation: str
    recommendation: str
    evidence_experiment_ids: tuple[str, ...]


class ProjectMemoryReader(Protocol):
    """Read-only interface to Git-tracked structured project memory [P§33]."""

    def get_hardware_constraints(self) -> HardwareConstraints:
        """Retrieve target deployment hardware budgets."""
        ...

    def get_architectural_decisions(self) -> tuple[ArchitecturalDecision, ...]:
        """Retrieve documented architectural decisions and justifications."""
        ...

    def get_learned_heuristics(self) -> tuple[LearnedHeuristic, ...]:
        """Retrieve empirical engineering heuristics."""
        ...


class ProjectMemoryWriter(Protocol):
    """Controlled mutation interface for Git-tracked structured project memory."""

    def update_hardware_constraints(self, constraints: HardwareConstraints) -> None:
        """Record or update hardware resource budgets."""
        ...

    def record_architectural_decision(self, decision: ArchitecturalDecision) -> None:
        """Append an architectural decision with justification and experiment links."""
        ...

    def append_learned_heuristic(self, heuristic: LearnedHeuristic) -> None:
        """Append a newly validated empirical engineering heuristic."""
        ...
```

```python
# module: cv_agent.experiments.interfaces

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol


class ExperimentStatus(str, Enum):
    """Lifecycle status of an experiment run [P§25]."""

    PROPOSED = "proposed"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkloadType(str, Enum):
    """Category of experimental workload."""

    TRAINING = "training"
    EVALUATION = "evaluation"
    OPTIMIZATION = "optimization"
    BENCHMARK = "benchmark"


@dataclass(frozen=True)
class AccuracyMetrics:
    """Standardized computer-vision evaluation accuracy metrics [P§12]."""

    map_50: float | None = None
    map_50_95: float | None = None
    precision: float | None = None
    recall: float | None = None
    f1_score: float | None = None
    additional_metrics: Mapping[str, float] | None = None


@dataclass(frozen=True)
class SystemMetrics:
    """Hardware and execution system performance metrics [P§12], [P§29.4]."""

    peak_vram_mb: float
    peak_ram_mb: float
    gpu_hours: float | None = None
    training_time_seconds: float | None = None
    latency_e2e_ms: float | None = None
    latency_inference_ms: float | None = None
    throughput_fps: float | None = None
    power_draw_watts: float | None = None


@dataclass(frozen=True)
class ArtifactManifest:
    """Output artifacts generated by an experiment run."""

    weights_uri: str | None = None
    engine_uri: str | None = None
    plots_uri: str | None = None
    failure_analysis_ref: str | None = None
    checksums: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ExperimentSpec:
    """Specification required to initialize an experiment run [P§25]."""

    name: str
    hypothesis: str
    workload_type: WorkloadType
    model_name: str
    model_variant: str
    dataset_version: str
    batch_size: int
    input_resolution: tuple[int, int]
    precision: str
    parent_exp_id: str | None = None
    baseline_id: str | None = None
    approval_ref: str | None = None
    hyperparameters: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ExperimentRecord:
    """Experiment record representing an append-oriented entry; terminal results are immutable."""

    exp_id: str
    spec: ExperimentSpec
    status: ExperimentStatus
    created_at: str
    started_at: str | None
    completed_at: str | None
    commit_sha: str
    job_id: str | None
    accuracy_metrics: AccuracyMetrics | None
    system_metrics: SystemMetrics | None
    artifacts: ArtifactManifest | None
    error_message: str | None
    notes: str | None


class ExperimentLedger(Protocol):
    """Append-oriented queryable interface to .cv_agent/state/experiments.sqlite."""

    def create_experiment(
        self, spec: ExperimentSpec, commit_sha: str
    ) -> ExperimentRecord:
        """Create a new experiment run in PROPOSED status."""
        ...

    def transition_status(
        self,
        exp_id: str,
        new_status: ExperimentStatus,
        job_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Transition experiment lifecycle status per the transition matrix."""
        ...

    def complete_experiment(
        self,
        exp_id: str,
        accuracy: AccuracyMetrics,
        system: SystemMetrics,
        artifacts: ArtifactManifest,
        notes: str | None = None,
    ) -> ExperimentRecord:
        """Atomically record results and mark experiment as COMPLETED (immutable thereafter)."""
        ...

    def fail_experiment(self, exp_id: str, error_message: str) -> None:
        """Mark an experiment as FAILED with diagnostic error message (immutable thereafter)."""
        ...

    def cancel_experiment(self, exp_id: str, reason: str) -> None:
        """Mark an experiment as CANCELLED with explanation (immutable thereafter)."""
        ...

    def get_experiment(self, exp_id: str) -> ExperimentRecord | None:
        """Retrieve a single experiment record by ID."""
        ...

    def get_baseline(
        self, workload_type: WorkloadType | None = None
    ) -> ExperimentRecord | None:
        """Retrieve the designated active baseline record for the specified workload [P§29.2]."""
        ...

    def set_active_baseline(self, exp_id: str, workload_type: WorkloadType) -> None:
        """Designate an experiment as the active baseline for a workload class.

        Precondition: exp_id must reference an existing completed experiment
        whose workload_type matches the designated workload class.
        """
        ...

    def list_experiments(
        self,
        status: ExperimentStatus | None = None,
        workload_type: WorkloadType | None = None,
        model_name: str | None = None,
        dataset_version: str | None = None,
    ) -> tuple[ExperimentRecord, ...]:
        """Query experiment runs matching specified filter criteria."""
        ...
```

## 6. Acceptance Criteria

Any Phase 2 implementation of ADR-0004 must satisfy the following acceptance criteria:

1. **Dual-Layer Workspace Scoping:** Project memory files are created under `<workspace_root>/.cv_agent/memory/` and the experiment ledger database is initialized at `<workspace_root>/.cv_agent/state/experiments.sqlite` (`Q1 / D-013`, `Q8 / D-018`).
2. **Terminal Result Immutability:** Once an experiment reaches a terminal status (`completed`, `failed`, `cancelled`), attempting to update its hyperparameters, accuracy metrics, system metrics, artifact pointers, or diagnostic error messages raises an immutability exception.
3. **Lifecycle Transition Matrix Enforcement:** Status transitions strictly adhere to the transition matrix defined in §2.4.B. Any illegal transition raises an invalid state transition exception.
4. **Relational Baseline Foreign Key Integrity:** `baseline_id` is a nullable foreign key referencing `experiments(exp_id)`. Non-null values must resolve to an existing experiment; root baselines store `NULL` indicating no predecessor baseline reference (`[P§29.2]`).
5. **Workload-Aware Metric Completeness:** Experiment completion evidence adheres strictly to §2.4.C: `training` workloads mandate validation accuracy and training resource metrics; `optimization` and `benchmark` workloads mandate on-target system metrics; `evaluation` workloads mandate task accuracy metrics and deployment system metrics when evaluating deployability (`[P§12]`, `[P§29.4]`).
6. **Reproducibility Metadata Completeness:** Every experiment record contains a valid Git commit SHA, dataset version reference (`[P§26]`), input resolution, and hyperparameter payload (`[P§25]`).
7. **Lineage Foreign Key Integrity:** `parent_exp_id` is a nullable foreign key referencing `experiments(exp_id)`.
8. **Decoupled Approval Reference:** When an experiment requires approval per `docs/APPROVALS.md`, `approval_ref` stores the ticket string without duplicating approval engine state (`Q3 / D-015`).
9. **WAL Concurrency & Crash Safety:** `experiments.sqlite` operates with `PRAGMA journal_mode=WAL;` and transaction boundaries ensure no partial experiment metric rows are committed on process interruption.
10. **Orchestration Separation:** `checkpoints.sqlite` operations do not mutate or query `experiments.sqlite`, and pruning `checkpoints.sqlite` does not affect any record in `experiments.sqlite` (ADR-0003 §3.1).
11. **Project Memory Markdown Integrity:** Project memory files are human-readable markdown/YAML, suitable for Git commits, diffs, and code reviews.
12. **Boundary Compliance:** Neither Project Memory nor the Experiment Ledger stores uncurated chat/session transcripts (`[P§34]`).

## 7. Consequences

- **Makes easier:**
  - Answering *"Why did we choose this model?"* quantitatively from verified historical records `[P§25]`;
  - Filtering, comparing, and benchmarking model candidates across hardware configurations;
  - Decoupling long-term domain knowledge from short-lived orchestration graph executions;
  - Preserving clean Git diffs for high-level decisions while storing high-volume metric sweeps in SQLite.
- **Makes harder:**
  - Logging an experiment requires all reproducibility metadata to be assembled up front (no "quick runs without config logging");
  - Developers and agents must maintain structured domain memory documents alongside code modifications.
- **Neutral:**
  - Introduces a second SQLite file under `.cv_agent/state/`, which is cleanly separated by lifecycle and access pattern.

## 8. Dependencies and Unresolved Questions

- **Settled Dependencies:**
  - `Q1 / D-013`: Project-scoped isolation.
  - `Q2 / D-014`: External job execution tracking (`job_id`).
  - `Q3 / D-015`: Persistent approval reference (`approval_ref`).
  - `Q5 / D-022`: NVIDIA capability inventory and unavailable state reporting.
  - `Q8 / D-018`: Dual-layer persistence foundation.
  - `Q10 / D-028 / ADR-0009`: Manifest-first content-addressed dataset storage; `dataset_version` in `ExperimentSpec` binds to ADR-0009 `DatasetManifest` identities.
  - `ADR-0003 / D-020`: Runtime checkpoint separation in `checkpoints.sqlite`.
- **Open Questions:**
  - None. All architectural dependencies for ADR-0004 are resolved and frozen.
