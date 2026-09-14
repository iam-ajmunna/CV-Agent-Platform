# ADR-0011: Evaluation Subsystem Architecture

- **Status:** Amended — Pending Second Architecture Review
- **Date:** 2026-09-09
- **Last Reviewed:** 2026-09-15
- **Layer:** evaluation / empirical measurement
- **Canon:** `[P§7]`, `[P§12]`, `[P§13]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§27]`, `[P§29.2]`, `[P§29.3]`, `[P§29.4]`, `[P§29.10]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #6; remediation gate #58
- **Preceded by Decisions:** D-030 (ADR-0010 Training Subsystem), D-029 (ADR-0009 Dataset Subsystem), D-026 (ADR-0008 Reasoning Subsystem), D-025 (ADR-0007 Skills), D-023 (ADR-0005 Tools & MCP Boundary), D-021 (ADR-0004 Experiment Ledger), D-020 (ADR-0003 Orchestration & Approval), D-014 (Out-of-process GPU compute), D-022 (NVIDIA hardware inventory)

> **Architecture gate:** This amendment is documentation-only. No runtime implementation in `cv_agent/evaluation/` is authorized until Issue #58 receives a second Official PM review with an APPROVED/FROZEN disposition.

## 1. Context

In empirical computer vision engineering, the agent must be measurement-driven. Evaluation must prevent false confidence, distinguish model quality from deployability, and provide reproducible evidence for every performance claim.

The Evaluation Subsystem therefore provides deterministic empirical measurement while remaining strictly separated from cognitive reasoning, workflow orchestration, dataset authority, training, and physical execution transport.

The principal risks addressed by this ADR are:
1. **Single-metric evaluation:** accuracy without hardware performance can hide an undeployable model.
2. **Subjective evaluation:** LLM narrative cannot substitute for quantitative measurement.
3. **Baseline-free claims:** improvement claims require comparison against an authoritative, comparable baseline.
4. **Shallow diagnostics:** aggregate metrics alone do not identify physical failure modes.
5. **Lineage loss:** evaluation against unversioned datasets or transient model weights is non-reproducible.
6. **Hardware misrepresentation:** target latency and throughput must be measured on the declared execution target, not a developer workstation.
7. **Authority duplication:** Evaluation must not create a second approval, operation-identity, execution-lifecycle, lease/fencing, or recovery system alongside ADR-0003.

## 2. Responsibility and Authority Boundaries

### 2.1 Evaluation owns

- Immutable evaluation-domain specification (`EvaluationPlan`).
- Dataset and model binding validation using authoritative ADR-0009 and ADR-0010 references.
- Versioned task-specific metric definitions and deterministic metric calculation semantics.
- Systems benchmark configuration and result semantics.
- Baseline comparison and statistical-comparison semantics.
- Structured quantitative error analysis.
- Reproducibility environment metadata.
- Evaluation result/report construction and serialization.
- Translation of evaluation semantics into ADR-0005 `ToolInvocation` inputs.

### 2.2 Evaluation does NOT own

- **ADR-0001:** capability taxonomy, stable capability/tool identities, or tool catalog authority.
- **ADR-0002:** LLM inference, provider selection, prompts, provider SDK types, or model execution.
- **ADR-0003:** operation identity authority, canonical operation payload hashing authority, approval authority, workflow lifecycle, leases, fencing, checkpoint persistence, external-operation state, recovery/reconciliation, or authoritative execution lineage.
- **ADR-0004:** authoritative experiment-ledger persistence in `experiments.sqlite` or baseline-record authority.
- **ADR-0005:** subprocess/SSH/MCP transport, physical process management, target registry authority, or credential handling.
- **ADR-0006:** knowledge/RAG retrieval and provenance authority.
- **ADR-0007:** skill discovery, skill lifecycle, or capability-resolution authority.
- **ADR-0008:** hypothesis formulation, causal diagnosis, or next-strategy selection.
- **ADR-0009:** dataset manifest schema authority, payload storage, split leakage verification, or dataset identity authority.
- **ADR-0010:** training, optimization of training parameters, checkpoint creation, or checkpoint identity authority.
- Future ADR-0012/0013: model optimization/deployment transformation and live production monitoring respectively.

No Evaluation contract may imply authority over any of the above.

## 3. Decision

The Evaluation Subsystem is a stateless domain-measurement layer around immutable evaluation specifications. It consumes authoritative identities and evidence from prior subsystems, performs deterministic calculations, delegates physical execution through ADR-0005, and emits immutable results for ADR-0004 persistence and ADR-0008 reasoning consumption.

```text
ADR-0008 Reasoning
  evaluation intent / hypothesis / baseline intent
             │
             ▼
      EvaluationPlan
             │
             │ authoritative validation + ADR-0003 approval context
             ▼
      EvaluationRunner
             │
             ├──────────────► MetricCalculator
             │
             ├──────────────► BenchmarkRunner
             │
             └──────────────► ErrorAnalyzer
             │
             ▼
      ADR-0005 ToolExecutor
             │
             ▼
   LOCAL / REMOTE / MCP target
             │
             ▼
      empirical outputs
             │
             ▼
      EvaluationResult
        │             │
        ▼             ▼
 ADR-0004 Ledger   ADR-0008 Reasoning
