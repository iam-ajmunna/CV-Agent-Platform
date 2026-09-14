# STATUS

> **Rewritten** every session. Describes **now**, never history — history lives in
> `JOURNAL.md`. Hard cap: 60 lines. If it exceeds that, you are logging, not stating.

**Updated:** 2026-09-14 · **Phase:** Phase 2G Knowledge & RAG Closed / Next Milestone Pending · **Health:** green

## Where we are

Phase 2A through Phase 2G are formally merged to `main`.
Phase 2G (ADR-0006 Knowledge & RAG Subsystem, Issue #47) is complete and closed:
- PR #48 squash-merged to `main` (`6cc400f`).
- Issue #47 closed as completed; feature branch deleted locally and on origin.
- Decision `D-052` recorded in `DECISIONS.md`.
- Passive evidence retrieval substrate operating with deterministic BM25 lexical index and
  dense embeddings exclusively through ADR-0002 LLM Gateway (`[P§20]`).
- Normalized hybrid scoring (S_dense in [0,1], min-max S_sparse in [0,1], exact 0.65/0.35 fusion).
- 6-domain freshness horizons with volatile-domain expired suppression.
- LinkedIn signal isolation (`PROFESSIONAL_POST` weight <= 0.2, signal-only).
- Passive prompt-injection quarantine via `<knowledge_citation>` with HTML entity encoding (`[P§34]`).
- Single-project workspace containment under `.cv_agent/knowledge/` (`D-013`).
- Crash-safe atomic persistence (`_atomic_write_text`), bidirectional rollback (new + overwritten files),
  collision-free partition identity via SHA-256 model hashing, and immutable vector dimensionality.
- Zero writes to Project Memory (`.cv_agent/memory/`) or experiment ledgers (`experiments.sqlite`).
Test suite stands at 314 passed (29 Phase 2G contract and boundary tests), 0 warnings.
All quality gates (Ruff check, format, Mypy default, CLI, diff check) are green.

## In flight

| Item | Issue | State |
|---|---|---|
| Phase 2 Milestone / Next Subsystem Kickoff | TBD | Awaiting Official PM instruction |

## Next 3 actions

1. Synchronize rolling state and commit D-052 record directly to `main`.
2. Await Official PM direction on Phase 2 next milestone kickoff.
3. Keep main clean, tested, and green.

## Blockers

- None. Phase 2G is formally closed; all gates and boundaries green.

## Scope guard

Knowledge Subsystem does not own active research, reasoning, orchestration state, or physical
fetching tools. Zero writes to project memory or experiment ledgers. Passive context only `[P§34]`.
