"""cv_agent.llm — Authoritative ADR-0002 LLM Gateway and Provider Abstraction."""

from cv_agent.llm.contracts import (
    ContentBlock,
    CostRecord,
    FallbackPolicy,
    ImageBlock,
    LLMRequest,
    LLMRequirements,
    LLMResponse,
    LLMStreamEvent,
    Message,
    ModelProfile,
    ModelSelector,
    RetryPolicy,
    RoutingPolicy,
    StreamDoneEvent,
    StreamErrorEvent,
    StructuredOutputSpec,
    TextBlock,
    TextDeltaEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolContentBlock,
    ToolResultBlock,
    UsageEvent,
    deep_freeze,
)
from cv_agent.llm.errors import (
    AuthenticationError,
    CapabilityMismatchError,
    ContextLengthExceededError,
    FailureCategory,
    InvalidRequestError,
    LLMError,
    PolicyDeniedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitError,
    TransientServerError,
    UnsupportedModelError,
)
from cv_agent.llm.gateway import LLMGateway
from cv_agent.llm.mock import FakeLLMProvider, MockProviderAdapter
from cv_agent.llm.provider import LLMProvider
from cv_agent.llm.registry import (
    create_gateway,
    get_provider,
    list_providers,
    register_adapter,
    register_model_profile,
    register_provider,
)
from cv_agent.llm.schema import SchemaValidationError, validate_json_schema

__all__ = [
    "AuthenticationError",
    "CapabilityMismatchError",
    "ContentBlock",
    "ContextLengthExceededError",
    "CostRecord",
    # Errors
    "FailureCategory",
    "FakeLLMProvider",
    "FallbackPolicy",
    "ImageBlock",
    "InvalidRequestError",
    "LLMError",
    "LLMGateway",
    # Interfaces & Gateway
    "LLMProvider",
    # Requests & Responses
    "LLMRequest",
    # Requirements & Profiles
    "LLMRequirements",
    "LLMResponse",
    # Streaming
    "LLMStreamEvent",
    "Message",
    "MockProviderAdapter",
    "ModelProfile",
    # Policies
    "ModelSelector",
    "PolicyDeniedError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "RateLimitError",
    "RetryPolicy",
    "RoutingPolicy",
    "SchemaValidationError",
    "StreamDoneEvent",
    "StreamErrorEvent",
    # Structured Output & Tools
    "StructuredOutputSpec",
    # Content & Messages
    "TextBlock",
    "TextDeltaEvent",
    "ToolCall",
    "ToolCallDeltaEvent",
    "ToolContentBlock",
    "ToolResultBlock",
    "TransientServerError",
    "UnsupportedModelError",
    "UsageEvent",
    # Registry functions
    "create_gateway",
    "deep_freeze",
    "get_provider",
    "list_providers",
    "register_adapter",
    "register_model_profile",
    "register_provider",
    "validate_json_schema",
]
