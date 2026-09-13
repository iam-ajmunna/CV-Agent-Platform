# JOURNAL

> **Append-only.** Never edit or delete a past entry — if an entry was wrong, write a new
> entry saying so. One entry per session. This is the project's memory of *how* it got
> here; `STATUS.md` holds only *where* it is.

Entry format:

```
## YYYY-MM-DD — <session title> (<branch or PR>)
**Did:**       what changed, concretely
**Why:**       the reason, citing [P§n] or ADR-XXXX
**Broke:**     what went wrong, including dead ends — record these, they are the
               most valuable lines in this file
**Learned:**   what a future session should know
**Left open:** what was deliberately not done
```

---

## 2026-08-30 — Governance scaffold (docs bootstrap)

**Did:** Froze the project definition as `docs/PROJECT.md` (§1–§35, verbatim). Created
`CLAUDE.md`, `AGENTS.md`, architecture overview with the responsibility table, ADR
template, seed ADR-0001 (capability model), roadmap, rolling state files, and the
approvals / research / evaluation / data contracts.

**Why:** `[P§35]` — the canonical document must be turned into operational repository
files without losing or contradicting any of it. The `[P§__]` citation convention exists
so drift from canon is greppable rather than a matter of opinion.

**Broke:** Nothing yet — no code touched.

**Learned:** The document's own §29 principles and §34 boundary test are strong enough to
serve directly as machine-enforceable rules; they were lifted into `CLAUDE.md` rather
than paraphrased.

**Left open:** Every ADR except the seed. GitHub scaffolding. Answers to Q1–Q5 in
`OPEN_QUESTIONS.md`.

---

## 2026-09-01 — Documentation + capability-state correction pass

**Did:** Full documentation consistency audit, then corrected everything it found.
(1) Retired `spec/00-vision.md`, `01-principles.md`, `02-architecture.md` (fully
duplicated `docs/PROJECT.md` or conflicted with ADR-governed `docs/architecture/
OVERVIEW.md`) and `07-human-approval-and-safety.md`, `09-artifact-and-experiment-
contracts.md` (conflicted with `docs/APPROVALS.md` / `docs/state/EXPERIMENTS.md`;
unique content merged in first). Reduced `spec/03-agent-runtime.md` and `spec/05-
cv-engineering-lifecycle.md` to technical elaborations that cite the canon instead of
restating it, with explicit "not implemented yet" headers.
(2) Rewrote `docs/development/GITHUB_FLOW_V1.md` and `GITHUB_DEVELOPMENT_MODEL.md`:
retired the two-trunk `main`/`dev-munna` model with its two-stage PM approval gate
(neither is how the project actually works); replaced with single trunk `main` +
`feature/<owner>/<work>` branches, PR reviewed and merged by the project owner.
Updated `CLAUDE.md` §6 and `AGENTS.md`'s Workflow section to match.
(3) Added `spec/` and `docs/development/` to README's document map and a pointer
(not a bulk-read requirement) in `CLAUDE.md` §2, since neither tree was discoverable
from any prescribed entry point before this.
(4) Capability registry semantics: found `spec/capability_registry.json` marked
every capability `status: "available"`/`"partial"` (20 entries, after a concurrent
session's additions) with zero executable skill/tool bindings anywhere in
`cv_agent/`, and `CapabilityRegistry.check_item()` hardcoded `available: True`
unconditionally. Introduced `status: "planned"` (declared, no executable binding),
tightened `"available"` to require a verified executable binding, set all 20
capabilities to `"planned"`, and made `check_item()` report `executable: False` with
a reason. `select()`/`Capability.is_available` now require `status == "available"`
strictly (previously counted `"partial"` too). Logged as an interim correction in
ADR-0001 §8a — does not implement the ADR's `Resolution`/`resolve()` design.

**Why:** `[P§35]`, `[P§23]`, ADR-0001 §3 ("known but unavailable" as a first-class
state) — a registry that claims a capability is usable when nothing can invoke it is
a false claim, not a documentation nitpick. The git-workflow and spec-duplication
fixes were explicit user corrections to what this repo's process and canon actually
are, not architectural choices of my own.

**Broke:** Found and fixed two unrelated concurrency-introduced bugs while verifying:
`cv_agent/capabilities/registry.py` and `cv_agent/config/settings.py` imported
`importlib.resources.abc.Traversable`, which doesn't exist before Python 3.12 (this
repo targets 3.10+) — added a fallback to `importlib.abc.Traversable`. Also found
`cv_agent/config/settings.py` referenced an undefined `_REPO_ROOT` name in dead code
(`_default_config_path`/`_default_registry_path`, unused elsewhere) — defined it.
Also found `cv_agent/resources/spec/capability_registry.json` (the packaged runtime
copy) had drifted from `spec/capability_registry.json` (the source of truth) — still
9 stale `"available"` entries after the source had grown to 20 `"planned"` ones, so
the actual running CLI was reporting the old, wrong data. Synced it and added
`tests/test_package_resources.py::test_packaged_registry_matches_source_of_truth`
so this can't silently drift again.

**Learned:** Multiple files in this repo (`spec/00-11`, `docs/development/*`,
`cv_agent/capabilities/registry.py`) were edited concurrently by another session
while this pass was running — several `Read`-then-`Edit` calls failed with
"file modified since read" and had to be re-read. Don't trust a file's content from
earlier in a long session without re-reading it immediately before editing,
especially anything under active multi-session development.

**Left open:** `docs/development/GITHUB_FLOW_V1.md` and `GITHUB_DEVELOPMENT_MODEL.md`
still duplicate each other substantially (by design — one is the procedural
reference, one the executive summary — but they should be watched for drift the same
way the capability registry copies were). Q1–Q5 in `OPEN_QUESTIONS.md` remain
unanswered and still block ADR-0003. No commit or push was made — see git status.

---

## 2026-09-01 — Phase 1 state reconciliation (docs/reconcile-dev-munna-phase1-state)

**Did:** Reconciled the project state onto the agreed `main` → `dev-munna` → short-lived
branch workflow; restored the Phase 1 status, restored ADR-0002 as a draft, recorded the
Q7/Q8 decisions as D-016/D-017, and added the current `AGENT_HANDOFF.md`.

**Why:** The active `dev-munna` state had reverted to Phase 0 wording and still described
the retired single-trunk workflow, while the project is continuing Phase 1 architecture
work. The repository state must describe the actual operating model before more ADR work
proceeds.

**Broke:** The existing `DECISIONS.md` update repeatedly returned a GitHub contents API
409 despite the blob SHA matching the fetched file. No overwrite or force update was made
to that file through the API; its existing content therefore remains unchanged on this
branch until it can be updated safely.

**Learned:** The prior ADR-0002 draft still exists on the historical short-lived branch
`docs/issue-11-adr-0002-llm-gateway` and can be recovered without reconstructing it from
memory.

**Left open:** `DECISIONS.md` still needs D-008 marked superseded and D-015–D-018 appended.
Q1–Q5 remain unanswered and therefore continue to block the dependent ADRs.

---

## 2026-09-01 — ADR-0003 Orchestration State & Checkpointing (docs/issue-adr-0003-orchestration-state)

**Did:** Drafted `docs/architecture/adr/ADR-0003-orchestration-state.md` specifying the
LangGraph orchestration state machine, typed `AgentState` schema, local SQLite checkpointer
backed persistence at `.cv_agent/state/checkpoints.sqlite` (Q1 / D-013, Q8 / D-018),
persistent human approval interrupt/resume mechanics surviving process restarts (Q3 / D-015,
`[P§24]`), and out-of-process asynchronous external job handles (Q2 / D-014). Updated
`STATUS.md`, `AGENT_HANDOFF.md`, and appended `D-017`, `D-018`, `D-019` to `DECISIONS.md`.

**Why:** `[P§21]`, `[P§24]`, `[P§25]`, `[P§34]` — the orchestration layer must own workflow
transitions, persistent checkpoints, approval gates, and error recovery contracts without
leaking domain reasoning, LLM provider specifics, or tool execution into the graph.

**Broke:** Nothing broken. Running tests and static analysis passed.

**Learned:** Scoping checkpoints to local workspace SQLite satisfies both Q1 (per-project
isolation) and Q3 (cross-process restart survival) cleanly without external network services.

**Left open:** ADR-0003 revised with dual-SQLite reconciliation and full acceptance criteria, awaiting PM review/acceptance. ADR-0004 (project memory / experiment ledger) is unblocked for drafting. Q5 remains deferred.

---

## 2026-09-03 — ADR-0005 Tools and MCP Execution Boundary Accepted (dev-munna)

**Did:** Formally accepted `docs/architecture/adr/ADR-0005-tools-and-mcp-boundary.md` (`D-023`). Established the execution boundary: typed `ToolDescriptor` interfaces decoupled from physical transport; execution target abstraction (`LOCAL`, `REMOTE`, `MCP_ENDPOINT`); operational discovery subordinated to ADR-0001's authoritative `ToolId` registry; structured argument vector invocation prohibiting raw shell strings; approval precondition verification per `docs/APPROVALS.md`; and type-safe asynchronous execution handoff via `ActiveJobHandoff` mechanically aligned with ADR-0003's `ActiveJobRef`. Updated `DECISIONS.md`, `STATUS.md`, and `AGENT_HANDOFF.md`.

