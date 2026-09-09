# ADR-0002: LLM Gateway and Provider Abstraction

- **Status:** Accepted
- **Date:** 2026-09-01
- **Amended:** 2026-09-09
- **Layer:** cross-cutting
- **Canon:** `[P§20]`, `[P§29.3]`, `[P§29.5]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #11; Amendment #14

## 1. Context

The CV Engineering Agent requires language-model capabilities while remaining independent of any specific LLM provider. `[P§20]` requires provider abstraction so provider-specific SDKs, authentication, execution semantics, and model behavior do not leak into orchestration or reasoning layers.

The gateway is a foundational boundary for later Reasoning, Orchestration, Tool/MCP, Evaluation, Optimization, and Monitoring contracts. It therefore defines provider-independent semantics rather than mirroring any one vendor API.

The original ADR established a single typed gateway, configuration-driven provider selection, normalized errors, and transient fallback. This amendment hardens that contract before Phase 2 implementation because several original abstractions were too text-centric or underspecified for the platform's CV and autonomous-engineering requirements.

The gateway supports an extensible set of provider classes. Anthropic, OpenAI, and Local/Ollama are initial provider classes, not architectural limits. Specific credentials, API keys, endpoints, concrete model identifiers, SDK versions, and runtime availability remain deployment/configuration concerns.

The gateway must preserve operational metadata required for reproducibility and quantitative comparison `[P§29.3]`, `[P§29.5]`, while preventing provider-specific details from leaking above the `cv_agent/llm/` boundary.

## 2. Responsibility (required — `[P§34]`)

### 2.1 This subsystem owns

- provider-independent request/response contracts;
- multimodal content representation and provider translation;
- provider adapter lifecycle and provider SDK isolation;
- model capability profiles and compatibility validation;
- configuration/policy-driven model routing;
- retry and fallback semantics;
- request idempotency and attempt identity;
- structured-output normalization;
- LLM-side tool-call representation, but not tool execution;
- streaming event normalization;
- token usage and versioned cost accounting;
- latency and provider-attempt telemetry;
- normalized, provider-independent error creation;
- request/trace correlation metadata;
- provider/data-governance eligibility constraints.

### 2.2 This subsystem does NOT own

- business prompts, CV hypotheses, or reasoning logic → Reasoning layer (ADR-0008);
- workflow sequencing, checkpoints, or approval state → Orchestration (ADR-0003);
- capability taxonomy or global capability resolution → Capability Model (ADR-0001);
- actual tool/process/MCP execution → Tools/Execution Boundary (ADR-0005);
- project memory or experiment persistence → Memory/Experiment Ledger (ADR-0004);
- dataset storage, hashing, or payload retrieval → Dataset (ADR-0009);
- training execution → Training (ADR-0010);
- offline model evaluation → Evaluation (ADR-0011);
- optimization or deployment lifecycle → Optimization/Deployment (ADR-0012);
- production monitoring or active-learning decisions → Monitoring (ADR-0013).

### 2.3 Boundary principle

The gateway is a **semantic translation boundary**, not a universal application abstraction. It exposes only provider-independent concepts required by the platform. Provider-specific SDK objects, authentication mechanisms, HTTP semantics, vendor message formats, and vendor-specific feature flags MUST remain inside provider adapters.

## 3. Decision

Use a single typed LLM Gateway with explicit capability requirements, model profiles, routing policy, retry/fallback policy, provider adapters, normalized responses/errors, and correlation metadata.

The canonical request path is:

```text
Reasoning / Orchestration
        │
        ▼
   LLMRequest
        │
        ├── LLMRequirements
        ├── StructuredOutputSpec (optional)
        └── content references / multimodal blocks
        │
        ▼
   LLM Gateway
        │
        ├── validate request
        ├── resolve eligible ModelProfile(s)
        ├── apply RoutingPolicy
        ├── execute RetryPolicy
        └── apply FallbackPolicy where safe
        │
        ▼
   ProviderAdapter
        │
        ▼
   External / Local Provider
        │
        ▼
   Normalized LLMResponse / LLMStreamEvent / LLMError
