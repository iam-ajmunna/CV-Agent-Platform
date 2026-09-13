"""cv_agent.llm.errors — Normalized, provider-independent failure taxonomy.

Implements ADR-0002 §11 FailureCategory and LLMError taxonomy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

FailureCategory = Literal[
    "rate_limited",
    "transient_server",
    "timeout",
    "authentication",
    "invalid_request",
    "context_length_exceeded",
    "unsupported_model",
    "capability_mismatch",
    "policy_denied",
    "provider_unavailable",
    "unknown",
]


@dataclass(frozen=True, kw_only=True)
class LLMError(Exception):
    """Normalized provider-independent LLM error representation (ADR-0002 §11)."""

    category: FailureCategory
    provider: str
    model: str | None
    request_id: str
    attempt_id: str | None
    retryable: bool
    fallback_eligible: bool
    message: str
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Defensively freeze details mapping to preserve immutability
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        super().__init__(self.message)

    def __str__(self) -> str:
        return (
            f"[{self.category}] provider={self.provider!r} model={self.model!r} "
            f"request_id={self.request_id!r} attempt_id={self.attempt_id!r}: {self.message}"
        )


@dataclass(frozen=True, kw_only=True)
class RateLimitError(LLMError):
    """Provider rate limit or quota exceeded."""

    category: FailureCategory = "rate_limited"
    retryable: bool = True
    fallback_eligible: bool = True


@dataclass(frozen=True, kw_only=True)
class TransientServerError(LLMError):
    """Provider-side transient 5xx error or gateway drop."""

    category: FailureCategory = "transient_server"
    retryable: bool = True
    fallback_eligible: bool = True


@dataclass(frozen=True, kw_only=True)
class ProviderTimeoutError(LLMError):
    """Provider request timed out. Note: execution state may be unknown."""

    category: FailureCategory = "timeout"
    retryable: bool = False
    fallback_eligible: bool = False


@dataclass(frozen=True, kw_only=True)
class AuthenticationError(LLMError):
    """Authentication or credential failure."""

    category: FailureCategory = "authentication"
    retryable: bool = False
    fallback_eligible: bool = False


@dataclass(frozen=True, kw_only=True)
class InvalidRequestError(LLMError):
    """Malformed request or unsupported parameters."""

    category: FailureCategory = "invalid_request"
    retryable: bool = False
    fallback_eligible: bool = False


@dataclass(frozen=True, kw_only=True)
class ContextLengthExceededError(LLMError):
    """Prompt tokens exceed model context window."""

    category: FailureCategory = "context_length_exceeded"
    retryable: bool = False
    fallback_eligible: bool = True


@dataclass(frozen=True, kw_only=True)
class UnsupportedModelError(LLMError):
    """Configured model is unknown or unsupported by the provider."""

    category: FailureCategory = "unsupported_model"
    retryable: bool = False
    fallback_eligible: bool = True


@dataclass(frozen=True, kw_only=True)
class CapabilityMismatchError(LLMError):
    """Request requires capabilities not supported by candidate model profile."""

    category: FailureCategory = "capability_mismatch"
    retryable: bool = False
    fallback_eligible: bool = True


@dataclass(frozen=True, kw_only=True)
class PolicyDeniedError(LLMError):
    """Request or provider denied by data-governance or safety policy."""

    category: FailureCategory = "policy_denied"
    retryable: bool = False
    fallback_eligible: bool = False


@dataclass(frozen=True, kw_only=True)
class ProviderUnavailableError(LLMError):
    """Provider service is down, unreachable, or DNS failed."""

    category: FailureCategory = "provider_unavailable"
    retryable: bool = True
    fallback_eligible: bool = True
