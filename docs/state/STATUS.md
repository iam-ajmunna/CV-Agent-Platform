# STATUS

> **Rewritten** every session. Describes **now**, never history — history lives in
> `JOURNAL.md`. Hard cap: 60 lines. If it exceeds that, you are logging, not stating.

**Updated:** 2026-09-09 · **Phase:** 1 — Architecture & ADR Specification · **Health:** green

## Where we are

The repository operates on a single-trunk development flow (`main` + short-lived work
branches) under the sole approval authority of the project owner as Official PM.
`dev-munna` is permanently retired (D-027).

The frozen project definition is `docs/PROJECT.md`. ADR-0001 through ADR-0012 are
formally accepted. Decisions Q1–Q5, Q7–Q8, Q10 (D-028), and D-022 through D-032 are
recorded. The optimization and deployment architecture is settled: model optimization
pipelines (`OptimizationPlan`), dynamic shape profiles, calibration dataset bindings
to ADR-0009 manifests, cryptographic artifact lineage, mandatory post-optimization
verification via ADR-0011, runtime-agnostic deployment architecture (`DeploymentRuntime`:
Standalone TensorRT, DeepStream, Triton, ONNX Runtime), hardware-locked target profiles
(`DeploymentTarget`), production human approval gates, and health probing with rollback.

## In flight

| Item | Issue | State |
|---|---|---|
| None | — | — |

## Next 3 actions

1. Await Official PM directive establishing scope for the next architectural milestone (ADR-0013 — Monitoring & Active Learning Subsystem Architecture).
2. Open GitHub Issue for ADR-0013 upon authorization.
3. Cut short-lived branch `docs/issue-adr-0013-monitoring` from `main` to draft ADR-0013.

## Blockers

- Downstream implementation work is gated on Phase 1 architecture ADR acceptance.
- ADR-0013 drafting requires explicit Official PM authorization.

## Do not start yet

ADR-0013 specification, runtime optimization, deployment execution, or stage runtime code.
These depend on explicit PM gate authorization.
