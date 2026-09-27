"""cv_agent.llm.registry — Provider adapter registry and LLMGateway factory.

Registers concrete LLMProvider adapters and provides configuration-driven factory
functions to instantiate LLMGateway with ModelProfiles and RoutingPolicies.
Preserves legacy get_provider() for backward compatibility.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from cv_agent.llm.base import LLMProvider as LegacyLLMProvider
from cv_agent.llm.contracts import (
    CostRecord,
    FallbackPolicy,
    LLMRequest,
    ModelProfile,
    ModelSelector,
    RetryPolicy,
    RoutingPolicy,
)
from cv_agent.llm.errors import UnsupportedModelError
from cv_agent.llm.gateway import LLMGateway
from cv_agent.llm.mock import FakeLLMProvider, MockProviderAdapter
from cv_agent.llm.provider import LLMProvider

# Authoritative Provider Adapter Registry: provider_id -> LLMProvider
_ADAPTER_REGISTRY: dict[str, LLMProvider] = {
    "mock": MockProviderAdapter(provider_id="mock"),
}

# Standard Model Profiles for known models
_DEFAULT_MODEL_PROFILES: dict[str, ModelProfile] = {
    "mock:fake-1": ModelProfile(
        provider_id="mock",
        model_id="fake-1",
        profile_version="1.0",
        context_window_tokens=4096,
        input_modalities=frozenset({"text"}),
        output_modalities=frozenset({"text"}),
        supports_structured_output=True,
        supports_tool_calls=True,
        supports_streaming=True,
        supports_reasoning=False,
        pricing_version="2026-09",
    ),
    "mock:fake-2": ModelProfile(
        provider_id="mock",
        model_id="fake-2",
        profile_version="1.0",
        context_window_tokens=8192,
        input_modalities=frozenset({"text", "image"}),
        output_modalities=frozenset({"text"}),
        supports_structured_output=True,
        supports_tool_calls=True,
        supports_streaming=True,
        supports_reasoning=True,
        pricing_version="2026-09",
    ),
    "mock:fake-vision": ModelProfile(
        provider_id="mock",
        model_id="fake-vision",
        profile_version="1.0",
        context_window_tokens=16384,
        input_modalities=frozenset({"text", "image"}),
        output_modalities=frozenset({"text"}),
        supports_structured_output=True,
        supports_tool_calls=True,
        supports_streaming=True,
        supports_reasoning=False,
        pricing_version="2026-09",
    ),
}

# Legacy Registry: provider name -> legacy provider class
_LEGACY_REGISTRY: dict[str, type[LegacyLLMProvider]] = {
    FakeLLMProvider.PROVIDER_NAME: FakeLLMProvider,
}


def register_adapter(provider_id: str, adapter: LLMProvider) -> None:
    """Register an authoritative LLMProvider adapter instance."""
    _ADAPTER_REGISTRY[provider_id] = adapter


def register_model_profile(profile: ModelProfile) -> None:
    """Register a ModelProfile in the authoritative default catalog."""
    key = f"{profile.provider_id}:{profile.model_id}"
    if key in _DEFAULT_MODEL_PROFILES:
        raise ValueError(
            f"Duplicate model profile canonical identity {key!r} is already registered."
        )
    _DEFAULT_MODEL_PROFILES[key] = profile


def create_gateway(
    provider_id: str = "mock",
    model_id: str = "fake-1",
    *,
    custom_adapters: Mapping[str, LLMProvider] | None = None,
    custom_profiles: Mapping[str, ModelProfile] | None = None,
    custom_routing_policy: RoutingPolicy | None = None,
    custom_retry_policy: RetryPolicy | None = None,
    custom_fallback_policy: FallbackPolicy | None = None,
    denied_providers: frozenset[str] = frozenset(),
    pricing_records: Mapping[str, CostRecord] | None = None,
    repeatability_evaluator: Callable[[LLMRequest], bool] | None = None,
) -> LLMGateway:
    """Instantiate and configure an authoritative LLMGateway."""
    adapters = dict(_ADAPTER_REGISTRY)
    if custom_adapters:
        adapters.update(custom_adapters)

    profiles = dict(_DEFAULT_MODEL_PROFILES)
    if custom_profiles:
        for p in custom_profiles.values():
            canonical_key = f"{p.provider_id}:{p.model_id}"
            if canonical_key in profiles:
                raise ValueError(
                    f"Duplicate model profile canonical identity {canonical_key!r}: "
                    "cannot overwrite existing profile."
                )
            profiles[canonical_key] = p

    # Ensure selected model has a configured profile; do NOT manufacture synthetic capabilities
    key = f"{provider_id}:{model_id}"
    if key not in profiles:
        raise UnsupportedModelError(
            category="unsupported_model",
            provider=provider_id,
            model=model_id,
            request_id="",
            attempt_id=None,
            retryable=False,
            fallback_eligible=False,
            message=(
                f"Model '{provider_id}:{model_id}' is not configured in registered model profiles. "
                "The LLM Gateway does not manufacture synthetic model profiles (ADR-0002 §5)."
            ),
        )

    routing_policy = custom_routing_policy or RoutingPolicy(
        policy_id="default-routing",
        policy_version="1.0",
        candidates=(ModelSelector(provider_id=provider_id, model_id=model_id),),
        max_attempts=3,
    )

    retry_policy = custom_retry_policy or RetryPolicy(
        max_retries=2,
        initial_backoff_ms=50,
        max_backoff_ms=200,
        jitter=False,
    )

    fallback_policy = custom_fallback_policy or FallbackPolicy(
        enabled=True,
        allowed_failure_categories=frozenset(
            {"rate_limited", "transient_server", "provider_unavailable"}
        ),
        max_provider_switches=2,
    )

    return LLMGateway(
        providers=adapters,
        model_profiles=profiles,
        default_routing_policy=routing_policy,
        default_retry_policy=retry_policy,
        default_fallback_policy=fallback_policy,
        data_governance_denied_providers=denied_providers,
        pricing_records=pricing_records,
        repeatability_evaluator=repeatability_evaluator,
    )


# --- Legacy Provider Registry Functions ---


def register_provider(name: str, cls: type[LegacyLLMProvider]) -> None:
    """Register a legacy provider class."""
    if not issubclass(cls, LegacyLLMProvider):
        raise TypeError(f"{cls!r} must be a subclass of LLMProvider")
    _LEGACY_REGISTRY[name] = cls


def get_provider(name: str, model: str) -> LegacyLLMProvider:
    """Instantiate a legacy provider adapter by name and model."""
    if name not in _LEGACY_REGISTRY:
        available = ", ".join(sorted(_LEGACY_REGISTRY))
        raise ValueError(
            f"Unknown LLM provider {name!r}. "
            f"Registered providers: {available}"
        )
    cls = _LEGACY_REGISTRY[name]
    return cls(model=model)


def list_providers() -> list[str]:
    """Return sorted list of registered legacy provider names."""
    return sorted(_LEGACY_REGISTRY)
