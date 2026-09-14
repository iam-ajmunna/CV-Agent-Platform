# Issue #47 — Phase 2G Implementation Report

**Issue:** [#47](https://github.com/iam-ajmunna/CV-Agent-Platform/issues/47)
**Branch:** `feature/ajmunna/phase-2g-knowledge-rag`
**Status:** Implemented; all 301 tests pass (including 16 Phase 2G contract and boundary tests); all quality gates green.

## Overview

This report documents the implementation of Phase 2G — ADR-0006 Knowledge & RAG Subsystem under Issue #47 according to the frozen ADR-0006 specification, the 10 non-negotiable acceptance gates, and system-level cross-subsystem boundaries (`[P§16]`–`[P§18]`, `[P§19]`, `[P§20]`, `[P§29.3]`, `[P§29.7]`, `[P§34]`).

The subsystem establishes a passive, evidence-weighted external computer vision engineering knowledge layer:
1. **Hybrid Indexing Substrate:** Combines pure-Python deterministic Okapi BM25 lexical token matching with dense semantic embeddings generated through the LLM Gateway (`ADR-0002`).
2. **Mathematical Hybrid Fusion & Re-Ranking:** Enforces strictly normalized dense cosine similarity ($S_{\text{dense}} = (S_{\text{cos}} + 1)/2 \in [0, 1]$) and min-max scaled sparse BM25 scores ($S_{\text{sparse}} \in [0, 1]$) with fixed $0.65 S_{\text{dense}} + 0.35 S_{\text{sparse}}$ fusion, scaled by architectural evidence weight and topic freshness decay.
3. **Workspace Containment & Boundary Rigor:** Confines all persistent knowledge data strictly to `.cv_agent/knowledge/` under the project root (`D-013`), enforcing zero writes to Project Memory or experiment ledgers (`ADR-0004`), zero direct LLM provider SDK imports (`ADR-0002`), and quarantine of untrusted external content via passive `<knowledge_citation>` boundaries with zero execution authority (`[P§34]`).

---

## The 10 Non-Negotiable Acceptance Gates

### Gate 1: Mandatory Provenance Verification (`[P§18]`, ADR-0006 §3.4)
- Every stored document and chunk must have complete `Provenance` containing: `source_uri`, `source_class`, `author_or_organization`, `published_date` (ISO-8601 YYYY-MM-DD), and `origin_anchor`.
- Ingestion strictly fails closed, raising `MissingProvenanceError` if any mandatory field is missing, blank, or invalid.

### Gate 2: Local File Anchoring Invariant (ADR-0006 §3.4, §8.2)
- Local workspace files (`file://` or relative/absolute local paths) require a Git commit SHA (7–40 hex chars) or a cryptographic content SHA-256 hash (64 hex chars).
- Anchors relying on file modification time (`mtime` timestamps or explicit `mtime` strings) are strictly rejected with `MissingProvenanceError`.

### Gate 3: Normalized Hybrid Scoring Contract (ADR-0006 §3.6)
- Dense cosine similarity $S_{\text{cos}} \in [-1.0, 1.0]$ is normalized:
  $$S_{\text{dense}} = \frac{S_{\text{cos}} + 1.0}{2.0} \in [0.0, 1.0]$$
- Sparse Okapi BM25 scores are min-max normalized over candidate sets:
  $$S_{\text{sparse}} \in [0.0, 1.0]$$
- Hybrid fusion combines both bounded scores:
  $$S_{\text{hybrid}} = 0.65 S_{\text{dense}} + 0.35 S_{\text{sparse}} \in [0.0, 1.0]$$
- Composite ranking score:
  $$\text{composite\_score} = S_{\text{hybrid}} \times \text{evidence\_weight} \times \text{freshness\_factor}$$

### Gate 4: Topic Freshness Horizons & Degradation Lifecycle (`[P§18]`, ADR-0006 §3.5)
- Categorizes knowledge into 6 canonical topic domains with defined staleness horizons:
  - `FRAMEWORK_API`: 30 days
  - `ECOSYSTEM_RELEASE`: 60 days
  - `HARDWARE_SPEC`: 180 days
  - `BENCHMARK_RESULT`: 180 days
  - `CV_ALGORITHM`: 365 days
  - `CORE_PRINCIPLES`: 1825 days
- Lifecycle states:
  - Active ($\text{age} \le \text{horizon}$): $F = 1.0$, `is_stale = False`, `is_expired = False`.
  - Degraded ($\text{horizon} < \text{age} \le 2 \times \text{horizon}$): linear decay down to 0.1, `is_stale = True`, attaches `WARNING_STALE_CONTEXT`.
  - Expired ($\text{age} > 2 \times \text{horizon}$):
    - Volatile domains (`FRAMEWORK_API`, `ECOSYSTEM_RELEASE`): $F = 0.05$, `is_expired = True`. Chunks are **suppressed from standard retrieval** unless caller explicitly sets `allow_expired = True`.
    - Foundational domains (`CV_ALGORITHM`, `CORE_PRINCIPLES`): $F = 0.20$, `is_expired = True`.

### Gate 5: LinkedIn Signal Isolation (`[P§17]`, ADR-0006 §3.3)
- `PROFESSIONAL_POST` is assigned `evidence_weight` $\le 0.2$ and flagged `signal_only = True`.
- May inspire exploratory experiments; cannot serve as the evidential basis for selecting a model or claiming superiority over a baseline.

### Gate 6: Prompt-Injection Quarantine (`[P§34]`, ADR-0006 §3.9)
- Primary security control is architectural data/instruction separation: retrieved context is encapsulated in passive `<knowledge_citation id="..." source="..." date="..." evidence="...">` tags.
- The Knowledge subsystem possesses zero execution authority; retrieved directives cannot trigger tools, grant approvals, or alter agent state.
- Secondary sanitization: strips null bytes (`\x00`) and terminal escape sequences without heuristically censoring technical source text.

### Gate 7: Single-Project Workspace Containment (`D-013`)
- All persistent indices (BM25 JSON, vector embeddings JSON, document metadata) reside strictly within `<workspace_root>/.cv_agent/knowledge/`.
- Path resolution verifies containment with `is_relative_to(workspace_root)` and raises `WorkspaceContainmentBreachError` on traversal or external escapes.

### Gate 8: LLM Gateway Separation (`[P§20]`, ADR-0006 §3.6)
- Dense vector embeddings delegate exclusively through the `EmbeddingGateway` protocol.
- Vector dimensionality is dynamically determined by the gateway provider rather than hardcoded.
- AST and import audits verify zero direct imports of external LLM provider SDKs (`openai`, `anthropic`, `cohere`, etc.) inside `cv_agent/knowledge/`.

### Gate 9: Memory Subsystem Separation (ADR-0004, `[P§34]`)
- The Knowledge subsystem maintains strict separation from Project Memory (`.cv_agent/memory/`) and the Experiment Ledger (`experiments.sqlite`).
- Zero writes to `.cv_agent/memory/` or `experiments.sqlite`.

### Gate 10: Embedding Model Invariant & Re-Indexing (ADR-0006 §3.6, §8.10)
- The persistent store records the `embedding_model_id` used to generate stored vectors.
- On query, the retriever checks `embedding_model_id` consistency; if the active gateway model differs from the partition model, the query fails closed with `EmbeddingModelMismatchError`, requiring re-indexing.

---

## File and Module Structure

```text
cv_agent/knowledge/
├── __init__.py          # Clean public exports (types, protocols, components, exceptions)
├── interfaces.py        # Frozen typed dataclasses, Enums, NewTypes, Protocols
├── exceptions.py        # Normalized failure taxonomy (ADR-0006 §5)
├── bm25.py              # Pure-Python deterministic Okapi BM25 index & tokenizer
├── store.py             # Workspace-contained persistent hybrid store (.cv_agent/knowledge/)
├── ingester.py          # Document normalization, provenance gate, sanitization, chunking
├── retriever.py         # Normalized hybrid scoring, freshness engine, composite ranking
└── quarantine.py        # Passive <knowledge_citation> data boundary formatting
tests/
└── test_knowledge_rag_boundary.py  # 16 contract & regression tests covering all 10 gates
```

---

## Verification Results

1. **Pytest Suite:**
   - `tests/test_knowledge_rag_boundary.py`: 16 passed in 0.10s.
   - Entire test suite: **301 passed** (0 warnings) across 16 test modules in 3.48s.
2. **Static Analysis & Linters:**
   - `ruff check .`: All checks passed.
   - `ruff format --check . ...`: 119 files already formatted (clean).
   - `mypy cv_agent`: Success: no issues found in 59 source files.
   - `git diff --check`: Clean (zero trailing whitespace).