**Why:** `[P§10]`, `[P§15]`, `[P§22]`, `[P§23]`, `[P§24]`, `[P§29.4]`, `[P§34]` — isolates execution adapters from reasoning, skills, and graph orchestration. Enforces the Apple Silicon / macOS reality (`D-022`) where local execution lacks NVIDIA runtimes, preventing invalid host execution assumptions while providing a uniform interface for future remote GPU targets (`D-014`) and MCP servers.

**Broke:** Nothing broken. Architecture and documentation changes only; no runtime implementation code modified.

**Learned:** Subordinating MCP tool discovery to ADR-0001's authoritative `ToolId` catalogue prevents external tool endpoints from silently mutating or expanding the agent's capability surface without governance.

**Left open:** ADR-0006 (Knowledge / RAG) is the next architectural specification, under the roadmap's Phase 2 — Knowledge & Research sequence. Q10 remains open and continues to block ADR-0009 (Dataset subsystem).

---

## 2026-09-03 — ADR-0006 Knowledge & RAG Architecture Drafting (docs/issue-adr-0006-knowledge-rag)

**Did:** Created task branch `docs/issue-adr-0006-knowledge-rag` from `dev-munna`. Drafted `docs/architecture/adr/ADR-0006-knowledge-and-rag.md` in `Status: Proposed`. Established the Knowledge & RAG boundary: dual knowledge mechanisms (persistent knowledge vs. live research per `[P§16]`); 8-tier evidence weighting hierarchy (`[P§17]`); LinkedIn as signal-only (`[P§17]`); mandatory provenance and un-dated document rejection (`[P§18]`); topic-domain freshness horizons with temporal decay (`[P§18]`); workspace-isolated hybrid dense/lexical indexing under `.cv_agent/knowledge/` (`D-013`); strict separation from Project Memory (`ADR-0004`) and LLM Gateway (`ADR-0002`); and proposed Phase 2 interface contracts (`KnowledgeIngester`, `KnowledgeRetriever`). Updated `STATUS.md` and `AGENT_HANDOFF.md`.

**Why:** `[P§16]`, `[P§17]`, `[P§18]`, `[P§19]`, `[P§29.3]`, `[P§29.7]`, `[P§34]` — the CV Engineering Agent requires current ecosystem knowledge without falling victim to hallucinations, promotional marketing claims, or ungrounded retrieval volume.

**Broke:** Nothing broken. Architecture and specification contracts only; no runtime implementation code modified.

**Learned:** Explicitly decoupling external general CV engineering knowledge (`ADR-0006`) from internal project-specific memory and experimental baselines (`ADR-0004`) prevents knowledge drift and preserves reproducible measurement integrity.

**Left open:** ADR-0006 specification drafted in `Proposed` status, awaiting architectural review. Q10 remains open and continues to block ADR-0009 (Dataset subsystem).

---

## 2026-09-03 — ADR-0006 Knowledge & RAG Subsystem Formally Accepted (dev-munna)

**Did:** Formally accepted `docs/architecture/adr/ADR-0006-knowledge-and-rag.md` as the authoritative decision (`D-024`) following PR #20 and PR #21 merge into `dev-munna`. Settled the Knowledge architecture: dual knowledge mechanisms (persistent knowledge vs. live research); Research Engine (Roadmap 05) as active investigator vs. Knowledge Subsystem (Roadmap 04) as passive evidence substrate; 8-tier evidence hierarchy with LinkedIn as discovery signal only (weight <= 0.2); mandatory provenance invariant requiring canonical URI, origin anchor (Git SHA, content SHA-256, or ETag), author/org, date, and source class; domain freshness horizons with linear decay and volatile domain expiration suppression; normalized hybrid scoring (S_dense and S_sparse normalized to [0, 1] before fusion 0.65 S_dense + 0.35 S_sparse); single-project workspace containment at `.cv_agent/knowledge/` (`D-013`); strict boundary decoupling from Project Memory (`ADR-0004`) and LLM Gateway (`ADR-0002`, dynamic vector dimensionality); and prompt-injection defense establishing the architectural data/instruction boundary with zero execution authority. Recorded `D-024` in `DECISIONS.md`; synchronized `STATUS.md`, `AGENT_HANDOFF.md`, and `JOURNAL.md`.

**Why:** `[P§16]`, `[P§17]`, `[P§18]`, `[P§19]`, `[P§20]`, `[P§29.3]`, `[P§29.7]`, `[P§34]` — establishes settled architectural specification for Phase 2 Knowledge & RAG prior to Phase 3 Skills architecture.

**Broke:** Nothing broken. Governance and state synchronization only; no runtime implementation code modified.

**Learned:** Explicit normalization of dense and lexical scores is required prior to linear fusion to guarantee mathematically sound, deterministic retrieval ranking across disparate metric scales. Enforcing data/instruction boundaries as the primary security control protects agent reasoning without altering or censoring evidential source text.

**Left open:** ADR-0007 (Skills Architecture & NVIDIA Discovery) is the next architectural specification. Q10 remains open and continues to block ADR-0009 (Dataset subsystem).

---

## 2026-09-03 — ADR-0007 Skills Architecture & NVIDIA Discovery Drafting (docs/issue-adr-0007-skills)

**Did:** Created task branch `docs/issue-adr-0007-skills` from `dev-munna` to implement Issue #22. Drafted `docs/architecture/adr/ADR-0007-skills-architecture.md` in `Status: Proposed`. Established the Skills architecture: declarative `SkillDescriptor` contracts decoupled from tool execution; `CapabilityId → SkillId → ToolId` resolution hierarchy preserving ADR-0001 registry authority; typed skill availability state machine (`KNOWN`, `DISCOVERED`, `CONFIGURED`, `AVAILABLE`, `EXECUTABLE`, `UNAVAILABLE`, `KNOWN_BUT_UNAVAILABLE`); declarative NVIDIA ecosystem adapters (TensorRT, Model Optimizer PTQ/QAT, TAO, DeepStream, DALI, Triton, Jetson, CUDA Agent) per `[P§15]`; the known-but-unavailable constraint enforcing zero silent CPU fallback on unconfigured NVIDIA targets (`D-022`); execution target binding aligned with ADR-0005 (`LOCAL`, `REMOTE`, `MCP_ENDPOINT`); discovery authority boundaries ensuring MCP/remote probing does not silently mutate capability identities; and proposed Phase 3 interface contracts (`SkillRegistry`, `SkillResolver`, `SkillExecutor`). Updated `STATUS.md` and `AGENT_HANDOFF.md`.

**Why:** `[P§15]`, `[P§23]`, `[P§29.9]`, `[P§34]` — establishes specialized procedural knowledge linking abstract capabilities to executable tools without hard-coding prompts or reimplementing vendor expertise.

**Broke:** Nothing broken. Architecture and specification contracts only; no runtime implementation code modified.

**Learned:** Explicit target requirement schemas in skill descriptors allow the resolver to detect missing hardware runtimes ahead of invocation, turning silent execution failures or invalid fallback assumptions into deterministic, transparent `KNOWN_BUT_UNAVAILABLE` states.

**Left open:** ADR-0007 specification drafted in `Proposed` status, awaiting architectural review. Q10 remains open and continues to block ADR-0009 (Dataset subsystem).

---

## 2026-09-03 — ADR-0007 Acceptance Synchronization (D-025)

**Did:** Formally marked ADR-0007 (`docs/architecture/adr/ADR-0007-skills-architecture.md`) as `Status: Accepted` on `dev-munna` following review, corrections, and merge of PR #23. Recorded decision `D-025` in `DECISIONS.md`. Synchronized rolling state ledgers (`STATUS.md`, `AGENT_HANDOFF.md`, `JOURNAL.md`). ADR-0008 (Reasoning subsystem) is unblocked as the next Phase 1 architecture milestone. Issue #24 remains open but parked until Phase 3 skills implementation is scheduled.

**Why:** `[P§15]`, `[P§23]`, `[P§34]` — completes the Phase 1 architectural contract for the Skills subsystem, formalizing procedural contracts, the NVIDIA declarative inventory, the 7-state availability machine, and the known-but-unavailable constraint (`D-022`) ahead of reasoning subsystem architecture.

**Broke:** Nothing broken. Governance and state synchronization only; zero runtime implementation code altered.

**Learned:** Explicit separation between authoritative registry entries and ephemeral discovery cache prevents dynamically discovered MCP endpoints or remote targets from polluting core capability identities while preserving flexible runtime extensibility.

**Left open:** ADR-0008 (Reasoning Subsystem) is unblocked and next in sequence. Issue #24 parked. Q10 remains open and continues to block ADR-0009 (Dataset subsystem).

---

## 2026-09-03 — ADR-0008 Reasoning Subsystem Drafting (docs/issue-adr-0008-reasoning)

