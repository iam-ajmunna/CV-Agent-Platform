"""cv_agent.llm.contracts — Authoritative ADR-0002 dataclasses and types.

Frozen specifications for multimodal messages, requirements, profiles,
policies, structured output, tool calls, streaming events, and cost accounting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

from cv_agent.llm.errors import FailureCategory, LLMError


def deep_freeze(val: Any) -> Any:
    """Recursively freeze mappings, lists, tuples, and sets for defensive immutability."""
    if isinstance(val, Mapping):
        return MappingProxyType({k: deep_freeze(v) for k, v in val.items()})
    if isinstance(val, (list, tuple)):
        return tuple(deep_freeze(v) for v in val)
    if isinstance(val, (set, frozenset)):
        return frozenset(deep_freeze(v) for v in val)
    return val


# --- Multimodal Content Blocks (ADR-0002 §6) ---


@dataclass(frozen=True)
class TextBlock:
    """Canonical text content block."""

    type: Literal["text"]
    text: str


@dataclass(frozen=True)
class ImageBlock:
    """Canonical multimodal image block carrying external URI/data references."""

    type: Literal["image"]
    media_type: str
    uri: str | None = None
    data_ref: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.uri is None and self.data_ref is None:
            raise ValueError("ImageBlock requires at least one of 'uri' or 'data_ref'")


ToolContentBlock = TextBlock | ImageBlock


@dataclass(frozen=True)
class ToolResultBlock:
    """Canonical tool result block referencing tool_call_id."""

    type: Literal["tool_result"]
    tool_call_id: str
    content: tuple[ToolContentBlock, ...]

    def __post_init__(self) -> None:
        for item in self.content:
            if isinstance(item, ToolResultBlock):
                raise TypeError("ToolResultBlock cannot nest another ToolResultBlock")


ContentBlock = TextBlock | ImageBlock | ToolResultBlock


@dataclass(frozen=True)
class Message:
    """Canonical message carrying role and immutable content blocks."""

    role: Literal["system", "user", "assistant", "tool"]
    content: tuple[ContentBlock, ...]

    @classmethod
    def text(
        cls, role: Literal["system", "user", "assistant", "tool"], text: str
    ) -> Message:
        """Convenience factory for single text content block messages."""
        return cls(role=role, content=(TextBlock(type="text", text=text),))


# --- Model Profiles & Requirements (ADR-0002 §5) ---


@dataclass(frozen=True)
class LLMRequirements:
    """Declared capability requirements for an LLM request."""

    input_modalities: frozenset[str] = frozenset({"text"})
    output_modalities: frozenset[str] = frozenset({"text"})
    structured_output: bool = False
    tool_calling: bool = False
    streaming: bool = False
    reasoning: bool = False
    minimum_context_tokens: int | None = None


@dataclass(frozen=True)
class ModelProfile:
    """Provider-neutral model capability and configuration profile."""

    provider_id: str
    model_id: str
    profile_version: str
    context_window_tokens: int | None
    input_modalities: frozenset[str]
    output_modalities: frozenset[str]
    supports_structured_output: bool
    supports_tool_calls: bool
    supports_streaming: bool
    supports_reasoning: bool
    pricing_version: str | None = None


# --- Routing, Retry & Fallback Policies (ADR-0002 §4) ---


@dataclass(frozen=True)
class ModelSelector:
    """Identifies a candidate provider and model."""

    provider_id: str
    model_id: str


@dataclass(frozen=True)
class RoutingPolicy:
    """Policy selecting candidate models in preference order."""

    policy_id: str
    policy_version: str
    candidates: tuple[ModelSelector, ...]
    max_attempts: int


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry parameters for transient failures against the same provider."""

    max_retries: int
    initial_backoff_ms: int
    max_backoff_ms: int
    jitter: bool


@dataclass(frozen=True)
class FallbackPolicy:
    """Policy governing provider switches upon eligible failures."""

    enabled: bool
    allowed_failure_categories: frozenset[FailureCategory]
    max_provider_switches: int


# --- Structured Output & Tool Calls (ADR-0002 §8, §9) ---


@dataclass(frozen=True)
class StructuredOutputSpec:
    """Canonical specification for structured output schemas."""

    schema_id: str
    schema_version: str
    schema: Mapping[str, object]
    strict: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", deep_freeze(self.schema))


@dataclass(frozen=True)
class ToolCall:
    """Representation of an LLM-generated tool call."""

    call_id: str
    tool_name: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", deep_freeze(self.arguments))


# --- Cost & Operational Metadata (ADR-0002 §12) ---


@dataclass(frozen=True)
class CostRecord:
    """Versioned pricing and cost record."""

    currency: str
    amount: Decimal
    pricing_source: str
    pricing_version: str


# --- Canonical Request & Response (ADR-0002 §7, §9) ---

_SENSITIVE_KEY_PATTERNS = ("key", "token", "secret", "password", "credential", "auth")


@dataclass(frozen=True)
class LLMRequest:
    """Canonical request contract sent to the LLM Gateway (frozen ADR-0002 §8)."""

    request_id: str
    idempotency_key: str
    trace_id: str
    messages: tuple[Message, ...]
    requirements: LLMRequirements = field(default_factory=LLMRequirements)
    response_schema: StructuredOutputSpec | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Enforce Invariant 14 secret isolation
        for k in self.metadata:
            lower = k.lower()
            if any(pattern in lower for pattern in _SENSITIVE_KEY_PATTERNS):
                raise ValueError(
                    f"LLMRequest metadata contains sensitive key {k!r} violating Invariant 14 secret isolation"
                )
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))


@dataclass(frozen=True)
class LLMResponse:
    """Canonical normalized response from the LLM Gateway."""

    content: str
    provider: str
    model: str
    request_id: str
    trace_id: str
    attempt_id: str
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: CostRecord | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    structured_output: object | None = None
    fallback_used: bool = False


# --- Streaming Events (ADR-0002 §10) ---


@dataclass(frozen=True)
class TextDeltaEvent:
    type: Literal["text_delta"]
    delta: str


@dataclass(frozen=True)
class ToolCallDeltaEvent:
    type: Literal["tool_call_delta"]
    call_id: str
    delta: str


@dataclass(frozen=True)
class UsageEvent:
    type: Literal["usage"]
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class StreamErrorEvent:
    type: Literal["error"]
    error: LLMError


@dataclass(frozen=True)
class StreamDoneEvent:
    type: Literal["done"]


LLMStreamEvent = (
    TextDeltaEvent
    | ToolCallDeltaEvent
    | UsageEvent
    | StreamErrorEvent
    | StreamDoneEvent
)