```

### 3.1 Reasoning boundary: hypothesis versus measurement

ADR-0008 may propose an evaluation intent and hypothesis. ADR-0011 turns that intent into a rigorous `EvaluationPlan`, executes/analyses measurements, and reports empirical results. Reasoning may diagnose the returned evidence and propose subsequent strategies, but cannot alter, invent, or override measured values.

### 3.2 Model-quality and systems-performance dimensions

Evaluation treats model quality and systems performance as distinct, co-equal dimensions.

**Model quality** may include task-specific measures such as mAP, precision, recall, F1, IoU, AUROC, MOTA, MOTP, IDF1, or HOTA.

**Systems performance** may include latency percentiles, throughput, VRAM/RAM consumption, CPU/GPU utilization, power, and thermal behavior.

A deployability recommendation must consider both dimensions when a systems benchmark is required by the evaluation plan. Missing required on-target system evidence makes a deployability claim inadmissible.

### 3.3 Dataset binding and verification

`EvaluationDatasetBinding` consumes an authoritative ADR-0009 `DatasetId`, `DatasetVersion`, manifest/root checksum, and split identifier. Evaluation must verify rather than merely trust these values:

1. Resolve the referenced manifest through the ADR-0009 authority.
2. Verify manifest identity (`dataset_id`, canonical version) and exact `payload_root_checksum`.
3. Verify the requested split exists and its membership is authoritative.
4. Reject unversioned directories, caller-supplied substitute manifests, checksum mismatches, traversal, or split leakage.
5. `calibration` is not a normal evaluation split. It is rejected unless the plan explicitly declares a calibration/PTQ validation purpose governed by the relevant future optimization contract.

Evaluation does not implement a second dataset-manifest authority.

### 3.4 Model artifact binding and verification

`ModelArtifactReference` consumes an authoritative ADR-0010 checkpoint reference. Evaluation must verify:

1. The checkpoint identity exists in the authoritative checkpoint/experiment lineage.
2. The resolved checkpoint manifest identifies the same `CheckpointId`.
3. The resolved content hash exactly matches the authoritative `weights_hash`.
4. The resolved artifact URI is permitted by the storage/execution boundary and uses an allowed explicit scheme (`file://`, `s3://`, or `gcs://`); no arbitrary local path or inferred URI is accepted.
5. Architecture, precision, and input-resolution metadata are consistent with the authoritative checkpoint metadata.

Transient in-memory weights and independently invented hash/URI pairs are not valid evaluation artifacts.

### 3.5 ADR-0003 operation identity and approval authority

ADR-0003 is the sole authority for operation identity, canonical payload hashing, approval, lifecycle, leases/fencing, and recovery.

`EvaluationRunner.execute()` therefore consumes external authoritative values:

- `operation_identity: OperationIdentity`
- `approval_request: ApprovalRequest`
- `approval_decision: ApprovalDecision`
- `tool_id: ToolId`
- `tool_executor: ToolExecutor`