**Did:** Created short-lived task branch `docs/issue-adr-0008-reasoning` from `dev-munna` to implement Issue #24. Drafted `docs/architecture/adr/ADR-0008-reasoning-subsystem.md` in `Status: Proposed`. Established the Reasoning architecture: stateless provider-agnostic reasoning components (`ProblemFramingNode`, `BaselineSelectionNode`, `ExperimentDiagnosisNode`, `StrategyFormulationNode`); standardized Pydantic v2 decision contracts wrapped in a mandatory `DecisionPayload[T]` envelope with `DecisionAuditRecord`; strict LLM Gateway consumption boundary with zero provider SDK imports per `[P§20]`; read-only quarantined knowledge evidence consumption (`<knowledge_citation>`) verified by a dedicated `GroundingValidator` per `[P§16]`, `[P§17]`, ADR-0006; capability intent formulation requesting `CapabilityId` with authoritative validation and skill resolution delegated to ADR-0007 / ADR-0001; approval recommendation flagging (`recommends_approval = True`) deferred to Orchestration (ADR-0003, `docs/APPROVALS.md`) per `[P§24]`; and proposed Phase 4 interface contracts. Synchronized `STATUS.md` and `AGENT_HANDOFF.md`.

**Why:** `[P§4]`, `[P§5]`, `[P§6]`, `[P§19]`, `[P§20]`, `[P§29.1]`, `[P§29.2]`, `[P§34]` — isolates cognitive engineering judgment from workflow orchestration, tool execution, and knowledge storage while preventing prompt piles and conversational chatbot anti-patterns.

**Broke:** Nothing broken. Architecture and specification contracts only; zero runtime implementation code modified.

**Learned:** Explicitly decoupling capability intent from registry authority allows reasoning nodes to formulate hypotheses and capability needs without becoming a capability authority, preserving single-source registry authority in ADR-0001 and procedural mapping in ADR-0007.

**Left open:** PR into `dev-munna` prepared for CI verification and merge. Q10 remains open and continues to block ADR-0009 (Dataset subsystem).

---

## 2026-09-03 — ADR-0008 Acceptance Synchronization (D-026)

**Did:** Formally marked ADR-0008 (`docs/architecture/adr/ADR-0008-reasoning-subsystem.md`) as `Status: Accepted` on `dev-munna` following review, corrections (Revisions 1–3), and merge of PR #25 (`e4b0527`). Recorded decision `D-026` in `DECISIONS.md`. Synchronized rolling state ledgers (`STATUS.md`, `AGENT_HANDOFF.md`, `JOURNAL.md`). Deleted local and remote short-lived branch `docs/issue-adr-0008-reasoning`. Issue #24 is closed by PR #25.

**Why:** `[P§4]`, `[P§5]`, `[P§6]`, `[P§19]`, `[P§20]`, `[P§29.1]`, `[P§29.2]`, `[P§34]` — completes the architecture specification for the Reasoning Subsystem ahead of dataset and experiment execution phases.

**Broke:** Nothing broken. Governance and state synchronization only; zero runtime implementation code modified.

**Learned:** Mandatory envelope wrapping (`DecisionPayload[T]`) guarantees that every empirical decision emitted by a reasoning component carries cryptographic context references, model metadata, citation URIs, and assumptions without relying on ad-hoc model compliance.

**Left open:** Q10 (dataset storage/versioning) remains open and blocks ADR-0009 (Dataset subsystem).

---

## 2026-09-07 — Governance Realignment: Single-Trunk Main & Retirement of dev-munna (D-027)

**Did:** Realigned the repository development model to a single-trunk workflow (`main` + short-lived work branches) under the sole review and approval authority of the project owner as Official PM (`D-027`, reversing `D-010` via `R-003`). Updated `AGENTS.md`, `CLAUDE.md`, `docs/development/GITHUB_DEVELOPMENT_MODEL.md`, `docs/development/GITHUB_FLOW_V1.md`, `.github/workflows/ci.yml`, `docs/state/DECISIONS.md`, `docs/state/STATUS.md`, and `docs/state/AGENT_HANDOFF.md`. Updated repository target references to `iam-ajmunna/CV-Agent-Platform`. Permanently retired and scheduled deletion of the `dev-munna` integration branch.

**Why:** Simplifies development flow and aligns review authority directly with the Official PM, eliminating redundant two-stage promotion gates while preserving strict branch protection, CI gates, and issue traceability on `main`.

**Broke:** Nothing broken. Documentation, CI triggers, and governance state aligned.

**Learned:** Single-trunk workflow combined with mandatory short-lived branch PRs and automated CI provides faster delivery velocity while maintaining full verification and review integrity.

**Left open:** Q10 (dataset storage/versioning) remains open and blocks ADR-0009 (Dataset subsystem).

---

## 2026-09-08 — Q10 Resolution: Manifest-First Content-Addressed Dataset Storage (D-028)

**Did:** Formally resolved Q10 (`docs/state/OPEN_QUESTIONS.md`) by adopting the manifest-first content-addressed dataset storage architecture with pluggable payload backends (`local_fs`, `s3`, `gcs`, `minio`). Updated `docs/DATA.md` with the authoritative manifest schema, 10 required invariants, and decoupled payload storage. Recorded decision `D-028` in `DECISIONS.md`. Updated `docs/state/STATUS.md` and `docs/state/AGENT_HANDOFF.md`. Unblocked ADR-0009 (Dataset Subsystem Architecture).

**Why:** `[P§7]`, `[P§25]`, `[P§26]`, `[P§29.5]`, `[P§34]` — establishes deterministic dataset reproducibility and independent annotation versioning without imposing heavy external tool dependencies (DVC or Git LFS) on the core runtime, while enabling remote GPU compute targets to retrieve datasets without cloning Git history.

**Broke:** Nothing broken. Architecture and state synchronization only; zero runtime implementation code altered.

**Learned:** Storing authoritative manifests in Git while keeping raw media payloads in content-addressed external storage ensures git diffs remain fast and readable, enables remote compute workers to pull exact slices, and guarantees deterministic reproduction across experiment runs.

**Left open:** ADR-0009 (Dataset Subsystem Architecture) is unblocked and scheduled next.

---

## 2026-09-08 — ADR-0009 Acceptance Synchronization (D-029)

**Did:** Formally marked ADR-0009 (`docs/architecture/adr/ADR-0009-dataset-subsystem.md`) as `Status: Accepted` on `main` following review and merge of PR #3 (`10b693f`). Recorded decision `D-029` in `DECISIONS.md`. Synchronized rolling state ledgers (`STATUS.md`, `AGENT_HANDOFF.md`, `JOURNAL.md`). Deleted and pruned task branch `docs/issue-adr-0009-dataset`. Issue #2 is closed by PR #3.

**Why:** `[P§7]`, `[P§25]`, `[P§26]`, `[P§27]`, `[P§29.5]`, `[P§34]` — establishes the authoritative dataset subsystem architecture (manifest schema, content-addressed storage abstraction, independent annotation versioning, explicit split integrity, automated 5-point leakage auditing, lineage DAG, remote worker retrieval, and mutation approval gates).

**Broke:** Nothing broken. Architecture and governance state synchronization only; zero runtime implementation code altered.

**Learned:** Codifying the 5-point leakage audit (temporal, camera/site, duplicate, identity, annotation-round) into the dataset manifest contract guarantees that optimistic evaluation errors are trapped before training jobs consume compute resources.

**Left open:** Phase 1 architecture sequence for ADR-0001 through ADR-0009 is fully accepted. ADR-0010 (Training Subsystem) is parked awaiting explicit Official PM instruction and gate authorization.

---

## 2026-09-08 — ADR-0010 Acceptance Synchronization (D-030)

**Did:** Formally marked ADR-0010 (`docs/architecture/adr/ADR-0010-training-subsystem.md`) as `Status: Accepted` on `main` following review and merge of PR #5 (`61f9421`). Recorded decision `D-030` in `DECISIONS.md`. Synchronized rolling state ledgers (`STATUS.md`, `AGENT_HANDOFF.md`, `JOURNAL.md`). Pruned task branch `docs/issue-adr-0010-training`. Issue #4 is closed by PR #5.

**Why:** `[P§10]`, `[P§11]`, `[P§13]`, `[P§15]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§29.2]`, `[P§29.4]`, `[P§29.8]`, `[P§34]` — establishes the authoritative training subsystem architecture (immutable `TrainingPlan`, reasoning vs training proposal/materialization boundary, `TrainingLauncher` dispatch delegation to ADR-0005, mandatory cost estimation and human approval preconditions per `docs/APPROVALS.md` and `[P§24]`, 11-state lifecycle state machine, real-time telemetry streaming, checkpoint manifests and retention, online failure diagnostics, and `TrainingEnvironmentRecord` reproducibility metadata per `[P§25]`).

**Broke:** Nothing broken. Architecture and governance state synchronization only; zero runtime implementation code altered.

**Learned:** Decoupling training job semantics from physical transport via `TrainingLauncher` → ADR-0005 maintains strict layer isolation while ensuring remote compute execution remains fully hardware-aware and cost-gated.

**Left open:** Phase 1 architecture sequence for ADR-0001 through ADR-0010 is fully accepted. ADR-0011 (Evaluation Subsystem) is parked awaiting explicit Official PM instruction and gate authorization.

