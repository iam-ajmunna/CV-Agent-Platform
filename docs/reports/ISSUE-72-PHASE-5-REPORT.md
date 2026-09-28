# Issue #72 — Phase 5: Data & Baseline Workflow Report

**Date:** 2026-09-27<br>
**Trunk:** `main`<br>
**Branch:** `feature/ajmunna/phase-5-data-baseline`<br>
**Pull Request:** [PR #73](https://github.com/iam-ajmunna/CV-Agent-Platform/pull/73)<br>
**Issue:** [Issue #72](https://github.com/iam-ajmunna/CV-Agent-Platform/issues/72)<br>
**Authoritative Contracts:** Frozen `ADR-0009`, `ADR-0011`, `ADR-0005`, `ADR-0004`, `[P§7]`, `[P§12]`, `[P§13]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§26]`, `[P§29.2]`, `[P§29.4]`, `[P§34]`<br>
**Status:** ACCEPTED AND SQUASH-MERGED TO MAIN (`0e9dd84f232e0b00530a3accb2bc6ddd268abc05`)

---

## 1. Executive Summary

Phase 5 operationalizes the Data & Baseline Establishment workflow pursuant to `ROADMAP.md` Phase 5 and **Issue #72**.

The core architectural mandate of Phase 5 is:
> **Baseline first — measure before optimizing.** (`[P§29.2]`)<br>
> **Measurable ground truth.** (`[P§26]`)<br>
> Accuracy without deployability is incomplete (`[P§29.4]`). A baseline run is not established until measurable ground truth is established with co-equal measurement of model quality, latency, memory, and power on a named target accelerator with hardware-synchronized timing (`[P§13]`, `[P§29.4]`).

Following PM reviews of PR #73, all blockers have been remediated:
- **B29 (Authoritative Ledger Provenance):** Eliminated substring target guessing, commit SHA repair/hashing, seed defaulting to 42, and manufactured configs/hyperparameters. Enforced explicit authoritative provenance in `BaselineEstablishmentRequest`.
- **B30 (Atomic Baseline Publication Transaction & Elimination of Fallback):** Implemented `complete_and_set_baseline` in `SQLiteExperimentLedger` within a single immediate SQLite transaction (`BEGIN IMMEDIATE ... COMMIT;`), ensuring zero split-brain orphan baseline states. Eliminated legacy two-step fallback (`complete_experiment` + `set_active_baseline`) in `cv_agent/orchestration/data_baseline.py` and enforced fail-closed validation rejecting non-conforming ledgers.
- **B31 (Rejection of Upstream Placeholder Evidence):** Enabled `DefaultEvaluationRunner` to ingest authentic environment telemetry, and enforced fail-closed validation rejecting placeholder commit SHAs (`"0"*40`), dirty git state, placeholder hardware names, and missing seeds.
- **Non-blocking Cleanups:** Restored 6 unrelated frozen files to clean `main` format.

---

## 2. Fulfillment of Five Phase 5 Acceptance Gates

| Gate | Name | Architectural Requirement | Implementation & Test Coverage | Result |
|---|---|---|---|---|
| **G1** | **Dataset Versioning & Integrity** | Manifest verification, content-addressed storage hashes, payload root checksum validation, and write-once immutability in `DatasetRegistry`. | `verify_payload_root_checksum`, `test_case_04_payload_root_mismatch_rejects`, `test_case_13_attempt_to_overwrite_immutable_dataset_version_rejects` | **PASS** |
| **G2** | **Leakage Fail-Closed Gate** | Mandatory 5-dimension leakage check (temporal, camera/site, duplicate, identity, annotation-round). Leaky splits fail closed with zero evaluation dispatch. | `StandardLeakageAuditor.audit_splits`, `test_case_01_temporal_leakage_only_rejects`, `test_case_02_camera_site_leakage_only_rejects`, `test_case_03_both_leakages_rejects_and_zero_evaluation_dispatch` | **PASS** |
| **G3** | **Clean Baseline Evaluation** | Evaluation on named target accelerator via ADR-0005. Requires empirical accuracy, latency, memory (VRAM), and power, with hardware-synchronized timing (`timing_synchronized=True`). Ingests authentic environment evidence. | `establish_dataset_baseline`, `test_case_07_target_mismatch_or_unauthorized_target_rejects`, `test_case_08_unsynchronized_accelerator_timing_rejects`, `test_case_09_missing_power_baseline_not_publishable`, `test_case_10_missing_memory_baseline_not_publishable`, `test_case_15`–`test_case_19` | **PASS** |
| **G4** | **Ledger Integrity & Active Root Baseline** | Atomic SQLite ledger persistence. Single-transaction completion and active baseline designation (`complete_and_set_baseline`). Zero corrupt/orphan baselines. Explicit non-empty hyperparameters (`[P§25]`). Unconditional atomic publication with zero legacy two-step fallback. | `SQLiteExperimentLedger.complete_and_set_baseline`, `test_case_12_failed_evaluation_no_active_baseline`, `test_case_14_clean_dataset_complete_evidence_creates_root_baseline`, `test_case_20`–`test_case_24` | **PASS** |
| **G5** | **Boundary & Adversarial Regression** | 24 mandatory adversarial cases, zero direct subprocess/SSH execution in orchestration, zero target synthesis, clean service delegation. | `tests/test_data_baseline_phase5_exit.py` (all 27 test cases passing) | **PASS** |

---

## 3. Mandatory Adversarial Verification Matrix

| Case | Scenario | Invariant Verified | Outcome |
|---|---|---|---|
| **Case 1** | Temporal leakage only | Train items occurring after val/test items fail closed | `SplitLeakageError: ['temporal']`, 0 tool calls |
| **Case 2** | Camera/site leakage only | Train items sharing camera locations with val/test fail closed | `SplitLeakageError: ['camera_site']`, 0 tool calls |
| **Case 3** | Dual temporal + camera leakage | Both failures identified; strictly zero tool calls dispatched | `SplitLeakageError: ['temporal', 'camera_site']`, 0 tool calls |
| **Case 4** | Payload root checksum mismatch | Tampered manifest checksum fails integrity check | `PayloadIntegrityError`, 0 tool calls |
| **Case 5** | Plan dataset version mismatch | Evaluation plan bound to different dataset version fails | `EvaluationValidationError: dataset version mismatch` |
| **Case 6** | Calibration split as test split | Calibration split reserved for quantization cannot be evaluated | `EvaluationValidationError: Calibration split reserved` |
| **Case 7** | Unauthorized target execution | Execution target conflicting with approval scope fails | `EvaluationApprovalRequiredError: conflicts with authorized scope` |
| **Case 8** | Unsynchronized accelerator timing | Wall clock timing on accelerator is rejected fail closed | `BenchmarkExecutionError: Hardware-synchronized timing required` |
| **Case 9** | Missing empirical power telemetry | Power cannot default to zero or None; baseline unpublishable | `BaselineValidationError: Missing empirical power measurement` |
| **Case 10** | Missing empirical memory telemetry | VRAM cannot default to zero or None; baseline unpublishable | `BaselineValidationError: Missing empirical memory measurement` |
| **Case 11** | Checkpoint weights hash mismatch | Model artifact conflicting with checkpoint manifest fails | `EvaluationValidationError: weights hash mismatch` |
| **Case 12** | Tool failure during evaluation | Tool execution failure rolls back; zero active baseline stored | `EvaluationError: Tool execution failed`; active baseline is None |
| **Case 13** | Mutating immutable dataset version | Overwriting accepted dataset version fails | `DatasetVersionConflictError` |
| **Case 14** | Clean dataset + complete evidence | Clean dataset produces exactly one active root baseline | `is_root_baseline=True`, `active_baseline.spec.baseline_id=None` |
| **Case 15** | Placeholder commit SHA (`"0"*40`) | Rejects placeholder git commit provenance fail-closed | `BaselineValidationError: placeholder git_commit_sha` |
| **Case 16** | Malformed git commit SHA | Rejects non-40 hex commit SHA without attempting hash repair | `BaselineValidationError: not a valid 40-character hex SHA` |
| **Case 17** | Placeholder target hardware | Rejects `"Target Hardware"` placeholder hardware telemetry | `BaselineValidationError: placeholder target_hardware` |
| **Case 18** | Missing random seed | Rejects `seed=None` without defaulting to 42 | `BaselineValidationError: Missing or non-integer random seed` |
| **Case 19** | Dirty git state (`git_is_dirty=True`) | Rejects uncommitted changes in evaluation environment | `BaselineValidationError: git_is_dirty=True` |
| **Case 20** | Missing/invalid `ExecutionTarget` | Rejects missing `ExecutionTarget` without substring guessing | `BaselineValidationError: missing execution_target` |
| **Case 21** | Invalid config provenance | Rejects empty config URI or malformed 64-hex config hash | `BaselineValidationError: config_uri / config_hash` |
| **Case 22** | Empty hyperparameters mapping | Rejects empty hyperparameters without manufacturing placeholders | `BaselineValidationError: hyperparameters must be non-empty` |
| **Case 23** | Atomic publication & rollback | `complete_and_set_baseline` rolls back completely on error | Atomic transaction verified; full rollback on integrity error |
| **Case 24** | Legacy non-conforming ledger | Rejects missing `complete_and_set_baseline` fail-closed | `BaselineValidationError`, 0 calls to `complete_experiment` / `set_active_baseline` |

---

## 4. Architectural Boundary Tests

1. **Zero Subprocess / SSH in Orchestration (`test_no_direct_subprocess_or_ssh_in_workflow`)**: Mechanical AST inspection of `cv_agent/orchestration/data_baseline.py` proves zero imports or calls to `subprocess`, `os.system`, `os.popen`, `paramiko`, or `asyncssh`. All physical execution delegates strictly to ADR-0005 `ToolExecutor`.
2. **Zero Target Synthesis Permitted (`test_no_target_synthesis_permitted`)**: Omitting target ID or attempting local placeholder synthesis raises `EvaluationApprovalRequiredError` fail-closed.
3. **Clean Service Delegation (`test_workflow_service_class_instantiation`)**: `DatasetBaselineWorkflow` service class cleanly orchestrates the workflow.

---

## 5. Repository Quality Gates

- **Test Suite**: 718/718 passing tests across 27 suites with zero regressions.
- **Phase 5 Exit Suite**: 27/27 passing tests in `tests/test_data_baseline_phase5_exit.py`.
- **Ruff Linter**: `ruff check .` clean (0 errors).
- **Ruff Formatter**: `ruff format --check` clean (198 source files formatted).
- **Mypy Type Checker**: `mypy cv_agent` clean (0 issues across 121 source files).
- **Git Diffs**: `git diff --check` clean.
- **Package Build**: `python -m build` passes.
- **Security Audit**: `pip-audit` clean (0 known vulnerabilities).
