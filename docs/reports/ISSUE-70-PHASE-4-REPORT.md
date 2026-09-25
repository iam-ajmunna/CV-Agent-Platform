# Issue #70 — Phase 4: DISCOVER / DEFINE Workflow Report

**Date:** 2026-09-25<br>
**Branch:** `feature/ajmunna/phase-4-discover-define-workflow`<br>
**Issue:** [Issue #70](https://github.com/iam-ajmunna/CV-Agent-Platform/issues/70)<br>
**Authoritative Contracts:** Frozen `ADR-0008`, `ADR-0003`, `ADR-0001`, `ADR-0004`, `[P§5]`, `[P§30]`, `[P§29.1]`, `[P§29.4]`, `[P§34]`<br>
**Status:** IMPLEMENTATION COMPLETE & VERIFIED — PENDING PM REVIEW

---

## 1. Executive Summary

Phase 4 operationalizes the interactive DISCOVER / DEFINE workflow from `[P§5]` and `[P§30]` under **Issue #70**.

The core architectural mandate of Phase 4 is:
> **Problem first — characterize before selecting.** (`[P§29.1]`)<br>
> When given an operational request (such as the canonical prison escape-attempt detection scenario), the agent must systematically ask targeted operational questions rather than jumping to model proclamation.

Strict authority boundaries are preserved across subsystems:
- **ADR-0008 (Reasoning):** Owns cognitive judgment, elicitation generation (`ElicitationNode`), problem characterization (`ProblemFramingNode`), and domain task decomposition (`TaskDecompositionNode`).
- **ADR-0003 (Orchestration):** Owns workflow execution, state machine lifecycle (`READY` -> `PLANNING` -> `PAUSED` -> `READY` -> `PLANNING` -> `EXECUTING` -> `EVALUATING` -> `COMPLETED`), checkpointer state (`SQLiteLangGraphCheckpointer`), and execution lease management (`SQLiteOrchestrationStore`).
- **ADR-0001 (Capabilities):** Authoritative identity for all `CapabilityId` entries referenced in `CapabilityIntent`.
- **ADR-0004 (Memory):** Owns structured project memory persistence under `.cv_agent/memory/`.
- **ADR-0005 / ADR-0007 (Execution & Skills):** Strict zero-execution barrier enforced via `Phase4ExecutionBarrier`.

---

## 2. Fulfillment of Official PM Scoping Corrections

All six required architectural corrections from the Official PM review were implemented and verified:

| # | Official PM Correction | Resolution & Implementation Detail |
|---|---|---|
| **1** | **Anti-YOLO Gate as Runtime Invariant** | Implemented `_PROHIBITED_MODEL_TERMS` and `assert_zero_prohibited_models(text, context_label)` in `cv_agent/reasoning/contracts.py`. Checked across input queries, reasoning prompts, elicitation inquiries, and project memory specifications. Raises typed `PrematureModelSelectionError` immediately upon detecting concrete model names (YOLO, RT-DETR, Faster-RCNN, etc.) during DISCOVER/DEFINE. |
| **2** | **Full 8-Dimension Problem Characterization** | Implemented `ProblemCharacterization` validating all 8 canonical dimensions from `[P§5]` and `[P§30]`: (1) `task_type`, (2) `input_modality`, (3) `target_hardware`, (4) `latency_budget_ms`, (5) `throughput_fps`, (6) `accuracy_target`, (7) `environmental_conditions`, (8) `data_availability`. `is_well_formed` returns `True` only when all 8 are non-empty and valid. |
| **3** | **Task DAG Domain Primitives vs CapabilityIds** | `TaskNode.task_type` represents pure domain decomposition concepts (`object_detection`, `tracking`, `action_recognition`, `spatial_geometry`). Actual execution intents specify `CapabilityIntent.capability_id` strictly referencing identifiers authoritative in ADR-0001 (`cv.detection.train`, `cv.deployment.optimization`, etc.). |
| **4** | **Checkpointed Resumption Contract** | Implemented `create_discover_define_graph()` utilizing `SQLiteLangGraphCheckpointer` and `SQLiteOrchestrationStore`. Underspecified inputs transition the workflow to `PAUSED` and interrupt at `await_human_input`. Resuming with `human_answers` on the active `thread_id` restarts the graph into `READY` -> `PLANNING` without losing session state or creating dangling leases. |
| **5** | **Atomic Project Memory Persistence** | Added `commit_project_specification()` to `cv_agent/memory/service.py`. Enforces 5 pre-commit validation checks (`is_well_formed`, anti-YOLO verification, DAG cycle check, capability identity validation) and performs transactional multi-file replacement/rollback across `project_spec.md`, `hardware_constraints.yaml`, and `architectural_choices.md`. |
| **6** | **Execution Barrier & Boundary Verification** | Implemented `Phase4ExecutionBarrier` implementing `ToolExecutor` and trapping any `.execute()` call with `WorkflowBoundaryViolationError`. Mechanical AST audits prove zero provider SDKs and zero CUDA launches in reasoning and discover/define orchestration modules. |

---

## 3. Five Phase 4 Exit Gates Verification

| Gate | Requirement | Evidence / Test | Result |
|---|---|---|---|
| **G1 DISCOVER** | Zero concrete model names before operational questions; produces structured `OperationalInquiry`. | `test_gate_g1_discover_zero_yolo_and_operational_questions` | **PASS** |
| **G2 Resume** | Resumes multi-turn session from SQLite checkpoint on active `thread_id` with human answers; transitions `PAUSED` -> `READY` -> `PLANNING`. | `test_gate_g2_resumption_from_checkpoint` | **PASS** |
| **G3 DEFINE** | Produces `ProblemCharacterization` with `is_well_formed == True` across all 8 dimensions and valid `CVTaskDecompositionDAG`. | `test_gate_g3_define_characterization_and_task_dag` | **PASS** |
| **G4 Atomic Memory** | Commits `project_spec.md`, `hardware_constraints.yaml`, `architectural_choices.md`; rolls back cleanly on invalid/prohibited input. | `test_gate_g4_atomic_memory_persistence`, `test_gate_g4_atomic_memory_rollback_on_prohibited_model`, `test_gate_g4_atomic_memory_rejects_incomplete_characterization` | **PASS** |
| **G5 Boundary** | Execution barrier blocks all tool/skill calls; AST audit verifies zero provider SDKs and zero CUDA kernel launches. | `test_gate_g5_execution_barrier_blocks_all_tools`, `test_gate_g5_ast_audit_zero_provider_sdks_and_zero_cuda_launches` | **PASS** |

---

## 4. Adversarial & Boundary Test Coverage

The test suite in `tests/test_discover_define_phase4_exit.py` exercises 11 focused test scenarios:

1. **`test_gate_g1_discover_zero_yolo_and_operational_questions`**:
   - Asserts underspecified prompt `"I have a prison project — escape-attempt detection"` triggers `node_discover`.
   - Verifies produced inquiry contains zero prohibited model names.
   - Verifies graph interrupts at `await_human_input` with orchestration state `PAUSED`.
2. **`test_gate_g2_resumption_from_checkpoint`**:
   - Verifies paused state can be resumed using active `thread_id` with human answers.
   - Verifies state transitions `PAUSED` -> `READY` -> `PLANNING` and proceeds to `node_define`.
3. **`test_gate_g3_define_characterization_and_task_dag`**:
   - Verifies `ProblemCharacterization` contains all 8 required dimensions and reports `is_well_formed == True`.
   - Verifies task DAG decomposes into domain primitives (`object_detection`, `tracking`, etc.) without circular dependencies.
4. **`test_gate_g4_atomic_memory_persistence`**:
   - Verifies atomic creation of `project_spec.md`, `hardware_constraints.yaml`, and `architectural_choices.md` under `.cv_agent/memory/`.
   - Verifies round-trip retrieval via `get_project_specification()`.
5. **`test_gate_g4_atomic_memory_rollback_on_prohibited_model`**:
   - Verifies pre-commit validation rejects specification containing prohibited model names with `PrematureModelSelectionError`.
   - Verifies transactional rollback leaves zero residual files on disk.
6. **`test_gate_g4_atomic_memory_rejects_incomplete_characterization`**:
   - Verifies specification with missing dimensions raises `MemoryTransactionValidationError` before disk write.
7. **`test_gate_g5_execution_barrier_blocks_all_tools`**:
   - Verifies `Phase4ExecutionBarrier` denies any `ToolExecutor.execute()` call with `WorkflowBoundaryViolationError`.
8. **`test_gate_g5_ast_audit_zero_provider_sdks_and_zero_cuda_launches`**:
   - Mechanically parses AST of `cv_agent/reasoning/` and `cv_agent/orchestration/discover_define.py`.
   - Asserts 0 provider SDK imports (`openai`, `anthropic`, `ollama`), 0 CUDA launches (`<<<...>>>`), 0 direct CUDA bindings, and 0 TensorRT builders.
9. **`test_cvagent_facade_run_discover_define_end_to_end`**:
   - Validates `CVAgent.run_discover_define()` multi-turn facade coordinating discovery, interruption, human input update, and completion.
10. **`test_adversarial_cyclic_dag_rejection`**:
    - Asserts `CVTaskDecompositionDAG` construction fails immediately on cyclic task dependencies (`t1 -> t2 -> t1`).
11. **`test_adversarial_premature_model_terms_detection`**:
    - Validates regex catching various forms of premature model architecture references (`yolo`, `rt-detr`, `faster-rcnn`).
12. **`test_adversarial_b26_partial_human_answers_keeps_discover_active`**:
    - Verifies partial human answers (2 of 8 dimensions) keep DISCOVER active in `PAUSED` state without prematurely routing to DEFINE.
    - Demonstrates multi-turn loop: 8 unresolved -> answer 2 -> 6 unresolved -> answer remainder -> DEFINE completed.
13. **`test_adversarial_b27_unregistered_capability_fails_closed`**:
    - Verifies syntactically valid but unregistered/synthetic `CapabilityId` (`cv.synthetic.unregistered_vision_capability`) causes DEFINE to fail closed with `DomainValidationError` before project memory publication.
14. **`test_adversarial_b28_define_does_not_fabricate_hardware_defaults`**:
    - Verifies unspecified hardware properties remain `None` / `()` without hallucinating defaults from product strings like `"jetson"`.
15. **`test_adversarial_b26_mock_llm_false_complete_fails_closed`**:
    - Verifies that when a mock LLM falsely claims zero unresolved dimensions with only 2 dimensions answered, the workflow deterministically evaluates accumulated evidence, rejects the false completeness claim, remains in `PAUSED` state (`needs_elicitation=True`), and does not enter DEFINE.
16. **`test_b27_capability_intent_lossless_round_trip`**:
    - Verifies lossless, deterministic round-trip serialization and deserialization of typed `CapabilityIntent` across `capability_id`, `intent_description`, nested `parameter_intent` (primitives, lists/tuples, nested dicts), and `target_preference` in `GitProjectMemory`.
17. **`test_adversarial_b26_missing_operational_definition_cannot_enter_define`**:
    - Verifies that supplying every dimension except `operational_definition` (camera, physical environment, hardware, latency, throughput, error tolerance, data availability, intervention modality) while the mock LLM falsely claims completeness (`unresolved_dimensions=[]`, `targeted_questions=[]`) deterministically keeps DISCOVER active, remains in `PAUSED` state with `needs_elicitation=True`, specifically flags `operational_definition` in operational inquiries, and fails closed with `IncompleteCharacterizationError` via `route_after_discover` and `validate_define_preconditions`.

---

## 5. Official PM Review & Re-Review Remediation (B26, B27, B28)

| Finding | Architectural Blocker | Root Cause & Resolution | Status |
|---|---|---|---|
| **B26** | **Deterministic Operational Completeness / operational_definition Enforcement** | Initially, `node_discover` relied on LLM-reported `unresolved_dimensions` and truthy answers. Remediation implemented deterministic checks `find_missing_operational_dimensions()` and `validate_define_preconditions()` from accumulated evidence across all 8 canonical dimensions (`operational_definition`, `camera_infrastructure`, `physical_environment`, `target_hardware`, `error_tolerance`, `data_availability`, `intervention_modality`, plus timing constraints `latency_budget_ms`/`throughput_fps`). Workflow fails closed (`needs_elicitation=True`, `PAUSED`, `IncompleteCharacterizationError` barrier) if evidence is incomplete, even if the model claims completeness. Verified via adversarial tests: partial answers, mock LLM false complete, and omission of `operational_definition` specifically. | **DISCHARGED** |
| **B27** | **Lossy CapabilityIntent persistence** | `CapabilityIntent` was previously serialized with only ID and description, dropping `target_preference` and clearing `parameter_intent` on reload. Remediation implemented structured Markdown block serialization (`### {capability_id}`) with inline JSON for `parameter_intent` (handled via recursive `_to_json_serializable`) and explicit `target_preference`. Verified by 100% round-trip equality test. | **DISCHARGED** |
| **B28** | **Fabricated hardware facts from "jetson"** | `node_define()` previously manufactured VRAM (16GB), RAM (32GB), power (60W), and precisions from substring. Remediation removed all fabricated defaults; unestablished properties strictly remain `None` / `()`. | **DISCHARGED (PM Verified)** |

---

## 6. Repository Quality Gates

```text
============================= test session starts ==============================
platform darwin -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0
collected 691 items
691 passed in 6.15s
============================= 691 passed in 6.15s ==============================
```

- **Pytest:** 691/691 tests passing across 26 test suites (0 regressions).
- **Ruff Check:** Clean (0 errors).
- **Ruff Format:** Clean across 195 source files.
- **Mypy:** Clean (0 issues across 120 source files).
- **Git diff check:** Clean (no whitespace or conflict markers).

---

## 7. Scope Boundary Affirmation

During Phase 4, the following boundaries were strictly observed:
- Zero model selection or architecture recommendation performed.
- Zero dataset acquisition or manifest publication performed.
- Zero training, benchmarking, evaluation, or optimization performed.
- Zero tool invocations, subprocess calls, MCP requests, or skill executions performed.
- Zero provider SDKs imported outside the LLM gateway.