---

## 2026-09-09 — ADR-0011 Acceptance Synchronization (D-031)

**Did:** Formally marked ADR-0011 (`docs/architecture/adr/ADR-0011-evaluation-subsystem.md`) as `Status: Accepted` on `main` following review and merge of PR #7 (`ee1e61a`). Recorded decision `D-031` in `DECISIONS.md`. Synchronized rolling state ledgers (`STATUS.md`, `AGENT_HANDOFF.md`, `JOURNAL.md`). Deleted and pruned task branch `docs/issue-adr-0011-evaluation`. Issue #6 is closed by PR #7.

**Why:** `[P§7]`, `[P§12]`, `[P§13]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§27]`, `[P§29.2]`, `[P§29.3]`, `[P§29.4]`, `[P§29.10]`, `[P§34]` — establishes the authoritative evaluation subsystem architecture (co-equal measurement of model quality and systems performance, immutable dataset split and model checkpoint bindings, mandatory baseline comparison prohibiting unsubstantiated claims, extensible versioned metrics, hardware-aware benchmarking on target accelerators, structured diagnostic error analysis, dispatch delegation to ADR-0005, immutable result handoff to ADR-0004 `experiments.sqlite`, and 12 codified architectural invariants).

**Broke:** Nothing broken. Architecture and governance state synchronization only; zero runtime implementation code altered.

**Learned:** Treating systems performance (latency, throughput, VRAM) as a co-equal evaluation dimension alongside model quality (mAP, F1) prevents edge-deployability blindness, while mandatory baseline comparisons architecturally eliminate unsupported claims of model improvement.

**Left open:** Phase 1 architecture sequence for ADR-0001 through ADR-0011 is fully accepted. ADR-0012 (Optimization & Deployment Subsystem) is parked awaiting explicit Official PM instruction and gate authorization.

---

## 2026-09-09 — ADR-0012 Acceptance Synchronization (D-032)

**Did:** Formally marked ADR-0012 (`docs/architecture/adr/ADR-0012-optimization-deployment-subsystem.md`) as `Status: Accepted` on `main` following review and squash-merge of PR #9 (`02db943`). Recorded decision `D-032` in `DECISIONS.md`. Synchronized rolling state ledgers (`STATUS.md`, `AGENT_HANDOFF.md`, `JOURNAL.md`). Deleted and pruned task branch `docs/issue-adr-0012-optimization-deployment`. Issue #8 is closed by PR #9.

**Why:** `[P§8]`, `[P§9]`, `[P§13]`, `[P§14]`, `[P§15]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§28]`, `[P§29.4]`, `[P§34]` — establishes the authoritative optimization and deployment subsystem architecture (immutable `OptimizationPlan` pipeline covering ONNX export, TensorRT compilation, dynamic shape profiles, and quantization PTQ/QAT; immutable calibration dataset bindings to ADR-0009 manifests; cryptographic transformation lineage DAG; mandatory post-optimization verification via ADR-0011 against unoptimized baselines; runtime-agnostic deployment architecture across Standalone TensorRT, DeepStream, Triton, and ONNX Runtime; hardware-locked target profiles per `[P§13]`, `[P§29.4]`, `D-014`, `D-022`; production deployment human approval gates per `docs/APPROVALS.md` and `[P§24]`; automated health probing with zero-downtime rollback; and 12 codified architectural invariants).

**Broke:** Nothing broken. Architecture and governance state synchronization only; zero runtime implementation code altered.

**Learned:** Coupling quantization calibration to frozen ADR-0009 dataset manifests and requiring mandatory post-optimization evaluation (accuracy retention and latency speedup) via ADR-0011 guarantees that optimized deployment candidates are verifiably correct and never degrade task performance silently.

**Left open:** Phase 1 architecture sequence for ADR-0001 through ADR-0012 is fully accepted. ADR-0013 (Monitoring & Active Learning Subsystem) is parked awaiting explicit Official PM instruction and gate authorization.

---

## 2026-09-09 — ADR-0013 Acceptance Synchronization (D-033)

**Did:** Formally marked ADR-0013 (`docs/architecture/adr/ADR-0013-monitoring-active-learning-subsystem.md`) as `Status: Accepted` on `main` following review and squash-merge of PR #11 (`a723f94`). Recorded decision `D-033` in `DECISIONS.md`. Synchronized rolling state ledgers (`STATUS.md`, `AGENT_HANDOFF.md`, `JOURNAL.md`). Deleted and pruned task branch `docs/issue-adr-0013-monitoring-active-learning`. Issue #10 is closed by PR #11.

**Why:** `[P§8]`, `[P§9]`, `[P§16]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§26]`, `[P§27]`, `[P§28]`, `[P§29.6]`, `[P§29.7]`, `[P§34]` — establishes the authoritative monitoring and active learning subsystem architecture (tri-dimensional operational telemetry streaming across Model, System, and Data dimensions per `[P§28]`; baseline drift detection against immutable ADR-0011 baselines via KS, PSI, Wasserstein, and Chi-Square tests; algorithmic active learning candidate selection across uncertainty, feature diversity, and anomaly heuristics; cryptographic frame provenance; zero autonomous retraining invariant; advisory retraining trigger proposals to ADR-0008 Reasoning requiring mandatory human approval per `docs/APPROVALS.md`; and candidate batch handoffs to ADR-0009 Dataset manifests).

**Broke:** Nothing broken. Architecture and governance state synchronization only; zero runtime implementation code altered.

**Learned:** Enforcing the observe $\rightarrow$ detect $\rightarrow$ sample $\rightarrow$ propose separation guarantees that active learning feeds high-value edge data into the data flywheel without ever creating an unconstrained, unsupervised retraining loop that could cause catastrophic forgetting or unbounded compute burn.

**Left open:** Phase 1 architecture sequence (ADR-0001 through ADR-0013) is fully accepted and complete. Phase 2 (Runtime Substrate & Core Type Implementation) is parked awaiting explicit Official PM instruction and milestone sequencing.

---

## 2026-09-10 — ADR-0001 Freeze Synchronization (D-035, PR #20)

**Did:** Hardened and froze ADR-0001 (`docs/architecture/adr/ADR-0001-capability-model.md`) on `main` following review and squash-merge of PR #20. Recorded decision `D-035` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Specified canonical namespaced identifier format for `CapabilityId`, `SkillId`, `ToolId`, `AgentId` (`[a-z0-9_.-]+(/[a-z0-9_.-]+)*`).
2. Formalized `AvailabilityState` Literal type in §3.5 and §5; clarified strict orthogonality between operational readiness and workflow authorization (`authorized: bool`) per ADR-0003/APPROVALS.md.
3. Coupled tool ID and tool version into `ResolvedToolRef` in `ResolutionSnapshot` §3.10 to eliminate parallel tuple index-drift risks.
4. Defined `Stage` Literal enum per `[P§6]` in §5 Interface.
5. Added snapshot deserialization round-trip reproducibility test to §7 Acceptance Tests.
6. Marked status as `Accepted — Frozen`.

**Why:** `[P§6]`, `[P§15]`, `[P§22]`, `[P§23]`, `[P§32]`, `[P§34]` — enforces strict semantic boundaries and deterministic resolution contracts before runtime implementation.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Learned:** Explicitly decoupling `AvailabilityState` (operational readiness of tool/target binding) from `authorized` (workflow/human policy evaluation) prevents the capability resolver from ever usurping authorization authority.

**Left open:** Phase 1 architecture sequence (ADR-0001 through ADR-0013) is fully accepted and complete. Phase 2 (Runtime Substrate & Core Type Implementation) is parked awaiting explicit Official PM instruction and milestone sequencing.

---

## 2026-09-10 — ADR-0002 Freeze Synchronization (D-036, PR #21)

**Did:** Hardened and froze ADR-0002 (`docs/architecture/adr/ADR-0002-llm-gateway.md`) on `main` following review and squash-merge of PR #21. Recorded decision `D-036` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Typed `allowed_failure_categories` in `FallbackPolicy` as `frozenset[FailureCategory]` in §4.3.
2. Added `__post_init__` validation on `ImageBlock` requiring at least one of `uri` or `data_ref` in §6.
3. Defined `ToolContentBlock = TextBlock | ImageBlock` for `ToolResultBlock.content` in §6, eliminating forward-reference recursion.
4. Specified sensitive key rejection pattern in request metadata in §7 to enforce Invariant 14 at construction time.
5. Codified streaming lifecycle ordering invariant in §10 requiring `UsageEvent` before `StreamDoneEvent`.
6. Updated `CostRecord.amount` to `Decimal` in §12.
7. Clarified gateway model resolution and profile binding before calling provider adapters in §16.
8. Marked status as `Accepted — Frozen`.

**Why:** `[P§20]`, `[P§29.3]`, `[P§29.5]`, `[P§34]` — ensures robust provider isolation, type safety, and error containment in the foundational gateway contract.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Left open:** Phase 1 architecture sequence (ADR-0001 through ADR-0013) is fully accepted and complete. Phase 2 (Runtime Substrate & Core Type Implementation) is parked awaiting explicit Official PM instruction and milestone sequencing.