Evaluation may construct only its **functional operation payload** through:

`extract_evaluation_operation_payload(plan: EvaluationPlan) -> Mapping[str, Any]`

The payload is passed to ADR-0003 `compute_payload_hash()`. Evaluation MUST NOT synthesize operation IDs, operation versions, target identities, canonical payload hashes, approval IDs/tokens, approval status, policy versions, or lease/fencing state.

Before dispatch, the runner verifies that the externally supplied operation identity and approval evidence agree on operation ID, operation version, canonical payload hash, target/resource scope, request identity, and approval decision. Any mismatch fails closed.

Transient execution tokens such as `execution_id`, lease identifiers, attempt identifiers, and transport job handles are excluded from the functional payload hash preimage and are never treated as operation identity.

### 3.6 No competing execution lifecycle

ADR-0011 owns no execution lifecycle state machine. The former `EvaluationStatus` and `EvaluationExecution.status` concepts are removed.

ADR-0003 exclusively owns persisted lifecycle state, including running/terminal states, leases, fencing, external-operation state, recovery, reconciliation, checkpointing, and authoritative state transitions.

Evaluation may expose pure domain semantics such as `EvaluationVerdict`:

- `PASSED`
- `FAILED_QUALITY`
- `FAILED_SYSTEMS`
- `INCONCLUSIVE`

`EvaluationVerdict` is a measurement finding, not a workflow state, and MUST NOT be persisted as an alternative orchestration lifecycle.

### 3.7 Target selection and no local target synthesis

`EvaluationPlan` contains no `compute_target_id` and no synthetic default target.

The execution target is selected only from:
1. an authoritative `target:<id>` resource scope supplied by ADR-0003 approval; or
2. an explicit `TargetId` supplied by the caller and verified to be within the approved resource scope.

Optional hardware requirements may constrain candidate selection:

- `required_hardware_class`
- `min_cuda_compute_capability`
- other declarative hardware requirements defined by the target contract.

If no authorized compatible target exists, execution fails closed. Evaluation never creates, invents, or silently substitutes a target.

### 3.8 ADR-0005 execution and asynchronous handoff

Evaluation owns evaluation semantics, not physical transport. All execution is dispatched through ADR-0005 `ToolExecutor` using an authoritative `ToolId` and `TargetId`.

For long-running evaluation jobs, ADR-0005 may return an `ActiveJobHandoff`. The handoff is linked to the authoritative ADR-0003 external-operation identity and job lineage. ADR-0003 owns asynchronous polling, lease renewal/fencing, external-operation state, recovery/reconciliation, and terminal workflow transitions.

`ActiveJobHandoff.is_expired()` is a handoff/TTL condition, **not** evidence that the evaluation failed or was never submitted. Evaluation must not create a second UNKNOWN/recovery state machine.

A stateless `parse_evaluation_result()` function may parse a successful authoritative tool result into immutable domain records. Parsing cannot mutate orchestration state or declare an external operation complete independently of ADR-0003.

### 3.9 Baseline comparison and statistical significance

Every non-root evaluation MUST identify an existing completed ADR-0004 baseline experiment with a comparable evaluation record. A root baseline may explicitly use `baseline_experiment_id = None` only when ADR-0004 has designated that experiment as the active baseline.

Comparability requires, at minimum:

- identical authoritative dataset manifest identity and evaluated split;
- compatible metric definition IDs/versions and parameters;
- compatible task semantics;
- compatible model-output semantics;
- compatible benchmark target/configuration when comparing systems metrics;
- valid completed baseline evidence.

`BaselineComparisonResult` records deterministic deltas and statistical evidence. Percentage change is defined only when the baseline denominator is non-zero; otherwise the percentage field is unavailable rather than fabricated.

Supported statistical methods are explicitly closed:

- `PAIRED_T_TEST`
- `WILCOXON_SIGNED_RANK`
- `BOOTSTRAP_RESAMPLING`
- `MCNEMAR_TEST`

Method selection is metric/data-type specific and recorded with its version and parameters. The implementation must define deterministic seeds/resampling rules where stochastic resampling is used. Statistical significance requires paired/per-observation evidence appropriate to the selected test; an aggregate scalar alone is insufficient.

