# AGENT HANDOFF

> Current-session handoff. Read after `STATUS.md` and `DECISIONS.md` when continuity
> context is needed. This file is state, not canon; normative project facts cite `[P§n]`.

## Current state

- **Phase:** 1 — Architecture & ADR Specification
- **Integration baseline:** `main`
- **Work pattern:** `main` ← short-lived work branches
- **Current architecture gate:** ADR-0001 through ADR-0013 accepted; ADR-0002 amended and re-frozen. Phase 2 remains paused pending sequential ADR review.

## ADR status

| ADR | State |
|---|---|
| ADR-0001 — Capability model | **Accepted** |
| ADR-0002 — LLM Gateway | **Accepted — Amended/Frozen** |
| ADR-0003 — Orchestration state | **Accepted** |
| ADR-0004 — Project memory / experiment ledger | **Accepted** |
| ADR-0005 — Tools / MCP | **Accepted** |
| ADR-0006 — Knowledge / RAG | **Accepted** |
| ADR-0007 — Skills | **Accepted** |
| ADR-0008 — Reasoning | **Accepted** |
| ADR-0009 — Dataset subsystem | **Accepted** |
| ADR-0010 — Training | **Accepted** |
| ADR-0011 — Evaluation | **Accepted** |
| ADR-0012 — Optimization / deployment | **Accepted** |
| ADR-0013 — Monitoring & Active Learning | **Accepted** |

## Agreed decisions