---

## 2026-09-10 — ADR-0004 Freeze Synchronization (D-037, PR #22)

**Did:** Hardened and froze ADR-0004 (`docs/architecture/adr/ADR-0004-project-memory-and-ledger.md`) on `main` following review and squash-merge of PR #22. Recorded decision `D-037` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Defined explicit YAML schema for `hardware_constraints.yaml` in §2.3 mapping directly to `HardwareConstraints`.
2. Strengthened immutable typing in §5 interfaces using `Mapping` and `tuple` across dataclasses and query return types (`additional_metrics: Mapping[str, float]`, `checksums: Mapping[str, str]`, `hyperparameters: Mapping[str, Any]`, `list_experiments -> tuple[ExperimentRecord, ...]`).
3. Reconciled resolved dependencies in §8 (Q5 resolved via D-022, Q10 resolved via D-028 / ADR-0009 manifest binding).
4. Marked status as `Accepted — Frozen`.

**Why:** `[P§10]`, `[P§12]`, `[P§24]`, `[P§25]`, `[P§26]`, `[P§29.2]`, `[P§29.4]`, `[P§29.5]`, `[P§33]`, `[P§34]` — guarantees reproducible experiment lineage, immutability of terminal results, and structured domain memory contracts before runtime implementation.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Learned:** Explicit YAML schema definitions for qualitative project memory prevent drift when agents distill hardware budgets and operational limits from project conversations into Git-tracked state.

**Left open:** Proceeding to ADR-0005 freeze review.

---

## 2026-09-10 — ADR-0005 Freeze Synchronization (D-038, PR #23)

**Did:** Hardened and froze ADR-0005 (`docs/architecture/adr/ADR-0005-tools-and-mcp-boundary.md`) on `main` following review and squash-merge of PR #23. Recorded decision `D-038` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Updated amendment status to `Accepted — Frozen`.
2. Documented identifier syntax rule in §5 (`TargetId`, `ExecutionId`, `MCPServerId`, `ToolId` conforming to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1).
3. Added `heartbeat_timeout_seconds: int = 3600` to `ActiveJobHandoff` in §5.
4. Enforced `Mapping[str, Any]` across dataclasses (`target_metadata`, `input_schema`, `output_schema`, `metadata`, `parameters`, `structured_output`, `diagnostics`) to guarantee immutability and prevent layer leakage.

**Why:** `[P§10]`, `[P§15]`, `[P§22]`, `[P§23]`, `[P§24]`, `[P§29.4]`, `[P§34]` — establishes strict execution boundaries, immutable data transfer objects, and deterministic asynchronous handoffs.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Learned:** Enforcing immutable mappings across tool descriptors, invocations, and results prevents tool execution adapters from mutating operational state while maintaining transport independence.

**Left open:** Proceeding to ADR-0006 freeze review.

---

## 2026-09-10 — ADR-0006 Freeze Synchronization (D-039, PR #24)

**Did:** Hardened and froze ADR-0006 (`docs/architecture/adr/ADR-0006-knowledge-and-rag.md`) on `main` following review and squash-merge of PR #24. Recorded decision `D-039` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Updated amendment status to `Accepted — Frozen`.
2. Added Configurable Horizons Invariant in §3.5 enabling explicit workspace configuration overrides via `.cv_agent/config/knowledge.yaml`.
3. Added Embedding Model Versioning & Re-Indexing Invariant in §3.6 prohibiting mathematical comparison across disparate embedding models or vector dimensions and mandating partition invalidation/re-indexing on model change.
4. Documented canonical identifier syntax rule in §4 (`DocumentId` and `ChunkId` conforming to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1).
5. Enforced `Mapping[str, Any]` in `KnowledgeChunk.metadata` to guarantee immutability.
6. Added `embedding_model_id: str = ""` to `ScoredCitation`.
7. Added acceptance criterion 10 for embedding model compatibility and re-indexing.

**Why:** `[P§16]`, `[P§17]`, `[P§18]`, `[P§19]`, `[P§20]`, `[P§29.3]`, `[P§29.7]`, `[P§34]` — establishes rigorous knowledge provenance, configurable topic freshness horizons, mathematically valid hybrid vector scoring, and strict quarantine against prompt injection.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Learned:** Tracking embedding model identity directly in index partitions and retrieved citations prevents subtle cosine similarity corruption when underlying embedding models evolve.

**Left open:** Proceeding to ADR-0007 freeze review.

---

## 2026-09-10 — ADR-0007 Freeze Synchronization (D-040, PR #25)

**Did:** Hardened and froze ADR-0007 (`docs/architecture/adr/ADR-0007-skills-architecture.md`) on `main` following review and squash-merge of PR #25. Recorded decision `D-040` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Updated amendment status to `Accepted — Frozen`.
2. Documented canonical identifier syntax rule in §4 (`SkillId` conforming to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1).
3. Added `timeout_seconds: int = 600` and `deprecated_in_favor_of: SkillId | None = None` to `SkillDescriptor` in §3.2 and §4.
4. Enforced `Mapping[str, Any]` across `parameter_schema`, `metadata`, and `SkillExecutor.execute` parameter/return types to guarantee immutability.
5. Added acceptance criterion 10 for bounded execution and deprecation lineage.

**Why:** `[P§15]`, `[P§23]`, `[P§29.9]`, `[P§34]` — establishes typed procedural skill descriptors, declarative NVIDIA adapters without code duplication, strict 7-state availability tracking, and deterministic resolution boundaries.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Learned:** Bounded execution timeouts and explicit deprecation pointers in skill descriptors give orchestration predictable failure semantics and clean migration paths when procedural recipes are superseded.

**Left open:** Proceeding to ADR-0008 freeze review.

---

## 2026-09-10 — ADR-0008 Freeze Synchronization (D-041, PR #26)

**Did:** Hardened and froze ADR-0008 (`docs/architecture/adr/ADR-0008-reasoning-subsystem.md`) on `main` following review and squash-merge of PR #26. Recorded decision `D-041` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Updated amendment status to `Accepted — Frozen`.
2. Documented canonical identifier syntax rule in §4 (`HypothesisId` conforming to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1).
3. Hardened `ReasoningChain` and `DiagnosticEvaluation` schemas with immutable `Mapping[str, Any]` and `tuple` collection fields.
4. Added hypothesis lifecycle states (`ABANDONED`, `INCONCLUSIVE`) to `HypothesisStatus` Literal to guarantee explicit terminal resolution and prevent orphaned hypothesis branches.
5. Added `parent_hypothesis_id: HypothesisId | None = None` to `HypothesisRecord` to track causal lineage of revised hypotheses.
6. Added acceptance criterion 11 for terminal hypothesis resolution and lineage tracking.

**Why:** `[P§4]`, `[P§5]`, `[P§7]`, `[P§12]`, `[P§19]`, `[P§20]`, `[P§21]`, `[P§29.1]`, `[P§29.2]`, `[P§29.3]`, `[P§29.10]`, `[P§34]` — establishes cognitive judgment decoupled from execution, provider-isolated reasoning, grounded evidence validation, and explicit hypothesis lifecycle semantics.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Learned:** Explicit terminal states and parent pointers in hypothesis tracking prevent reasoning engines from getting trapped in circular diagnostic loops or leaving abandoned investigations without an audit trail.

**Left open:** Proceeding to ADR-0009 freeze review.

---

## 2026-09-10 — ADR-0009 Freeze Synchronization (D-042, PR #27)

**Did:** Hardened and froze ADR-0009 (`docs/architecture/adr/ADR-0009-dataset-subsystem.md`) on `main` following review and squash-merge of PR #27. Recorded decision `D-042` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Updated amendment status to `Accepted — Frozen`.
2. Documented canonical identifier syntax rules in §4 for `DatasetId`, `DatasetVersion`, `ContentHash`, `AnnotationVersion`, and `GuidelineRef`.
3. Added `CALIBRATION = "calibration"` to `SplitName` and `calibration_hashes` to `SplitMembership` to support leakage-free INT8 PTQ quantization without compromising test splits per ADR-0012.
4. Documented geometric validity and non-negative class ID invariants for annotation primitives in `AnnotationManifest`.
5. Added Section 6 (`Implementation Acceptance Criteria`) covering manifest round-trips, deterministic retrieval, disjoint partitions, mandatory leakage gate, annotation decoupling, storage protocol independence, and approval gate integration.

**Why:** `[P§7]`, `[P§25]`, `[P§26]`, `[P§27]`, `[P§29.5]`, `[P§34]` — establishes authoritative manifest-first dataset management, cryptographic content-addressed payload identity, decoupled annotation versioning, and rigorous leakage audit guarantees.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Learned:** Codifying a dedicated calibration split directly in dataset split membership prevents downstream optimization pipelines (ADR-0012) from inadvertently sampling frames from evaluation test splits for INT8 PTQ histograms.

**Left open:** Proceeding to ADR-0010 freeze review.

---

## 2026-09-10 — ADR-0010 Freeze Synchronization (D-043, PR #28)

