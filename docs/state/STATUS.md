# STATUS

> **Rewritten** every session. Describes **now**, never history — history lives in
> `JOURNAL.md`. Hard cap: 60 lines. If it exceeds that, you are logging, not stating.

**Updated:** 2026-09-08 · **Phase:** 1 — Architecture & ADR Specification · **Health:** green

## Where we are

The repository operates on a single-trunk development flow (`main` + short-lived work
branches) under the sole approval authority of the project owner as Official PM.
`dev-munna` is permanently retired (D-027).

The frozen project definition is `docs/PROJECT.md`. ADR-0001 through ADR-0008 are
accepted. Decisions Q1–Q5, Q7–Q8, Q10 (D-028), and D-022 through D-028 are recorded.
Q10 is resolved: manifest-first content-addressed dataset storage with pluggable
payload backends (`local_fs`, `s3`, `gcs`, `minio`).

## In flight

| Item | Issue | State |
|---|---|---|
| None | — | — |

## Next 3 actions

1. Open GitHub Issue for ADR-0009 (Dataset Subsystem Architecture).
2. Cut short-lived branch `docs/issue-adr-0009-dataset` from `main`.
3. Draft ADR-0009 specification in `Status: Proposed` for Official PM review.

## Blockers

- Downstream implementation work is gated on architecture ADR acceptance.
- ADR-0009 specification requires completion and Official PM review before ADR-0010.

## Do not start yet

Stage workflows, RAG, training execution, SkillSource/skill discovery, or
CapabilityResolver implementation. These depend on accepted architecture ADRs.