For every significance-bearing comparison, the result records:

- `test_type`
- `test_version`
- `sample_count`
- `p_value`
- `confidence_level`
- confidence interval where applicable
- `statistically_significant`
- metric delta(s)
- comparable baseline/candidate experiment identities
- the evidence identity used for the comparison.

`confidence_level` is strictly between 0 and 1 and `p_value` is within `[0, 1]`. Invalid, missing, incomplete, or incomparable baseline evidence fails closed. A summary string cannot establish statistical significance.

### 3.10 Structured error analysis

`ErrorAnalysisRecord` is quantitative and evidence-based. It may include false-positive/false-negative content hashes, localization/classification error counts, confusion matrices, slice-level performance, and representative failure evidence. It is consumed by ADR-0008 as evidence, not as a free-form narrative authority.

### 3.11 Reproducibility and canonicalization

Evaluation contracts use recursive defensive immutability aligned with the project's existing canonicalization discipline:

- nested mappings are recursively converted to immutable mapping proxies;
- lists/tuples become immutable tuples;
- sets become frozensets;
- arbitrary mutable nested objects are rejected or normalized before contract publication.

Canonical functional payload serialization uses the established canonicalization rules:

- UTF-8 JSON representation;
- Unicode NFC normalization for strings;
- deterministic Unicode-code-point key ordering;
- `None` represented as JSON `null`;
- finite numeric values only;
- `-0.0` normalized to `0.0`;
- shortest round-trip floating representation (David Gay/IEEE-754-compatible shortest representation);
- deterministic separators and no non-finite JSON values.

The resulting functional payload is hashed only by ADR-0003 `compute_payload_hash()`. ADR-0011 does not create a competing operation-identity hash algorithm.

### 3.12 Metric-result validity

All externally produced numeric results are fail-closed:

- every float must satisfy `math.isfinite()`;
- probability/rate metrics such as mAP, precision, recall, and F1 are within `[0.0, 1.0]`;
- confidence interval bounds satisfy `0.0 <= lower <= upper <= 1.0` where the metric is probability/rate bounded;
- counts are non-negative integers;
- per-class values use the same metric bounds as their metric definition;
- unsupported metric ranges must be declared explicitly by the metric definition rather than silently accepting arbitrary values.

`MetricResult` validation is metric-definition-aware. Metrics whose mathematical domains differ from `[0,1]` are validated against their declared domain rather than incorrectly clipped.

### 3.13 Systems benchmark validity

`BenchmarkResult` must satisfy:

`0 <= min <= p50 <= p90 <= p95 <= p99 <= max`

and all latency values are finite and non-negative.

Throughput is finite and strictly positive. Memory, utilization, and power values are finite and non-negative; utilization percentages cannot exceed their declared physical domain. Device/target identity is mandatory for systems measurements.

Benchmark configuration enforces minimum warmup and measured-iteration counts, explicit batch sizes, precision mode, and target identity. Hardware-synchronized timing is required for asynchronous accelerator measurements.

### 3.14 Confusion-matrix validity

For a confusion matrix with `K` class labels:

- the matrix must be exactly `K x K`;
- all entries must be non-negative integer counts;
- an empty matrix is valid only when the task/metric definition explicitly permits zero classes;
- mismatched matrix dimensions fail closed.

### 3.15 Result and report semantics

`EvaluationResult` is immutable and contains:

- evaluation identity and plan;
- validated quality metrics;
- validated system benchmarks when required;
- baseline comparison when applicable;
- structured error analysis when produced;
- immutable reproducibility environment;
- `EvaluationVerdict`;
- completion timestamp supplied by the authoritative result-processing boundary.

It is an empirical result object, not an orchestration record. Authoritative persistence remains ADR-0004.

## 4. Proposed Interface Contracts — Phase 2 Target

These interfaces are implementation targets only. They are not runtime code and do not authorize implementation before Issue #58 approval.

