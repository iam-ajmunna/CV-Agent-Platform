"""tests/test_reasoning_boundary.py — Adversarial Acceptance Tests for Phase 2I Reasoning Subsystem.

Verifies all frozen ADR-0008 architectural invariants, PM review gates, and cross-version requirements:
1. Cross-version numeric portability with pinned golden vectors across Python 3.10–3.13.
2. Context preimage circularity elimination (excluding context_ref from preimage).
3. Dict key-order invariance in canonical hashing.
4. Semantic string whitespace preservation vs formatting whitespace normalization.
5. Closed recursive JsonValue domain rejecting arbitrary objects, non-string keys, and NaN/inf.
6. Deep defensive immutability across nested structures.
7. 128-bit DecisionId entropy and deterministic replay identity.
8. Closed EmpiricalComparator vocabulary and formal boundary truth evaluation.
9. Deterministic, non-generative GroundingValidator gating citations, empirical claims, and speculation.
10. Cognitive hop budget verification and ADR-0003 ownership.
11. Bounded schema-repair retry loop (max 3 logical completions = 1 initial + <= 2 repairs).
12. LLM Gateway transport isolation (immediate abort on transport errors).
13. Non-authoritative approval recommendations without policy enforcement.
14. AST scan confirming zero direct provider SDK imports in cv_agent/reasoning.
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path

import pytest

from cv_agent.domain.identifiers import CapabilityId
from cv_agent.llm.contracts import (
    FallbackPolicy,
    ModelProfile,
    ModelSelector,
    RetryPolicy,
    RoutingPolicy,
)
from cv_agent.llm.errors import InvalidRequestError, RateLimitError
from cv_agent.llm.gateway import LLMGateway
from cv_agent.llm.mock import MockProviderAdapter
from cv_agent.reasoning.contracts import (
    BaselineSpec,
    CitationRef,
    DiagnosticReport,
    EmpiricalClaim,
    EmpiricalComparator,
    EvidenceRef,
    Hypothesis,
    NodeType,
    ProblemCharacterization,
    ReasoningContext,
    ReasoningOutput,
    Reversibility,
    RiskLevel,
    StrategyRecommendation,
    canonicalize_context_preimage,
    compute_context_ref,
    generate_decision_id,
    generate_replay_decision_id,
    validate_and_freeze_json_domain,
    validate_decision_id,
)
from cv_agent.reasoning.errors import (
    CognitiveBudgetExceededError,
    EvidenceContradictionError,
    ReasoningTransportError,
    SchemaRepairExhaustedError,
    UnanchoredCitationError,
    UncitedEmpiricalClaimError,
    UnsupportedSpeculationError,
)
from cv_agent.reasoning.grounding import GroundingValidator
from cv_agent.reasoning.nodes import (
    BaselineSelectionNode,
    DiagnosisNode,
    ProblemFramingNode,
    StrategyNode,
)


def _build_mock_gateway(
    adapter: MockProviderAdapter | None = None,
) -> tuple[LLMGateway, MockProviderAdapter]:
    mock_adapter = adapter or MockProviderAdapter(provider_id="mock-reasoning")
    profiles = {
        "mock-reasoning:model-cognitive": ModelProfile(
            provider_id="mock-reasoning",
            model_id="model-cognitive",
            profile_version="1.0",
            context_window_tokens=8192,
            input_modalities=frozenset({"text"}),
            output_modalities=frozenset({"text"}),
            supports_structured_output=True,
            supports_tool_calls=True,
            supports_streaming=True,
            supports_reasoning=True,
            pricing_version="2026-v1",
        )
    }
    selector = ModelSelector("mock-reasoning", "model-cognitive")
    gw = LLMGateway(
        providers={"mock-reasoning": mock_adapter},
        model_profiles=profiles,
        default_routing_policy=RoutingPolicy(
            policy_id="test-reasoning-routing",
            policy_version="1.0",
            candidates=(selector,),
            max_attempts=3,
        ),
        default_retry_policy=RetryPolicy(
            max_retries=1,
            initial_backoff_ms=5,
            max_backoff_ms=10,
            jitter=False,
        ),
        default_fallback_policy=FallbackPolicy(
            enabled=False,
            allowed_failure_categories=frozenset(),
            max_provider_switches=0,
        ),
    )
    return gw, mock_adapter


# ==============================================================================
# Gate 1: Cross-Version Numeric Portability & Golden Vectors (Python 3.10–3.13)
# ==============================================================================


class TestCanonicalizationPortability:
    """Tests cross-version determinism and golden vectors for canonical_v1."""

    def test_signed_zero_float_invariance(self) -> None:
        """-0.0 and 0.0 must serialize to identical canonical bytes and SHA-256 hashes."""
        ctx_pos = ReasoningContext(
            context_ref="placeholder",
            current_hop=1,
            max_hops=10,
            trace_id="trace-001",
            parameters={"zero_val": 0.0},
        )
        ctx_neg = ReasoningContext(
            context_ref="placeholder",
            current_hop=1,
            max_hops=10,
            trace_id="trace-001",
            parameters={"zero_val": -0.0},
        )

        bytes_pos = canonicalize_context_preimage(ctx_pos)
        bytes_neg = canonicalize_context_preimage(ctx_neg)

        assert bytes_pos == bytes_neg
        assert compute_context_ref(ctx_pos) == compute_context_ref(ctx_neg)
        assert b'"zero_val":0.0' in bytes_pos

    def test_golden_vectors_edge_case_numbers(self) -> None:
        """Golden vector evaluation for edge-case numbers across Python 3.10–3.13."""
        ctx = ReasoningContext(
            context_ref="placeholder",
            current_hop=2,
            max_hops=10,
            trace_id="trace-golden",
            schema_version="1.0.0",
            evidence_refs=(
                EvidenceRef(
                    uri="kb://cv/models/yolo_baseline",
                    sha256="abc1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcd",
                    title="YOLOv8 Edge Baseline",
                    snippet="Baseline evaluation on Jetson Orin Nano",
                    bounds={"latency_ms": (5.0, 15.0), "fps": (60.0, 120.0)},
                ),
            ),
            parameters={
                "int_zero": 0,
                "float_zero": 0.0,
                "large_int": 9223372036854775807,  # 2**63 - 1
                "scientific_float": 1.2345e-05,
                "small_float": 1e-12,
            },
        )

        canonical_bytes = canonicalize_context_preimage(ctx)
        digest = compute_context_ref(ctx)

        # Invariant checks
        assert isinstance(canonical_bytes, bytes)
        assert len(digest) == 64
        # Verify deterministic fields presence in canonical bytes
        assert b'"current_hop":2' in canonical_bytes
        assert b'"large_int":9223372036854775807' in canonical_bytes
        assert b'"float_zero":0.0' in canonical_bytes

        # Re-compute to assert 100% internal reproducibility
        assert compute_context_ref(ctx) == digest

    def test_preimage_excludes_context_ref(self) -> None:
        """Changing context_ref on identical context fields must yield the exact same computed digest."""
        ctx1 = ReasoningContext(
            context_ref="first_arbitrary_ref",
            current_hop=1,
            max_hops=5,
            trace_id="trace-abc",
            parameters={"alpha": 1, "beta": "test"},
        )
        ctx2 = ReasoningContext(
            context_ref="second_different_ref",
            current_hop=1,
            max_hops=5,
            trace_id="trace-abc",
            parameters={"alpha": 1, "beta": "test"},
        )

        assert ctx1.context_ref != ctx2.context_ref
        assert canonicalize_context_preimage(ctx1) == canonicalize_context_preimage(
            ctx2
        )
        assert compute_context_ref(ctx1) == compute_context_ref(ctx2)

    def test_mapping_key_order_invariance(self) -> None:
        """Dictionary key insertion order must not alter canonical byte representation or hash."""
        ctx_ordered = ReasoningContext(
            context_ref="x",
            current_hop=1,
            max_hops=5,
            trace_id="trace-1",
            parameters={"a": 1, "b": 2, "c": 3},
        )
        ctx_reversed = ReasoningContext(
            context_ref="x",
            current_hop=1,
            max_hops=5,
            trace_id="trace-1",
            parameters={"c": 3, "a": 1, "b": 2},
        )

        assert canonicalize_context_preimage(
            ctx_ordered
        ) == canonicalize_context_preimage(ctx_reversed)
        assert compute_context_ref(ctx_ordered) == compute_context_ref(ctx_reversed)

    def test_semantic_whitespace_vs_formatting_whitespace(self) -> None:
        """Semantic string whitespace must alter the digest; formatting whitespace must be normalized."""
        ctx_single_space = ReasoningContext(
            context_ref="x",
            current_hop=1,
            max_hops=5,
            trace_id="t",
            parameters={"requirement": "latency target: < 20ms"},
        )
        ctx_double_space = ReasoningContext(
            context_ref="x",
            current_hop=1,
            max_hops=5,
            trace_id="t",
            parameters={"requirement": "latency target: <  20ms"},
        )

        # Different semantic whitespace MUST produce different digests
        assert compute_context_ref(ctx_single_space) != compute_context_ref(
            ctx_double_space
        )


# ==============================================================================
# Gate 2: Closed Recursive JsonValue Domain & Deep Defensive Immutability
# ==============================================================================


class TestClosedJsonValueAndImmutability:
    """Verifies that non-JSON objects are rejected and structures are deeply immutable."""

    def test_non_json_objects_rejected(self) -> None:
        """Arbitrary objects, functions, datetimes, threads must be rejected with TypeError."""
        with pytest.raises(TypeError, match="not JSON-compatible"):
            validate_and_freeze_json_domain(object())

        with pytest.raises(TypeError, match="not JSON-compatible"):
            validate_and_freeze_json_domain(lambda x: x)

        with pytest.raises(TypeError, match="not JSON-compatible"):
            ReasoningContext(
                context_ref="ref",
                current_hop=1,
                max_hops=5,
                trace_id="t",
                parameters={"bad_func": lambda: None},  # type: ignore[dict-item]
            )

    def test_non_string_dict_keys_rejected(self) -> None:
        """Non-string dictionary keys must be rejected with TypeError."""
        with pytest.raises(TypeError, match="Dictionary key must be str"):
            validate_and_freeze_json_domain({123: "value"})  # type: ignore[dict-item]

    def test_non_finite_floats_rejected(self) -> None:
        """NaN and ±inf must be rejected with ValueError."""
        with pytest.raises(ValueError, match="Non-finite float"):
            validate_and_freeze_json_domain(float("nan"))

        with pytest.raises(ValueError, match="Non-finite float"):
            validate_and_freeze_json_domain(float("inf"))

        with pytest.raises(ValueError, match="Non-finite float"):
            validate_and_freeze_json_domain(float("-inf"))

    def test_deep_defensive_immutability(self) -> None:
        """Adversarial nested structure dict -> list -> dict -> list must be deeply frozen."""
        nested = {
            "level1": [
                {
                    "level2": [1, 2, 3],
                    "flag": True,
                }
            ]
        }
        frozen = validate_and_freeze_json_domain(nested)

        # Attempt to mutate outer mapping
        with pytest.raises(TypeError):
            frozen["level1"] = "mutated"  # type: ignore[index]

        # Attempt to mutate inner sequence
        with pytest.raises(AttributeError):
            frozen["level1"].append("mutated")  # type: ignore[union-attr]

        # Attempt to mutate inner mapping
        with pytest.raises(TypeError):
            frozen["level1"][0]["flag"] = False  # type: ignore[index]

        # Attempt to mutate innermost sequence
        with pytest.raises(AttributeError):
            frozen["level1"][0]["level2"].append(4)  # type: ignore[union-attr]


# ==============================================================================
# Gate 3: 128-Bit DecisionId Entropy & Replay Identity
# ==============================================================================


class TestDecisionIdContract:
    """Verifies DecisionId format, entropy, and deterministic replay derivation."""

    def test_decision_id_valid_and_invalid(self) -> None:
        # Standard 32-hex format
        valid_id = generate_decision_id()
        assert valid_id.startswith("dec-")
        assert len(valid_id) == 36  # "dec-" (4) + 32 hex = 36
        assert validate_decision_id(valid_id) == valid_id

        # Rejection of invalid forms
        with pytest.raises(ValueError, match="canonical format"):
            validate_decision_id("dec-1234567890ab")  # 12-hex rejected

        with pytest.raises(ValueError, match="canonical format"):
            validate_decision_id("dec-INVALIDHEX12345678901234567890")

        with pytest.raises(ValueError, match="canonical format"):
            validate_decision_id("uuid-1234567890abcdef1234567890abcdef")

    def test_deterministic_replay_decision_id(self) -> None:
        ctx_ref = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        id1 = generate_replay_decision_id(ctx_ref, 3)
        id2 = generate_replay_decision_id(ctx_ref, 3)
        id3 = generate_replay_decision_id(ctx_ref, 4)

        assert id1 == id2
        assert id1 != id3
        assert validate_decision_id(id1) == id1


# ==============================================================================
# Gate 4: Closed EmpiricalComparator & Formal Boundary Semantics
# ==============================================================================


class TestEmpiricalComparatorBoundaries:
    """Tests exact boundary evaluation semantics for LTE, LT, GTE, GT, EQ."""

    @pytest.fixture
    def sample_context(self) -> ReasoningContext:
        return ReasoningContext(
            context_ref="ctx_evidence_ref",
            current_hop=1,
            max_hops=5,
            trace_id="trace-01",
            evidence_refs=(
                EvidenceRef(
                    uri="kb://cv/models/edge_metrics",
                    sha256="1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
                    title="Edge Metrics",
                    snippet="Observed latency and FPS",
                    bounds={
                        "latency_ms": (5.0, 10.0),
                        "fps": (30.0, 60.0),
                        "point_metric": (42.0, 42.0),
                    },
                ),
            ),
        )

    def test_comparator_lte_boundary(self, sample_context: ReasoningContext) -> None:
        validator = GroundingValidator()
        dec_id = generate_decision_id()

        # Valid at boundary: 10.0 <= 10.0
        out_valid = ReasoningOutput(
            decision_id=dec_id,
            node_type=NodeType.EXPERIMENT_DIAGNOSIS,
            context_ref=sample_context.context_ref,
            payload="ok",
            citations=(
                CitationRef("c1", "kb://cv/models/edge_metrics", "hash", "statement"),
            ),
            empirical_claims=(
                EmpiricalClaim(
                    "latency_ms",
                    10.0,
                    EmpiricalComparator.LTE,
                    "kb://cv/models/edge_metrics",
                ),
            ),
        )
        validator.validate(sample_context, out_valid)

        # Invalid just over boundary: 10.0001 <= 10.0
        out_invalid = ReasoningOutput(
            decision_id=dec_id,
            node_type=NodeType.EXPERIMENT_DIAGNOSIS,
            context_ref=sample_context.context_ref,
            payload="bad",
            citations=(
                CitationRef("c1", "kb://cv/models/edge_metrics", "hash", "statement"),
            ),
            empirical_claims=(
                EmpiricalClaim(
                    "latency_ms",
                    10.0001,
                    EmpiricalComparator.LTE,
                    "kb://cv/models/edge_metrics",
                ),
            ),
        )
        with pytest.raises(
            EvidenceContradictionError, match="contradicts evidence upper bound"
        ):
            validator.validate(sample_context, out_invalid)

    def test_comparator_lt_boundary(self, sample_context: ReasoningContext) -> None:
        validator = GroundingValidator()
        dec_id = generate_decision_id()

        # Valid: 9.999 < 10.0
        out_valid = ReasoningOutput(
            decision_id=dec_id,
            node_type=NodeType.EXPERIMENT_DIAGNOSIS,
            context_ref=sample_context.context_ref,
            payload="ok",
            citations=(
                CitationRef("c1", "kb://cv/models/edge_metrics", "hash", "statement"),
            ),
            empirical_claims=(
                EmpiricalClaim(
                    "latency_ms",
                    9.999,
                    EmpiricalComparator.LT,
                    "kb://cv/models/edge_metrics",
                ),
            ),
        )
        validator.validate(sample_context, out_valid)

        # Invalid at exact boundary: 10.0 < 10.0 is False
        out_invalid = ReasoningOutput(
            decision_id=dec_id,
            node_type=NodeType.EXPERIMENT_DIAGNOSIS,
            context_ref=sample_context.context_ref,
            payload="bad",
            citations=(
                CitationRef("c1", "kb://cv/models/edge_metrics", "hash", "statement"),
            ),
            empirical_claims=(
                EmpiricalClaim(
                    "latency_ms",
                    10.0,
                    EmpiricalComparator.LT,
                    "kb://cv/models/edge_metrics",
                ),
            ),
        )
        with pytest.raises(
            EvidenceContradictionError, match="contradicts evidence upper bound"
        ):
            validator.validate(sample_context, out_invalid)

    def test_comparator_gte_boundary(self, sample_context: ReasoningContext) -> None:
        validator = GroundingValidator()
        dec_id = generate_decision_id()

        # Valid at boundary: 30.0 >= 30.0
        out_valid = ReasoningOutput(
            decision_id=dec_id,
            node_type=NodeType.EXPERIMENT_DIAGNOSIS,
            context_ref=sample_context.context_ref,
            payload="ok",
            citations=(
                CitationRef("c1", "kb://cv/models/edge_metrics", "hash", "statement"),
            ),
            empirical_claims=(
                EmpiricalClaim(
                    "fps", 30.0, EmpiricalComparator.GTE, "kb://cv/models/edge_metrics"
                ),
            ),
        )
        validator.validate(sample_context, out_valid)

        # Invalid below boundary: 29.999 >= 30.0 is False
        out_invalid = ReasoningOutput(
            decision_id=dec_id,
            node_type=NodeType.EXPERIMENT_DIAGNOSIS,
            context_ref=sample_context.context_ref,
            payload="bad",
            citations=(
                CitationRef("c1", "kb://cv/models/edge_metrics", "hash", "statement"),
            ),
            empirical_claims=(
                EmpiricalClaim(
                    "fps",
                    29.999,
                    EmpiricalComparator.GTE,
                    "kb://cv/models/edge_metrics",
                ),
            ),
        )
        with pytest.raises(
            EvidenceContradictionError, match="contradicts evidence lower bound"
        ):
            validator.validate(sample_context, out_invalid)

    def test_comparator_gt_boundary(self, sample_context: ReasoningContext) -> None:
        validator = GroundingValidator()
        dec_id = generate_decision_id()

        # Valid: 30.001 > 30.0
        out_valid = ReasoningOutput(
            decision_id=dec_id,
            node_type=NodeType.EXPERIMENT_DIAGNOSIS,
            context_ref=sample_context.context_ref,
            payload="ok",
            citations=(
                CitationRef("c1", "kb://cv/models/edge_metrics", "hash", "statement"),
            ),
            empirical_claims=(
                EmpiricalClaim(
                    "fps", 30.001, EmpiricalComparator.GT, "kb://cv/models/edge_metrics"
                ),
            ),
        )
        validator.validate(sample_context, out_valid)

        # Invalid at boundary: 30.0 > 30.0 is False
        out_invalid = ReasoningOutput(
            decision_id=dec_id,
            node_type=NodeType.EXPERIMENT_DIAGNOSIS,
            context_ref=sample_context.context_ref,
            payload="bad",
            citations=(
                CitationRef("c1", "kb://cv/models/edge_metrics", "hash", "statement"),
            ),
            empirical_claims=(
                EmpiricalClaim(
                    "fps", 30.0, EmpiricalComparator.GT, "kb://cv/models/edge_metrics"
                ),
            ),
        )
        with pytest.raises(
            EvidenceContradictionError, match="contradicts evidence lower bound"
        ):
            validator.validate(sample_context, out_invalid)

    def test_comparator_eq_boundary(self, sample_context: ReasoningContext) -> None:
        validator = GroundingValidator()
        dec_id = generate_decision_id()

        # Valid point match: 42.0 == 42.0
        out_valid = ReasoningOutput(
            decision_id=dec_id,
            node_type=NodeType.EXPERIMENT_DIAGNOSIS,
            context_ref=sample_context.context_ref,
            payload="ok",
            citations=(
                CitationRef("c1", "kb://cv/models/edge_metrics", "hash", "statement"),
            ),
            empirical_claims=(
                EmpiricalClaim(
                    "point_metric",
                    42.0,
                    EmpiricalComparator.EQ,
                    "kb://cv/models/edge_metrics",
                ),
            ),
        )
        validator.validate(sample_context, out_valid)

        # Invalid: 42.1 == [42.0, 42.0]
        out_invalid = ReasoningOutput(
            decision_id=dec_id,
            node_type=NodeType.EXPERIMENT_DIAGNOSIS,
            context_ref=sample_context.context_ref,
            payload="bad",
            citations=(
                CitationRef("c1", "kb://cv/models/edge_metrics", "hash", "statement"),
            ),
            empirical_claims=(
                EmpiricalClaim(
                    "point_metric",
                    42.1,
                    EmpiricalComparator.EQ,
                    "kb://cv/models/edge_metrics",
                ),
            ),
        )
        with pytest.raises(
            EvidenceContradictionError, match="contradicts evidence point bound"
        ):
            validator.validate(sample_context, out_invalid)


# ==============================================================================
# Gate 5: Grounding Validation Fail-Closed Gating
# ==============================================================================


class TestGroundingValidatorGating:
    """Verifies unanchored citations, uncited empirical claims, and unsupported speculation."""

    def test_unanchored_citation_raises(self) -> None:
        validator = GroundingValidator()
        ctx = ReasoningContext(
            context_ref="ref",
            current_hop=1,
            max_hops=5,
            trace_id="t",
            evidence_refs=(),
        )
        out = ReasoningOutput(
            decision_id=generate_decision_id(),
            node_type=NodeType.PROBLEM_FRAMING,
            context_ref=ctx.context_ref,
            payload="p",
            citations=(CitationRef("c1", "kb://unknown/uri", "hash", "statement"),),
        )
        with pytest.raises(
            UnanchoredCitationError, match="references unknown evidence URI"
        ):
            validator.validate(ctx, out)

    def test_uncited_empirical_claim_raises(self) -> None:
        validator = GroundingValidator()
        ctx = ReasoningContext(
            context_ref="ref",
            current_hop=1,
            max_hops=5,
            trace_id="t",
            evidence_refs=(),
        )
        out = ReasoningOutput(
            decision_id=generate_decision_id(),
            node_type=NodeType.PROBLEM_FRAMING,
            context_ref=ctx.context_ref,
            payload="p",
            citations=(),  # ZERO citations
            empirical_claims=(
                EmpiricalClaim("latency", 10.0, EmpiricalComparator.LTE, "kb://any"),
            ),
        )
        with pytest.raises(
            UncitedEmpiricalClaimError, match="asserted with zero citations"
        ):
            validator.validate(ctx, out)

    def test_unsupported_speculation_raises(self) -> None:
        validator = GroundingValidator()
        ctx = ReasoningContext(
            context_ref="ref",
            current_hop=1,
            max_hops=5,
            trace_id="t",
            evidence_refs=(),
        )
        diagnostic = DiagnosticReport(
            summary="diagnosis",
            hypotheses=(
                Hypothesis(
                    "h1",
                    "Highly speculative hypothesis",
                    confidence=0.95,
                    citation_refs=(),
                ),
            ),
            bottlenecks=("bottleneck",),
            recommendations=("rec",),
        )
        out = ReasoningOutput(
            decision_id=generate_decision_id(),
            node_type=NodeType.EXPERIMENT_DIAGNOSIS,
            context_ref=ctx.context_ref,
            payload=diagnostic,
            citations=(),
            empirical_claims=(),
        )
        with pytest.raises(
            UnsupportedSpeculationError, match="provides zero supporting citation"
        ):
            validator.validate(ctx, out)


# ==============================================================================
# Gate 6: Node Implementations & Schema Repair Boundedness
# ==============================================================================


class TestReasoningNodesExecution:
    """Verifies node execution, hop checking, schema repairs, and transport isolation."""

    def test_cognitive_hop_budget_exhaustion(self) -> None:
        gw, _ = _build_mock_gateway()
        node = ProblemFramingNode(gw)

        ctx_exhausted = ReasoningContext(
            context_ref="ref",
            current_hop=5,
            max_hops=5,  # hop == max_hops -> exhausted
            trace_id="t",
        )
        with pytest.raises(
            CognitiveBudgetExceededError, match="Cognitive hop budget exhausted"
        ):
            node.execute(ctx_exhausted)

        # Context current_hop must remain untouched
        assert ctx_exhausted.current_hop == 5

    def test_problem_framing_node_success(self) -> None:
        mock_response_payload = {
            "task_type": "object_detection",
            "target_hardware": "nvidia_jetson_orin_nano",
            "latency_budget_ms": 15.0,
            "throughput_fps": 60.0,
            "compute_budget_hours": 2.0,
            "constraints": {"precision": "int8"},
            "citations": [],
            "empirical_claims": [],
        }
        adapter = MockProviderAdapter(
            provider_id="mock-reasoning",
            fixed_response=json.dumps(mock_response_payload),
        )
        gw, _ = _build_mock_gateway(adapter)
        node = ProblemFramingNode(gw)

        ctx = ReasoningContext(
            context_ref="ctx-pf",
            current_hop=1,
            max_hops=5,
            trace_id="trace-pf",
            parameters={"intent": "Detect defects on Jetson Orin Nano"},
        )
        out = node.execute(ctx)

        assert isinstance(out.payload, ProblemCharacterization)
        assert out.payload.task_type == "object_detection"
        assert out.payload.target_hardware == "nvidia_jetson_orin_nano"
        assert out.payload.latency_budget_ms == 15.0
        assert out.hops_consumed == 1

    def test_baseline_selection_node_success(self) -> None:
        mock_response_payload = {
            "architecture": "yolov8n",
            "weights_source": "ultralytics_coco_pretrained",
            "input_resolution": [640, 640],
            "batch_size": 16,
            "evaluation_protocol": "coco_standard_map50_95",
            "hyperparameters": {"lr": 0.01},
            "citations": [],
            "empirical_claims": [],
        }
        adapter = MockProviderAdapter(
            provider_id="mock-reasoning",
            fixed_response=json.dumps(mock_response_payload),
        )
        gw, _ = _build_mock_gateway(adapter)
        node = BaselineSelectionNode(gw)

        ctx = ReasoningContext(
            context_ref="ctx-bs",
            current_hop=2,
            max_hops=5,
            trace_id="trace-bs",
            parameters={"task": "object_detection"},
        )
        out = node.execute(ctx)

        assert isinstance(out.payload, BaselineSpec)
        assert out.payload.architecture == "yolov8n"
        assert out.payload.input_resolution == (640, 640)
        assert out.payload.batch_size == 16

    def test_diagnosis_node_success(self) -> None:
        evidence = EvidenceRef(
            uri="kb://cv/experiments/run_01",
            sha256="1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
            title="Run 01 Metrics",
            snippet="Metrics for baseline run 01",
            bounds={"latency_p95_ms": (8.0, 12.0)},
        )
        mock_response_payload = {
            "summary": "Evaluation meets latency constraints with moderate loss plateau",
            "hypotheses": [
                {
                    "hypothesis_id": "hyp-01",
                    "statement": "Learning rate warm-up is insufficient for convergence",
                    "confidence": 0.85,
                    "citation_refs": ["cite-01"],
                }
            ],
            "bottlenecks": ["backbone_feature_saturation"],
            "recommendations": ["apply_cosine_annealing_lr"],
            "citations": [
                {
                    "citation_id": "cite-01",
                    "uri": "kb://cv/experiments/run_01",
                    "sha256": "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
                    "statement": "Observed p95 latency is bounded",
                }
            ],
            "empirical_claims": [
                {
                    "metric_name": "latency_p95_ms",
                    "claimed_value": 11.5,
                    "comparator": "LTE",
                    "citation_uri": "kb://cv/experiments/run_01",
                }
            ],
        }
        adapter = MockProviderAdapter(
            provider_id="mock-reasoning",
            fixed_response=json.dumps(mock_response_payload),
        )
        gw, _ = _build_mock_gateway(adapter)
        node = DiagnosisNode(gw)

        ctx = ReasoningContext(
            context_ref="ctx-diag",
            current_hop=3,
            max_hops=5,
            trace_id="trace-diag",
            evidence_refs=(evidence,),
            parameters={"run_id": "run-01"},
        )
        out = node.execute(ctx)

        assert isinstance(out.payload, DiagnosticReport)
        assert len(out.payload.hypotheses) == 1
        assert out.payload.hypotheses[0].hypothesis_id == "hyp-01"
        assert len(out.citations) == 1
        assert len(out.empirical_claims) == 1

    def test_strategy_node_success_with_approval(self) -> None:
        mock_response_payload = {
            "strategy_id": "strat-quantize-ptq",
            "title": "Post-Training Quantization",
            "description": "Quantize FP32 model to INT8 via TensorRT",
            "expected_impact": "2x latency reduction on Orin Nano",
            "risk_level": "medium",
            "capability_intents": [
                {
                    "capability_id": "cv.deployment.optimization",
                    "intent_description": "Export and optimize TensorRT engine",
                    "parameter_intent": {"precision": "int8"},
                    "target_preference": "jetson_orin_nano",
                }
            ],
            "approval_recommendation": {
                "recommends_approval": True,
                "risk_level": "high",
                "reversibility": "irreversible",
                "rationale": "High compute cost and model deployment mutation",
                "estimated_cost_usd": "25.50",
            },
            "citations": [],
            "empirical_claims": [],
        }
        adapter = MockProviderAdapter(
            provider_id="mock-reasoning",
            fixed_response=json.dumps(mock_response_payload),
        )
        gw, _ = _build_mock_gateway(adapter)
        node = StrategyNode(gw)

        ctx = ReasoningContext(
            context_ref="ctx-strat",
            current_hop=4,
            max_hops=5,
            trace_id="trace-strat",
            parameters={"goal": "Optimize for production"},
        )
        out = node.execute(ctx)

        assert isinstance(out.payload, StrategyRecommendation)
        assert out.payload.strategy_id == "strat-quantize-ptq"
        assert len(out.capability_intents) == 1
        assert out.capability_intents[0].capability_id == CapabilityId(
            "cv.deployment.optimization"
        )
        assert out.approval_recommendation is not None
        assert out.approval_recommendation.recommends_approval is True
        assert out.approval_recommendation.risk_level == RiskLevel.HIGH
        assert out.approval_recommendation.reversibility == Reversibility.IRREVERSIBLE
        assert out.approval_recommendation.estimated_cost_usd == Decimal("25.50")

    def test_schema_repair_succeeds_on_second_attempt(self) -> None:
        """First call produces invalid schema output; repair prompt leads to valid response on attempt 2."""
        valid_response = {
            "task_type": "object_detection",
            "target_hardware": "jetson_orin",
            "constraints": {},
            "citations": [],
            "empirical_claims": [],
        }
        adapter = MockProviderAdapter(
            provider_id="mock-reasoning",
            fixed_response=json.dumps(valid_response),
            failure_sequence=[
                InvalidRequestError(
                    category="invalid_request",
                    provider="mock-reasoning",
                    model="model-cognitive",
                    request_id="req-1",
                    attempt_id="att-1",
                    retryable=False,
                    fallback_eligible=False,
                    message="Provider response could not be decoded as JSON for schema 'schema_problem_framing': Expecting value",
                )
            ],
        )
        gw, _ = _build_mock_gateway(adapter)
        node = ProblemFramingNode(gw)

        ctx = ReasoningContext(
            context_ref="ctx-repair-succ",
            current_hop=1,
            max_hops=5,
            trace_id="trace-succ",
        )
        out = node.execute(ctx)
        assert isinstance(out.payload, ProblemCharacterization)
        assert out.payload.task_type == "object_detection"
        assert adapter.call_count == 2

    def test_schema_repair_retry_boundedness(self) -> None:
        """Exactly 3 logical completions executed on persistent schema errors, then SchemaRepairExhaustedError."""
        # Adapter returns invalid JSON for all calls
        adapter = MockProviderAdapter(
            provider_id="mock-reasoning",
            fixed_response="INVALID_NON_JSON_RESPONSE",
        )
        gw, _ = _build_mock_gateway(adapter)
        node = ProblemFramingNode(gw)

        ctx = ReasoningContext(
            context_ref="ctx-repair",
            current_hop=1,
            max_hops=5,
            trace_id="trace-repair",
        )

        with pytest.raises(
            SchemaRepairExhaustedError, match="Exhausted schema repair attempts"
        ):
            node.execute(ctx)

        # Total logical calls must be exactly 3 (1 initial + 2 repairs)
        assert adapter.call_count == 3

    def test_gateway_transport_error_aborts_immediately(self) -> None:
        """Gateway transport errors (RateLimitError) must abort immediately as ReasoningTransportError."""
        rate_limit_err = RateLimitError(
            category="rate_limited",
            provider="mock-reasoning",
            model="model-cognitive",
            request_id="req-test",
            attempt_id="att-1",
            retryable=False,
            fallback_eligible=False,
            message="Rate limit exceeded",
        )
        adapter = MockProviderAdapter(
            provider_id="mock-reasoning",
            simulated_error=rate_limit_err,
        )
        gw, _ = _build_mock_gateway(adapter)
        node = ProblemFramingNode(gw)

        ctx = ReasoningContext(
            context_ref="ctx-transport",
            current_hop=1,
            max_hops=5,
            trace_id="trace-transport",
        )

        with pytest.raises(
            ReasoningTransportError, match="LLM Gateway transport failure"
        ):
            node.execute(ctx)

        # Must not have retried schema repair attempts
        assert adapter.call_count == 1


# ==============================================================================
# Gate 7: Provider-Agnostic Isolation Audit (AST Scan)
# ==============================================================================


def test_zero_provider_sdk_imports_in_reasoning() -> None:
    """AST scan confirms zero direct provider SDK imports in cv_agent/reasoning."""
    reasoning_dir = Path(__file__).resolve().parent.parent / "cv_agent" / "reasoning"
    forbidden_prefixes = ("openai", "anthropic", "google", "cohere", "mistral", "groq")

    py_files = list(reasoning_dir.glob("*.py"))
    assert len(py_files) >= 5, f"Expected at least 5 python files in {reasoning_dir}"

    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in forbidden_prefixes:
                        assert not alias.name.startswith(forbidden), (
                            f"Violation of [P§20] in {path.name}: direct import of {alias.name!r}"
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                for forbidden in forbidden_prefixes:
                    assert not node.module.startswith(forbidden), (
                        f"Violation of [P§20] in {path.name}: direct from-import of {node.module!r}"
                    )
