"""cv_agent.reasoning.contracts — Authoritative ADR-0008 Reasoning Subsystem Contracts.

Frozen specifications for cognitive nodes, decision contracts, capability intent,
grounding evidence, approval recommendations, and deterministic context canonicalization.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, NewType

from cv_agent.domain.identifiers import CapabilityId

# --- Decision Identification (ADR-0008, Gate M1/I1) ---

DecisionId = NewType("DecisionId", str)
_DECISION_ID_REGEX = re.compile(r"^dec-[0-9a-f]{32}$")


def validate_decision_id(value: str) -> DecisionId:
    """Validate that a DecisionId matches the canonical 32-hex format."""
    if not isinstance(value, str) or not _DECISION_ID_REGEX.match(value):
        raise ValueError(
            f"DecisionId {value!r} does not match canonical format '^dec-[0-9a-f]{{32}}$'"
        )
    return DecisionId(value)


def generate_decision_id() -> DecisionId:
    """Generate a globally unique 128-bit DecisionId."""
    return DecisionId(f"dec-{uuid.uuid4().hex}")


def generate_replay_decision_id(context_ref: str, step_index: int) -> DecisionId:
    """Derive a deterministic DecisionId from canonical reasoning context and step index."""
    preimage = f"{context_ref}:{step_index}".encode()
    digest = hashlib.sha256(preimage).hexdigest()[:32]
    return DecisionId(f"dec-{digest}")


# --- Enums & Value Classifications (ADR-0008 §3.2) ---


class NodeType(str, Enum):
    """Canonical cognitive reasoning node taxonomy."""

    PROBLEM_FRAMING = "problem_framing"
    BASELINE_SELECTION = "baseline_selection"
    EXPERIMENT_DIAGNOSIS = "experiment_diagnosis"
    STRATEGY_FORMULATION = "strategy_formulation"
    ELICITATION = "elicitation"
    TASK_DECOMPOSITION = "task_decomposition"


class RiskLevel(str, Enum):
    """Qualitative risk categorization for engineering proposals."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Reversibility(str, Enum):
    """Operational reversibility classification."""

    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class EmpiricalComparator(str, Enum):
    """Closed comparator vocabulary for empirical claims against evidence bounds."""

    EQ = "EQ"
    LT = "LT"
    LTE = "LTE"
    GT = "GT"
    GTE = "GTE"


# --- Closed JSON Value Domain (Gate I3) ---

JsonPrimitive = str | int | float | bool | None
JsonValue = JsonPrimitive | Mapping[str, "JsonValue"] | tuple["JsonValue", ...]


def _normalize_float(val: float) -> float:
    """Ensure float is finite and -0.0 is normalized to 0.0."""
    if math.isnan(val) or math.isinf(val):
        raise ValueError(
            f"Non-finite float (NaN/inf) is not allowed in JsonValue domain: {val}"
        )
    if val == 0.0:
        return 0.0
    return val


