# ADR-0008: Reasoning Subsystem Architecture

- **Status:** Accepted
- **Amendment Status:** Frozen
- **Date:** 2026-09-03
- **Last Reviewed:** 2026-09-10
- **Layer:** reasoning / orchestration
- **Canon:** `[P§4]`, `[P§5]`, `[P§6]`, `[P§19]`, `[P§20]`, `[P§23]`, `[P§24]`, `[P§29.1]`, `[P§29.2]`, `[P§29.3]`, `[P§29.4]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #24

## 1. Context

In the CV Engineering Agent architecture, the system operates across distinct operational layers `[P§19]`, `docs/architecture/OVERVIEW.md`:

$$\text{Reasoning (ADR-0008)} \longleftrightarrow \text{Orchestration (ADR-0003)} \longleftrightarrow \text{Knowledge (ADR-0006)} \longleftrightarrow \text{Skills (ADR-0007)} \longleftrightarrow \text{Tools (ADR-0005)}$$

Reasoning is the cognitive substrate responsible for **engineering judgment, task framing, problem decomposition, strategy selection, and diagnostic analysis** `[P§4]`, `[P§5]`. Computer vision engineering is fundamentally empirical and constrained:
1. **Problem Characterization (`[P§6]`, `[P§29.1]`):** Objectives must be formally framed against latency, throughput, compute budget, and edge deployment constraints before any architecture is selected.
2. **Baseline Formulation (`[P§4]`, `[P§29.2]`):** Engineering progress requires a measurable baseline before speculative exploration or fine-tuning begins.
3. **Hardware-Aware Selection (`[P§29.4]`):** Architecture and quantization choices must explicitly account for target hardware capabilities (e.g. Jetson Orin vs. server GPU vs. CPU).
4. **Diagnostic Analysis (`[P§4]`, `[P§29.3]`):** Post-experiment analysis must interpret quantitative metrics (loss curves, mAP, FPS, memory footprint) against hypotheses to determine next steps.

Without an explicit Reasoning architecture, LLM-based systems collapse into classic anti-patterns `[P§31]`:
- **The "Prompt Pile" Failure:** Ad-hoc monolithic prompts mix domain reasoning, tool calling, JSON parsing, and state flow into unmaintainable prompt heaps.
- **Layer Leakage (`[P§19]`, `[P§34]`):** Reasoning nodes attempt to manage workflow checkpointing (ADR-0003), call subprocesses/tools directly (ADR-0005), or perform raw vector retrieval (ADR-0006).
- **The Conversational Chatbot Fallacy:** Producing unstructured conversational prose rather than typed, structured engineering decisions.
- **Unbounded Provider Coupling (`[P§20]`):** Direct imports of proprietary provider SDKs (`openai`, `anthropic`) inside reasoning logic.

## 2. Responsibility (`[P§34]`)

- **This owns:**
  - The architectural contracts, taxonomy, and schemas for **Reasoning Nodes** (problem framing, baseline selection, experiment diagnosis, strategy formulation).
  - The transformation of empirical context (user requirements, hardware constraints, experiment history, retrieved evidence) into structured engineering decisions.
  - The formulation of **Capability Intent**: expressing *what* capability (`CapabilityId` per ADR-0001) is needed next, without executing tools, resolving skills, or validating registry authority.
  - The prompt construction standards, system persona definitions, and schema enforcement mechanisms.
  - The strict boundary governing consumption of LLM capabilities exclusively through the LLM Gateway (`ADR-0002`).
  - Grounding validation via a dedicated reasoning-layer validator (`GroundingValidator`) against supplied read-only evidence chunks (`ADR-0006`).
  - The diagnostic failure and uncertainty taxonomy (identifying metric saturation, overfitting, latency bottlenecks, and ambiguous goals).
  - The identification of actions requiring human approval recommendations per `docs/APPROVALS.md` `[P§24]`.
- **This does NOT own:**
  - Workflow graph lifecycle, state transitions, checkpointing, or pause/resume loops → Orchestration (ADR-0003) `[P§21]`;
  - High-volume experiment logging, SQLite ledger schema, or Git-tracked memory persistence → Project Memory & Ledger (ADR-0004) `[P§25]`;
  - Tool execution, subprocess dispatch, transport protocols, or MCP communication → Tools & MCP Boundary (ADR-0005) `[P§22]`;
  - Document indexing, hybrid search, RAG retrieval, or freshness decay → Knowledge Subsystem (ADR-0006) `[P§16]`–`[P§19]`;
  - Procedural recipes, command-line flags, vendor adapters, or skill resolution → Skills Architecture (ADR-0007) `[P§15]`, `[P§23]`;
  - LLM provider client SDKs, API keys, retry/fallback logic, or cost tracking → LLM Gateway (ADR-0002) `[P§20]`;
  - Authoritative capability registration, capability validation, or taxonomy definition → Capability Model (ADR-0001) `[P§23]`;
  - Authoritative approval policy evaluation, threshold verification, or token validation → Orchestration & Approvals (ADR-0003, `docs/APPROVALS.md`) `[P§24]`.
- **Why this responsibility does not belong elsewhere:**
  A workflow graph (ADR-0003) routes state transitions but cannot reason about computer vision trade-offs. Tools (ADR-0005) and skills (ADR-0007) execute discrete procedures but cannot synthesize diagnostic hypotheses. Knowledge (ADR-0006) provides passive evidence but does not exercise judgment. Isolating Reasoning into modular nodes preserves auditability, enables unit testing of decision logic with mocked LLM responses, and eliminates layer leakage.

## 3. Decision

We establish a **Structured Reasoning Subsystem Architecture** based on stateless, provider-agnostic reasoning components, typed decision contracts, provider-isolated LLM Gateway consumption, and grounded evidence consumption.

### 3.1. Architectural Model & Component Operation

Reasoning operates as stateless, provider-agnostic reasoning components embedded within Orchestration graph workflows (ADR-0003). While invoking `LLMGateway.complete()` executes an external model call with operational side effects (network I/O, token usage, non-zero temperature sampling), reasoning components maintain zero hidden in-process state between invocations:

```text
                  Orchestration Workflow (ADR-0003)
                                 │
                 passes StateSnapshot & Context
                                 ▼
      ┌──────────────────────────────────────────────────────┐
      │             Reasoning Subsystem Component             │
      │                                                      │
      │   1. Extracts Context (State, Memory, Evidence)      │
      │   2. Formulates Structured Prompt (Template + Schema)│
      │   3. Invokes LLM Gateway (ADR-0002)                  │
      │   4. Validates & Parses Structured Decision Payload  │
      │   5. Validates Evidence Grounding (GroundingValidator)│
      │   6. Flags Approval Recommendations                  │
      └──────────┬────────────────────────────┬──────────────┘
                 │                            │
      calls via provider-neutral API          │ returns typed DecisionPayload
                 ▼                            ▼
      ┌─────────────────────┐      ┌─────────────────────────┐
      │ LLM Gateway (ADR-2) │      │  Orchestration State    │
      │ (Anthropic/OpenAI/  │      │  - Capability Intent    │
      │  Ollama abstraction)│      │  - Hypothesis / Spec    │
      └─────────────────────┘      │  - Approval Flag        │
                                   └─────────────────────────┘
