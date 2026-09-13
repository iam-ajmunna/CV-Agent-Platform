# Issue #39 — Phase 2C Implementation Report

**Issue:** [#39](https://github.com/iam-ajmunna/CV-Agent-Platform/issues/39)
**PR:** [#40](https://github.com/iam-ajmunna/CV-Agent-Platform/pull/40)
**Branch:** `feature/ajmunna/phase-2c-llm-gateway`
**Status:** Implemented; PR #40 re-review corrections applied; all 173 tests pass; all quality gates pass.

## Overview

This report documents the implementation of the Phase 2C LLM Gateway Subsystem under Issue #39 according to the frozen ADR-0002 specification, the approved Phase 2C constraints, and the subsequent resolution of the review and re-review findings raised during the Official PM Review of PR #40.

The LLM Gateway provides a provider-independent model inference substrate with typed contracts, capability-aware candidate routing, bounded retry and fallback policies, timeout and idempotency safety, two-layer structured output schema validation, streaming lifecycle guarantees, deep defensive immutability, and total provider isolation.

## Implementation Details & Contract Guarantees

### 1. Faithful Timeout & Idempotency Semantics (Frozen ADR-0002 §8)
- `LLMRequest` adheres strictly to frozen ADR-0002 without additions (`request_id`, `idempotency_key`, `trace_id`, `messages`, `requirements`, `response_schema`, `metadata`).
- Removed `metadata["idempotent"]` completely.
- `LLMGateway.is_operation_repeatable(request)` implements the fail-closed safe default: an operation is non-repeatable (`False`) unless an explicit gateway `repeatability_evaluator` callback establishes safe repeatability.
- A timeout represents an **unknown remote execution state** (ADR-0002 §4.3).
- Non-repeatable operations receive **zero automatic retry and zero automatic fallback** on timeout.
- Repeatable operations may retry on the same provider **only** when the provider provides an explicit provider-side idempotency guarantee (`supports_idempotency_keys = True`).
- Timeout fallback across distinct providers is strictly prohibited because distinct providers cannot guarantee deduplication of an in-flight request.

### 2. Default Fail-Fast Authentication
- Phase 2C strictly enforces the default fail-fast authentication behavior (`AuthenticationError.retryable = False`, `fallback_eligible = False`).
- Authentication failures immediately abort execution with zero automatic retry and zero fallback.

### 3. Canonical Model Profile Resolution & Duplicate Rejection
- Canonical profile identity is strictly `f"{provider_id}:{model_id}"`.
- Removed ambiguous fallback lookup. Profiles are resolved strictly by the composite `(provider_id, model_id)` pair.
- Construction of `LLMGateway`, `register_model_profile()`, and `create_gateway()` deterministically reject duplicate canonical identities with `ValueError` rather than silently overwriting existing configurations.
- Regression tests prove distinct profiles for shared model identifiers across providers (`provider-a:model-x` vs `provider-b:model-x`) and deterministic rejection of duplicate canonical registrations.

### 4. Zero Model Profile Manufacturing
- Removed synthetic capability manufacturing.
- `create_gateway()` verifies the selected model exists in registered profiles and raises `UnsupportedModelError` if unconfigured.
- Routing candidates referencing unconfigured models are rejected statically during capability filtering with zero provider calls.

### 5. Pure-Python Deterministic JSON Schema Validation
- Created `cv_agent/llm/schema.py` implementing a deterministic JSON Schema validator in pure Python standard library (zero third-party dependencies).
- Enforces property types (`string`, `number`, `integer`, `boolean`, `array`, `object`, `null`), required fields, nested object properties, array `items`, `minItems`/`maxItems`, string `minLength`/`maxLength`/`pattern`, numeric `minimum`/`maximum`, `enum`, and `additionalProperties: False`.
- Negative regression tests verify that malformed types (e.g. `{"bbox": "not-an-array", "label": 123}`) against object detection schemas are rejected with normalized `InvalidRequestError`.

### 6. Streaming Lifecycle Ordering Enforcement (ADR-0002 §10)
- `LLMGateway.stream()` enforces event lifecycle ordering:
  - `Usage → Done`: `UsageEvent` precedes `StreamDoneEvent` whenever token usage is reported.
  - `Text → Done`: `StreamDoneEvent` terminates the stream when usage is unavailable.
  - `Error`: `StreamErrorEvent` is terminal.
  - `Done → Usage`: Stream protocol violation rejected with `LLMError(category="invalid_request")`.
  - `Error → Done`: Terminal error suppresses any subsequent `StreamDoneEvent`.

### 7. Deep Defensive Immutability
- Implemented `deep_freeze()` in `cv_agent/llm/contracts.py` (and `_deep_freeze()` in `cv_agent/llm/errors.py`) recursively converting nested mappings to `MappingProxyType`, lists to immutable tuples, and sets to frozensets.
- Applied to `StructuredOutputSpec.schema`, `ToolCall.arguments`, `LLMRequest.metadata`, and `LLMError.details`.

## Quality Gates & Verification

| Gate | Result |
|---|---|
| Ruff lint (`ruff check .`) | Pass (All checks passed!) |
| Ruff format check (`ruff format --check .`) | Pass (81 files already formatted) |
| Mypy (`mypy cv_agent`) | Pass (Success: no issues found in 28 source files) |
| Mypy (`mypy --python-version 3.10 cv_agent/llm cv_agent/domain cv_agent/capabilities`) | Pass (Success: no issues found in 18 source files) |
| Pytest (`pytest -q`) | Pass — 173 passed in 3.13s (0 warnings) |
| CLI Health Check (`python -m cv_agent`) | Pass — Runtime status OK, Provider mock, Model fake-1, 20 registered capabilities |
| Git diff check (`git diff --check`) | Pass (0 trailing whitespace or format issues) |