def validate_and_freeze_json_domain(val: Any) -> Any:
    """Recursively validate JSON compatibility and deeply freeze the structure."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return unicodedata.normalize("NFC", val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return _normalize_float(val)
    if isinstance(val, Mapping):
        frozen_mapping: dict[str, Any] = {}
        for k, v in val.items():
            if not isinstance(k, str):
                raise TypeError(
                    f"Dictionary key must be str, got {type(k).__name__}: {k!r}"
                )
            norm_k = unicodedata.normalize("NFC", k)
            if norm_k in frozen_mapping:
                raise ValueError(
                    f"Canonical key collision after NFC normalization: {k!r} collides with {norm_k!r}"
                )
            frozen_mapping[norm_k] = validate_and_freeze_json_domain(v)
        return MappingProxyType(frozen_mapping)
    if isinstance(val, (list, tuple)):
        return tuple(validate_and_freeze_json_domain(elem) for elem in val)
    raise TypeError(
        f"Value of type {type(val).__name__} is not JSON-compatible and cannot be frozen"
    )


# --- Grounding, Evidence, & Citations (ADR-0008 §3.3) ---


@dataclass(frozen=True)
class EvidenceRef:
    """Reference to quarantined read-only knowledge evidence (ADR-0006)."""

    uri: str
    sha256: str
    title: str
    snippet: str
    bounds: Mapping[str, tuple[float, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        norm_bounds: dict[str, tuple[float, float]] = {}
        for k, v in self.bounds.items():
            if not isinstance(k, str):
                raise TypeError(f"Metric bound key must be str: {k!r}")
            norm_k = unicodedata.normalize("NFC", k)
            if norm_k in norm_bounds:
                raise ValueError(
                    f"Canonical key collision after NFC normalization in evidence bounds: {k!r}"
                )
            if not (isinstance(v, (list, tuple)) and len(v) == 2):
                raise ValueError(
                    f"Metric bound for {k!r} must be a 2-tuple (min, max): {v!r}"
                )
            min_val = _normalize_float(float(v[0]))
            max_val = _normalize_float(float(v[1]))
            if min_val > max_val:
                raise ValueError(
                    f"Invalid evidence bound interval for {k!r}: min ({min_val}) cannot exceed max ({max_val})"
                )
            norm_bounds[norm_k] = (min_val, max_val)
        object.__setattr__(self, "bounds", MappingProxyType(norm_bounds))
        object.__setattr__(self, "uri", unicodedata.normalize("NFC", self.uri))
        object.__setattr__(self, "title", unicodedata.normalize("NFC", self.title))
        object.__setattr__(self, "snippet", unicodedata.normalize("NFC", self.snippet))


@dataclass(frozen=True)
class CitationRef:
    """Citation linking a decision claim to verified evidence."""

    citation_id: str
    uri: str
    sha256: str
    statement: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "uri", unicodedata.normalize("NFC", self.uri))
        object.__setattr__(
            self, "statement", unicodedata.normalize("NFC", self.statement)
        )


@dataclass(frozen=True)
class EmpiricalClaim:
    """Quantitative empirical claim asserting a bounded metric against cited evidence."""

    metric_name: str
    claimed_value: float
    comparator: EmpiricalComparator
    citation_uri: str

    def __post_init__(self) -> None:
        norm_val = _normalize_float(float(self.claimed_value))
        object.__setattr__(self, "claimed_value", norm_val)
        object.__setattr__(
            self, "metric_name", unicodedata.normalize("NFC", self.metric_name)
        )
        object.__setattr__(
            self, "citation_uri", unicodedata.normalize("NFC", self.citation_uri)
        )
        if not isinstance(self.comparator, EmpiricalComparator):
            try:
                object.__setattr__(
                    self, "comparator", EmpiricalComparator(self.comparator)
                )
            except Exception as exc:
                raise ValueError(
                    f"Invalid EmpiricalComparator: {self.comparator!r}"
                ) from exc


@dataclass(frozen=True)
class Hypothesis:
    """Diagnostic or exploratory engineering hypothesis."""

    hypothesis_id: str
    statement: str
    confidence: float
    citation_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        norm_conf = _normalize_float(float(self.confidence))
        if not (0.0 <= norm_conf <= 1.0):
            raise ValueError(f"Confidence must be in [0.0, 1.0], got {norm_conf}")
        object.__setattr__(self, "confidence", norm_conf)
        object.__setattr__(
            self, "statement", unicodedata.normalize("NFC", self.statement)
        )
        object.__setattr__(self, "citation_refs", tuple(self.citation_refs))


# --- Capability Intent (ADR-0008 §3.2 — Non-authoritative intent) ---


@dataclass(frozen=True)
class CapabilityIntent:
    """Expression of needed platform capability without executing tools or skills."""

    capability_id: CapabilityId
    intent_description: str
    parameter_intent: Mapping[str, JsonValue] = field(default_factory=dict)
    target_preference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parameter_intent",
            validate_and_freeze_json_domain(self.parameter_intent),
        )
        object.__setattr__(
            self,
            "intent_description",
            unicodedata.normalize("NFC", self.intent_description),
        )


# --- Approval Recommendation (ADR-0008 §3.2 — Non-authoritative recommendation) ---


@dataclass(frozen=True)
class ApprovalRecommendation:
    """Risk-aware recommendation for human authorization per docs/APPROVALS.md."""

    recommends_approval: bool
    risk_level: RiskLevel
    reversibility: Reversibility
    rationale: str
    estimated_cost_usd: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "rationale", unicodedata.normalize("NFC", self.rationale)
        )


# --- Domain Node Payload Contracts (ADR-0008 §3.2) ---


# --- Prohibited Premature Model Architectures (PM Correction 1, [P§31], ROADMAP.md) ---

_PROHIBITED_MODEL_TERMS = (
    "yolo",
    "yolov5",
    "yolov7",
    "yolov8",
    "yolov9",
    "yolov10",
    "yolov11",
    "rt-detr",
    "rtdetr",
    "faster-rcnn",
    "faster_rcnn",
    "mask-rcnn",
    "mask_rcnn",
    "retinanet",
    "ssd",
    "efficientdet",
    "resnet",
    "vit",
    "vision transformer",
)


def assert_zero_prohibited_models(text: str, context_label: str = "output") -> None:
    """Assert that text contains zero premature model architecture names [P§31], ROADMAP.md."""
    lower = text.lower()
    for term in _PROHIBITED_MODEL_TERMS:
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, lower):
            from cv_agent.reasoning.errors import PrematureModelSelectionError

            raise PrematureModelSelectionError(
                f"Premature model architecture {term!r} detected in {context_label} "
                f"prior to complete operational characterization [P§29.1], [P§31]"
            )


@dataclass(frozen=True)
class OperationalInquiry:
    """Targeted operational inquiry produced when problem characterization is incomplete."""

    inquiry_id: str
    unresolved_dimensions: tuple[str, ...]
    targeted_questions: tuple[str, ...]
    context_summary: str
    assumptions_and_risks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "inquiry_id", unicodedata.normalize("NFC", self.inquiry_id.strip())
        )
        object.__setattr__(
            self,
            "unresolved_dimensions",
            tuple(
                unicodedata.normalize("NFC", d.strip())
                for d in self.unresolved_dimensions
            ),
        )
        object.__setattr__(
            self,
            "targeted_questions",
            tuple(
                unicodedata.normalize("NFC", q.strip()) for q in self.targeted_questions
            ),
        )
        object.__setattr__(
            self,
            "context_summary",
            unicodedata.normalize("NFC", self.context_summary.strip()),
        )
        object.__setattr__(
            self,
            "assumptions_and_risks",
            tuple(
                unicodedata.normalize("NFC", r.strip())
                for r in self.assumptions_and_risks
            ),
        )

        # Enforce Anti-YOLO invariant at schema level (PM Correction 1)
        assert_zero_prohibited_models(
            self.context_summary, "OperationalInquiry.context_summary"
        )
        for q in self.targeted_questions:
            assert_zero_prohibited_models(q, "OperationalInquiry.targeted_questions")


def is_operationally_complete(char: ProblemCharacterization) -> bool:
    """Evaluate deterministic domain completeness across the 8 canonical dimensions [P§5], [P§30]."""
    if (
        not isinstance(char.operational_definition, str)
        or not char.operational_definition.strip()
    ):
        return False
    if (
        not isinstance(char.physical_environment, str)
        or not char.physical_environment.strip()
    ):
        return False
    if (
        not isinstance(char.camera_infrastructure, str)
        or not char.camera_infrastructure.strip()
    ):
        return False
    if not isinstance(char.target_hardware, str) or not char.target_hardware.strip():
        return False
    if char.latency_budget_ms is None and char.throughput_fps is None:
        return False
    if not isinstance(char.error_tolerance, str) or not char.error_tolerance.strip():
        return False
    if (
        not isinstance(char.data_availability, str)
        or not char.data_availability.strip()
    ):
        return False
    return bool(
        isinstance(char.intervention_modality, str)
        and char.intervention_modality.strip()
    )


@dataclass(frozen=True)
class ProblemCharacterization:
    """Structured characterization produced by ProblemFramingNode."""

    task_type: str
    target_hardware: str
    operational_definition: str = ""
    physical_environment: str = ""
    camera_infrastructure: str = ""
    error_tolerance: str = ""
    data_availability: str = ""
    intervention_modality: str = ""
    latency_budget_ms: float | None = None
    throughput_fps: float | None = None
    compute_budget_hours: float | None = None
    constraints: Mapping[str, JsonValue] = field(default_factory=dict)
    is_well_formed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "task_type", unicodedata.normalize("NFC", self.task_type.strip())
        )
        object.__setattr__(
            self,
            "target_hardware",
            unicodedata.normalize("NFC", self.target_hardware.strip()),
        )
        object.__setattr__(
            self,
            "operational_definition",
            unicodedata.normalize("NFC", self.operational_definition.strip()),
        )
        object.__setattr__(
            self,
            "physical_environment",
            unicodedata.normalize("NFC", self.physical_environment.strip()),
        )
        object.__setattr__(
            self,
            "camera_infrastructure",
            unicodedata.normalize("NFC", self.camera_infrastructure.strip()),
        )
        object.__setattr__(
            self,
            "error_tolerance",
            unicodedata.normalize("NFC", self.error_tolerance.strip()),
        )
        object.__setattr__(
            self,
            "data_availability",
            unicodedata.normalize("NFC", self.data_availability.strip()),
        )
        object.__setattr__(
            self,
            "intervention_modality",
            unicodedata.normalize("NFC", self.intervention_modality.strip()),
        )
        if self.latency_budget_ms is not None:
            object.__setattr__(
                self, "latency_budget_ms", _normalize_float(self.latency_budget_ms)
            )
        if self.throughput_fps is not None:
            object.__setattr__(
                self, "throughput_fps", _normalize_float(self.throughput_fps)
            )
        if self.compute_budget_hours is not None:
            object.__setattr__(
                self,
                "compute_budget_hours",
                _normalize_float(self.compute_budget_hours),
            )
        object.__setattr__(
            self, "constraints", validate_and_freeze_json_domain(self.constraints)
        )
        computed_well_formed = is_operationally_complete(self)
        object.__setattr__(self, "is_well_formed", computed_well_formed)


@dataclass(frozen=True)
class TaskNode:
    """Single node in the domain CV task decomposition DAG [P§4], [P§6].

    task_type represents a domain decomposition concept (e.g. object_detection,
    tracking, spatial_geometry, action_recognition, temporal_reasoning), NOT an ADR-0001 CapabilityId.
    """

    task_id: str
    task_type: str
    description: str
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "task_id", unicodedata.normalize("NFC", self.task_id.strip())
        )
        object.__setattr__(
            self, "task_type", unicodedata.normalize("NFC", self.task_type.strip())
        )
        object.__setattr__(
            self,
            "description",
            unicodedata.normalize("NFC", self.description.strip()),
        )
        object.__setattr__(
            self,
            "inputs",
            tuple(unicodedata.normalize("NFC", x.strip()) for x in self.inputs),
        )
        object.__setattr__(
            self,
            "outputs",
            tuple(unicodedata.normalize("NFC", x.strip()) for x in self.outputs),
        )
        object.__setattr__(
            self,
            "dependencies",
            tuple(unicodedata.normalize("NFC", x.strip()) for x in self.dependencies),
        )


@dataclass(frozen=True)
class CVTaskDecompositionDAG:
    """Acyclic directed graph of decomposed CV domain tasks."""

    tasks: tuple[TaskNode, ...]
    root_task_ids: tuple[str, ...] = ()
    terminal_task_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.tasks:
            raise ValueError(
                "CVTaskDecompositionDAG must contain at least one TaskNode."
            )

        task_ids = {t.task_id for t in self.tasks}
        if len(task_ids) != len(self.tasks):
            raise ValueError("Duplicate task_id detected in CVTaskDecompositionDAG.")

        for t in self.tasks:
            for dep in t.dependencies:
                if dep not in task_ids:
                    raise ValueError(
                        f"Task {t.task_id} has unknown dependency {dep!r}."
                    )
                if dep == t.task_id:
                    raise ValueError(f"Task {t.task_id} cannot depend on itself.")

        adj: dict[str, list[str]] = {t.task_id: [] for t in self.tasks}
        in_degree: dict[str, int] = {t.task_id: 0 for t in self.tasks}
        for t in self.tasks:
            for dep in t.dependencies:
                adj[dep].append(t.task_id)
                in_degree[t.task_id] += 1

        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        visited_count = 0
        while queue:
            curr = queue.pop(0)
            visited_count += 1
            for nxt in adj[curr]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)

        if visited_count != len(self.tasks):
            raise ValueError("Cycle detected in CVTaskDecompositionDAG.")

        if not self.root_task_ids:
            roots = tuple(t.task_id for t in self.tasks if not t.dependencies)
            object.__setattr__(self, "root_task_ids", roots)
        if not self.terminal_task_ids:
            has_outgoing = {dep for t in self.tasks for dep in t.dependencies}
            terminals = tuple(
                t.task_id for t in self.tasks if t.task_id not in has_outgoing
            )
            object.__setattr__(self, "terminal_task_ids", terminals)


@dataclass(frozen=True)
class BaselineSpec:
    """Measurable reference baseline specification produced by BaselineSelectionNode."""

    architecture: str
    weights_source: str
    input_resolution: tuple[int, int]
    batch_size: int
    evaluation_protocol: str
    hyperparameters: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_resolution", tuple(self.input_resolution))
        object.__setattr__(
            self,
            "hyperparameters",
            validate_and_freeze_json_domain(self.hyperparameters),
        )


@dataclass(frozen=True)
class DiagnosticReport:
    """Diagnostic analysis of completed run metrics produced by DiagnosisNode."""

    summary: str
    hypotheses: tuple[Hypothesis, ...]
    bottlenecks: tuple[str, ...]
    recommendations: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", unicodedata.normalize("NFC", self.summary))
        object.__setattr__(self, "hypotheses", tuple(self.hypotheses))
        object.__setattr__(self, "bottlenecks", tuple(self.bottlenecks))
        object.__setattr__(self, "recommendations", tuple(self.recommendations))


@dataclass(frozen=True)
class StrategyRecommendation:
    """Actionable next-step engineering strategy produced by StrategyNode."""

    strategy_id: str
    title: str
    description: str
    expected_impact: str
    risk_level: RiskLevel
    capability_intents: tuple[CapabilityIntent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", unicodedata.normalize("NFC", self.title))
        object.__setattr__(
            self, "description", unicodedata.normalize("NFC", self.description)
        )
        object.__setattr__(
            self, "expected_impact", unicodedata.normalize("NFC", self.expected_impact)
        )
        object.__setattr__(self, "capability_intents", tuple(self.capability_intents))


# --- Closed Reasoning Output Payload Union (Gate B1) ---

ReasoningOutputPayload = (
    ProblemCharacterization
    | BaselineSpec
    | DiagnosticReport
    | StrategyRecommendation
    | OperationalInquiry
    | CVTaskDecompositionDAG
)


# --- Reasoning Context & Output (ADR-0008 §3.1) ---


@dataclass(frozen=True)
class ReasoningContext:
    """Immutable input context provided by Orchestration to a ReasoningNode.

    Per Gate B3, context_ref MUST equal the digest computed from its canonical preimage.
    """

    context_ref: str
    current_hop: int
    max_hops: int
    trace_id: str
    schema_version: str = "1.0.0"
    evidence_refs: tuple[EvidenceRef, ...] = ()
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != "1.0.0":
            raise ValueError(
                f"Unsupported schema_version {self.schema_version!r}; supported version is '1.0.0'"
            )
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(
            self, "parameters", validate_and_freeze_json_domain(self.parameters)
        )

        # Content binding verification (Gate B3)
        expected_ref = compute_context_ref(self)
        if self.context_ref != expected_ref:
            raise ValueError(
                f"context_ref mismatch: supplied {self.context_ref!r} does not match computed {expected_ref!r}"
            )

    @classmethod
    def create(
        cls,
        *,
        current_hop: int,
        max_hops: int,
        trace_id: str,
        schema_version: str = "1.0.0",
        evidence_refs: tuple[EvidenceRef, ...] | Sequence[EvidenceRef] = (),
        parameters: Mapping[str, JsonValue] | None = None,
    ) -> ReasoningContext:
        """Construct a ReasoningContext with its authoritative computed context_ref."""
        norm_params = validate_and_freeze_json_domain(parameters or {})
        norm_evidence = tuple(evidence_refs)
        computed_ref = compute_context_ref_from_fields(
            current_hop=current_hop,
            max_hops=max_hops,
            trace_id=trace_id,
            schema_version=schema_version,
            evidence_refs=norm_evidence,
            parameters=norm_params,
        )
        return cls(
            context_ref=computed_ref,
            current_hop=current_hop,
            max_hops=max_hops,
            trace_id=trace_id,
            schema_version=schema_version,
            evidence_refs=norm_evidence,
            parameters=norm_params,
        )


@dataclass(frozen=True)
class ReasoningOutput:
    """Immutable output package returned by a ReasoningNode to Orchestration."""

    decision_id: DecisionId
    node_type: NodeType
    context_ref: str
    payload: ReasoningOutputPayload
    capability_intents: tuple[CapabilityIntent, ...] = ()
    citations: tuple[CitationRef, ...] = ()
    empirical_claims: tuple[EmpiricalClaim, ...] = ()
    approval_recommendation: ApprovalRecommendation | None = None
    hops_consumed: int = 1

    def __post_init__(self) -> None:
        validate_decision_id(self.decision_id)
        if not isinstance(
            self.payload,
            (
                ProblemCharacterization,
                BaselineSpec,
                DiagnosticReport,
                StrategyRecommendation,
                OperationalInquiry,
                CVTaskDecompositionDAG,
            ),
        ):
            raise TypeError(
                f"ReasoningOutput payload must be an instance of ReasoningOutputPayload, got {type(self.payload).__name__}"
            )
        object.__setattr__(self, "capability_intents", tuple(self.capability_intents))
        object.__setattr__(self, "citations", tuple(self.citations))
        object.__setattr__(self, "empirical_claims", tuple(self.empirical_claims))


# --- Deterministic Canonicalization (cv_agent.canonical_v1) (Gate B2/B3) ---


def _to_canonical_json_value(val: Any) -> Any:
    """Recursively convert data into canonical JSON serializable primitives."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return unicodedata.normalize("NFC", val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return _normalize_float(val)
    if isinstance(val, Mapping):
        canonical_mapping: dict[str, Any] = {}
        for k, v in val.items():
            if not isinstance(k, str):
                raise TypeError(
                    f"Dictionary key must be str, got {type(k).__name__}: {k!r}"
                )
            norm_k = unicodedata.normalize("NFC", k)
            if norm_k in canonical_mapping:
                raise ValueError(
                    f"Canonical key collision after NFC normalization: {k!r} collides with {norm_k!r}"
                )
            canonical_mapping[norm_k] = _to_canonical_json_value(v)
        return {k: canonical_mapping[k] for k in sorted(canonical_mapping.keys())}
    if isinstance(val, (list, tuple)):
        return [_to_canonical_json_value(item) for item in val]
    raise TypeError(f"Cannot canonicalize non-JSON value of type {type(val).__name__}")


