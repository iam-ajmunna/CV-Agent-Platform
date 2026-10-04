# Issue #74 — Phase 6: Train / Evaluate / Diagnose Report

**Date:** 2026-10-03<br>
**Trunk:** `main`<br>
**Branch:** `feature/ajmunna/phase-6-train-evaluate-diagnose`<br>
**Issue:** [Issue #74](https://github.com/iam-ajmunna/CV-Agent-Platform/issues/74)<br>
**Authoritative Contracts:** Frozen `ADR-0003`, `ADR-0004`, `ADR-0005`, `ADR-0008`, `ADR-0009`, `ADR-0010`, `ADR-0011`, `[P§7]`, `[P§12]`, `[P§13]`, `[P§19]`, `[P§20]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§26]`, `[P§29.1]`, `[P§29.2]`, `[P§29.3]`, `[P§29.4]`, `[P§34]`<br>
**Status:** IMPLEMENTED AND VERIFIED — READY FOR PM REVIEW

---

## 1. Executive Summary

Phase 6 operationalizes the Train / Evaluate / Diagnose loop pursuant to `ROADMAP.md` Phase 6 and **Issue #74**.

The core architectural mandate of Phase 6 is to integrate existing subsystem authorities without creating competing orchestrators, training executors, evaluation runners, or experiment ledgers:
> **One approved training run must produce:**
> 1. A complete `ExperimentRecord` in `SQLiteExperimentLedger` (`TRAINING` workload with all mandatory validation accuracy and resource metrics per ADR-0004 §2.4.C).
> 2. A composite evaluation result per `docs/EVALUATION.md` (`ACCURACY + SYSTEM PERFORMANCE + TARGET CONSTRAINT + BASELINE DELTA`).
> 3. A ranked failure-category analysis grounded in empirical evidence (`FailureCategoryAnalysis`).
> 4. A proposed next experiment (`ProposedNextExperiment`) strictly undispatched (`is_dispatched: False`, `requires_fresh_approval: True`).

All 5 acceptance gates G1–G5 and all 20 mandatory adversarial test cases are fully satisfied and verified.

---

## 2. Fulfillment of Five Phase 6 Acceptance Gates

| Gate | Name | Architectural Requirement | Implementation & Test Coverage | Result |
|---|---|---|---|---|
| **G1** | **Plan & Cost Integrity** | Exact dataset manifest, split disjointness, checkpoint lineage, and target binding. Deterministic finite cost estimate; fail-closed rejection on non-finite values or invented hourly prices. | `compute_training_payload_hash`, `DeterministicTrainingEstimator`, `test_case_1`–`test_case_5` | **PASS** |
| **G2** | **Approval Verification & Recovery** | Mandatory human approval before GPU dispatch (`[P§24]`). Expiration check, canonical payload hash verification, target scope binding, restart/resume support, and ambiguous execution reconciliation (`UNKNOWN`/`RECONCILING`). | `ApprovalManager`, `ReconciliationCoordinator`, `test_case_6`–`test_case_10` | **PASS** |
| **G3** | **Training Execution & Ledger Recording** | Approved ADR-0005 execution via `ToolExecutor`, asynchronous handoff recovery, telemetry divergence monitoring (NaN/Inf, OOM), checkpoint provenance, and atomic ledger persistence (`WorkloadType.TRAINING`). | `DefaultTrainingLauncher`, `SQLiteExperimentLedger`, `test_case_11`–`test_case_14` | **PASS** |
| **G4** | **Composite Evaluation** | ADR-0011 evaluation plan referencing trained checkpoint, held-out test split, empirical quality + on-target latency/memory/power with hardware-synchronized timing (`[P§13]`, `[P§29.4]`), target constraints, and comparable root-baseline delta. | `DefaultEvaluationRunner`, `build_training_evaluation_plan`, `test_case_15`–`test_case_17` | **PASS** |
| **G5** | **Diagnosis & Proposed Next Experiment** | Empirical failure-category analysis with non-fabricated counts and distinct causal hypotheses. Proposed next experiment is strictly undispatched (`is_dispatched: False`, `requires_fresh_approval: True`, `cost_estimate_required: True`). | `FailureCategoryAnalysis`, `ProposedNextExperiment`, `test_case_18`–`test_case_20` | **PASS** |

---

## 3. Mandatory Adversarial Verification Matrix (20 Cases)

| Case | Scenario | Invariant Verified | Outcome |
|---|---|---|---|
| **Case 1** | Missing/mismatched dataset version or manifest hash | Plan dataset binding mismatch fails before dispatch | `Phase6ValidationError: Dataset manifest hash mismatch` |
| **Case 2** | Leakage / contamination detected | Temporal and camera/site leakage fails closed; 0 training jobs launched | `SplitLeakageError: ['temporal', 'camera_site']`, 0 jobs launched |
| **Case 3** | Unresolved base checkpoint | Pretrained weights reference unresolvable to valid checkpoint fails closed | `Phase6ValidationError: Base checkpoint manifest mismatch` |
| **Case 4** | Unregistered named target | Named target accelerator without registry authorization fails closed; 0 local synthesis | `Phase6ValidationError: Missing authorized execution TargetId` |
| **Case 5** | Non-finite cost inputs or missing hourly rate | Cost estimator fails closed on NaN/Inf latency or zero/missing price | `CostEstimationError: Positive finite float required` |
| **Case 6** | Approval denied | Zero dispatch permitted when approval decision is REJECTED (`[P§24]`) | `Phase6ApprovalError: Approval was NOT granted` |
| **Case 7** | Expired approval / mismatched payload / mismatched scope | Stale, tampered, or cross-target approvals fail closed | `Phase6ApprovalError: expired / payload hash mismatch / scope mismatch` |
| **Case 8** | Approval interrupt & restart | Approval request survives crash, persists in SQLite store, resumes once | Persistent approval recovered with matching payload hash |
| **Case 9** | Duplicate / replayed dispatch | Replaying identical training request rejects without launching duplicate paid jobs | `Phase6ExecutionError: Duplicate dispatch rejected` |
| **Case 10** | Ambiguous external execution | Unresolved external status remains `UNKNOWN`/`RECONCILING` without synthetic success | `ReconciliationOutcome.UNRESOLVED`, status `reconciling` |
| **Case 11** | Async dispatch without terminal checkpoint | Missing valid weights hash prevents marking training completed | `Phase6ValidationError: Checkpoint weights hash is missing or invalid` |
| **Case 12** | Telemetry divergence (NaN/Inf, OOM) | Telemetry loss divergence or OOM fails experiment cleanly with diagnostic details | `Phase6ExecutionError: Divergence detected in telemetry`, ledger FAILED |
| **Case 13** | Missing reproducibility metadata | Rejects missing commit SHA, placeholder commit (`"0"*40`), or missing random seed | `Phase6ValidationError: commit_sha / seed` |
| **Case 14** | Partial/failed training | Failed training jobs never publish completed ledger rows or evaluation artifacts | Ledger status `failed`, zero evaluation executed |
| **Case 15** | Evaluation disallowed split / wrong checkpoint | Evaluates strictly on held-out test split, rejecting train/val/calibration | `EvaluationValidationError: strictly disallowed` |
| **Case 16** | Missing telemetry or unsynchronized accelerator timing | Rejects unsynchronized timing or missing system telemetry on named accelerator | `Phase6EvaluationError: Hardware-synchronized timing / missing system_benchmarks` |
| **Case 17** | Incomparable baseline | Active baseline with different dataset version cannot establish comparative delta | `Phase6ValidationError: Incomparable baseline` |
| **Case 18** | Missing failure-category counts | Rejects empty or fabricated error ranking without empirical failure evidence | `Phase6DiagnosisError: Missing empirical failure-category counts` |
| **Case 19** | Next experiment proposal auto-dispatch prohibition | Next experiment is strictly undispatched (`is_dispatched: False`, `requires_fresh_approval: True`) | Hard assertion verified: `is_dispatched is False` |
| **Case 20** | Clean approved run full cycle | 1 training record in ledger, composite evaluation, ranked failure analysis, and 1 proposed next experiment | Complete cycle verified: `RUNNING` job, `COMPLETED` experiment, composite verdicts, ranked failures, undispatched proposal |

---

## 4. Architectural Boundaries

1. **Zero Layer Leakage:** Orchestration does not own model training, evaluation metrics, or experiment persistence. It delegates strictly to ADR-0010 (`TrainingLauncher`), ADR-0011 (`EvaluationRunner`), ADR-0004 (`SQLiteExperimentLedger`), and ADR-0003 (`OrchestrationStore`, `ApprovalManager`).
2. **Deterministic Reproducibility (`[P§25]`):** Explicit commit SHA, non-placeholder hardware, explicit integer seed, valid config URI and 64-hex SHA, and non-empty hyperparameters are strictly enforced.
3. **Hardware-Aware Deployability (`[P§13]`, `[P§29.4]`):** Evaluation on named accelerator requires hardware-synchronized timing (`timing_synchronized=True`). Wall-clock or unsynchronized timing fails closed.
4. **Approval Boundary (`[P§24]`):** Execution requires explicit human PM approval. Proposed next experiments are strictly proposals and require independent approval before any subsequent execution.

---

## 5. Repository Quality Gates

- **Total Test Suite:** 738/738 passed across 28 suites with zero regressions.
- **Phase 6 Exit Suite:** 20/20 passed in `tests/test_train_evaluate_diagnose_phase6_exit.py`.
- **Ruff Linter:** `ruff check .` clean (All checks passed!).
- **Ruff Formatter:** `ruff format --check cv_agent/orchestration/train_evaluate_diagnose.py cv_agent/evaluation/types.py tests/test_train_evaluate_diagnose_phase6_exit.py` clean.
- **Mypy Type Checker:** `mypy cv_agent` clean (`Success: no issues found in 122 source files`).
- **Git Diffs:** `git diff --check` clean.
