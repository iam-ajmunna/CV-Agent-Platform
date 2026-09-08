# STATUS

> **Rewritten** every session. Describes **now**, never history — history lives in
> `JOURNAL.md`. Hard cap: 60 lines. If it exceeds that, you are logging, not stating.

**Updated:** 2026-09-08 · **Phase:** 1 — Architecture & ADR Specification · **Health:** green

## Where we are

The repository operates on a single-trunk development flow (`main` + short-lived work
branches) under the sole approval authority of the project owner as Official PM.
`dev-munna` is permanently retired (D-027).

The frozen project definition is `docs/PROJECT.md`. ADR-0001 through ADR-0009 are
formally accepted. Decisions Q1–Q5, Q7–Q8, Q10 (D-028), and D-022 through D-029 are
recorded. The dataset architecture is fully settled: manifest-first content-addressed
storage with pluggable payload backends (`local_fs`, `s3`, `gcs`, `minio`).

## In flight

| Item | Issue | State |
|---|---|---|
| None | — | — |

## Next 3 actions

1. Await Official PM instruction establishing scope and requirements for ADR-0010.
2. Open GitHub Issue for ADR-0010 (Training Subsystem Architecture) upon authorization.
3. Cut short-lived branch `docs/issue-adr-0010-training` from `main` to draft ADR-0010.

## Blockers

- Downstream implementation work is gated on Phase 1 architecture ADR acceptance.
- ADR-0010 drafting requires explicit Official PM authorization.

## Do not start yet

ADR-0010 specification, stage workflows, training execution, or runtime code.
These depend on explicit PM gate authorization.