**Did:** Hardened and froze ADR-0010 (`docs/architecture/adr/ADR-0010-training-subsystem.md`) on `main` following review and squash-merge of PR #28. Recorded decision `D-043` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Updated amendment status to `Accepted — Frozen`.
2. Documented canonical identifier syntax rules in §4 for `PlanId`, `TrainingJobId`, `CheckpointId`, and `ApprovalId` conforming to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1.
3. Hardened `losses` in `TrainingPlan` and `devices_info` in `TrainingEnvironmentRecord` to immutable `tuple` types.
4. Bound `TrainingJobRecord` directly to ADR-0005 execution tokens via `execution_id: ExecutionId | None` and `target_id: TargetId | None`, and updated `TrainingLauncher.launch` with optional `target_id`.
5. Added Section 7 (`Implementation Acceptance Criteria`) covering plan immutability, cost estimation preconditions, approval gating, transport delegation, execution token traceability, telemetry normalization, online diagnostics, checkpoint lineage, and reproducibility packaging.

**Why:** `[P§10]`, `[P§11]`, `[P§13]`, `[P§15]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§29.2]`, `[P§29.4]`, `[P§29.8]`, `[P§34]` — establishes typed training plans, mandatory cost estimation guardrails, persistent human approval boundaries, real-time telemetry streaming, and deterministic checkpoint lineage.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Learned:** Explicitly recording ADR-0005 `execution_id` and `target_id` inside `TrainingJobRecord` maintains direct causal auditability from high-level training lifecycle states to low-level physical container and process handles.

**Left open:** Proceeding to ADR-0011 freeze review.

---

## 2026-09-10 — ADR-0011 Freeze Synchronization (D-044, PR #29)

**Did:** Hardened and froze ADR-0011 (`docs/architecture/adr/ADR-0011-evaluation-subsystem.md`) on `main` following review and squash-merge of PR #29. Recorded decision `D-044` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Updated amendment status to `Accepted — Frozen`.
2. Documented canonical identifier syntax rules in §4 for `EvaluationId`, `MetricId`, and `BenchmarkId` conforming to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1.
3. Enforced immutable `tuple` types across collection fields in `MetricResult`, `BenchmarkConfiguration`, `ErrorAnalysisRecord`, `EvaluationPlan`, and `EvaluationResult`.
4. Bound `EvaluationExecution` directly to ADR-0005 execution tokens via `execution_id: ExecutionId | None` and `target_id: TargetId | None`, and updated `EvaluationRunner.execute` with optional `target_id`.
5. Added Section 7 (`Implementation Acceptance Criteria`) covering plan immutability, co-equal quality and systems benchmarking, mandatory baseline comparison, structured error analysis, hardware benchmark guardrails, execution token traceability, and immutable result handoff to ADR-0004.

**Why:** `[P§7]`, `[P§12]`, `[P§13]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§27]`, `[P§29.2]`, `[P§29.3]`, `[P§29.4]`, `[P§29.10]`, `[P§34]` — establishes co-equal model quality and systems evaluation, immutable dataset split and checkpoint bindings, mandatory baseline comparison prohibiting unsubstantiated claims, and structured diagnostic error analysis.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Learned:** Binding evaluation execution directly to ADR-0005 execution tokens guarantees that hardware benchmark measurements (e.g. inference latency, peak VRAM) are verifiably tied to the specific physical execution target and container run.
**Left open:** Proceeding to ADR-0012 freeze review.

---

## 2026-09-10 — ADR-0012 Freeze Synchronization (D-045, PR #30)

**Did:** Hardened and froze ADR-0012 (`docs/architecture/adr/ADR-0012-optimization-deployment-subsystem.md`) on `main` following review and squash-merge of PR #30. Recorded decision `D-045` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Updated amendment status to `Accepted — Frozen`.
2. Documented canonical identifier syntax rules in §4 for `OptimizationPlanId`, `OptimizedArtifactId`, and `DeploymentId` conforming to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1.
3. Enforced immutable `tuple` types for `shape_profiles` in `OptimizationPlan` and `OptimizedModelArtifact`.
4. Bound `CalibrationDatasetBinding` to default `split_name = "calibration"` per ADR-0009 Invariant 13 to prevent test-split leakage.
5. Bound `OptimizedModelArtifact` and `DeploymentRecord` directly to ADR-0005 execution tokens via `execution_id: ExecutionId | None` and `target_id: TargetId | None`, and updated `ModelOptimizer.compile` and `DeploymentManager.stage` with optional `target_id`.
6. Added Section 7 (`Implementation Acceptance Criteria`) covering plan immutability, calibration dataset leakage gate, dynamic shape bounds, transformation lineage DAG, post-optimization verification gate, target-locked compilation, production deployment human approval, automated readiness probe gate, zero-downtime rollback guarantee, execution token traceability, and persistence protocol independence.

**Why:** `[P§8]`, `[P§9]`, `[P§13]`, `[P§14]`, `[P§15]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§28]`, `[P§29.4]`, `[P§34]` — establishes typed optimization plans, cryptographic artifact transformation lineage, mandatory post-optimization evaluation verification prohibiting accuracy drops, hardware-locked compilation, human-approved deployment, and zero-downtime automated rollback.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Learned:** Tracking `execution_id` and `target_id` across model compilation and deployment records guarantees end-to-end auditability from training checkpoint to edge container execution.

**Left open:** Proceeding to ADR-0013 freeze review.

---

## 2026-09-10 — ADR-0013 Freeze Synchronization (D-046, PR #31)

**Did:** Hardened and froze ADR-0013 (`docs/architecture/adr/ADR-0013-monitoring-active-learning-subsystem.md`) on `main` following review and squash-merge of PR #31. Recorded decision `D-046` in `DECISIONS.md`. Updated `STATUS.md` and `AGENT_HANDOFF.md`. Changes made:
1. Updated amendment status to `Accepted — Frozen`.
2. Documented canonical identifier syntax rules in §4 for `DriftAnalysisId`, `CandidateBatchId`, `CandidateSampleId`, `AlertId`, `TriggerProposalId`, and `SnapshotId` conforming to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1.
3. Enforced immutable `tuple` and `Mapping` collection types across all telemetry, drift analysis, candidate curation, and trigger proposal models.
4. Bound `BaselineReferenceBinding` directly to ADR-0011 `evaluation_id` and ADR-0009 `ContentHash` manifests.
5. Bound `ActiveLearningCandidateBatch` to `target_dataset_id: DatasetId`.
6. Verified mandatory `requires_human_approval = True` and added `approval_id: ApprovalId | None` link.
7. Added Section 14 (`Implementation Acceptance Criteria`) covering telemetry immutability, mandatory baseline comparison gate, statistical drift algorithmic verification, zero autonomous retraining invariant, cryptographic frame content hashing, active learning buffer bounds, candidate batch handoff to ADR-0009, structured advisory proposal formulation, persistence protocol independence, and transport agnosticism.

**Why:** `[P§8]`, `[P§9]`, `[P§16]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§26]`, `[P§27]`, `[P§28]`, `[P§29.6]`, `[P§29.7]`, `[P§34]` — establishes tri-dimensional operational telemetry streaming across Model, System, and Data dimensions, baseline drift detection against immutable ADR-0011 baselines via formal statistical tests, algorithmic active learning candidate selection, cryptographic frame provenance, zero autonomous retraining invariant, advisory retraining proposals to ADR-0008, and candidate batch handoffs to ADR-0009.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean.

**Learned:** Enforcing `target_dataset_id: DatasetId` on candidate batches and `evaluation_id: EvaluationId` on baseline bindings connects the online monitoring loop back to the offline dataset and evaluation ledgers without creating cyclic subsystem dependencies.

**Left open:** Phase 1 architectural freeze program complete. All 13 ADRs (ADR-0001 through ADR-0013) are frozen on `main`. Proceeding to final system-level dependency audit before Phase 2 milestone scheduling.

---

## 2026-09-10 — Cross-ADR Architectural Hardening Pass (D-047, PR #32)