```

Provider selection is policy-driven. The gateway selects only models whose declared `ModelProfile` satisfies the request's `LLMRequirements` and whose provider is eligible under the active routing/data-governance policy.

Anthropic, OpenAI, and Local/Ollama are initial adapters. New provider classes MUST be addable without changing reasoning/orchestration contracts.

## 4. Routing, Retry, and Fallback Semantics

These are separate concepts and MUST NOT be conflated.

### 4.1 Routing

Routing chooses an eligible provider/model before execution according to a versioned policy.

```python
@dataclass(frozen=True)
class ModelSelector:
    provider_id: str
    model_id: str


@dataclass(frozen=True)
class RoutingPolicy:
    policy_id: str
    policy_version: str
    candidates: tuple[ModelSelector, ...]
    max_attempts: int
```

The gateway MUST reject a configured candidate that cannot satisfy the request requirements or provider/data-governance constraints.

### 4.2 Retry

Retry means repeating an operation against the same provider/model under an explicit `RetryPolicy`.

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int
    initial_backoff_ms: int
    max_backoff_ms: int
    jitter: bool
```

Retries MUST be bounded. Backoff MUST NOT be implemented as an unbounded loop.

### 4.3 Fallback

Fallback means moving to another eligible provider/model after a failed attempt. Fallback is permitted only when the failure category is eligible and the operation is safe to repeat.

```python
@dataclass(frozen=True)
class FallbackPolicy:
    enabled: bool
    allowed_failure_categories: frozenset[str]
    max_provider_switches: int
```

A timeout is **not proof that the provider did not execute the request**. Therefore timeout fallback MUST treat the original operation as potentially executed. Automatic fallback is prohibited for non-idempotent operations unless an explicit provider-side idempotency guarantee is available.

The gateway MUST preserve the original request identity across retries/fallbacks while assigning a distinct `attempt_id` to each execution attempt.

## 5. Capability and Model Compatibility

### 5.1 ModelProfile

Model identity alone is insufficient to determine request compatibility. The gateway maintains a provider-neutral profile for each configured model.

```python
@dataclass(frozen=True)
class ModelProfile:
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
    pricing_version: str | None
```

Profiles describe observed/configured capabilities; they do not claim that every provider feature is semantically interchangeable.

### 5.2 LLMRequirements

```python
@dataclass(frozen=True)
class LLMRequirements:
    input_modalities: frozenset[str] = frozenset({"text"})
    output_modalities: frozenset[str] = frozenset({"text"})
    structured_output: bool = False
    tool_calling: bool = False
    streaming: bool = False
    reasoning: bool = False
    minimum_context_tokens: int | None = None
```

The gateway MUST perform compatibility validation before provider execution. Provider-specific capability checks MUST NOT be deferred until a provider call fails where the capability is statically knowable.

## 6. Canonical Multimodal Content

Text-only messages are insufficient for a computer-vision engineering agent. Multimodal input is therefore part of the canonical gateway contract rather than a future extension.

Content blocks MUST be provider-neutral and MUST NOT embed provider SDK objects.

```python
@dataclass(frozen=True)
class TextBlock:
    type: Literal["text"]
    text: str


@dataclass(frozen=True)
class ImageBlock:
    type: Literal["image"]
    media_type: str
    uri: str | None = None
    data_ref: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class ToolResultBlock:
    type: Literal["tool_result"]
    tool_call_id: str
    content: tuple["ContentBlock", ...]


ContentBlock = TextBlock | ImageBlock | ToolResultBlock


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: tuple[ContentBlock, ...]
```

Binary image/video payloads MUST NOT be owned or persisted by the LLM gateway. `uri`, `data_ref`, and `sha256` are references to externally managed content. Dataset-managed assets MUST preserve their ADR-0009 content identity when passed into the gateway.

The canonical contract MAY be extended with additional provider-neutral modalities in future amendments, but provider-specific content representations MUST remain adapter-local.

## 7. Canonical Request Contract

```python
@dataclass(frozen=True)
class LLMRequest:
    request_id: str
    idempotency_key: str
    trace_id: str
    messages: tuple[Message, ...]
    requirements: LLMRequirements = LLMRequirements()
    response_schema: "StructuredOutputSpec | None" = None
    metadata: Mapping[str, str] = field(default_factory=dict)
```