```

### 3.2. Core Reasoning Node Taxonomy

Reasoning is divided into specialized, decoupled node types matching the engineering lifecycle `[P§4]`:

1. **Problem Framing Node (`[P§6]`, `[P§29.1]`):**
   - *Input:* Raw user intent, environment description, edge target specifications.
   - *Function:* Formulates structured problem characterization; verifies constraint completeness (latency budget, target hardware, dataset availability).
   - *Output:* Typed `ProblemCharacterization`.
2. **Baseline Selection Node (`[P§4]`, `[P§29.2]`):**
   - *Input:* `ProblemCharacterization`, project memory history.
   - *Function:* Formulates reference baseline specification (architecture, weights, resolution, evaluation protocol).
   - *Output:* Typed `BaselineSpec`. Capability intent for executing or evaluating the baseline is emitted as a distinct decision where appropriate.
3. **Experiment Diagnosis Node (`[P§4]`, `[P§29.3]`):**
   - *Input:* Current experiment metrics (`RunRecord` from ADR-0004, terminal evaluation outcomes with `EvaluationResult.status == COMPLETED` per ADR-0011), baseline metrics, target constraints. Partial or in-flight runs cannot be diagnosed.
   - *Function:* Compares outcome against hypothesis; classifies empirical behavior (e.g. `OVERFITTING`, `LATENCY_BOTTLENECK`, `SATURATION`, `METRIC_IMPROVED`).
   - *Output:* Typed `DiagnosticRecord`.
4. **Strategy Formulation Node (`[P§5]`, `[P§29.4]`):**
   - *Input:* `DiagnosticRecord`, grounded knowledge evidence (`ADR-0006`), hardware profile.
   - *Function:* Selects next engineering hypothesis and expresses capability intent (`CapabilityId`).
   - *Output:* Typed `StrategyDecision` with requested `CapabilityId` and parameter constraints.

### 3.3. Layer Boundary Invariants

1. **The LLM Gateway Invariant (`[P§20]`, ADR-0002):**
   - Reasoning nodes MUST NOT import provider SDKs (`openai`, `anthropic`, `google.generativeai`).
   - All language model interactions occur strictly through `LLMGateway.complete()` using `CompletionRequest` and `CompletionResponse`.
   - Prompt templates are provider-agnostic. Provider-specific formatting or routing is strictly encapsulated inside ADR-0002.
2. **The Knowledge Consumption & Grounding Invariant (`[P§16]`, `[P§19]`, ADR-0006):**
   - Reasoning nodes consume retrieved knowledge passively as read-only evidence chunks.
   - External evidence MUST be wrapped in `<knowledge_citation>` isolation blocks with provenance metadata preserved.
   - Reasoning nodes own interpretation and judgment; they never execute retrieval, indexing, or freshness filtering directly.
   - **Grounding Verification Boundary:** Grounding validation is performed by a dedicated `GroundingValidator` within the Reasoning subsystem. The validator evaluates the emitted decision against the active `<knowledge_citation>` blocks provided in the input context; it validates adherence to supplied evidence (detecting evidence contradiction or unsupported speculative inference) and does NOT perform retrieval, freshness ranking, or become a knowledge authority (which remains strictly owned by ADR-0006).
3. **The Capability Intent & Downstream Authority Invariant (`[P§23]`, ADR-0001, ADR-0007):**
   - Reasoning nodes express operational needs solely as **Capability Intent** (`CapabilityId`).
   - **Downstream Authority Separation:** Reasoning does NOT validate whether a `CapabilityId` is registered or executable. Authority flow is:
     $$\text{Reasoning} \longrightarrow \text{Capability Intent} \longrightarrow \text{ADR-0007 SkillResolver} \longrightarrow \text{ADR-0001 Capability Authority} \longrightarrow \text{SkillId} \longrightarrow \text{ToolId}$$
   - If reasoning emits an invalid or unrecognized `CapabilityId`, authoritative rejection happens downstream: **ADR-0007 SkillResolver resolves the intent against the authoritative capability registry defined by ADR-0001**, rejecting unresolvable intents (`UNKNOWN_CAPABILITY`), not inside the reasoning node.
   - Reasoning nodes MUST NOT generate tool invocations, CLI arguments, or shell commands directly.
4. **The Approval Recommendation Invariant (`[P§24]`, `docs/APPROVALS.md`):**
   - When a reasoning node assesses that a proposed strategy involves high risk, destruction, or high expense (e.g. lengthy remote training, destructive data mutation, unverified edge deployment), it sets `recommends_approval = True` and provides an `approval_recommendation_reason`.
   - Reasoning does NOT evaluate or enforce approval policy. Authoritative gate evaluation, approval ticket verification, and interrupt lifecycles remain strictly owned by Orchestration (`ADR-0003`) and `docs/APPROVALS.md`.
5. **Reasoning Depth and Cycle Prevention Invariant:**
   - Reasoning components execute within orchestration workflows as bounded cognitive evaluations. A reasoning workflow MUST NOT exceed a configurable maximum cognitive iteration limit (default: 10 reasoning hops per task cycle).
   - If a sequence of diagnosis and hypothesis steps does not converge within the iteration budget, orchestration halts the loop and emits an escalation for human review, preventing unbounded LLM token consumption and recursive reasoning cycles.

### 3.4. Structured Output, Schema Recovery, and Mandatory Auditability

Reasoning components reject free-form conversational prose. The Reasoning subsystem standardizes on **Pydantic v2 (`pydantic.BaseModel`)** to enforce strict runtime schema generation, deserialization, and output validation for all LLM interactions.

- **Schema Recovery Boundary:** If an LLM response fails schema validation, the reasoning node executes a localized prompt repair loop (maximum 2 retries) requesting the model reformat the output to conform to schema. This recovery mechanism operates strictly at the reasoning output parsing layer; it is completely independent of and does NOT override or interfere with the transport retries, rate-limit backoffs, or provider fallback sequences governed by the LLM Gateway (`ADR-0002`).
- **Mandatory Auditability Envelope:** Every decision produced by the Reasoning subsystem is wrapped in a mandatory `DecisionPayload[T]` envelope containing a complete, traceable `DecisionAuditRecord` capturing:
  1. `decision_id`: Unique decision identifier (e.g. UUIDv4 or run-scoped monotonic identifier).
  2. `node_name`: Name of the reasoning component.
  3. `model_id`: Model identifier returned by LLM Gateway (`ADR-0002`).
  4. `context_ref`: Cryptographic hash or identifier of the input context.
  5. `evidence_citation_uris`: Tuple of cited source URIs from `<knowledge_citation>` blocks (matching ADR-0006 `source_uri`).
  6. `assumptions_and_uncertainties`: Explicit tuple of assumptions and unverified risks.
  7. `output_schema_version`: Version string of the emitted schema.
  8. `timestamp_utc`: ISO-8601 timestamp correlated with the active run.

## 4. Proposed Interface Contracts (Implementation Target — Phase 4 per ROADMAP.md)

*(Note: These interfaces represent the proposed type contracts for Phase 4 — DISCOVER / DEFINE workflow implementation per `docs/roadmap/ROADMAP.md`. They do not exist in the repository today).*

```python
# module: cv_agent.reasoning.types (Proposed — Phase 4 per docs/roadmap/ROADMAP.md)