- **D-010:** three-level development flow: short-lived work branches → `dev-munna` → final PR → `main`; `dev-munna` is the PM development/integration trunk and `main` is the Official PM integration/release trunk (superseded by D-027).
- **D-011:** ADR-0001 capability model accepted; four registry identities are semantic/type boundaries, not a requirement for independent storage backends.
- **D-012:** ADR-0002 LLM Gateway accepted; provider isolation, configuration-driven routing, transient fallback taxonomy, fast-fail request/configuration errors, normalized responses/errors, and reproducibility metadata are the original Phase 1 gateway contract; superseded in detail by D-034.
- **D-013:** Q1 — one CV Engineering Agent project per repository/workspace; project memory and experiment records are isolated to that project, with experiment identifiers scoped to the project.
- **D-014:** Q2 — the agent runs on the local workstation; training is submitted as an external job to a configured local, remote, or cloud GPU target and is not executed in-process.
- **D-015:** Q3 — human approvals are persistent workflow entities. The initial interaction surface may be CLI-based, but approval requests and state survive process restarts and support asynchronous approval.
- **D-016:** Q4 — Phase 1 architecture targets a real CV project; prison/garment examples are reference workloads and validation fixtures, not the architectural scope itself.
- **D-017:** Q7 — initial LLM provider classes are Anthropic, OpenAI, and Local/Ollama; routing is configuration-driven; transient failures may fall back sequentially; request/configuration failures fail fast; metadata is preserved. See ADR-0002.
- **D-018:** Q8 — dual-layer persistence: Git-tracked structured project memory plus local SQLite at `.cv_agent/state/experiments.sqlite` for high-volume experiment rows. See ADR-0004.
- **D-019:** ADR-0003 orchestration state, checkpointing, and persistent approvals proposed; SQLite-backed state persistence, approval interrupts, and asynchronous external job handles defined.
- **D-020:** ADR-0003 Orchestration State Machine, Checkpointing, and Persistent Approvals is accepted; runtime checkpoint persistence at `.cv_agent/state/checkpoints.sqlite` with configurable retention reconciled with D-018 experiment ledger.
- **D-021:** ADR-0004 Project Memory and Experiment Ledger Persistence is accepted; dual-layer persistence (.cv_agent/memory/ and .cv_agent/state/experiments.sqlite), immutable completed results, explicit lifecycle transitions, relational baseline integrity, and workload-scoped metrics defined.
- **D-022:** Q5 — factual NVIDIA capability inventory: verified local host is macOS arm64 with zero installed NVIDIA hardware, drivers, or SDKs; 20 registry capabilities remain documented/planned (D-009); local NVIDIA execution tools are absent; remote execution targets (D-014) and NVIDIA MCP servers are currently unconfigured. NVIDIA capabilities are currently known but unavailable until a compatible execution target is configured; ADR-0005 and ADR-0007 are unblocked.
- **D-023:** ADR-0005 Tools and MCP Execution Boundary is accepted; typed tool descriptor contracts decoupled from transport protocols; execution target abstraction (LOCAL, REMOTE, MCP_ENDPOINT); operational discovery preserving ADR-0001 ToolId registry authority; NVIDIA capabilities report "known but unavailable" without local fallback per D-022; structured argument vector execution without shell interpretation; approval precondition enforcement per docs/APPROVALS.md; and type-safe asynchronous ActiveJobHandoff mechanically aligned with ADR-0003 ActiveJobRef.
- **D-024:** ADR-0006 Knowledge & RAG Subsystem is accepted as the authoritative decision; dual knowledge mechanisms (persistent knowledge vs. live research); Research Engine (Roadmap 05) as active investigator vs. Knowledge (Roadmap 04) as passive evidence substrate; 8-tier evidence weighting hierarchy with LinkedIn as signal-only (weight <= 0.2); mandatory provenance anchor; freshness horizons and expired suppression; normalized hybrid scoring; single-project workspace containment; strict decoupling from Project Memory and LLM Gateway; and prompt-injection quarantine.
- **D-025:** ADR-0007 Skills Architecture & NVIDIA Capability Discovery is accepted; declarative SkillDescriptor contracts; CapabilityId -> SkillId -> ToolId resolution; authoritative SkillRegistry vs. ephemeral discovery cache; NVIDIA ecosystem adapters; typed 7-state availability machine; known-but-unavailable constraint; execution target binding aligned with ADR-0005; approval declaration decoupled from policy authority; and Phase 3 interface contracts.
- **D-026:** ADR-0008 Reasoning Subsystem Architecture is accepted; cognitive judgment decoupled from orchestration/execution/retrieval; stateless provider-agnostic reasoning components; provider-isolated LLM Gateway consumption; grounded evidence validation; capability intent formulation; frozen Pydantic v2 schemas; DecisionPayload[T] audit envelope; approval recommendation flagging; implementation targeted for Phase 4.
- **D-027:** single-trunk development flow: short-lived work branches → PR → `main`; `dev-munna` is permanently retired; the project owner is the Official PM who reviews and approves all merges into `main`.
- **D-028:** Q10 dataset storage architecture accepted: manifest-first content-addressed storage with pluggable backends (local_fs, s3, gcs, minio); Git-tracked manifests authoritative; raw payloads addressed by content hash externally; DVC/Git LFS not core runtime dependencies; ADR-0009 unblocked.
- **D-029:** ADR-0009 Dataset Subsystem Architecture is accepted; manifest-first content-addressed storage, authoritative manifests, SHA-256 payload identity, independent annotation manifests, immutable split memberships, leakage auditing, lineage DAG, remote worker retrieval, and mutation approval gates.
- **D-030:** ADR-0010 Training Subsystem Architecture is accepted; immutable TrainingPlan, reasoning/training boundary, TrainingLauncher delegation to ADR-0005, structured cost estimation and human approval, 11-state lifecycle, telemetry, checkpoint manifests, failure diagnostics, reproducibility metadata, and clean Evaluation boundary.
- **D-031:** ADR-0011 Evaluation Subsystem Architecture is accepted; co-equal model/system measurement, immutable dataset/checkpoint bindings, mandatory baseline comparison, versioned metrics, hardware-aware benchmarking, diagnostic error analysis, ADR-0005 dispatch, immutable experiment handoff, environment record, profiling approvals, and 12 invariants.
- **D-032:** ADR-0012 Optimization and Deployment Subsystem Architecture is accepted; optimization pipeline, immutable calibration bindings, cryptographic artifact lineage, mandatory post-optimization verification, runtime-agnostic deployment, hardware-locked targets, deployment approval, health/readiness probing, rollback, and 12 invariants.
- **D-033:** ADR-0013 Monitoring and Active Learning Subsystem Architecture is accepted; tri-dimensional telemetry, baseline drift detection, active-learning candidate curation, cryptographic provenance, zero autonomous retraining, advisory retraining proposals, ADR-0009 candidate handoff, and 12 invariants.
- **D-034:** ADR-0002 amendment is accepted as authoritative; canonical multimodal content; capability-aware ModelProfile/LLMRequirements; separate routing/retry/fallback; idempotency-safe timeout semantics; normalized failure taxonomy; versioned structured outputs; canonical tool-call representation with ADR-0005 isolation; typed streaming lifecycle; correlation identifiers; versioned cost provenance; data-governance eligibility; reproducibility metadata without false determinism claims; secret isolation; extensible provider adapters; 16 invariants and 15 implementation acceptance criteria.

## Blocking questions

Q1–Q5, Q7–Q8, Q10 are resolved. ADR-0001 through ADR-0013 are accepted, with ADR-0002 amended and re-frozen. Phase 2 implementation remains intentionally gated while the ADR review program continues sequentially.

## Next session

1. Deep-review the next ADR against the complete architecture and future technology evolution.
2. Resolve every material architectural gap before freezing that ADR.
3. Keep runtime implementation paused until the full ADR review sequence is complete and explicitly approved by the Official PM.