```python
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, NewType, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from cv_agent.dataset import OperationIdentity
from cv_agent.orchestration import ApprovalDecision, ApprovalRequest
from cv_agent.capabilities import CapabilityId, ToolId
from cv_agent.dataset.types import ContentHash, DatasetId, DatasetVersion
from cv_agent.tools import ExecutionId, TargetId, ToolExecutor
from cv_agent.training.types import CheckpointId, TrainingPrecision

EvaluationId = NewType("EvaluationId", str)
MetricId = NewType("MetricId", str)
BenchmarkId = NewType("BenchmarkId", str)
ExperimentId = NewType("ExperimentId", str)


class TaskType(str, Enum):
    CLASSIFICATION = "classification"
    DETECTION = "detection"
    SEGMENTATION = "segmentation"
    TRACKING = "tracking"
    REPRESENTATION = "representation"


class HardwareClass(str, Enum):
    CPU = "cpu"
    NVIDIA_GPU = "nvidia_gpu"
    JETSON = "jetson"
    OTHER_ACCELERATOR = "other_accelerator"


class EvaluationVerdict(str, Enum):
    PASSED = "passed"
    FAILED_QUALITY = "failed_quality"
    FAILED_SYSTEMS = "failed_systems"
    INCONCLUSIVE = "inconclusive"


class StatisticalTestType(str, Enum):
    PAIRED_T_TEST = "paired_t_test"
    WILCOXON_SIGNED_RANK = "wilcoxon_signed_rank"
    BOOTSTRAP_RESAMPLING = "bootstrap_resampling"
    MCNEMAR_TEST = "mcnemar_test"


class EvaluationDatasetBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: DatasetId
    dataset_version: DatasetVersion
    dataset_manifest_hash: ContentHash
    split_name: str
    slice_filters: Mapping[str, Any] = Field(default_factory=dict)


class ModelArtifactReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_id: CheckpointId
    architecture_name: str
    weights_hash: ContentHash
    weights_uri: str
    precision: TrainingPrecision
    input_resolution: tuple[int, int]


class EvaluationTargetRequirements(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    required_hardware_class: HardwareClass | None = None
    min_cuda_compute_capability: tuple[int, int] | None = None


class MetricDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_id: MetricId
    name: str
    task_type: TaskType
    version: str
    higher_is_better: bool = True
    parameters: Mapping[str, Any] = Field(default_factory=dict)


class MetricResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: MetricDefinition
    primary_value: float
    per_class_values: Mapping[str, float] = Field(default_factory=dict)
    confidence_interval: tuple[float, float] | None = None
    sample_count: int = Field(ge=0)
    metadata: Mapping[str, Any] = Field(default_factory=dict)


class BenchmarkConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark_id: BenchmarkId
    batch_sizes: tuple[int, ...]
    warmup_iterations: int = Field(ge=5)
    measured_iterations: int = Field(ge=20)
    precision: TrainingPrecision
    measure_power: bool = False
    measure_memory: bool = True
    target_requirements: EvaluationTargetRequirements


class BenchmarkResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark_id: BenchmarkId
    batch_size: int
    precision: TrainingPrecision
    latency_min_ms: float
    latency_mean_ms: float
    latency_p50_ms: float
    latency_p90_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_max_ms: float
    throughput_fps: float
    peak_vram_gb: float
    cpu_utilization_pct: float
    gpu_utilization_pct: float
    power_watts_avg: float | None
    device_name: str
    target_id: TargetId


class ErrorAnalysisRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

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


class BaselineComparisonResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

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


class EvaluationEnvironmentRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    git_commit_sha: str
    git_is_dirty: bool
    dataset_manifest_hash: ContentHash
    weights_hash: ContentHash
    metric_definition_versions: tuple[str, ...]
    python_version: str
    torch_version: str
    cuda_version: str | None
    driver_version: str | None
    target_hardware: str
    seed: int | None


class EvaluationPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: EvaluationId
    experiment_id: ExperimentId
    created_at: datetime
    capability_id: CapabilityId
    dataset_binding: EvaluationDatasetBinding
    model_artifact: ModelArtifactReference
    task_type: TaskType
    metrics: tuple[MetricDefinition, ...]
    benchmark_config: BenchmarkConfiguration | None
    baseline_experiment_id: ExperimentId | None
    target_requirements: EvaluationTargetRequirements
    evaluation_requirements: Mapping[str, Any] = Field(default_factory=dict)
    reproducibility_requirements: Mapping[str, Any] = Field(default_factory=dict)


class EvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: EvaluationId
    plan: EvaluationPlan
    verdict: EvaluationVerdict
    quality_metrics: tuple[MetricResult, ...]
    system_benchmarks: tuple[BenchmarkResult, ...] = Field(default_factory=tuple)
    baseline_comparison: BaselineComparisonResult | None
    error_analysis: ErrorAnalysisRecord | None
    environment: EvaluationEnvironmentRecord
    completed_at: datetime


class MetricCalculator(Protocol):
    def compute(
        self,
        predictions: Any,
        ground_truth: Any,
        metric_definitions: Sequence[MetricDefinition],
    ) -> Sequence[MetricResult]: ...


class BenchmarkRunner(Protocol):
    def run_benchmark(
        self,
        model_artifact: ModelArtifactReference,
        config: BenchmarkConfiguration,
        target_id: TargetId,
    ) -> Sequence[BenchmarkResult]: ...


class ErrorAnalyzer(Protocol):
    def analyze(
        self,
        predictions: Any,
        ground_truth: Any,
        dataset_binding: EvaluationDatasetBinding,
    ) -> ErrorAnalysisRecord: ...


class EvaluationRunner(Protocol):
    def validate_plan(self, plan: EvaluationPlan) -> bool: ...

    def execute(
        self,
        plan: EvaluationPlan,
        operation_identity: OperationIdentity,
        approval_request: ApprovalRequest,
        approval_decision: ApprovalDecision,
        tool_id: ToolId,
        tool_executor: ToolExecutor,
        target_id: TargetId | None = None,
    ) -> EvaluationResult: ...


def extract_evaluation_operation_payload(
    plan: EvaluationPlan,
) -> Mapping[str, Any]: ...


def parse_evaluation_result(tool_output: Any) -> EvaluationResult: ...
```