**Did:** Executed the second validation pass and final cross-ADR hardening across all 13 ADRs following deep review. Merged PR #32 to `main` with passing CI on Python 3.10–3.13. Recorded decision `D-047` in `DECISIONS.md`. Updated `STATUS.md`, `AGENT_HANDOFF.md`, and review artifacts. Remediations applied:
1. **ADR-0002 (§3.2):** Replaced forward-reference string with typed `frozenset[FailureCategory]` Literal for strict type-checking of fallback policies.
2. **ADR-0003 (§3.1, §5):** Enforced Invariant #17 defining `EVALUATING` as terminal-adjacent (`COMPLETED`, `FAILED`, `PAUSED` only), forbidding illegal loops back to `EXECUTING`. Updated `Last Reviewed: 2026-09-10`.
3. **ADR-0004 (§3.1):** Formalized `{operation_id}:{request_id}` dereference format for `approval_ref` and added Invariant #5 mandating `PRAGMA foreign_keys = ON` on all SQLite connections.
4. **ADR-0005 (§3.2, §4):** Added `expires_at: str | None` to `ActiveJobHandoff` and mandated remote credential resolution via `TargetId` secrets vault.
5. **ADR-0009 (§6):** Elevated annotation bounding box and polygon validity checks into formal Acceptance Criterion #8 (`Annotation Geometry Validation Gate`). Added `Last Reviewed: 2026-09-10`.
6. **ADR-0010 (§4):** Bound `CheckpointManifest.weights_uri` to valid URI schemes (`file://`, `s3://`, `gs://`, `minio://`) via regex validation. Added `Last Reviewed: 2026-09-10`.
7. **ADR-0011 (§3.12, §4):** Reconciled `baseline_experiment_id` optionality (restricted `None` solely to root baselines) and mandated rejection of unresolved `"default_evaluator"` compute target. Added `Last Reviewed: 2026-09-10`.
8. **ADR-0012 (§3.7, §4):** Bound `OptimizationPlan` to `verification_dataset_binding: EvaluationDatasetBinding` and formalized first-deployment rollback failure escalation to `OPERATOR_REQUIRED`. Added `Last Reviewed: 2026-09-10`.
9. **ADR-0013 (§4):** Expanded `BaselineReferenceBinding` to multi-percentile metrics (`fps_p50`, `fps_p05`, `latency_p95`, `latency_p99`), decoupled drift thresholds (`statistic_threshold` vs `p_value_threshold`), and added `expires_at` to `CandidateSample`. Added `Last Reviewed: 2026-09-10`.

**Why:** `[P§6]`, `[P§7]`, `[P§8]`, `[P§9]`, `[P§10]`, `[P§11]`, `[P§12]`, `[P§13]`, `[P§14]`, `[P§15]`, `[P§16]`, `[P§20]`, `[P§21]`, `[P§22]`, `[P§24]`, `[P§25]`, `[P§26]`, `[P§27]`, `[P§28]`, `[P§29.1]`–`[P§29.10]`, `[P§34]` — resolves all 15 identified architectural contract defects, cross-ADR contradictions, data-integrity risks, and unrecoverable states, completing Phase 1 architecture with zero remaining critical or important defects.

**Broke:** Nothing broken. 99 pytest tests pass, ruff clean, mypy clean across matrix Python 3.10, 3.11, 3.12, 3.13.

**Learned:** Rigorous second-pass triaging against strict architectural criteria (contract defects vs implementation details) prevents specification bloat while ensuring rock-solid boundary interfaces before runtime substrate coding begins.

**Left open:** Phase 1 architecture is 100% frozen, hardened, and verified. Ready for Phase 2 issue hierarchy and milestone scheduling under Official PM approval.

---

## 2026-09-11 — Issue #35 Phase 2A Domain Contracts

**Did:** Implemented the scoped ADR-0001 domain substrate on
`feature/ajmunna/phase-2a-domain-contracts`: validated semantic identifiers,
immutable capability/skill/tool/agent bindings, availability and provenance value objects,
deterministic execution-free resolution, and UTC immutable resolution-snapshot
serialization. Added 11 focused contract tests, including deterministic ordering,
known-but-unavailable NVIDIA resolution, and snapshot round-trips.

**Why:** `[P§15]`, `[P§23]`, `[P§34]` — preserves the distinct capability, skill, tool,
and agent identities; reports unavailable NVIDIA capability honestly; and keeps capability
resolution separate from discovery, execution, approvals, and persistence.

**Broke:** Nothing. Ruff, format, Mypy, and the full pytest suite pass (110 tests;
one pre-existing `importlib.abc.Traversable` deprecation warning).

**Learned:** Accepting compatibility, policy-eligibility, and authorization as external
facts lets ADR-0001 record reproducible resolution outcomes without duplicating the
ADR-0003 approval authority or ADR-0005/ADR-0007 execution and discovery boundaries.

**Left open:** PR review for Issue #35; all Phase 2B and later subsystems remain out of scope.

---

## 2026-09-11 — PR #36 Python 3.10 Compatibility Correction

**Did:** Replaced Python 3.11-only `StrEnum`, `typing.Self`, and `datetime.UTC` in
the Issue #35 domain contracts with Python 3.10-compatible `str`/`Enum`,
`typing_extensions.Self`, and `datetime.timezone.utc` equivalents.

**Why:** The PR's Python 3.10 Mypy job correctly rejected the initial implementation;
the package supports Python 3.10 and the domain substrate must preserve that compatibility.

**Broke:** Initial PR #36 Python 3.10 CI failed in Mypy. The domain package now passes
Mypy with `--python-version 3.10`, and local Ruff, default Mypy, and 110 pytest tests pass.

**Left open:** Push the correction and await the CI matrix rerun before review.

---

## 2026-09-11 — Issue #35 Review Report

**Did:** Added `docs/reports/ISSUE-35-PHASE-2A-REPORT.md`, recording the implementation,
scope boundaries, modified files, tests, commits, and validation status for Issue #35 / PR #36.

**Broke:** Nothing. PR #36's Python 3.10–3.13 CI matrix and GitGuardian checks pass.

**Left open:** Official PM review and merge of PR #36.

---

## 2026-09-11 — PR #36 Deterministic Tie-Breaker Resolution Correction

**Did:** Implemented canonical candidate fact extraction (`canonical_candidate_facts`), a deterministic
canonical JSON tie-breaker (`candidate_tie_breaker`), and integrated it into `DeterministicCapabilityResolver.resolve()`
as the final sorting key. Added regression tests verifying order-independent resolution and deterministic
rejected-candidate ordering when priority, skill_id, and skill_version are identical.

**Why:** Addresses PR #36 blocking review finding, ADR-0001 §3.6, and Determinism Invariant #7: Python's
stable sort previously preserved registration/input order for candidates with identical priority,
skill_id, and skill_version.

**Broke:** Nothing. All 112 pytest tests, Ruff lint/format checks, and Mypy (including Python 3.10 target) pass.

**Learned:** Candidate facts must be canonicalized into a total-ordered JSON string (`canonical_json`) to
avoid Python comparison `TypeError` when optional candidate fields (e.g. `agent_id`, `target_reference`) contain `None`.

**Left open:** Push correction to PR #36 and await full CI matrix run.

---

## 2026-09-12 — Phase 2B ADR-0001 Capability Registry Implementation (feature/ajmunna/phase-2b-capability-registry)

**Did:** Implemented authoritative typed `CapabilityRegistry` subsystem over `CapabilityDefinition`,
`SkillBinding`, `ToolBinding`, and `AgentBinding` domain models. Enforced load-time validation,
explicit post-load immutability via `MappingProxyType` and frozen tuples, typed query APIs, and
deterministic capability resolution emitting immutable `ResolutionSnapshot` records. Retained
read-only legacy projections (`list`, `describe`, `check`, `select`, `list_items`, `describe_item`,
`capability_count`, `version`) ensuring backward compatibility for CLI health check (`python -m cv_agent`)
and existing tests without synthesizing execution readiness. Replaced deprecated `Traversable` imports
with versioned `importlib.resources.abc` on Python 3.11+. Added comprehensive contract test suite in
`tests/test_capability_registry_phase2b.py`.

**Why:** Addresses Issue #37, completing the operational capability registry layer of ADR-0001
on top of the Phase 2A typed domain substrate while preserving strict execution, discovery, and
authorization boundaries `[P§23]`, `[P§34]`.

**Broke:** Nothing. All 128 pytest tests pass with 0 warnings, Ruff lint/format passes, and Mypy
passes cleanly on both default and Python 3.10 targets.

**Learned:** Explicitly isolating legacy dictionary-based catalog models as read-only compatibility
projections prevents them from competing with or degrading the authoritative ADR-0001 domain models.

**Left open:** Open PR for Issue #37 and verify complete CI matrix.

---

## 2026-09-13 — PR #38 Official PM Review Corrections (feature/ajmunna/phase-2b-capability-registry)

**Did:** Addressed all three blocking review findings on PR #38:
1. Unified validation and publication pipeline: refactored `from_definitions()` and `_load_from_dict()` to both route through `_publish_validated_state(...)`. Enforced duplicate capability ID rejection, reference integrity across the binding hierarchy (skill bindings referencing registered capabilities, tool bindings referencing registered skills, agent bindings referencing registered tools), and duplicate binding pair rejection with structured `DomainValidationError`s.
2. Enforced post-load immutability and single-publication semantics: added public `is_loaded` property and raised `DomainValidationError` when `load()` or `_publish_validated_state()` is called on an already loaded/published registry. Guarded `CVAgent.health_check()` to check `is_loaded`. Added regression tests verifying second load attempts are rejected without state mutation.
3. Explicit `Traversable` migration documentation: documented in code comments and implementation report that `importlib.resources.abc.Traversable` is used on Python 3.11+ to eliminate Python 3.12+ deprecation warnings, while `importlib.abc.Traversable` is conditionally retained on Python 3.10 where `importlib.resources.abc` does not exist in standard library.
Added 9 new regression tests covering all findings, expanding test suite to 137 tests.

**Why:** Addresses PR #38 blocking review findings, ensuring full invariant enforcement, post-load immutability, and zero regression across Python 3.10–3.13 without architectural changes `[P§23]`, `[P§34]`.