`request_id` identifies the logical request. `idempotency_key` identifies the logical operation for safe retry semantics. `trace_id` correlates the request with the surrounding workflow.

Request metadata MUST NOT contain provider credentials or secrets.

## 8. Structured Output Contract

Structured output is a first-class gateway capability because downstream Reasoning and Orchestration require machine-readable contracts.

```python
@dataclass(frozen=True)
class StructuredOutputSpec:
    schema_id: str
    schema_version: str
    schema: Mapping[str, object]
    strict: bool = True
```

Provider adapters translate the canonical schema into provider-specific structured-output mechanisms. Provider-specific schema objects MUST NOT cross the gateway boundary.

A response carrying structured output MUST preserve `schema_id` and `schema_version` so consumers can validate against the exact contract requested.

## 9. Tool-Call Boundary

The gateway represents tool calls generated by the model but MUST NOT execute tools.

```python
@dataclass(frozen=True)
class ToolCall:
    call_id: str
    tool_name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class LLMResponse:
    content: str
    provider: str
    model: str
    request_id: str
    trace_id: str
    attempt_id: str
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: "CostRecord | None" = None
    tool_calls: tuple[ToolCall, ...] = ()
    structured_output: object | None = None
    fallback_used: bool = False
```

The execution boundary is:

```text
LLM Gateway
    │
    │ ToolCall
    ▼
Orchestration / Tool Boundary
    │
    ▼
ADR-0005 execution semantics
```

This prevents provider SDK tool execution from bypassing the platform's authorization, checkpoint, and execution controls.

## 10. Streaming Contract

Streaming MUST use typed lifecycle events rather than assuming every event is a text delta.

```python
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
    error: "LLMError"


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
```

```python
class LLMStreamingGateway(Protocol):
    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]: ...
```

Streaming is optional for a given provider/model, but if requested through `LLMRequirements.streaming`, the selected model MUST advertise streaming capability.

## 11. Error Semantics

Retryability and fallback eligibility are distinct.

```python
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


@dataclass(frozen=True)
class LLMError(Exception):
    category: FailureCategory
    provider: str
    model: str | None
    request_id: str
    attempt_id: str | None
    retryable: bool
    fallback_eligible: bool
    message: str
    details: Mapping[str, str] = field(default_factory=dict)
```

The gateway MUST classify at least:

| Category | Retry | Fallback | Default treatment |
|---|---:|---:|---|
| `rate_limited` | Yes, bounded | Yes, policy permitting | Backoff then switch if eligible |
| `transient_server` | Yes, bounded | Yes, policy permitting | Backoff then switch if eligible |
| `timeout` | Yes only when operation is safely repeatable | Yes only when safely repeatable | Treat execution state as unknown |
| `authentication` | No | No unless a distinct configured credential path is explicitly selected by policy | Fail fast |
| `invalid_request` | No | No | Fail fast |
| `context_length_exceeded` | No | Only if another eligible model satisfies requirements | Re-route only when compatibility is established |
| `unsupported_model` | No | Yes only if another eligible model satisfies requirements | Re-route only when compatible |
| `capability_mismatch` | No | Yes only to compatible candidate | Never blind-fallback |
| `policy_denied` | No | No | Fail fast |
| `provider_unavailable` | Bounded | Yes, policy permitting | Fail or switch |
| `unknown` | No by default | No by default | Fail closed |

No retry/fallback policy may override a `policy_denied` classification.

## 12. Cost and Operational Metadata

Cost must be represented independently of transient provider pricing.

```python
@dataclass(frozen=True)
class CostRecord:
    currency: str
    amount: float
    pricing_source: str
    pricing_version: str
```

Every successful attempt MUST expose, where available:

- provider identifier;
- model identifier;
- model profile version;
- request identifier;
- trace identifier;
- attempt identifier/number;
- latency;
- input/output token usage;
- versioned cost metadata;
- fallback state.

Unavailable provider measurements MUST be represented explicitly as unavailable rather than fabricated.