The actual implementation must add validators for finite numbers, metric-domain constraints, percentile ordering, matrix dimensions, allowed URI schemes, baseline comparability, recursive immutability, and exact ADR-0003 approval/identity matching.

## 5. Failure, Recovery, and Determinism

### 5.1 Fail-closed conditions

Evaluation fails closed on missing or mismatched authoritative identity, approval, target authorization, dataset/model evidence, baseline evidence, metric validity, benchmark validity, unsupported statistical configuration, or malformed tool output.

### 5.2 Recovery

Evaluation does not define recovery states. If a dispatched external operation becomes uncertain, ADR-0003 owns the transition into reconciliation and determines whether it completed, was not submitted, failed, or remains unresolved. Evaluation only consumes an authoritative reconciled outcome.

### 5.3 Determinism

Given identical evaluation plan, authoritative dataset/model references, operation payload, metric-definition versions, baseline evidence, benchmark configuration, target identity, and deterministic seeds, evaluation calculation must produce identical serialized domain results. Any stochastic statistical method must record deterministic seed and algorithm/version parameters.

## 6. Required Architectural Invariants

The following sixteen invariants are non-negotiable:

1. **ADR-0003 sole operation/approval authority.**
2. **No local operation identity/hash/version/approval synthesis.**
3. **No competing Evaluation execution lifecycle.**
4. **No synthetic/default target identity.**
5. **Target must be authorized by ADR-0003 scope or explicitly supplied and scope-verified.**
6. **All physical execution delegates to ADR-0005.**
7. **ActiveJobHandoff expiry is not execution failure; ADR-0003 owns reconciliation.**
8. **Dataset identity and root checksum come from ADR-0009 authority and are verified.**
9. **Model checkpoint identity and weights hash come from ADR-0010 authority and are verified.**
10. **Every non-root evaluation has a completed comparable ADR-0004 baseline.**
11. **Statistical significance is method/version/sample/evidence explicit and deterministic.**
12. **Recursive defensive immutability is mandatory for published contracts.**
13. **All numeric result domains are validated fail-closed; NaN/Inf are prohibited.**
14. **Benchmark percentiles, throughput, resources, and target identity are validated.**
15. **Evaluation results are immutable empirical records; ADR-0004 owns persistence.**
16. **Reasoning consumes evidence but cannot override measured outputs or substitute narrative for measurement.**

