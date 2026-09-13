# Issue #43 — Phase 2E Implementation Report

**Issue:** [#43](https://github.com/iam-ajmunna/CV-Agent-Platform/issues/43)  
**PR:** [#44](https://github.com/iam-ajmunna/CV-Agent-Platform/pull/44)  
**Branch:** `feature/ajmunna/phase-2e-orchestration`  
**Status:** Implemented and hardened; all 261 tests pass (including 51 Phase 2E orchestration contract tests); all quality gates pass.

## Overview

This report documents the implementation of Phase 2E — ADR-0003 Orchestration State Machine, Checkpointing, and Persistent Approvals under Issue #43 according to the frozen ADR-0003 specification and the architectural gates established by the Official PM, including the resolution of all 7 PM review findings on PR #44.

The orchestration subsystem provides durable state management, transactional lifecycle progression, monotonic event logging, CAS lease fencing, persistent approval verification, and side-effect reconciliation for computer vision engineering workflows. The architecture places **SQLite as the sole source of authority** (`.cv_agent/state/checkpoints.sqlite`), utilizing LangGraph strictly as a workflow/runtime execution mechanism rather than an alternative state store.

## The 8 Non-Negotiable PM Implementation Gates

### Gate 1: SQLite Authority, Not LangGraph
- Authoritative state records (sessions, leases, checkpoints, operations, events, approvals) reside strictly in SQLite at `.cv_agent/state/checkpoints.sqlite`.
- WAL mode, foreign keys (`PRAGMA foreign_keys = ON`), and immediate transactions ensure durable, concurrent, and crash-resilient persistence.
- LangGraph operates purely as a workflow graph runtime adapter (`SQLiteLangGraphCheckpointer` persists directly to SQLite tables `lg_checkpoints`, `lg_blobs`, `lg_writes`). Graph nodes record events and transition lifecycle state via `store.transition_lifecycle()` under active lease fencing.

### Gate 2: Atomic CAS Fencing at Mutation Time
- Fencing is enforced in SQL using atomic compare-and-swap (`UPDATE leases ... WHERE session_id = ? AND fencing_epoch = ?`).
- Competing mutations on stale epochs raise `StaleFencingLeaseError` without Python-side read-compare-mutate race windows.
- Lease renewals atomically increment `fencing_epoch`. Active leases held by a worker reject concurrent acquisition by other workers; expired leases can be adopted cleanly by a new worker.

### Gate 3: Transactional Monotonic Event Sequencing
- Within `SQLiteOrchestrationStore.record_event()` and `transition_lifecycle()`, sequence numbers are allocated transactionally inside `with self._conn: ... SELECT COALESCE(MAX(sequence_number), 0) + 1 ... INSERT INTO events ...`.
- A rolled-back transaction never consumes an observable sequence number.
- Concurrent writers cannot produce duplicate `(session_id, sequence_number)` pairs; sequence gaps and collisions are prevented.

### Gate 4: Deliberately Narrow Canonicalization
- `canonicalize_payload()` canonicalizes **effectful intent only**, deterministically normalizing Unicode strings via NFC (`unicodedata.normalize("NFC", ...)`), recursively sorting dictionary keys, sorting sets, and standardizing numerical representations.
- Observational metadata (`_OBSERVATIONAL_KEYS`: timestamps, trace IDs, execution IDs, worker IDs, retry counters, latency metrics, provider tokens) are stripped recursively before hashing.
- Material intent modifications alter the SHA-256 hash (`compute_payload_hash`), while formatting/ordering/observational differences yield identical hashes.

### Gate 5: Complete Security 4-Tuple Binding for Approvals
- Approvals bind the complete 4-tuple security envelope:
  `operation_version + canonical_payload_hash + policy_version + resource_scope`
- `ApprovalManager.verify_approval()` fails closed with `ApprovalSecurityError`:
  - Payload hash mismatch → rejected (`SECURITY_VIOLATION: Payload hash mismatch`)
  - Resource scope mismatch → rejected (`SECURITY_VIOLATION: Resource scope mismatch`)
  - Policy version mismatch → rejected (`SECURITY_VIOLATION: Policy version mismatch`)
  - Operation version mismatch → rejected (`SECURITY_VIOLATION: Operation version mismatch`)
  - Expiration boundary → rejected (`APPROVAL_EXPIRED`)
  - Rejection status → rejected (`APPROVAL_REJECTED`)

### Gate 6: Tri-State / Non-Terminal Side-Effect Reconciliation
- `OperationStatus.UNKNOWN` transitions only to `RECONCILING`.
- `ReconciliationCoordinator.reconcile()` maps external side-effect observations to:
  - `CONFIRMED_COMPLETED` → `COMPLETED`
  - `CONFIRMED_NOT_SUBMITTED` → `DISPATCHABLE`
  - `CONFIRMED_FAILED` → `FAILED`
  - `UNRESOLVED` → leaves operation in `RECONCILING` / operator escalation.
- Under no circumstances does `UNRESOLVED` default to success or failure.

### Gate 7: Conservative False-Negative Retention Pruning
- Pruning policy protects active leases, pending approvals, and active operations using relational foreign keys (`session_id`). Zero JSON substring searching is used.
- Any checkpoint associated with a session that has an active (unexpired) lease or an uncompleted operation/approval is retained.
- `experiments.sqlite` (and the experiments ledger) is completely isolated and never touched by orchestration pruning.

### Gate 8: LangGraph Workflow Integration Verified
- Target LangGraph version is pinned to `langgraph>=0.2,<2` (verified in virtual environment with `1.2.11`).
- `create_orchestration_graph()` sets up a `StateGraph(OrchestrationGraphState)` with nodes: `initialize`, `plan`, `await_approval`, `execute`, `evaluate`.
- Configured with `interrupt_before=["await_approval"]` to support human-in-the-loop suspension and resumption.
- State transitions strictly enforce Invariant 17: `EVALUATING` is terminal-adjacent (exits only to `COMPLETED`, `FAILED`, `PAUSED`); transition from `EVALUATING` to `EXECUTING` is illegal and raises `InvalidStateTransitionError`.

## Resolution of PM Review Findings (PR #44 Review)

| # | PM Finding | Resolution & Hardening | Regression Coverage |
|---|---|---|---|
| 1 | SQLite is not yet the true lifecycle-state authority | Created authoritative `sessions` table in SQLite with `session_id`, `thread_id`, `lifecycle_status`, `created_at`, `updated_at`. Implemented `store.transition_lifecycle()` and `store.get_lifecycle_status()`. Replaced LangGraph `MemorySaver` default with `SQLiteLangGraphCheckpointer`. | `test_sqlite_sole_lifecycle_authority`, `test_langgraph_sqlite_checkpointer_persistence` |
| 2 | Operation status mutations bypass `validate_operation_transition()` | `update_operation_status()` now calls `validate_operation_transition(current_status, new_status)` and fails closed. `save_operation()` queries existing status and prohibits resetting an existing operation from non-`CREATED` back to `CREATED`. | `test_operation_state_transition_validation_rejects_illegal_jump`, `test_save_operation_cannot_reset_running_to_created` |
| 3 | Lifecycle transition + event sequencing aren't one atomic transaction | `store.transition_lifecycle()` executes CAS lease verification, status mutation in `sessions`, monotonic event sequence allocation, diagnostic event insertion, and optional checkpoint write inside a single `BEGIN IMMEDIATE ... COMMIT` block. Failure rolls back both status and sequence number. | `test_atomic_lifecycle_transition_and_event_sequence_rollback` |
| 4 | Checkpoint → lease FK structurally wrong | `sessions` is now the parent entity. `leases` has FK `session_id REFERENCES sessions(session_id) ON DELETE CASCADE`. `checkpoints` and `events` reference `sessions(session_id)`. `release_lease()` deletes the row from `leases` without deleting the session, checkpoints, or events. | `test_release_lease_preserves_checkpoints_and_session` |
| 5 | Pruning uses JSON substring searches | Added authoritative `session_id` columns to `operations` and `approvals`. Pruning uses pure relational SQL (`session_id IN (SELECT session_id FROM leases WHERE expires_at > ?)`, etc.) without JSON parsing or string searching. | `test_pruning_relational_protection_no_json_dependency` |
| 6 | Corrupt checkpoint handling stops at raising exception; doesn't persist `RECOVERY_REQUIRED` | `load_state()` now inspects all candidate checkpoints (newest first). If an older valid checkpoint exists, it is loaded. If all checkpoints are corrupt, it atomically transitions `sessions` to `RECOVERY_REQUIRED`, records a diagnostic event `checkpoint_corruption_detected`, and raises `CheckpointCorruptionError`. | `test_checkpoint_corruption_persists_recovery_required_and_diagnostic_event`, `test_recovery_selection_selects_newest_valid_checkpoint` |
| 7 | Exact numeric canonicalization policy | Replaced lossy `:.10g` truncation with David Gay's exact shortest representation in `json.dumps(..., allow_nan=False)`. Added `math.isfinite()` validation (rejects `nan`, `inf`), normalized `-0.0` to `0.0`, and preserved int vs float distinction. | `test_exact_numeric_policy_close_floats_produce_distinct_hashes`, `test_exact_numeric_policy_int_vs_float_distinct_hashes`, `test_exact_numeric_policy_negative_zero_normalized`, `test_exact_numeric_policy_rejects_non_finite_values` |

## Subsystem Architecture & Implementation

The implementation resides in `cv_agent/orchestration/`:

- `contracts.py`: Authoritative typed enums (`LifecycleStatus`, `OperationStatus`, `ApprovalStatus`, `ReconciliationOutcome`), immutable dataclasses and Pydantic models (`ExecutionLease`, `OperationPayloadEnvelope`, `OperationIntent`, `ApprovalRequest`, `ApprovalDecision`, `OrchestrationEvent`, `OrchestratorConfig`), and protocol abstractions (`OrchestrationStore`, `OperationReconciler`). Uses `class Foo(str, Enum)` for Python 3.10–3.13 compatibility.
- `canonicalization.py`: Deterministic JSON serialization, NFC Unicode normalization, set sorting, exact numerical normalization, observational metadata stripping, SHA-256 computation, and envelope construction.
- `state_machine.py`: Authoritative lifecycle and operation transition validation. Invariant 17 strictly enforced; legal transitions to and from `RECOVERY_REQUIRED` registered.
- `store.py`: `SQLiteOrchestrationStore` managing `sessions`, CAS fencing, transactional monotonic event allocation, checkpoint saving/loading with corrupt checkpoint recovery selection, schema version validation, and conservative relational retention pruning.
- `fencing.py`: `LeaseManager` coordinating lease acquisition, renewal, and context-managed execution sessions (`session_scope`).
- `approvals.py`: `ApprovalManager` creating approval requests and validating the full 4-tuple security binding.
- `reconciliation.py`: `ReconciliationCoordinator` driving ambiguous side-effect recovery.
- `graph.py`: LangGraph `StateGraph` adapter executing agent workflows atop the authoritative store, backed by `SQLiteLangGraphCheckpointer`.
- `__init__.py`: Public export interface.

## Acceptance Criteria & Regression Coverage (51 Tests)

The test suite in `tests/test_orchestration_boundary.py` covers 51 contract and regression test scenarios:

1. **Canonicalization & Hashing (6 tests):**
   - Unicode NFC equivalence across combining characters.
   - Numeric edge cases (floats vs ints, trailing zeros) and dictionary key order independence.
   - Set canonicalization order independence.
   - Nested observational metadata stripping (trace IDs, timestamps, retry counts).
   - Material intent changes alter SHA-256 hash.
   - Operation envelope construction.
2. **Authoritative State Machine (6 tests):**
   - Valid lifecycle transitions.
   - Illegal lifecycle transitions fail closed.
   - Invariant 17: `EVALUATING` to `EXECUTING` rejected.
   - `EVALUATING` terminal-adjacent exits (`COMPLETED`, `FAILED`, `PAUSED`).
   - Terminal states reject all outbound transitions.
   - Operation state transitions.
3. **Lease Fencing & Concurrency (7 tests):**
   - Initial lease acquisition starts at epoch 1.
   - Atomic CAS renewal increments epoch.
   - Competing mutations on stale epoch fail closed with `StaleFencingLeaseError`.
   - Lease expiration during mutation fails closed with `LeaseExpiredError`.
   - Concurrent lease acquisition rejected for active worker.
   - Adoption of expired lease by new worker allowed.
   - Context manager `session_scope` renews lease on exit.
4. **Transactional Event Sequencing (2 tests):**
   - Strict monotonic sequence allocation per session.
   - Failed transactions roll back and do not consume sequence numbers.
5. **Persistent Approval Security 4-Tuple (5 tests):**
   - Successful verification when all 4 elements match.
   - Payload hash mismatch fails closed.
   - Resource scope mismatch fails closed.
   - Policy version mismatch fails closed.
   - Expired and rejected approvals fail closed.
6. **Side-Effect Reconciliation Protocol (4 tests):**
   - `UNKNOWN` transitions to `RECONCILING`.
   - `CONFIRMED_COMPLETED` transitions to `COMPLETED`.
   - `CONFIRMED_NOT_SUBMITTED` transitions to `DISPATCHABLE`.
   - `UNRESOLVED` outcome leaves operation in `RECONCILING` (operator escalation, never defaults to success/failure).
7. **Durable Store Integrity & Safe Retention (5 tests):**
   - State persistence and recovery across separate SQLite instances.
   - Unsupported checkpoint schema version raises `CheckpointCorruptionError`.
   - Pruning protects active leases.
   - Pruning protects pending approvals.
   - `experiments.sqlite` is completely untouched by pruning.
8. **LangGraph Workflow Integration & Isolation (3 tests):**
   - Unapproved workflow executes to completion.
   - Human-in-the-loop interruption before `await_approval` and resumption after approval.
   - Subsystem isolation: zero external LLM provider SDKs imported in `cv_agent/orchestration`.
9. **PM Ruling Regression Coverage (13 tests):**
   - `test_sqlite_sole_lifecycle_authority`: Verifies SQLite `sessions` table is sole lifecycle authority.
   - `test_atomic_lifecycle_transition_and_event_sequence_rollback`: Proves lifecycle transition and monotonic sequence allocation are atomic.
   - `test_operation_state_transition_validation_rejects_illegal_jump`: Validates illegal jumps in operation status fail closed.
   - `test_save_operation_cannot_reset_running_to_created`: Prevents resetting active operations back to `CREATED`.
   - `test_release_lease_preserves_checkpoints_and_session`: Verifies `release_lease()` deletes lease row while preserving checkpoints/events.
   - `test_pruning_relational_protection_no_json_dependency`: Confirms relational-only pruning without JSON searching.
   - `test_checkpoint_corruption_persists_recovery_required_and_diagnostic_event`: Tests persistent `RECOVERY_REQUIRED` and diagnostic event on unrecoverable corruption.
   - `test_recovery_selection_selects_newest_valid_checkpoint`: Tests fallback to newest valid checkpoint when recent ones are corrupt.
   - `test_exact_numeric_policy_close_floats_produce_distinct_hashes`: Tests exact IEEE-754 precision preservation without lossy truncation.
   - `test_exact_numeric_policy_int_vs_float_distinct_hashes`: Tests int vs float distinction.
   - `test_exact_numeric_policy_negative_zero_normalized`: Tests `-0.0` normalization to `0.0`.
   - `test_exact_numeric_policy_rejects_non_finite_values`: Tests rejection of `nan`, `inf`, `-inf`.
   - `test_langgraph_sqlite_checkpointer_persistence`: Tests LangGraph persistence directly to SQLite tables.

## Quality Gates & Verification Results

| Gate | Command | Result |
|---|---|---|
| Ruff Lint | `ruff check .` | Pass (All checks passed) |
| Ruff Format | `ruff format --check . [excludes]` | Pass (100 files already formatted) |
| Mypy (Default) | `mypy cv_agent` | Pass (Success: no issues found in 43 source files) |
| Mypy (Python 3.10) | `mypy --python-version 3.10 cv_agent/orchestration` | Pass (Success: no issues found in 9 source files) |
| Pytest Test Suite | `pytest -q` | Pass — 261 passed in 3.29s (0 warnings) |
| CLI Health Check | `python -m cv_agent` | Pass — Runtime status OK, LangGraph available (v1.2.11) |
| Git Diff Check | `git diff --check` | Pass (Zero whitespace or conflict errors) |