from enum import Enum
from typing import (
    Any,
    Generic,
    Mapping,
    NewType,
    Protocol,
    TypeVar,
    Union,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field

from cv_agent.capabilities.registry import CapabilityId
from cv_agent.llm.gateway import LLMGateway

HypothesisId = NewType("HypothesisId", str)
T = TypeVar("T")

# Identifier syntax rule: HypothesisId and decision IDs MUST be non-empty ASCII strings
# conforming to [a-z0-9_.-]+(/[a-z0-9_.-]+)* per ADR-0001 §3.1.


class DiagnosticClassification(str, Enum):
    """Normalized empirical diagnosis of an experiment outcome."""

    METRIC_IMPROVED = "metric_improved"
    OVERFITTING = "overfitting"
    UNDERFITTING = "underfitting"
    LATENCY_BOTTLENECK = "latency_bottleneck"
    ACCURACY_COLLAPSE = "accuracy_collapse"
    SATURATION = "saturation"
    ANOMALOUS_METRIC = "anomalous_metric"


class TargetConstraints(BaseModel):
    """Hardware and performance constraints for problem characterization."""

    model_config = ConfigDict(frozen=True)

    target_hardware: str  # e.g. "nvidia_jetson_orin_nano", "apple_m2", "cuda_gpu"
    max_latency_ms: float | None = None
    min_throughput_fps: float | None = None
    max_memory_mb: float | None = None
    max_model_size_mb: float | None = None
    accuracy_target_metric: str = "mAP50"
    min_accuracy_threshold: float = 0.50


class DatasetSummary(BaseModel):
    """Structured summary of dataset statistics and partitions."""

    model_config = ConfigDict(frozen=True)

    total_samples: int
    classes: tuple[str, ...]
    split_proportions: Mapping[str, float]
    annotation_type: str
    metadata: Mapping[str, Any] = Field(default_factory=dict)


class ProblemCharacterization(BaseModel):
    """Formalized computer vision task definition per [P§6]."""

    model_config = ConfigDict(frozen=True)

    task_type: str  # e.g. "object_detection", "instance_segmentation"
    domain_description: str
    target_constraints: TargetConstraints
    dataset_summary: DatasetSummary
    is_well_formed: bool
    framing_notes: str


class BaselineSpec(BaseModel):
    """Measurable reference baseline specification per [P§4], [P§29.2]."""

    model_config = ConfigDict(frozen=True)

    architecture_name: str
    pretrained_weights: str | None
    input_resolution: tuple[int, int]
    primary_metric: str
    rationale: str


class DiagnosticRecord(BaseModel):
    """Analytical evaluation of an empirical experiment run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    classification: DiagnosticClassification
    primary_delta: float
    analysis: str
    identified_bottlenecks: tuple[str, ...] = ()


class DecisionAuditRecord(BaseModel):
    """Complete audit trail for an engineering reasoning decision."""

    model_config = ConfigDict(frozen=True)

    decision_id: str  # Unique decision identifier
    node_name: str
    model_id: str
    context_ref: str  # Hash or ID of input context snapshot
    evidence_citation_uris: tuple[str, ...]  # Cited source URIs from ADR-0006
    assumptions_and_uncertainties: tuple[str, ...]
    output_schema_version: str
    timestamp_utc: str  # ISO-8601 UTC timestamp
    correlation_id: str  # Workflow correlation ID from ADR-0003


class StrategyDecision(BaseModel):
    """Strategic next action selected by engineering reasoning."""

    model_config = ConfigDict(frozen=True)

    hypothesis_id: HypothesisId
    hypothesis_statement: str
    next_capability: CapabilityId  # Capability intent; validated downstream
    parameter_intent: Mapping[str, Any]
    recommends_approval: bool = False
    approval_recommendation_reason: str | None = None
    evidence_citations: tuple[str, ...] = ()
    rationale: str = ""
    audit_record: DecisionAuditRecord


ReasoningOutputPayload = Union[
    ProblemCharacterization,
    BaselineSpec,
    DiagnosticRecord,
    StrategyDecision,
]


class DecisionPayload(BaseModel, Generic[T]):
    """Common envelope for reasoning outputs, enforcing mandatory auditability."""

    model_config = ConfigDict(frozen=True)

    payload: T
    audit_record: DecisionAuditRecord


class ExperimentSummaryRef(BaseModel):
    """Typed reference to a historical experiment run from ADR-0004."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    capability_id: CapabilityId
    primary_metric_name: str
    primary_metric_value: float
    status: str
    duration_seconds: float | None = None


class ReasoningContext(BaseModel):
    """Aggregated context passed to a reasoning node.

    Residual mappings represent explicit boundary adapters to upstream
    contracts (e.g. ADR-0003 workflow StateSnapshot).
    """

    model_config = ConfigDict(frozen=True)

    workflow_state_ref: str  # Checkpoint or run identifier from ADR-0003
    problem_char: ProblemCharacterization | None
    active_baseline: BaselineSpec | None
    recent_experiments: tuple[ExperimentSummaryRef, ...] = ()
    quarantined_evidence: tuple[str, ...] = ()  # Injected <knowledge_citation>
    upstream_state_adapter: Mapping[str, Any] = Field(
        default_factory=dict
    )  # Boundary adapter to ADR-0003 StateSnapshot


@runtime_checkable
class GroundingValidator(Protocol):
    """Validates decision payload adherence to supplied evidence."""

    def validate_grounding(
        self,
        decision: Any,
        citations: tuple[str, ...],
    ) -> tuple[bool, str | None]:
        """Verify no evidence contradictions or unsupported inferences exist."""
        ...


@runtime_checkable
class ReasoningNode(Protocol):
    """Architectural contract for a functional reasoning component."""

    @property
    def node_name(self) -> str:
        """Name of the reasoning component."""
        ...

    async def reason(
        self,
        context: ReasoningContext,
        gateway: LLMGateway,
    ) -> DecisionPayload[Any]:
        """Execute reasoning over context using the LLM Gateway."""
        ...
```

## 5. Normalized Failure & Escalation Taxonomy

The Reasoning subsystem defines structured exception and uncertainty types:

### A. Formulation & Contract Failures
1. **`AMBIGUOUS_OBJECTIVE`:** The user task cannot be characterized into measurable constraints (`[P§6]`).
2. **`INSUFFICIENT_FEASIBILITY_EVIDENCE`:** Reasoning flags that available empirical evidence and reference architectures indicate the stated latency/throughput/accuracy trade-off is unlikely to be met on the target hardware. Reasoning does NOT possess authority to declare physical impossibility; it identifies an evidence gap or apparent infeasibility requiring human clarification or experimental investigation.
3. **`MISSING_BASELINE`:** An optimization or fine-tuning strategy was requested before a reference baseline was established (`[P§29.2]`).

### B. Execution, Parsing & Evidence Failures
4. **`SCHEMA_PARSING_FAILED`:** The LLM failed to return valid JSON conforming to the node's schema after localized recovery retries.
5. **`UNGROUNDED_REASONING`:** The generated decision violates evidence grounding. Specifically distinguished into:
   - **Evidence Contradiction:** Reasoning asserts factual or capability claims that directly contradict citations provided in `<knowledge_citation>` blocks.
   - **Unsupported Speculative Inference:** Reasoning asserts empirical hardware performance numbers, domain constraints, or architectural capabilities without supporting evidence citations or empirical experiment records.
6. **`UNRESOLVABLE_CAPABILITY_INTENT`:** Downstream resolution (`ADR-0007 SkillResolver`) rejects the `CapabilityId` emitted by Reasoning because it is unrecognized by the authoritative ADR-0001 registry or lacks a satisfying skill.

## 6. Alternatives Considered

| Alternative | Evidence For | Evidence Against | Why Not Chosen |
|---|---|---|---|
| **Monolithic ReAct / Chatbot Loop** | Simple to implement initially; common in naive agents. | Degrades into "prompt piles" `[P§31]`; unpredictable state transitions; mixes tools with reasoning `[P§19]`. | Violates `[P§19]`, `[P§31]`, `[P§34]`. |
| **Vendor-Specific Framework (e.g. LangChain / CrewAI agents)** | Pre-built agents and prompt libraries. | Heavy dependencies; provider leaks; poor determinism; violates architecture neutrality `[P§19]`. | Violates `[P§20]`, `[P§34]`. |
| **Hard-Coded Heuristics (Rule-Based Expert System)** | 100% deterministic; zero LLM inference cost. | Brittle; fails on novel CV domains or unstructured user specifications. | Fails `[P§5]` (requires adaptive engineering judgment). |
| **Merge Reasoning into Orchestration Nodes** | Fewer files; direct graph execution. | Tight coupling between workflow graphs and prompt logic; impossible to unit-test reasoning without running graph engines. | Violates `[P§19]`, `[P§34]`. |

## 7. Consequences

### Positive
- **Architectural Purity (`[P§19]`, `[P§34]`):** Reasoning owns pure judgment; orchestration owns workflow; tools own execution; knowledge owns facts.
- **Provider Agnostic (`[P§20]`):** Zero provider lock-in; reasoning functions identically across Anthropic, OpenAI, or local Ollama models.
- **Grounded & Disciplined (`[P§29.1]`–`[P§29.4]`):** Enforces problem-first, baseline-first, and hardware-aware engineering principles.
- **Auditability:** Every decision produces a mandatory `DecisionAuditRecord` with explicit rationale, schema version, and citation URIs.

### Negative / Costs
- **Indirection Overhead:** Structured prompts and schema parsing add serialization latency compared to unstructured streaming.
- **Schema Rigidity:** Changing decision structures requires versioned Pydantic models.

## 8. Acceptance Criteria

1. **Layer Separation Preserved:** Reasoning components operate as stateless, provider-agnostic decision components without owning workflow transitions (ADR-0003), tool execution (ADR-0005), or retrieval (ADR-0006) `[P§19]`.
2. **Provider Agnostic LLM Access:** Reasoning components interface with LLMs exclusively via `LLMGateway` (ADR-0002); no direct provider SDK imports exist in reasoning modules `[P§20]`.
3. **Capability Intent Decoupled from Authority:** Reasoning expresses action intent by emitting `CapabilityId`s; **ADR-0007 SkillResolver resolves the intent against the authoritative capability registry defined by ADR-0001** `[P§23]`.
4. **Structured Decision Outputs via Pydantic:** All reasoning nodes output typed, schema-validated structures via Pydantic v2 `BaseModel`; unstructured free text is disallowed.
5. **Baseline-First Enforcement:** Strategy selection nodes mandate a valid baseline measurement before proposing optimizations or fine-tuning runs `[P§29.2]`.
6. **Hardware-Aware Framing:** Problem characterization and strategy selection schemas explicitly model target hardware constraints and compute budgets `[P§29.4]`.
7. **Grounded Evidence Consumption & Verification:** Retrieved external facts are consumed as read-only `<knowledge_citation>` blocks; evidence contradiction and unsupported speculative inference are validated by `GroundingValidator`.
8. **Approval Preconditions Recommended, Not Enforced:** Reasoning nodes recommend approval (`recommends_approval = True`) and provide rationale; authoritative policy evaluation and gate enforcement remain owned by ADR-0003 and `docs/APPROVALS.md` `[P§24]`.
9. **Mandatory Auditability:** All decisions preserve comprehensive audit records (`decision_id`, `node_name`, `model_id`, `context_ref`, citation URIs, schema version, and timestamp).
10. **Zero Runtime Implementation in Phase 1:** Specification defines interface contracts and architectural boundaries without introducing speculative runtime implementation code.
11. **Cognitive Boundedness & Cycle Prevention:** Reasoning workflows enforce a configurable maximum cognitive iteration budget (default 10 hops) and operate exclusively on terminal evaluation outcomes (`status == COMPLETED`).

## 9. References

- `[P§4]` Core engineering loop
- `[P§5]` Decision framework & engineering judgment
- `[P§6]` Problem characterization
- `[P§10]` External compute target submission
- `[P§15]` Don't reinvent existing vendor expertise
- `[P§16]` Two knowledge mechanisms: persistent vs. live research
- `[P§17]` Evidence weighting hierarchy
- `[P§19]` Subsystem isolation: reasoning vs. orchestration vs. knowledge vs. execution
- `[P§20]` Provider-agnostic LLM access
- `[P§21]` Workflow graph & state machine
- `[P§23]` Vocabulary: Capability vs. Skill vs. Tool vs. Agent
- `[P§24]` Human-in-the-loop approvals
- `[P§25]` Experiment tracking & metadata
- `[P§29.1]` Problem first: characterize before selecting
- `[P§29.2]` Baseline first: measure before optimizing
- `[P§29.3]` Evidence over hype: quantitative comparison or no claim
- `[P§29.4]` Hardware-aware: accuracy without deployability is incomplete
- `[P§31]` What this is NOT: not a prompt pile, not a chatbot
- `[P§34]` Architecture boundary test: what does this own, and why not elsewhere?
- `docs/architecture/OVERVIEW.md` Architecture overview and 9-step sequence
- `docs/APPROVALS.md` Human approval policies
- `docs/roadmap/ROADMAP.md` Project phases and milestone exit tests
- `ADR-0001` Capability Model
- `ADR-0002` LLM Gateway and Provider Abstraction
- `ADR-0003` Orchestration State Machine, Checkpointing, and Persistent Approvals
- `ADR-0004` Project Memory and Experiment Ledger Persistence
- `ADR-0005` Tools and MCP Execution Boundary
- `ADR-0006` Knowledge and RAG Subsystem
- `ADR-0007` Skills Architecture and NVIDIA Capability Discovery
