# Issue #45 — Phase 2F Implementation Report

**Issue:** [#45](https://github.com/iam-ajmunna/CV-Agent-Platform/issues/45)  
**PR:** To be opened  
**Branch:** `feature/ajmunna/phase-2f-memory-ledger`  
**Status:** Implemented; all 280 tests pass (including 19 Phase 2F contract tests); all quality gates pass.

## Overview

This report documents the implementation of Phase 2F — ADR-0004 Project Memory & Experiment Ledger Persistence under Issue #45 according to the frozen ADR-0004 specification, the 8 non-negotiable architectural gates, and the 10 mandatory PM corrections.

The subsystem establishes the dual-layer persistence architecture:
1. **Git-Tracked Structured Project Memory (`.cv_agent/memory/`):** Captures qualitative domain state, operational requirements, camera setups, architectural decisions, dataset lineage, and learned empirical heuristics in human-readable YAML and Markdown documents designed for Git versioning.
2. **Permanent SQLite Experiment Ledger (`.cv_agent/state/experiments.sqlite`):** Dedicated local SQLite database operating with Write-Ahead Logging (`PRAGMA journal_mode=WAL;`) and foreign keys enforced (`PRAGMA foreign_keys = ON;`), capturing machine-readable experiment records, reproducibility metadata, and paired accuracy and system metrics with strict terminal result immutability.

## The 8 Non-Negotiable PM Implementation Gates

### Gate 1: Dual-Layer Workspace Containment
- Git-tracked structured memory resides strictly under `<workspace_root>/.cv_agent/memory/`.
- Experiment ledger resides strictly at `<workspace_root>/.cv_agent/state/experiments.sqlite`.
- Path traversal and symlink resolution attacks are blocked using `Path.resolve()` and `is_relative_to(workspace_root)` validation.

### Gate 2: Terminal Result Immutability
- Once an experiment reaches a terminal status (`completed`, `failed`, `cancelled`), the entire record is completely frozen `[P§25]`.
- Any attempt to modify lifecycle status, metrics, hyperparameters, artifacts, or notes on a terminal experiment raises `ExperimentImmutabilityError`.
- In-flight experiments permit mutation only of designated lifecycle fields (`status`, `job_id`, `started_at`, `completed_at`, `error_message`) according to the formal state transition matrix.

### Gate 3: Formal Lifecycle State Transition Matrix
- Status transitions strictly adhere to ADR-0004 §2.4.B:
  - `proposed` → `queued`, `running`, `cancelled`, `failed`
  - `queued` → `running`, `cancelled`, `failed`
  - `running` → `completed`, `failed`, `cancelled`
  - Terminal states reject all transitions.
- Illegal jumps fail closed with `InvalidExperimentTransitionError`.

### Gate 4: SQLite Foreign Key & WAL Invariants
- Every connection executes `PRAGMA foreign_keys = ON;` before executing any statement.
- WAL mode is enabled for crash-resilient concurrency.
- Lineage relationships (`parent_exp_id` and `baseline_id`) enforce `ON DELETE RESTRICT`. Root baselines store `NULL` indicating no predecessor baseline.

### Gate 5: Workload-Aware Metric Completeness
- `complete_experiment()` validates required evidence per ADR-0004 §2.4.C:
  - `training`: Validation accuracy metrics (`map_50`, `map_50_95`, `precision`, `recall`) + training resource metrics (`gpu_hours` or `training_time_seconds`, `peak_vram_mb`).
  - `evaluation`: Task-specific accuracy metrics + on-target system metrics when evaluating deployability `[P§12]`.
  - `optimization`: Post-optimization accuracy/tolerance metrics + on-target system metrics (`latency_inference_ms` or `latency_e2e_ms`, `throughput_fps`, `peak_vram_mb`, `power_draw_watts`).
  - `benchmark`: On-target system metrics under production stream conditions (`throughput_fps`, `latency_inference_ms` or `latency_e2e_ms`, `peak_vram_mb`, `power_draw_watts`) + relevant accuracy verification.
- Enforces `[P§29.4]`: Any completion lacking required on-target metrics fails closed with `IncompleteMetricsError`.

### Gate 6: Active Baseline Mutability Isolation
- Active baseline designations are maintained in a dedicated `active_baselines` table (`workload_type PRIMARY KEY`, `exp_id`, `updated_at`).
- Updating or promoting an active baseline updates project baseline mapping without modifying historical, immutable experiment records `[P§29.2]`.
- Transactionally enforces that candidate experiments must be in `COMPLETED` status and match the target workload class.

### Gate 7: Complete Reproducibility Metadata
- `ExperimentSpec` enforces complete reproducibility metadata: `commit_sha` (Git commit hash), `dataset_version` (ADR-0009 manifest binding), `seed` (weight initialization and sampling), `target` (`local_gpu`, `remote_gpu`, `cloud`), input resolution, and hyperparameter payload `[P§25]`, `[P§29.5]`.

### Gate 8: Decoupled Approval Dereferencing
- Validates the `{operation_id}:{request_id}` regex format matching authoritative ADR-0003 approval records without duplicating approval engine state or tables.

## Subsystem Architecture & Implementation

### 1. `cv_agent/memory/`
- `interfaces.py`: Typed immutable dataclasses (`HardwareConstraints`, `ArchitecturalDecision`, `LearnedHeuristic`) and protocols (`ProjectMemoryReader`, `ProjectMemoryWriter`).
- `service.py`: `GitProjectMemory` service providing symlink-safe workspace containment, canonical YAML serialization for `hardware_constraints.yaml`, and deterministic structured markdown parsing/serialization for `architectural_choices.md` and `learned_heuristics.md`.
- `__init__.py`: Public exports.

### 2. `cv_agent/experiments/`
- `interfaces.py`: Enums (`ExperimentStatus`, `WorkloadType`, `ExecutionTarget`), immutable dataclasses (`AccuracyMetrics`, `SystemMetrics`, `ArtifactManifest`, `ExperimentSpec`, `ExperimentRecord`), and protocol (`ExperimentLedger`).
- `state_machine.py`: State transition validator enforcing the §2.4.B matrix and terminal immutability.
- `metrics_validator.py`: Workload-specific metric completeness validator enforcing §2.4.C.
- `ledger.py`: `SQLiteExperimentLedger` managing schema creation, WAL/FK enforcement, atomic terminal transitions (`BEGIN IMMEDIATE ... COMMIT`), lineage verification, baseline registry management, and querying.
- `__init__.py`: Public exports.

## Test Suite & Verification Results (19 Tests)

`tests/test_memory_ledger_boundary.py` covers 19 contract scenarios:
1. `test_initialization_and_workspace_scoping`: Verifies memory tree creation and workspace scoping.
2. `test_symlink_aware_workspace_containment`: Proves symlink traversal attacks outside workspace root fail closed with `WorkspaceContainmentError`.
3. `test_hardware_constraints_roundtrip`: Verifies YAML serialization/deserialization.
4. `test_invalid_hardware_constraints_schema_fails`: Proves corrupted/invalid YAML raises `ProjectMemoryFormatError`.
5. `test_architectural_decisions_append_and_retrieve`: Verifies append-oriented markdown decisions.
6. `test_learned_heuristics_append_and_retrieve`: Verifies append-oriented markdown heuristics.
7. `test_initialization_and_containment`: Verifies ledger database creation at `.cv_agent/state/experiments.sqlite`.
8. `test_symlink_containment_violation_fails`: Proves ledger path traversal outside workspace root fails closed with `LedgerWorkspaceContainmentError`.
9. `test_create_experiment_reproducibility_fields_and_query`: Verifies creation with all reproducibility fields.
10. `test_approval_ref_format_validation`: Verifies opaque `{operation_id}:{request_id}` validation.
11. `test_lifecycle_transitions_enforce_matrix`: Verifies legal transitions and rejects illegal jumps.
12. `test_terminal_immutability_completed`: Proves completed experiments reject all mutations.
13. `test_terminal_immutability_failed_and_cancelled`: Proves failed and cancelled experiments reject all mutations.
14. `test_metric_completeness_training_workload`: Proves training completion requires validation accuracy and resources.
15. `test_metric_completeness_optimization_workload`: Proves optimization completion requires on-target latency/throughput/vram/power.
16. `test_relational_lineage_and_foreign_keys`: Verifies `parent_exp_id` foreign keys and `ON DELETE RESTRICT`.
17. `test_active_baseline_registry_isolation`: Verifies baseline designation updates registry without mutating experiment records.
18. `test_concurrency_competing_writers`: Multi-threaded competing writers test under SQLite WAL.
19. `test_cross_backend_isolation`: Explicit test verifying `experiments.sqlite ≠ checkpoints.sqlite`.

## Quality Gates Summary

| Gate | Command | Result |
|---|---|---|
| Ruff Lint | `ruff check .` | Pass (All checks passed) |
| Ruff Format | `ruff format --check . [excludes]` | Pass (109 files already formatted) |
| Mypy (Default) | `mypy cv_agent` | Pass (Success: no issues found in 51 source files) |
| Mypy (Python 3.10) | `mypy --python-version 3.10 cv_agent/memory cv_agent/experiments` | Pass (Success: no issues found in 8 source files) |
| Pytest Test Suite | `pytest -q` | Pass — 280 passed in 3.38s (0 warnings) |
| CLI Health Check | `python -m cv_agent` | Pass — Runtime status OK |
| Git Diff Check | `git diff --check` | Pass (Clean) |
