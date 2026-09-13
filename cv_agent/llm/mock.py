"""cv_agent.llm.mock — Deterministic MockProviderAdapter and legacy FakeLLMProvider.

Provides fully configurable deterministic behavior for unit and contract testing:
multimodal content handling, structured output synthesis, tool call generation,
streaming lifecycle events, and scriptable failure sequences.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

from cv_agent.llm.base import LLMProvider as LegacyLLMProvider
from cv_agent.llm.contracts import (
    CostRecord,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    ModelProfile,
    StreamDoneEvent,
    StreamErrorEvent,
    TextDeltaEvent,
    ToolCall,
    UsageEvent,
)
from cv_agent.llm.errors import LLMError
from cv_agent.llm.provider import LLMProvider


class MockProviderAdapter(LLMProvider):
    """Deterministic mock provider adapter implementing LLMProvider protocol."""

    def __init__(
        self,
        provider_id: str = "mock",
        *,
        fixed_response: str | None = None,
        fixed_structured_output: Any | None = None,
        fixed_tool_calls: tuple[ToolCall, ...] = (),
        fixed_cost: CostRecord | None = None,
        simulated_tokens: tuple[int, int] = (15, 25),
        simulated_error: LLMError | None = None,
        failure_sequence: list[LLMError] | None = None,
        stream_chunks: tuple[str, ...] = ("Mock", " response", " stream"),
        emit_usage_in_stream: bool = True,
        stream_error: LLMError | None = None,
        supports_idempotency_keys: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.supports_idempotency_keys = supports_idempotency_keys
        self._fixed_response = fixed_response
        self._fixed_structured_output = fixed_structured_output
        self._fixed_tool_calls = fixed_tool_calls
        self._fixed_cost = fixed_cost
        self._simulated_tokens = simulated_tokens
        self._simulated_error = simulated_error
        self._failure_sequence = list(failure_sequence) if failure_sequence else []
        self._stream_chunks = stream_chunks
        self._emit_usage_in_stream = emit_usage_in_stream
        self._stream_error = stream_error
        self._call_count: int = 0
        self._recorded_requests: list[LLMRequest] = []

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def recorded_requests(self) -> tuple[LLMRequest, ...]:
        return tuple(self._recorded_requests)

    def reset(self) -> None:
        self._call_count = 0
        self._recorded_requests.clear()

    def complete(self, request: LLMRequest, model: ModelProfile) -> LLMResponse:
        self._call_count += 1
        self._recorded_requests.append(request)

        # Check scriptable failure sequence first
        if self._failure_sequence:
            err = self._failure_sequence.pop(0)
            raise err

        if self._simulated_error is not None:
            raise self._simulated_error

        # Generate response content
        if self._fixed_response is not None:
            content = self._fixed_response
        elif request.response_schema is not None:
            # If structured output requested without fixed response, synthesize valid JSON
            props = request.response_schema.schema.get("properties")
            synthetic = (
                {str(k): f"mock_value_{k}" for k in props}
                if isinstance(props, Mapping)
                else {}
            )
            content = json.dumps(synthetic)
        else:
            first_text = ""
            if request.messages and request.messages[0].content:
                c = request.messages[0].content[0]
                if hasattr(c, "text"):
                    first_text = c.text[:40]
            content = f"[{self.provider_id}:{model.model_id}] Response #{self._call_count}: {first_text}"

        structured_output = self._fixed_structured_output
        if structured_output is None and request.response_schema is not None:
            try:
                structured_output = json.loads(content)
            except Exception:  # noqa: BLE001
                structured_output = None

        prompt_tokens, comp_tokens = self._simulated_tokens
        cost = self._fixed_cost

        return LLMResponse(
            content=content,
            provider=self.provider_id,
            model=model.model_id,
            request_id=request.request_id,
            trace_id=request.trace_id,
            attempt_id=f"mock_att_{self._call_count}",
            latency_ms=1.0,
            input_tokens=prompt_tokens,
            output_tokens=comp_tokens,
            cost=cost,
            tool_calls=self._fixed_tool_calls,
            structured_output=structured_output,
            fallback_used=False,
        )

    def stream(
        self, request: LLMRequest, model: ModelProfile
    ) -> Iterator[LLMStreamEvent]:
        self._call_count += 1
        self._recorded_requests.append(request)

        if self._simulated_error is not None:
            yield StreamErrorEvent(type="error", error=self._simulated_error)
            return

        if self._stream_error is not None:
            # Yield some text deltas before error
            if self._stream_chunks:
                yield TextDeltaEvent(type="text_delta", delta=self._stream_chunks[0])
            yield StreamErrorEvent(type="error", error=self._stream_error)
            return

        for chunk in self._stream_chunks:
            yield TextDeltaEvent(type="text_delta", delta=chunk)

        # Emit UsageEvent before StreamDoneEvent if enabled (ADR-0002 §10)
        if self._emit_usage_in_stream:
            prompt_tokens, comp_tokens = self._simulated_tokens
            yield UsageEvent(
                type="usage",
                input_tokens=prompt_tokens,
                output_tokens=comp_tokens,
            )

        yield StreamDoneEvent(type="done")


# --- Legacy Compatibility Wrapper ---


class FakeLLMProvider(LegacyLLMProvider):
    """Legacy compatibility wrapper preserving the original cv_agent.llm.base.LLMProvider interface."""

    PROVIDER_NAME: str = "mock"

    def __init__(
        self,
        model: str = "fake-1",
        fixed_response: str | None = None,
    ) -> None:
        super().__init__(model=model)
        self._adapter = MockProviderAdapter(
            provider_id=self.PROVIDER_NAME,
            fixed_response=fixed_response,
        )

    @property
    def provider_name(self) -> str:
        return self.PROVIDER_NAME

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def call_count(self) -> int:
        return self._adapter.call_count

    def reset(self) -> None:
        self._adapter.reset()

    def complete(self, request: Any) -> Any:
        from cv_agent.llm.base import LLMResponse as LegacyLLMResponse
        from cv_agent.llm.contracts import Message, ModelProfile

        if hasattr(request, "prompt"):
            # Legacy request object
            typed_req = LLMRequest(
                request_id="legacy_req",
                idempotency_key="legacy_idem",
                trace_id="legacy_trace",
                messages=(Message.text("user", request.prompt),),
            )
        else:
            typed_req = request

        model_profile = ModelProfile(
            provider_id=self.PROVIDER_NAME,
            model_id=self._model,
            profile_version="1.0",
            context_window_tokens=4096,
            input_modalities=frozenset({"text", "image"}),
            output_modalities=frozenset({"text"}),
            supports_structured_output=True,
            supports_tool_calls=True,
            supports_streaming=True,
            supports_reasoning=True,
        )

        resp = self._adapter.complete(typed_req, model_profile)

        # Wrap in legacy response structure if caller expects legacy type
        prompt_str = getattr(request, "prompt", resp.content)
        prompt_tokens = len(prompt_str.split())
        comp_tokens = len(resp.content.split())
        return LegacyLLMResponse(
            content=resp.content,
            model=self._model,
            provider=self.PROVIDER_NAME,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": comp_tokens,
                "total_tokens": prompt_tokens + comp_tokens,
            },
        )

    def __repr__(self) -> str:
        return f"FakeLLMProvider(provider={self.provider_name!r}, model={self.model_name!r})"