## 13. Reproducibility Contract

A gateway invocation MUST preserve enough metadata to reconstruct the logical invocation and explain provider selection. Where supported by the provider, capture:

- provider and model identifiers;
- model profile/version;
- gateway contract/version;
- routing policy/version;
- retry/fallback policy/version;
- system/user input content references;
- content hashes for externally referenced assets;
- structured-output schema/version;
- tool definitions/version when applicable;
- sampling parameters;
- seed where supported;
- provider API version where exposed;
- attempt/fallback sequence;
- token usage and pricing version.

The gateway MUST NOT claim bit-for-bit deterministic model behavior merely because request metadata is captured. Reproducibility metadata records the invocation conditions; actual determinism remains provider/model dependent.

## 14. Security and Data-Governance Boundary

Provider routing is constrained by data-governance policy.

A fallback from provider A to provider B MUST NOT implicitly authorize data transfer to provider B. Each candidate provider/model MUST be checked for eligibility under the active deployment/data policy before execution.

The gateway MUST:

- keep credentials and secrets outside request/response contracts;
- avoid raw prompt/image logging by default;
- avoid placing secret material into telemetry metadata;
- preserve only references/hashes for externally managed binary content;
- prevent provider SDK response objects from escaping the adapter boundary;
- fail closed when routing policy denies a provider.

## 15. Provider Adapter Contract

```python
class LLMProvider(Protocol):
    provider_id: str

    def complete(
        self,
        request: LLMRequest,
        model: ModelProfile,
    ) -> LLMResponse: ...

    def stream(
        self,
        request: LLMRequest,
        model: ModelProfile,
    ) -> Iterator[LLMStreamEvent]: ...
```

Provider adapters are responsible for:

1. authentication using deployment-managed credentials;
2. translating canonical messages/content into provider-native requests;
3. invoking the provider SDK/API/local server;
4. translating provider-native responses/errors into canonical gateway contracts;
5. reporting provider-supported usage/cost metadata;
6. preventing provider-native objects from escaping `cv_agent/llm/`.

Provider adapters MUST NOT contain business reasoning, experiment policy, dataset storage, or workflow orchestration.

## 16. Gateway Contract

```python
class LLMGateway(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...


class LLMStreamingGateway(Protocol):
    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]: ...
```

The gateway implementation is responsible for request validation, compatibility filtering, routing, retry/fallback policy application, correlation metadata, and normalization.

## 17. Alternatives Considered

| Alternative | Evidence for | Evidence against | Decision |
|---|---|---|---|
| Provider-specific calls from reasoning/orchestration | Minimal implementation | Vendor coupling and semantic leakage | Rejected |
| Single hard-coded provider | Simplest operation | Provider lock-in and poor resilience | Rejected |
| Text-only gateway with multimodality deferred | Simple initial interface | Conflicts with CV requirements and creates a future breaking change | Rejected |
| Gateway mirroring one vendor's native API | Fast access to provider features | Couples canonical architecture to transient vendor semantics | Rejected |
| Gateway with canonical multimodal contracts, model profiles, routing/retry/fallback policies, and provider adapters | Strong isolation, capability validation, future provider evolution | Higher initial contract complexity | **Accepted by amendment** |

## 18. Architectural Invariants

1. **Provider Isolation:** Provider SDKs, credentials, and native request/response types remain inside `cv_agent/llm/`.
2. **Canonical Semantics:** Higher layers consume provider-neutral contracts only.
3. **Capability Safety:** A request is executed only by a model whose declared profile satisfies its requirements.
4. **Multimodal First-Class:** CV-relevant multimodal content is represented canonically and is not deferred to a vendor-specific extension.
5. **Routing Separation:** Routing, retry, and fallback are distinct policy decisions.
6. **Bounded Recovery:** Retries and provider switches are always bounded.
7. **Idempotency Safety:** Timeout/fallback behavior never assumes that an unknown-state request was unexecuted.
8. **Tool Execution Isolation:** The gateway may represent tool calls but never executes tools outside ADR-0005 controls.
9. **Structured Output Integrity:** Structured outputs preserve schema identity/version and are provider-independent.
10. **Streaming Integrity:** Streaming exposes typed lifecycle events rather than text-only deltas.
11. **Data-Governance Safety:** Fallback never bypasses provider eligibility or data-governance policy.
12. **Reproducibility:** Provider/model/routing/attempt metadata is captured without falsely claiming deterministic model behavior.
13. **Cost Provenance:** Cost records carry pricing source/version and never fabricate unavailable measurements.
14. **Secret Isolation:** Credentials and secrets never cross canonical LLM request/response contracts.
15. **Fail-Closed Unknowns:** Unknown failure categories default to no retry and no fallback.
16. **Extensible Providers:** Current providers are implementations, not architectural limits.