**Broke:** Nothing. All 137 pytest tests pass (0 warnings), Ruff lint/format pass, Mypy passes on both default and Python 3.10 targets, and CLI health check passes.

**Learned:** Having a single `_publish_validated_state(...)` pipeline guarantees memory-constructed registries have identical semantic and invariant guarantees to resource-loaded registries.

**Left open:** Push review corrections to PR #38, verify CI matrix, and request Official PM re-review.

---

## 2026-09-13 — Phase 2C ADR-0002 LLM Gateway Implementation (feature/ajmunna/phase-2c-llm-gateway)

**Did:** Implemented the authoritative ADR-0002 `LLMGateway` Subsystem under Issue #39 according to the frozen ADR-0002 contract and the Official PM's 10 review constraints:
1. Canonical contracts in `cv_agent/llm/contracts.py`: `TextBlock`, `ImageBlock`, `ToolContentBlock`, `ToolResultBlock`, `ContentBlock`, `Message`, `LLMRequirements`, `ModelProfile`, `ModelSelector`, `RoutingPolicy`, `RetryPolicy`, `FallbackPolicy`, `StructuredOutputSpec`, `ToolCall`, `CostRecord`, `LLMRequest`, `LLMResponse`, `LLMStreamEvent`. Defensively wrapped mappings in `MappingProxyType` to enforce post-init immutability. Enforced Invariant 14 secret isolation rejecting sensitive metadata keys.
2. Normalized error hierarchy in `cv_agent/llm/errors.py`: standardized `FailureCategory` and frozen error classes. Nuanced authentication fallback support.
3. Provider protocol in `cv_agent/llm/provider.py`: `LLMProvider` protocol (`complete`, `stream`).
4. Gateway in `cv_agent/llm/gateway.py`: pre-execution capability and governance filtering (configured vs eligible candidates; 0 provider calls on mismatch); single bounded execution loop with unique `attempt_id` and stable `request_id`; strict timeout idempotency safety (no retry/fallback on timeout for non-idempotent requests); fail-closed policy denial; two-layer structured output schema validation; streaming terminal lifecycle invariant (`UsageEvent` precedes `StreamDoneEvent` only if reported; `StreamErrorEvent` is terminal).
5. Mock adapter in `cv_agent/llm/mock.py`: `MockProviderAdapter` with multimodal, structured output, tool calling, and scriptable failure/recovery sequences. Preserved legacy `FakeLLMProvider` compatibility.
6. Triple-layer provider isolation audit: AST imports, repository-wide text audit, and public export type boundary audit. Zero external provider SDK dependencies.
Added 21 contract tests covering all 15 acceptance criteria in `tests/test_llm_gateway.py`. Total test suite expanded to 158 tests.

**Why:** Addresses Issue #39, implementing the foundational model-inference substrate of ADR-0002 without crossing into tool execution (ADR-0005) or workflow orchestration (ADR-0003) `[P§20]`, `[P§34]`.

**Broke:** Nothing. All 158 pytest tests pass (0 warnings), Ruff lint/format pass, Mypy passes on both default and Python 3.10 targets, CLI health check passes.

**Learned:** Distinguishing static candidate eligibility from configured routing candidates before provider invocation guarantees that invalid hardware, multimodal, or token requirements fail fast without incurring latency or provider retry overhead.

**Left open:** Open PR for Issue #39, verify full CI matrix, and request Official PM review.

---

## 2026-09-13 — PR #40 Official PM Review Corrections (feature/ajmunna/phase-2c-llm-gateway)

**Did:** Addressed all six blocking review findings on PR #40:
1. Faithful timeout/idempotency semantics: eliminated uncontracted `metadata["idempotent"]` logic; introduced `is_operation_repeatable(request)` gateway decision mechanism; codified that timeout means execution state is potentially unknown; non-repeatable operations receive zero automatic retry and zero automatic fallback on timeout; repeatable operations retry only when provider has explicit `supports_idempotency_keys = True`; timeout fallback across distinct providers is prohibited.
2. Default fail-fast authentication: clarified and narrowed implementation and report to default fail-fast authentication (`AuthenticationError.retryable = False`, `fallback_eligible = False`), failing fast without automatic retry or fallback.
3. Canonical model profile resolution: model profiles are indexed strictly by `f"{provider_id}:{model_id}"`; eliminated ambiguous fallback lookup by bare `model_id`. Added regression test verifying distinct profiles for shared model identifiers across providers (`provider-a:model-x` vs `provider-b:model-x`).
4. Zero model profile manufacturing: removed synthetic capability synthesis in `create_gateway()`; unconfigured models raise `UnsupportedModelError`. Statically rejected unconfigured routing candidates with zero provider calls.
5. Deterministic JSON Schema validator: implemented pure-Python standard library validator in `cv_agent/llm/schema.py` enforcing types, properties, required keys, additionalProperties, items, enums, and constraints. Added negative test rejecting malformed types (`{"bbox": "not-an-array", "label": 123}`).
6. Deep defensive immutability: implemented recursive `deep_freeze()` converting nested mappings to `MappingProxyType`, lists to tuples, and sets to frozensets. Added regression test verifying nested schema immutability and `TypeError` on attempted mutation.
Added 9 new regression tests covering all findings, expanding test suite to 167 tests (0 warnings).

**Why:** Addresses PR #40 blocking review findings, ensuring full fidelity to frozen ADR-0002 contracts, robust idempotency and timeout safety, complete schema enforcement, and deep immutability `[P§20]`, `[P§34]`.

**Broke:** Nothing. All 167 pytest tests pass (0 warnings), Ruff lint/format pass, Mypy passes on both default and Python 3.10 targets, CLI health check passes.

**Learned:** Pure-Python recursive data freezing and deterministic JSON schema validation satisfy strict contract guarantees without bloating core dependencies with heavy third-party packages.

**Left open:** Push review corrections to PR #40, verify GitHub Actions CI matrix, and request Official PM re-review.

---

## 2026-09-13 — PR #40 PM Re-Review Corrections (feature/ajmunna/phase-2c-llm-gateway)

**Did:** Resolved all three blocking findings from the PM re-review on PR #40:
1. Removed `tools` field from `LLMRequest` to adhere strictly to frozen ADR-0002 §8 verbatim. Reworked `LLMGateway.is_operation_repeatable()` to operate only on legitimate Phase 2C contracts, establishing a fail-closed safe default of non-repeatable (`False`) unless an explicit gateway `repeatability_evaluator` callback determines repeatability.
2. Canonical model profile duplicate rejection: updated `LLMGateway.__init__`, `register_model_profile()`, and `create_gateway()` to deterministically reject duplicate canonical identities (`provider_id:model_id`) with `ValueError` instead of silently overwriting existing profiles.
3. Enforced streaming lifecycle invariants: `LLMGateway.stream()` validates event ordering, buffering `StreamDoneEvent` to ensure `UsageEvent` always precedes `StreamDoneEvent` when reported, rejecting out-of-order `Done → Usage` with `LLMError(category="invalid_request")`, and suppressing any `StreamDoneEvent` following a terminal `StreamErrorEvent`.
Added 6 new regression tests covering all three re-review findings, expanding the test suite to 173 passing tests (0 warnings). Updated PR #40 description to reflect default fail-fast authentication and frozen contracts.

**Why:** Addresses PM re-review requirements, ensuring strict fidelity to frozen ADR-0002 contracts, deterministic configuration registration, and robust stream event sequence validation `[P§20]`, `[P§34]`.

**Broke:** Nothing. All 173 pytest tests pass, Ruff lint/format pass, Mypy passes on default and Python 3.10 targets, CLI health check passes.

**Learned:** Buffering terminal completion events in streaming generators provides deterministic boundary validation for malformed provider event sequences without discarding or reordering legitimate streaming chunks.

**Left open:** Verify GitHub Actions CI matrix on PR #40 and merge upon Official PM authorization.

---

## 2026-09-13 — Phase 2C ADR-0002 Integration & Merge to main (main)

**Did:** Official PM approved and squash-merged PR #40 into `main` at commit `9c3a6e6c5ea9077ca57dbd635bff2be1d68432b0`. Fast-forwarded local `main` to `origin/main` and deleted the short-lived feature branch `feature/ajmunna/phase-2c-llm-gateway` both locally and on remote. Verified full quality gates on `main`: 173 pytest tests pass (0 warnings), Ruff lint/format pass, Mypy passes on default and Python 3.10 targets, CLI health check passes, and `git diff --check` is clean.

**Why:** Completes Phase 2C milestone under Issue #39 and PR #40, integrating the authoritative ADR-0002 LLM Gateway Subsystem into the trunk `[P§20]`, `[P§34]`.

**Broke:** Nothing. Trunk `main` is completely green.

**Learned:** Following the strict dependency order (Phase 2A Domain Contracts → Phase 2B Capability Registry → Phase 2C LLM Gateway) provides a clean, typed foundation for subsequent Phase 2D (ADR-0005 Tools & MCP Protocol) without premature coupling to orchestration.

**Left open:** Phase 2D (ADR-0005 Tool & MCP Protocol Subsystem).


