"""cv_agent.llm.gateway — Authoritative ADR-0002 LLM Gateway.

Implements provider-independent LLMGateway enforcing capability compatibility,
routing policies, bounded retry/fallback, structured output validation, and streaming.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from cv_agent.llm.contracts import (
    CostRecord,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    ModelProfile,
    ModelSelector,
    RetryPolicy,
    RoutingPolicy,
    StreamDoneEvent,
    StreamErrorEvent,
    StructuredOutputSpec,
    UsageEvent,
)
from cv_agent.llm.errors import (
    CapabilityMismatchError,
    InvalidRequestError,
    LLMError,
    PolicyDeniedError,
    ProviderTimeoutError,
    UnsupportedModelError,
)
from cv_agent.llm.provider import LLMProvider
from cv_agent.llm.schema import SchemaValidationError, validate_json_schema


class LLMGateway:
    """Authoritative ADR-0002 typed LLM Gateway.

    Handles model resolution, static capability checks, routing, bounded retries,
    fallback policies, idempotency and timeout safety, structured output validation,
    and normalized responses.
    """

    def __init__(
        self,
        providers: Mapping[str, LLMProvider],
        model_profiles: Mapping[str, ModelProfile],
        default_routing_policy: RoutingPolicy,
        default_retry_policy: RetryPolicy,
        default_fallback_policy: Any,
        data_governance_denied_providers: frozenset[str] = frozenset(),
        pricing_records: Mapping[str, CostRecord] | None = None,
        repeatability_evaluator: Callable[[LLMRequest], bool] | None = None,
    ) -> None:
        self._providers = MappingProxyType(dict(providers))
        # Canonicalize model profiles strictly by f"{provider_id}:{model_id}"
        canonical_profiles = {}
        for p in model_profiles.values():
            canonical_profiles[f"{p.provider_id}:{p.model_id}"] = p
        self._model_profiles = MappingProxyType(canonical_profiles)
        self._routing_policy = default_routing_policy
        self._retry_policy = default_retry_policy
        self._fallback_policy = default_fallback_policy
        self._data_governance_denied = data_governance_denied_providers
        self._pricing_records = (
            MappingProxyType(dict(pricing_records))
            if pricing_records is not None
            else MappingProxyType({})
        )
        self._repeatability_evaluator = repeatability_evaluator

    def is_operation_repeatable(self, request: LLMRequest) -> bool:
        """Determine whether the request operation is safe to repeat.

        Per ADR-0002 §8, idempotency_key identifies the logical operation, but
        repeatability requires both operation identity and safety. By default,
        an operation is repeatable if it specifies a non-empty idempotency_key
        and does not define state-mutating tool invocations, or if an explicit
        repeatability evaluator evaluates it as safe.
        """
        if not request.idempotency_key or not request.idempotency_key.strip():
            return False
        if self._repeatability_evaluator is not None:
            return self._repeatability_evaluator(request)
        return not bool(request.tools)

    def _resolve_model_profile(self, selector: ModelSelector) -> ModelProfile:
        key = f"{selector.provider_id}:{selector.model_id}"
        if key in self._model_profiles:
            return self._model_profiles[key]
        raise UnsupportedModelError(
            category="unsupported_model",
            provider=selector.provider_id,
            model=selector.model_id,
            request_id="",
            attempt_id=None,
            retryable=False,
            fallback_eligible=False,
            message=f"Model {selector.model_id!r} not configured for provider {selector.provider_id!r}",
        )

    def _check_compatibility(
        self, request: LLMRequest, profile: ModelProfile
    ) -> str | None:
        """Statically validate whether model profile satisfies request requirements."""
        req = request.requirements

        if not req.input_modalities.issubset(profile.input_modalities):
            missing = req.input_modalities - profile.input_modalities
            return f"Model does not support required input modalities: {missing}"

        if not req.output_modalities.issubset(profile.output_modalities):
            missing = req.output_modalities - profile.output_modalities
            return f"Model does not support required output modalities: {missing}"

        if (
            req.structured_output or request.response_schema is not None
        ) and not profile.supports_structured_output:
            return "Model does not support structured output"

        if req.tool_calling and not profile.supports_tool_calls:
            return "Model does not support tool calling"

        if req.streaming and not profile.supports_streaming:
            return "Model does not support streaming"

        if req.reasoning and not profile.supports_reasoning:
            return "Model does not support reasoning"

        if (
            req.minimum_context_tokens is not None
            and profile.context_window_tokens is not None
            and profile.context_window_tokens < req.minimum_context_tokens
        ):
            return (
                f"Model context window ({profile.context_window_tokens}) is less than "
                f"required minimum context ({req.minimum_context_tokens})"
            )

        return None

    def resolve_eligible_candidates(
        self,
        request: LLMRequest,
        routing_policy: RoutingPolicy | None = None,
    ) -> tuple[tuple[ModelSelector, ModelProfile], ...]:
        """Filter configured candidates into statically compatible and policy-eligible candidates.

        Distinguishes configured from eligible candidates (ADR-0002 §3, Invariant 3).
        """
        policy = routing_policy or self._routing_policy
        eligible: list[tuple[ModelSelector, ModelProfile]] = []
        incompatible_reasons: list[str] = []
        policy_denied_candidates: list[str] = []

        for selector in policy.candidates:
            # 1. Provider existence
            if selector.provider_id not in self._providers:
                incompatible_reasons.append(
                    f"Provider {selector.provider_id!r} is not registered"
                )
                continue

            # 2. Data-governance check
            if selector.provider_id in self._data_governance_denied:
                policy_denied_candidates.append(selector.provider_id)
                continue

            # 3. ModelProfile lookup
            try:
                profile = self._resolve_model_profile(selector)
            except UnsupportedModelError as err:
                incompatible_reasons.append(str(err))
                continue

            # 4. Static capability compatibility check
            mismatch = self._check_compatibility(request, profile)
            if mismatch is not None:
                incompatible_reasons.append(
                    f"[{selector.provider_id}:{selector.model_id}] {mismatch}"
                )
                continue

            eligible.append((selector, profile))

        if not eligible:
            if policy_denied_candidates and not incompatible_reasons:
                raise PolicyDeniedError(
                    category="policy_denied",
                    provider="gateway",
                    model=None,
                    request_id=request.request_id,
                    attempt_id=None,
                    retryable=False,
                    fallback_eligible=False,
                    message=(
                        f"All candidate providers were denied by data-governance policy: "
                        f"{policy_denied_candidates}"
                    ),
                )
            raise CapabilityMismatchError(
                category="capability_mismatch",
                provider="gateway",
                model=None,
                request_id=request.request_id,
                attempt_id=None,
                retryable=False,
                fallback_eligible=True,
                message=(
                    "No configured candidate satisfies request requirements. Reasons: "
                    + "; ".join(incompatible_reasons)
                ),
            )

        return tuple(eligible)

    def _validate_structured_output(
        self,
        raw_output: Any,
        spec: StructuredOutputSpec,
        request_id: str,
        attempt_id: str,
        provider: str,
        model: str,
    ) -> Any:
        """Validate and normalize structured output matching StructuredOutputSpec."""
        data: Any
        if isinstance(raw_output, str):
            try:
                data = json.loads(raw_output)
            except Exception as exc:
                raise InvalidRequestError(
                    category="invalid_request",
                    provider=provider,
                    model=model,
                    request_id=request_id,
                    attempt_id=attempt_id,
                    retryable=False,
                    fallback_eligible=False,
                    message=f"Provider response could not be decoded as JSON for schema {spec.schema_id!r}: {exc}",
                ) from exc
        else:
            data = raw_output

        try:
            validate_json_schema(data, spec.schema)
        except SchemaValidationError as exc:
            raise InvalidRequestError(
                category="invalid_request",
                provider=provider,
                model=model,
                request_id=request_id,
                attempt_id=attempt_id,
                retryable=False,
                fallback_eligible=False,
                message=f"Structured output schema validation failed for schema {spec.schema_id!r}: {exc}",
            ) from exc

        return data

    def complete(
        self,
        request: LLMRequest,
        *,
        routing_policy: RoutingPolicy | None = None,
        retry_policy: RetryPolicy | None = None,
        fallback_policy: Any | None = None,
    ) -> LLMResponse:
        """Execute request using bounded routing, retry, and fallback policies."""
        routing = routing_policy or self._routing_policy
        retries = retry_policy or self._retry_policy
        fallback = fallback_policy or self._fallback_policy

        # 1. Resolve eligible candidates before execution (zero provider calls on mismatch)
        eligible = self.resolve_eligible_candidates(request, routing)

        total_attempts = 0
        provider_switches = 0
        fallback_used = False
        last_error: LLMError | None = None

        for candidate_idx, (selector, profile) in enumerate(eligible):
            adapter = self._providers[selector.provider_id]
            retries_for_candidate = 0

            while True:
                if total_attempts >= routing.max_attempts:
                    if last_error is not None:
                        raise last_error
                    raise LLMError(
                        category="unknown",
                        provider=selector.provider_id,
                        model=profile.model_id,
                        request_id=request.request_id,
                        attempt_id=None,
                        retryable=False,
                        fallback_eligible=False,
                        message=f"Exceeded maximum attempt budget ({routing.max_attempts})",
                    )

                total_attempts += 1
                attempt_id = f"{request.request_id}_att_{total_attempts}"
                start_time = time.monotonic()

                try:
                    response = adapter.complete(request, profile)
                    latency = (time.monotonic() - start_time) * 1000.0

                    # Validate structured output if requested
                    validated_structured = response.structured_output
                    if request.response_schema is not None:
                        target = (
                            response.structured_output
                            if response.structured_output is not None
                            else response.content
                        )
                        validated_structured = self._validate_structured_output(
                            target,
                            request.response_schema,
                            request.request_id,
                            attempt_id,
                            selector.provider_id,
                            profile.model_id,
                        )

                    # Determine cost record if pricing available
                    cost = response.cost
                    if cost is None and profile.pricing_version is not None:
                        pricing_key = f"{profile.provider_id}:{profile.model_id}:{profile.pricing_version}"
                        if pricing_key in self._pricing_records:
                            cost = self._pricing_records[pricing_key]

                    # Normalize response and enforce gateway tracking
                    return replace(
                        response,
                        provider=selector.provider_id,
                        model=profile.model_id,
                        request_id=request.request_id,
                        trace_id=request.trace_id,
                        attempt_id=attempt_id,
                        latency_ms=latency,
                        structured_output=validated_structured,
                        cost=cost,
                        fallback_used=fallback_used,
                    )

                except LLMError as err:
                    last_error = err

                    # ADR-0002 §11: policy_denied fails fast immediately
                    if err.category == "policy_denied":
                        raise

                    # Phase 2C enforces default fail-fast authentication behavior:
                    if err.category == "authentication":
                        raise

                    # Invalid request fails fast
                    if err.category == "invalid_request":
                        raise

                    # Timeout safety: timeout does not prove non-execution (ADR-0002 §4.3)
                    if err.category == "timeout":
                        if not self.is_operation_repeatable(request):
                            raise ProviderTimeoutError(
                                category="timeout",
                                provider=selector.provider_id,
                                model=profile.model_id,
                                request_id=request.request_id,
                                attempt_id=attempt_id,
                                retryable=False,
                                fallback_eligible=False,
                                message=(
                                    f"Request timed out on provider {selector.provider_id!r}. Execution state is unknown; "
                                    "automatic retry and fallback are prohibited for non-repeatable operations."
                                ),
                                details=err.details,
                            )

                        has_provider_guarantee = (
                            getattr(adapter, "supports_idempotency_keys", False) is True
                        )
                        if not has_provider_guarantee:
                            raise ProviderTimeoutError(
                                category="timeout",
                                provider=selector.provider_id,
                                model=profile.model_id,
                                request_id=request.request_id,
                                attempt_id=attempt_id,
                                retryable=False,
                                fallback_eligible=False,
                                message=(
                                    f"Request timed out on provider {selector.provider_id!r}. Execution state is unknown; "
                                    "retry is prohibited without an explicit provider-side idempotency guarantee."
                                ),
                                details=err.details,
                            )

                    # Check bounded retry against same candidate
                    if err.retryable and retries_for_candidate < retries.max_retries:
                        retries_for_candidate += 1
                        backoff_ms = min(
                            retries.initial_backoff_ms
                            * (2 ** (retries_for_candidate - 1)),
                            retries.max_backoff_ms,
                        )
                        if retries.jitter:
                            backoff_ms = random.uniform(0.5, 1.5) * backoff_ms
                        time.sleep(backoff_ms / 1000.0)
                        continue

                    # Fallback to next candidate:
                    # Timeout fallback across providers is prohibited because distinct providers
                    # cannot guarantee deduplication of the timed-out attempt.
                    can_fallback = (
                        fallback.enabled
                        and provider_switches < fallback.max_provider_switches
                        and err.category in fallback.allowed_failure_categories
                        and err.fallback_eligible
                        and err.category != "timeout"
                        and (candidate_idx + 1 < len(eligible))
                    )

                    if can_fallback:
                        provider_switches += 1
                        fallback_used = True
                        break  # Break inner loop to move to next candidate in eligible list

                    # Fallback not permitted or not possible
                    raise

        if last_error is not None:
            raise last_error

        raise LLMError(
            category="unknown",
            provider="gateway",
            model=None,
            request_id=request.request_id,
            attempt_id=None,
            retryable=False,
            fallback_eligible=False,
            message="Gateway failed to produce response",
        )

    def stream(
        self,
        request: LLMRequest,
        *,
        routing_policy: RoutingPolicy | None = None,
    ) -> Iterator[LLMStreamEvent]:
        """Execute streaming request with lifecycle event guarantees (ADR-0002 §10)."""
        routing = routing_policy or self._routing_policy
        eligible = self.resolve_eligible_candidates(request, routing)

        selector, profile = eligible[0]
        adapter = self._providers[selector.provider_id]

        done_emitted = False
        error_emitted = False

        for event in adapter.stream(request, profile):
            if isinstance(event, UsageEvent):
                yield event
            elif isinstance(event, StreamErrorEvent):
                error_emitted = True
                yield event
                break  # Terminal error: do NOT emit StreamDoneEvent afterward
            elif isinstance(event, StreamDoneEvent):
                done_emitted = True
                yield event
                break
            else:
                yield event

        if not done_emitted and not error_emitted:
            yield StreamDoneEvent(type="done")