## 7. Implementation Acceptance Criteria

1. `EvaluationPlan` is immutable, recursively defensive, schema-strict, and contains no lifecycle state or synthetic target.
2. `EvaluationRunner.execute()` requires authoritative ADR-0003 operation identity, approval request/decision, tool identity, target authorization, and `ToolExecutor`.
3. Functional payload extraction delegates canonical hashing to ADR-0003 and excludes transient execution tokens.
4. Static analysis proves Evaluation does not import or mutate ADR-0003 persistence/lifecycle internals or implement a second state machine.
5. Dataset tests prove manifest identity/root checksum/split membership are verified through ADR-0009 authority.
6. Model tests prove checkpoint identity/content-hash/URI metadata are verified through ADR-0010 authority.
7. Target tests prove no default/synthetic target exists and unauthorized target overrides fail closed.
8. Transport tests prove no direct subprocess/SSH/socket execution exists in `cv_agent/evaluation/`; all dispatch uses ADR-0005.
9. Handoff tests prove expiry is not mapped to failure and unresolved external operations are delegated to ADR-0003 reconciliation.
10. Baseline tests prove missing, incomplete, incompatible, or mismatched baselines fail closed except for an explicitly designated root baseline.
11. Statistical-comparison tests prove method/version/sample-count/p-value/confidence semantics and deterministic seeded resampling.
12. Metric/benchmark tests reject NaN/Inf, invalid metric ranges, invalid confidence intervals, invalid percentile order, non-positive throughput, negative resources, and malformed confusion matrices.
13. Reproducibility tests prove immutable environment metadata, dataset/model hashes, metric versions, target identity, and seeds are retained.
14. Cross-ADR static boundary tests prove Evaluation owns neither experiment persistence nor orchestration lifecycle/lease/fencing/recovery.
15. Result-handoff tests prove `EvaluationResult` is immutable, verdict semantics are domain findings only, and no empirical value can be replaced by narrative text.
16. Full project quality gates pass before implementation PR approval: tests, Ruff, format, mypy, build, pip-audit, and `git diff --check`.

## 8. Consequences

### Positive

- Objective empirical evidence replaces narrative evaluation claims.
- Model quality and deployability are evaluated together where required.
- Dataset/model lineage remains tied to authoritative subsystem identities.
- Approval and execution remain under ADR-0003/ADR-0005 authority.
- Baseline comparisons become statistically explicit and reproducible.
- Structured diagnostics provide evidence for ADR-0008 reasoning.

### Tradeoffs

- Evaluation contracts and validators are more strict and therefore reject malformed or incomplete evidence rather than silently proceeding.
- Hardware benchmarking requires access to configured physical targets.
- Statistical comparison requires sufficient comparable samples and evidence; some evaluations may legitimately become `INCONCLUSIVE`.

## 9. References

- `docs/PROJECT.md`: evaluation philosophy, hardware-aware engineering, experiment-driven development, failure analysis, modular architecture.
- `docs/APPROVALS.md`: gated actions and profiling rules.
- `docs/DATA.md`: manifest-first content-addressed dataset specification.
- ADR-0001: Capability Registry (`D-011`).
- ADR-0002: LLM Gateway (`D-012`).
- ADR-0003: Orchestration State & Checkpointing (`D-020`).
- ADR-0004: Project Memory & Experiment Ledger (`D-021`).
- ADR-0005: Tools and MCP Execution Boundary (`D-023`).
- ADR-0006: Knowledge & RAG (`D-024`).
- ADR-0007: Skills Architecture & NVIDIA Discovery (`D-025`).
- ADR-0008: Reasoning Subsystem (`D-026`).
- ADR-0009: Dataset Subsystem (`D-029`).
- ADR-0010: Training Subsystem (`D-030`).
- Issue #58: Phase 2L ADR-0011 Architecture Review Gate.