## 19. Consequences

### Enables

- provider substitution without changes to Reasoning or Orchestration;
- native CV multimodal requests without future breaking message-model changes;
- capability-aware model routing;
- safe retry/fallback semantics;
- structured-output and tool-call interoperability;
- typed streaming;
- traceable latency/cost/provider behavior;
- stronger reproducibility and auditability;
- future provider/model evolution without rewriting higher layers.

### Makes harder

- direct access to provider-specific features from higher layers;
- provider-specific shortcuts outside adapters;
- blind fallback after ambiguous execution failures.

These restrictions are intentional `[P§20]`, `[P§34]`.

### Costs

- more gateway contract and adapter code;
- model capability/profile maintenance;
- routing-policy maintenance;
- additional metadata and validation overhead;
- fallback may increase latency and cost;
- local/self-hosted providers retain local operational responsibility.

## 20. Migration / Blast Radius

This amendment changes the Phase 2 target contract before runtime implementation. The known skeletal `complete()` implementation under `cv_agent/llm/` MUST be audited and adapted to the amended contract before production use.

No Reasoning or Orchestration layer should be implemented against the superseded text-only message contract.

If the gateway is later replaced, the primary blast radius is the gateway consumer boundary and provider adapters. Provider replacement remains isolated when the canonical contracts are preserved.

## 21. Acceptance Criteria

The implementation acceptance suite MUST demonstrate:

1. Two distinct configured providers can satisfy the same text request without changing Reasoning/Orchestration code.
2. A canonical multimodal request can be translated by an eligible provider adapter without exposing provider-native message types above `cv_agent/llm/`.
3. Provider SDK imports occur only under `cv_agent/llm/`.
4. Model capability validation rejects incompatible providers before execution when the incompatibility is statically knowable.
5. A bounded transient retry and/or fallback sequence works for configured 429/5xx conditions.
6. Timeout handling does not perform unsafe duplicate execution when idempotency cannot be established.
7. Authentication, policy-denied, malformed-request, and unknown failures fail closed according to the error taxonomy.
8. Structured-output requests preserve schema ID/version and normalize provider-specific mechanisms.
9. Tool calls are represented canonically but are not executed by the gateway.
10. Streaming produces typed lifecycle events when requested and supported.
11. Every successful response exposes correlation, provider/model, latency, attempt, token, and fallback metadata where available.
12. Cost records preserve pricing source/version where cost is available.
13. Provider fallback respects data-governance eligibility.
14. Provider replacement through configuration does not require Reasoning/Orchestration changes.
15. Reproducibility metadata records routing and attempt decisions without claiming unsupported deterministic behavior.

Named implementation test location: `tests/test_llm_gateway.py`.

## 22. Revisit Triggers

Reconsider this ADR only when a material architectural condition occurs, including:

- canonical requirements cannot represent a required modality or interaction mode;
- provider capability semantics cannot be normalized without semantic loss unacceptable to the platform;
- routing/data-governance requirements require a fundamentally different policy boundary;
- provider execution semantics require a new lifecycle model that cannot fit the gateway contract;
- measured gateway overhead or fallback behavior violates platform SLOs;
- a new class of model interaction requires a new canonical contract;
- the provider ecosystem introduces a durable capability that materially changes the abstraction boundary.

Vendor API version changes, SDK upgrades, new model names, and pricing changes alone are implementation/configuration changes and MUST NOT trigger an architectural rewrite unless they violate the invariants above.
