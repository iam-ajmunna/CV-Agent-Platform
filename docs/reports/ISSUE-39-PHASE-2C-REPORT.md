# Issue #39 — Phase 2C Implementation Report

**Issue:** [#39](https://github.com/iam-ajmunna/CV-Agent-Platform/issues/39)
**PR:** [#40](https://github.com/iam-ajmunna/CV-Agent-Platform/pull/40)
**Branch:** `feature/ajmunna/phase-2c-llm-gateway`
**Status:** Implemented; PR #40 review corrections applied; all 167 tests pass; all quality gates pass.

## Overview

This report documents the implementation of the Phase 2C LLM Gateway Subsystem under Issue #39 according to the frozen ADR-0002 specification, the approved Phase 2C constraints, and the subsequent resolution of the six blocking review findings raised during the Official PM Review of PR #40.

The LLM Gateway provides a provider-independent model inference substrate with typed contracts, capability-aware candidate routing, bounded retry and fallback policies, timeout and idempotency safety, two-layer structured output schema validation, streaming lifecycle guarantees, deep defensive immutability, and total provider isolation.

## Implementation Details & Resolution of PR #40 Findings

### 1. Faithful Timeout & Idempotency Semantics (Finding 1)
- **Problem:** Previous logic inferred idempotency from uncontracted `metadata["idempotent"]` or `bool(idempotency_key)`.
- **Resolution:**
  - Removed `metadata["idempotent"]` completely.
  - Implemented `is_operation_repeatable(request)` gateway decision mechanism with support for custom `repeatability_evaluator`. By default, operations with tools represent potential state-mutating side effects and are treated as non-repeatable.
  - Codified that timeout does **not** prove non-execution (ADR-0002 §4.3).
  - Non-repeatable operations receive **zero automatic retry and zero automatic fallback** on timeout.
  - Repeatable operations may retry on the same provider **only** when the provider provides an explicit provider-side idempotency guarantee (`supports_idempotency_keys = True`).
  - Timeout fallback across distinct providers is prohibited because distinct providers cannot guarantee deduplication of an in-flight request.

### 2. Default Fail-Fast Authentication (Finding 2)
- **Problem:** Report previously claimed nuanced credential-path authentication fallback was implemented.
- **Resolution:**
  - Corrected and narrowed the Phase 2C implementation and report: Phase 2C strictly enforces the default fail-fast authentication behavior (`AuthenticationError.retryable = False`, `fallback_eligible = False`).
  - Authentication failures immediately abort execution with zero automatic retry and zero fallback.

### 3. Canonical Model Profile Resolution (Finding 3)
- **Problem:** `_resolve_model_profile()` previously had an unsafe fallback to bare `selector.model_id`, risking cross-provider profile contamination.
- **Resolution:**
  - Canonical profile identity is strictly `f"{provider_id}:{model_id}"`.
  - Removed ambiguous fallback lookup. Profiles are resolved strictly by the `(provider_id, model_id)` pair.
  - Added regression test demonstrating that two providers sharing the same model identifier (`provider-a:model-x` vs `provider-b:model-x`) resolve distinct profiles without cross-contamination.

### 4. Zero Model Profile Manufacturing (Finding 4)
- **Problem:** `create_gateway()` previously synthesized fully-capable `ModelProfile` objects when an unknown model was requested.
- **Resolution:**
  - Removed synthetic capability manufacturing.
  - `create_gateway()` verifies the selected model exists in registered profiles and raises `UnsupportedModelError` if unconfigured.
  - Routing candidates referencing unconfigured models are rejected statically during capability filtering with zero provider calls.

### 5. Pure-Python Deterministic JSON Schema Validation (Finding 5)
- **Problem:** Structured output validation was previously limited to JSON decoding and top-level required property existence.
- **Resolution:**
  - Created `cv_agent/llm/schema.py` implementing a deterministic JSON Schema validator in pure Python standard library (zero third-party dependencies).
  - Enforces property types (`string`, `number`, `integer`, `boolean`, `array`, `object`, `null`), required fields, nested object properties, array `items`, `minItems`/`maxItems`, string `minLength`/`maxLength`/`pattern`, numeric `minimum`/`maximum`, `enum`, and `additionalProperties: False`.
  - Added negative regression test verifying that malformed types (e.g. `{"bbox": "not-an-array", "label": 123}`) against object detection schemas are rejected with normalized `InvalidRequestError`.

### 6. Deep Defensive Immutability (Finding 6)
- **Problem:** Defensive mapping copying was previously shallow, leaving retained nested dictionaries and lists mutable.
- **Resolution:**
  - Implemented `deep_freeze()` in `cv_agent/llm/contracts.py` (and `_deep_freeze()` in `cv_agent/llm/errors.py`) recursively converting nested mappings to `MappingProxyType`, lists to immutable tuples, and sets to frozensets.
  - Applied to `StructuredOutputSpec.schema`, `ToolCall.arguments`, `LLMRequest.metadata`, `LLMRequest.tools`, and `LLMError.details`.
  - Added regression test proving that mutating caller retained nested dictionaries after construction does not alter the contract, and attempting to mutate nested mappings on the contract raises `TypeError`.

## Files Changed

| File | Change |
|---|---|
| `cv_agent/llm/contracts.py` | Deep recursive immutability via `deep_freeze()`, added `tools` to `LLMRequest` |
| `cv_agent/llm/errors.py` | Recursive freezing on `LLMError.details`, fail-fast `AuthenticationError` |
| `cv_agent/llm/schema.py` | NEW: Pure-Python deterministic JSON Schema validator |
| `cv_agent/llm/gateway.py` | Canonical profile indexing, strict timeout idempotency safety, `is_operation_repeatable()`, full schema validation, fail-fast auth |
| `cv_agent/llm/mock.py` | `MockProviderAdapter` with `supports_idempotency_keys` flag |
| `cv_agent/llm/registry.py` | Reject unconfigured models in `create_gateway()` without manufacturing synthetic profiles |
| `cv_agent/llm/__init__.py` | Export `deep_freeze`, `SchemaValidationError`, `validate_json_schema` |
| `tests/test_llm_gateway.py` | Added targeted regression tests covering all 6 findings (167 total tests) |
| `docs/reports/ISSUE-39-PHASE-2C-REPORT.md` | Updated implementation report with PR #40 review corrections |
| `docs/state/STATUS.md` | Rolling status update |
| `docs/state/JOURNAL.md` | Rolling journal entry |

## Quality Gates & Verification

| Gate | Result |
|---|---|
| Ruff lint (`ruff check .`) | Pass (All checks passed!) |
| Ruff format check (`ruff format --check .`) | Pass (81 files already formatted) |
| Mypy (`mypy cv_agent`) | Pass (Success: no issues found in 28 source files) |
| Mypy (`mypy --python-version 3.10 cv_agent/llm cv_agent/domain cv_agent/capabilities`) | Pass (Success: no issues found in 18 source files) |
| Pytest (`pytest -q`) | Pass — 167 passed in 3.54s (0 warnings) |
| CLI Health Check (`python -m cv_agent`) | Pass — Runtime status OK, Provider mock, Model fake-1, 20 registered capabilities |
| Git diff check (`git diff --check`) | Pass (0 trailing whitespace or format issues) |
