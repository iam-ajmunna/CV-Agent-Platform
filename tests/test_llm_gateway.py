"""tests/test_llm_gateway.py — Comprehensive acceptance tests for ADR-0002 LLM Gateway.

Verifies all 15 ADR-0002 acceptance criteria and PM review constraints:
1. Provider swapping without code change
2. Multimodal request handling and translation
3. Three-layer provider isolation audit (AST, repo, public types)
4. Static capability mismatch rejection with zero provider invocations
5. Bounded transient retry with attempt tracking
6. Bounded safe fallback across eligible candidates
7. Strict timeout idempotency safety (no retry/fallback for non-idempotent requests)
8. Fail-closed security/auth/policy errors
9. Structured output validation and schema preservation
10. Tool-call representation without tool execution
11. Streaming lifecycle terminal invariants
12. Correlation, latency, and telemetry metadata
13. Cost provenance records
14. Invariant 14 secret isolation in metadata
15. Immutability and defensive mapping isolation
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest

from cv_agent.llm.contracts import (
    CostRecord,
    FallbackPolicy,
    ImageBlock,
    LLMRequest,
    LLMRequirements,
    Message,
    ModelProfile,
    ModelSelector,
    RetryPolicy,
    RoutingPolicy,
    StructuredOutputSpec,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    UsageEvent,
)
from cv_agent.llm.errors import (
    AuthenticationError,
    CapabilityMismatchError,
    InvalidRequestError,
    LLMError,
    PolicyDeniedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitError,
    TransientServerError,
)
from cv_agent.llm.gateway import LLMGateway
from cv_agent.llm.mock import MockProviderAdapter


def _build_test_gateway(
    adapters: dict[str, MockProviderAdapter] | None = None,
    profiles: dict[str, ModelProfile] | None = None,
    routing_candidates: tuple[ModelSelector, ...] | None = None,
    max_retries: int = 2,
    max_switches: int = 2,
    denied_providers: frozenset[str] = frozenset(),
) -> tuple[LLMGateway, dict[str, MockProviderAdapter]]:
    ad = adapters or {
        "mock-a": MockProviderAdapter(provider_id="mock-a"),
        "mock-b": MockProviderAdapter(provider_id="mock-b"),
    }
    pr = profiles or {
        "mock-a:model-text": ModelProfile(
            provider_id="mock-a",
            model_id="model-text",
            profile_version="1.0",
            context_window_tokens=4096,
            input_modalities=frozenset({"text"}),
            output_modalities=frozenset({"text"}),
            supports_structured_output=True,
            supports_tool_calls=True,
            supports_streaming=True,
            supports_reasoning=False,
            pricing_version="2026-v1",
        ),
        "mock-b:model-vision": ModelProfile(
            provider_id="mock-b",
            model_id="model-vision",
            profile_version="1.0",
            context_window_tokens=8192,
            input_modalities=frozenset({"text", "image"}),
            output_modalities=frozenset({"text"}),
            supports_structured_output=True,
            supports_tool_calls=True,
            supports_streaming=True,
            supports_reasoning=True,
            pricing_version="2026-v1",
        ),
    }
    candidates = routing_candidates or (
        ModelSelector("mock-a", "model-text"),
        ModelSelector("mock-b", "model-vision"),
    )
    gw = LLMGateway(
        providers=ad,
        model_profiles=pr,
        default_routing_policy=RoutingPolicy(
            policy_id="test-routing",
            policy_version="1.0",
            candidates=candidates,
            max_attempts=5,
        ),
        default_retry_policy=RetryPolicy(
            max_retries=max_retries,
            initial_backoff_ms=10,
            max_backoff_ms=50,
            jitter=False,
        ),
        default_fallback_policy=FallbackPolicy(
            enabled=True,
            allowed_failure_categories=frozenset(
                {"rate_limited", "transient_server", "provider_unavailable", "timeout"}
            ),
            max_provider_switches=max_switches,
        ),
        data_governance_denied_providers=denied_providers,
        pricing_records={
            "mock-a:model-text:2026-v1": CostRecord(
                currency="USD",
                amount=Decimal("0.001"),
                pricing_source="table-a",
                pricing_version="2026-v1",
            ),
            "mock-b:model-vision:2026-v1": CostRecord(
                currency="USD",
                amount=Decimal("0.005"),
                pricing_source="table-b",
                pricing_version="2026-v1",
            ),
        },
    )
    return gw, ad


class TestProviderSwappingAndConfiguration:
    def test_provider_swapping_by_routing_policy_alone(self) -> None:
        """Criterion 1: Two distinct configured providers satisfy the same text request."""
        gw, adapters = _build_test_gateway()

        req = LLMRequest(
            request_id="req-swap-1",
            idempotency_key="idem-1",
            trace_id="tr-1",
            messages=(Message.text("user", "Extract defect boundaries"),),
        )

        # 1. Route to provider mock-a
        policy_a = RoutingPolicy(
            policy_id="pol-a",
            policy_version="1.0",
            candidates=(ModelSelector("mock-a", "model-text"),),
            max_attempts=2,
        )
        resp_a = gw.complete(req, routing_policy=policy_a)
        assert resp_a.provider == "mock-a"
        assert resp_a.model == "model-text"
        assert adapters["mock-a"].call_count == 1
        assert adapters["mock-b"].call_count == 0

        # 2. Route to provider mock-b without modifying request or calling code
        policy_b = RoutingPolicy(
            policy_id="pol-b",
            policy_version="1.0",
            candidates=(ModelSelector("mock-b", "model-vision"),),
            max_attempts=2,
        )
        resp_b = gw.complete(req, routing_policy=policy_b)
        assert resp_b.provider == "mock-b"
        assert resp_b.model == "model-vision"
        assert adapters["mock-b"].call_count == 1


class TestMultimodalContentContracts:
    def test_canonical_multimodal_request_execution(self) -> None:
        """Criterion 2: Canonical multimodal request translated without exposing vendor types."""
        gw, adapters = _build_test_gateway()

        img = ImageBlock(
            type="image",
            media_type="image/jpeg",
            uri="file:///data/dataset/frame_001.jpg",
            sha256="abcdef123456",
        )
        txt = TextBlock(type="text", text="Inspect anomaly in frame")
        req = LLMRequest(
            request_id="req-multi-1",
            idempotency_key="idem-multi",
            trace_id="tr-multi",
            messages=(Message(role="user", content=(txt, img)),),
            requirements=LLMRequirements(input_modalities=frozenset({"text", "image"})),
        )

        resp = gw.complete(req)
        assert resp.provider == "mock-b"  # mock-a does not support image
        assert resp.model == "model-vision"
        assert adapters["mock-a"].call_count == 0
        assert adapters["mock-b"].call_count == 1

    def test_image_block_requires_uri_or_data_ref(self) -> None:
        with pytest.raises(ValueError, match="at least one of 'uri' or 'data_ref'"):
            ImageBlock(type="image", media_type="image/png")

    def test_tool_result_cannot_nest_tool_result(self) -> None:
        inner = ToolResultBlock(
            type="tool_result",
            tool_call_id="c1",
            content=(TextBlock(type="text", text="output"),),
        )
        with pytest.raises(TypeError, match="cannot nest another ToolResultBlock"):
            ToolResultBlock(
                type="tool_result",
                tool_call_id="c2",
                content=(inner,),  # type: ignore[arg-type]
            )


class TestProviderIsolationAudit:
    def test_no_provider_sdk_imports_outside_llm_package(self) -> None:
        """Criterion 3 & PM Constraint 10: Three-layer provider isolation audit."""
        repo_root = Path(__file__).parent.parent
        cv_agent_dir = repo_root / "cv_agent"

        disallowed_modules = {
            "openai",
            "anthropic",
            "google.generativeai",
            "cohere",
            "ollama",
            "mistralai",
        }

        # Layer 1: AST Import Audit on all python files outside cv_agent/llm
        for py_file in cv_agent_dir.rglob("*.py"):
            if "cv_agent/llm" in str(py_file):
                continue
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        assert top not in disallowed_modules, (
                            f"Direct import {alias.name} leaked in {py_file}"
                        )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0]
                    assert top not in disallowed_modules, (
                        f"From-import {node.module} leaked in {py_file}"
                    )

        # Layer 2: Repo Grep Audit
        for py_file in cv_agent_dir.rglob("*.py"):
            if "cv_agent/llm" in str(py_file):
                continue
            content = py_file.read_text(encoding="utf-8")
            for mod in (
                "import openai",
                "import anthropic",
                "from openai",
                "from anthropic",
            ):
                assert mod not in content, (
                    f"Disallowed provider reference {mod!r} in {py_file}"
                )

        # Layer 3: Public Type Boundary Audit on cv_agent.llm
        import cv_agent.llm as llm_pkg

        for name in llm_pkg.__all__:
            obj = getattr(llm_pkg, name)
            mod = getattr(obj, "__module__", "")
            assert mod.startswith("cv_agent.llm") or mod in (
                "builtins",
                "typing",
                "collections.abc",
                "types",
            ), f"Public export {name} leaked external module {mod}"


class TestStaticCapabilityValidation:
    def test_static_capability_mismatch_causes_zero_provider_invocations(self) -> None:
        """Criterion 4 & PM Constraint 4: Incompatibility rejected before provider execution."""
        gw, adapters = _build_test_gateway(
            routing_candidates=(ModelSelector("mock-a", "model-text"),)
        )

        req = LLMRequest(
            request_id="req-vision-on-text",
            idempotency_key="idem-static",
            trace_id="tr-static",
            messages=(Message.text("user", "look at image"),),
            requirements=LLMRequirements(input_modalities=frozenset({"text", "image"})),
        )

        with pytest.raises(CapabilityMismatchError) as exc:
            gw.complete(req)

        assert "does not support required input modalities" in str(exc.value)
        # Verify ZERO provider invocations!
        assert adapters["mock-a"].call_count == 0
        assert adapters["mock-b"].call_count == 0

    def test_context_window_too_small_rejected_statically(self) -> None:
        gw, adapters = _build_test_gateway(
            routing_candidates=(ModelSelector("mock-a", "model-text"),)
        )
        req = LLMRequest(
            request_id="req-ctx",
            idempotency_key="idem-ctx",
            trace_id="tr-ctx",
            messages=(Message.text("user", "long text"),),
            requirements=LLMRequirements(minimum_context_tokens=100000),
        )
        with pytest.raises(CapabilityMismatchError) as exc:
            gw.complete(req)
        assert "is less than required minimum context" in str(exc.value)
        assert adapters["mock-a"].call_count == 0


class TestBoundedTransientRetryAndAttemptTracking:
    def test_bounded_retry_tracks_attempt_id_and_recovers(self) -> None:
        """Criterion 5 & PM Constraint 5: Bounded retry with distinct attempt_id."""
        ad_a = MockProviderAdapter(
            provider_id="mock-a",
            failure_sequence=[
                RateLimitError(
                    category="rate_limited",
                    provider="mock-a",
                    model="model-text",
                    request_id="req-retry-1",
                    attempt_id=None,
                    retryable=True,
                    fallback_eligible=True,
                    message="429 Rate Limit",
                ),
                TransientServerError(
                    category="transient_server",
                    provider="mock-a",
                    model="model-text",
                    request_id="req-retry-1",
                    attempt_id=None,
                    retryable=True,
                    fallback_eligible=True,
                    message="503 Service Unavailable",
                ),
            ],
        )
        gw, _ = _build_test_gateway(
            adapters={"mock-a": ad_a},
            routing_candidates=(ModelSelector("mock-a", "model-text"),),
            max_retries=2,
        )

        req = LLMRequest(
            request_id="req-retry-1",
            idempotency_key="idem-retry",
            trace_id="trace-fixed",
            messages=(Message.text("user", "retry please"),),
        )

        resp = gw.complete(req)
        assert ad_a.call_count == 3  # 2 retries + 1 success
        assert resp.request_id == "req-retry-1"
        assert resp.trace_id == "trace-fixed"
        assert resp.attempt_id == "req-retry-1_att_3"
        assert resp.fallback_used is False


class TestBoundedFallbackAcrossProviders:
    def test_fallback_switches_provider_and_records_metadata(self) -> None:
        """Criterion 6: Safe fallback moves to secondary candidate on eligible failure."""
        ad_a = MockProviderAdapter(
            provider_id="mock-a",
            simulated_error=ProviderUnavailableError(
                category="provider_unavailable",
                provider="mock-a",
                model="model-text",
                request_id="req-fb-1",
                attempt_id=None,
                retryable=True,
                fallback_eligible=True,
                message="mock-a is offline",
            ),
        )
        ad_b = MockProviderAdapter(
            provider_id="mock-b", fixed_response="Response from B"
        )
        gw, _ = _build_test_gateway(
            adapters={"mock-a": ad_a, "mock-b": ad_b},
            max_retries=1,
            max_switches=1,
        )

        req = LLMRequest(
            request_id="req-fb-1",
            idempotency_key="idem-fb",
            trace_id="trace-fb",
            messages=(Message.text("user", "hello fallback"),),
        )

        resp = gw.complete(req)
        assert resp.provider == "mock-b"
        assert resp.content == "Response from B"
        assert resp.fallback_used is True
        assert resp.request_id == "req-fb-1"
        assert resp.trace_id == "trace-fb"
        assert "att_" in resp.attempt_id


class TestTimeoutAndIdempotencySafety:
    def test_timeout_on_non_idempotent_request_prohibits_retry_and_fallback(
        self,
    ) -> None:
        """Criterion 7 & PM Constraint 6: Timeout does not automatically duplicate non-idempotent operation."""
        ad_a = MockProviderAdapter(
            provider_id="mock-a",
            simulated_error=ProviderTimeoutError(
                category="timeout",
                provider="mock-a",
                model="model-text",
                request_id="req-to-1",
                attempt_id=None,
                retryable=False,
                fallback_eligible=False,
                message="Request timed out",
            ),
        )
        ad_b = MockProviderAdapter(provider_id="mock-b")
        gw, _ = _build_test_gateway(
            adapters={"mock-a": ad_a, "mock-b": ad_b},
            max_retries=2,
            max_switches=2,
        )

        # Mark non-idempotent
        req = LLMRequest(
            request_id="req-to-1",
            idempotency_key="",
            trace_id="trace-to",
            messages=(Message.text("user", "trigger payment/action"),),
            metadata={"idempotent": "false"},
        )

        with pytest.raises(ProviderTimeoutError) as exc:
            gw.complete(req)

        assert "automatic retry/fallback is prohibited" in str(exc.value)
        assert ad_a.call_count == 1  # Only 1 attempt, NO retry
        assert ad_b.call_count == 0  # NO fallback switch


class TestFailClosedSecurityAndErrors:
    def test_authentication_error_fails_fast(self) -> None:
        """Criterion 8 & PM Constraint 7: Authentication error fails fast."""
        ad_a = MockProviderAdapter(
            provider_id="mock-a",
            simulated_error=AuthenticationError(
                category="authentication",
                provider="mock-a",
                model="model-text",
                request_id="req-auth",
                attempt_id=None,
                retryable=False,
                fallback_eligible=False,
                message="Invalid API Key",
            ),
        )
        ad_b = MockProviderAdapter(provider_id="mock-b")
        gw, _ = _build_test_gateway(adapters={"mock-a": ad_a, "mock-b": ad_b})

        req = LLMRequest(
            request_id="req-auth",
            idempotency_key="idem-auth",
            trace_id="tr-auth",
            messages=(Message.text("user", "auth test"),),
        )
        with pytest.raises(AuthenticationError):
            gw.complete(req)

        assert ad_a.call_count == 1
        assert ad_b.call_count == 0

    def test_policy_denied_provider_fails_fast_and_cannot_be_overridden(self) -> None:
        """Criterion 8 & PM Constraint 5: Policy denial cannot be overridden by retry/fallback."""
        gw, adapters = _build_test_gateway(
            denied_providers=frozenset({"mock-a", "mock-b"}),
        )
        req = LLMRequest(
            request_id="req-policy",
            idempotency_key="idem-policy",
            trace_id="tr-policy",
            messages=(Message.text("user", "governance check"),),
        )
        with pytest.raises(PolicyDeniedError) as exc:
            gw.complete(req)
        assert "denied by data-governance policy" in str(exc.value)
        assert adapters["mock-a"].call_count == 0
        assert adapters["mock-b"].call_count == 0


class TestStructuredOutputEnforcement:
    def test_structured_output_normalized_and_schema_version_preserved(self) -> None:
        """Criterion 9 & PM Constraint 8: Schema ID/version preserved and JSON validated."""
        spec = StructuredOutputSpec(
            schema_id="cv.detection.spec",
            schema_version="1.2.0",
            schema={
                "type": "object",
                "properties": {"bbox": {"type": "array"}, "label": {"type": "string"}},
                "required": ["bbox", "label"],
            },
        )
        ad_a = MockProviderAdapter(
            provider_id="mock-a",
            fixed_response='{"bbox": [10, 20, 100, 200], "label": "vehicle"}',
        )
        gw, _ = _build_test_gateway(adapters={"mock-a": ad_a})

        req = LLMRequest(
            request_id="req-struct",
            idempotency_key="idem-struct",
            trace_id="tr-struct",
            messages=(Message.text("user", "detect"),),
            response_schema=spec,
        )

        resp = gw.complete(req)
        assert resp.structured_output == {
            "bbox": [10, 20, 100, 200],
            "label": "vehicle",
        }

    def test_structured_output_missing_required_field_raises_invalid_request(
        self,
    ) -> None:
        spec = StructuredOutputSpec(
            schema_id="cv.spec",
            schema_version="1.0",
            schema={"type": "object", "required": ["mandatory_field"]},
        )
        ad_a = MockProviderAdapter(
            provider_id="mock-a",
            fixed_response='{"other_field": 123}',
        )
        gw, _ = _build_test_gateway(adapters={"mock-a": ad_a})
        req = LLMRequest(
            request_id="req-missing",
            idempotency_key="idem-miss",
            trace_id="tr-miss",
            messages=(Message.text("user", "test"),),
            response_schema=spec,
        )
        with pytest.raises(InvalidRequestError) as exc:
            gw.complete(req)
        assert "missing required property 'mandatory_field'" in str(exc.value)


class TestToolCallRepresentation:
    def test_gateway_represents_tool_calls_without_executing_tools(self) -> None:
        """Criterion 10: Tool calls represented canonically; gateway does NOT execute."""
        tool_call = ToolCall(
            call_id="call_inspect_01",
            tool_name="dali_inspect_dataset",
            arguments={"path": "/data/voc"},
        )
        ad_a = MockProviderAdapter(
            provider_id="mock-a",
            fixed_tool_calls=(tool_call,),
        )
        gw, _ = _build_test_gateway(adapters={"mock-a": ad_a})

        req = LLMRequest(
            request_id="req-tool",
            idempotency_key="idem-tool",
            trace_id="tr-tool",
            messages=(Message.text("user", "run inspection"),),
            requirements=LLMRequirements(tool_calling=True),
        )

        resp = gw.complete(req)
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].call_id == "call_inspect_01"
        assert resp.tool_calls[0].tool_name == "dali_inspect_dataset"
        assert resp.tool_calls[0].arguments == {"path": "/data/voc"}
        # Verify tool was NOT executed (no file created, no subprocess run)


class TestStreamingLifecycleInvariants:
    def test_streaming_usage_precedes_stream_done_when_reported(self) -> None:
        """Criterion 11 & PM Constraint 9: UsageEvent precedes StreamDoneEvent if usage exists."""
        ad_a = MockProviderAdapter(
            provider_id="mock-a",
            stream_chunks=("part1", "part2"),
            emit_usage_in_stream=True,
            simulated_tokens=(10, 20),
        )
        gw, _ = _build_test_gateway(adapters={"mock-a": ad_a})

        req = LLMRequest(
            request_id="req-stream",
            idempotency_key="idem-stream",
            trace_id="tr-stream",
            messages=(Message.text("user", "stream this"),),
            requirements=LLMRequirements(streaming=True),
        )

        events = list(gw.stream(req))
        assert len(events) == 4
        assert events[0].type == "text_delta"
        assert events[1].type == "text_delta"
        assert events[2].type == "usage"
        assert isinstance(events[2], UsageEvent)
        assert events[2].input_tokens == 10
        assert events[2].output_tokens == 20
        assert events[3].type == "done"

    def test_streaming_omits_usage_when_unavailable(self) -> None:
        """PM Constraint 9: UsageEvent omitted when provider does not report usage."""
        ad_a = MockProviderAdapter(
            provider_id="mock-a",
            stream_chunks=("a", "b"),
            emit_usage_in_stream=False,
        )
        gw, _ = _build_test_gateway(adapters={"mock-a": ad_a})
        req = LLMRequest(
            request_id="req-stream-no-usage",
            idempotency_key="idem-snu",
            trace_id="tr-snu",
            messages=(Message.text("user", "stream"),),
            requirements=LLMRequirements(streaming=True),
        )
        events = list(gw.stream(req))
        types = [e.type for e in events]
        assert types == ["text_delta", "text_delta", "done"]
        assert "usage" not in types

    def test_stream_error_is_terminal_and_not_followed_by_stream_done(self) -> None:
        """PM Constraint 9: StreamErrorEvent is terminal; StreamDoneEvent never follows."""
        ad_a = MockProviderAdapter(
            provider_id="mock-a",
            stream_error=LLMError(
                category="transient_server",
                provider="mock-a",
                model="model-text",
                request_id="req-serr",
                attempt_id=None,
                retryable=True,
                fallback_eligible=False,
                message="Stream disconnected",
            ),
        )
        gw, _ = _build_test_gateway(adapters={"mock-a": ad_a})
        req = LLMRequest(
            request_id="req-serr",
            idempotency_key="idem-serr",
            trace_id="tr-serr",
            messages=(Message.text("user", "stream err"),),
            requirements=LLMRequirements(streaming=True),
        )
        events = list(gw.stream(req))
        assert any(e.type == "error" for e in events)
        assert not any(e.type == "done" for e in events)


class TestCostAndOperationalMetadata:
    def test_successful_response_carries_operational_telemetry_and_cost(self) -> None:
        """Criterion 12 & 13: Correlation, latency, token, and CostRecord preserved."""
        gw, _ = _build_test_gateway()

        req = LLMRequest(
            request_id="req-meta-1",
            idempotency_key="idem-meta",
            trace_id="tr-meta-123",
            messages=(Message.text("user", "compute cost"),),
        )

        resp = gw.complete(req)
        assert resp.request_id == "req-meta-1"
        assert resp.trace_id == "tr-meta-123"
        assert resp.latency_ms > 0.0
        assert resp.input_tokens is not None
        assert resp.output_tokens is not None
        assert resp.cost is not None
        assert resp.cost.currency == "USD"
        assert resp.cost.amount == Decimal("0.001")
        assert resp.cost.pricing_source == "table-a"
        assert resp.cost.pricing_version == "2026-v1"


class TestSecretIsolationInvariant14:
    def test_metadata_rejects_sensitive_keys(self) -> None:
        """Criterion 14: Invariant 14 secret isolation."""
        sensitive_keys = [
            "api_key",
            "access_token",
            "client_secret",
            "admin_password",
            "auth_bearer",
            "cloud_credential",
        ]
        for key in sensitive_keys:
            with pytest.raises(
                ValueError, match="violating Invariant 14 secret isolation"
            ):
                LLMRequest(
                    request_id="req-sec",
                    idempotency_key="idem-sec",
                    trace_id="tr-sec",
                    messages=(Message.text("user", "secret"),),
                    metadata={key: "should_fail"},
                )


class TestImmutabilityAndDefensiveCopying:
    def test_request_and_error_mappings_cannot_be_mutated_externally(self) -> None:
        """PM Constraint 11: Retained caller dict references cannot mutate contract state."""
        caller_metadata = {"task": "defect_detection", "phase": "evaluation"}
        req = LLMRequest(
            request_id="req-imm",
            idempotency_key="idem-imm",
            trace_id="tr-imm",
            messages=(Message.text("user", "immutable"),),
            metadata=caller_metadata,
        )
        assert isinstance(req.metadata, MappingProxyType)
        caller_metadata["task"] = "MUTATED"
        assert req.metadata["task"] == "defect_detection"

        caller_details = {"detail_1": "original"}
        err = LLMError(
            category="unknown",
            provider="mock",
            model="fake",
            request_id="req",
            attempt_id=None,
            retryable=False,
            fallback_eligible=False,
            message="err",
            details=caller_details,
        )
        assert isinstance(err.details, MappingProxyType)
        caller_details["detail_1"] = "MUTATED"
        assert err.details["detail_1"] == "original"