def canonicalize_preimage_fields(
    *,
    current_hop: int,
    max_hops: int,
    trace_id: str,
    schema_version: str = "1.0.0",
    evidence_refs: tuple[EvidenceRef, ...] | Sequence[EvidenceRef] = (),
    parameters: Mapping[str, JsonValue] | None = None,
) -> bytes:
    """Serialize the canonical preimage of reasoning context fields into UTF-8 bytes.

    Per the Gate B2 resolution, `context_ref` is strictly excluded from the preimage
    to eliminate circular hashing dependency.
    """
    sorted_evidence = sorted(evidence_refs, key=lambda r: (r.uri, r.sha256))
    evidence_payload = [
        {
            "bounds": {
                unicodedata.normalize("NFC", k): [
                    _normalize_float(v[0]),
                    _normalize_float(v[1]),
                ]
                for k, v in sorted(
                    ref.bounds.items(),
                    key=lambda item: unicodedata.normalize("NFC", item[0]),
                )
            },
            "sha256": ref.sha256,
            "snippet": unicodedata.normalize("NFC", ref.snippet),
            "title": unicodedata.normalize("NFC", ref.title),
            "uri": unicodedata.normalize("NFC", ref.uri),
        }
        for ref in sorted_evidence
    ]

    preimage: dict[str, Any] = {
        "current_hop": current_hop,
        "evidence_refs": evidence_payload,
        "max_hops": max_hops,
        "parameters": _to_canonical_json_value(parameters or {}),
        "schema_version": unicodedata.normalize("NFC", schema_version),
        "trace_id": unicodedata.normalize("NFC", trace_id),
    }

    # Strict compact formatting without extraneous whitespace, sorted keys, NaN rejection
    return json.dumps(
        preimage,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonicalize_context_preimage(context: ReasoningContext) -> bytes:
    """Serialize the canonical preimage of a ReasoningContext into UTF-8 bytes."""
    return canonicalize_preimage_fields(
        current_hop=context.current_hop,
        max_hops=context.max_hops,
        trace_id=context.trace_id,
        schema_version=context.schema_version,
        evidence_refs=context.evidence_refs,
        parameters=context.parameters,
    )


def compute_context_ref_from_fields(
    *,
    current_hop: int,
    max_hops: int,
    trace_id: str,
    schema_version: str = "1.0.0",
    evidence_refs: tuple[EvidenceRef, ...] | Sequence[EvidenceRef] = (),
    parameters: Mapping[str, JsonValue] | None = None,
) -> str:
    """Compute context_ref digest from preimage fields."""
    return hashlib.sha256(
        canonicalize_preimage_fields(
            current_hop=current_hop,
            max_hops=max_hops,
            trace_id=trace_id,
            schema_version=schema_version,
            evidence_refs=evidence_refs,
            parameters=parameters,
        )
    ).hexdigest()


def compute_context_ref(context: ReasoningContext) -> str:
    """Compute the authoritative 64-character SHA-256 context_ref digest."""
    return hashlib.sha256(canonicalize_context_preimage(context)).hexdigest()
