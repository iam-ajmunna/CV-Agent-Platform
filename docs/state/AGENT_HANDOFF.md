# AGENT HANDOFF

> Current-session handoff. Read after `STATUS.md` and `DECISIONS.md` when continuity
> context is needed. This file is state, not canon; normative project facts cite `[P§n]`.

## Current state

- **Phase:** 1 — Architecture & ADR Specification
- **Integration baseline:** `main`
- **Work pattern:** `main` ← short-lived work branches
- **Current architecture gate:** ADR-0001 through ADR-0012 accepted.

## ADR status

| ADR | State |
|---|---|
| ADR-0001 — Capability model | **Accepted** |
| ADR-0002 — LLM Gateway | **Accepted** |
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
| ADR-0013 — Monitoring | Not started |

## Agreed decisions

- **D-010:** three-level development flow: short-lived work branches → `dev-munna` → final PR → `main`; `dev-munna` is the PM development/integration trunk and `main` is the Official PM integration/release trunk (superseded by D-027).
- **D-011:** ADR-0001 capability model accepted; four registry identities are semantic/type boundaries, not a requirement for independent storage backends.
- **D-012:** ADR-0002 LLM Gateway accepted; provider isolation, configuration-driven routing, transient fallback taxonomy, fast-fail request/configuration errors, normalized responses/errors, and reproducibility metadata are the Phase 1 gateway contract.
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
- **D-024:** ADR-0006 Knowledge & RAG Subsystem is accepted as the authoritative decision; dual knowledge mechanisms (persistent knowledge vs. live research); Research Engine (Roadmap 05) as active investigator vs. Knowledge (Roadmap 04) as passive evidence substrate; 8-tier evidence weighting hierarchy with LinkedIn as signal-only (weight <= 0.2); mandatory provenance invariant requiring origin anchor (Git SHA, content SHA-256, or ETag); domain freshness horizons with linear decay and expired suppression for volatile domains; normalized hybrid scoring (S_dense and S_sparse normalized to [0,1] before linear fusion S_hybrid = 0.65*S_dense + 0.35*S_sparse); single-project workspace containment at .cv_agent/knowledge/; strict decoupling from Project Memory (ADR-0004) and LLM Gateway (ADR-0002, dynamic vector dimensionality); and prompt-injection quarantine enforcing data/instruction separation without altering evidential text.
- **D-025:** ADR-0007 Skills Architecture & NVIDIA Capability Discovery is accepted as the authoritative decision; declarative SkillDescriptor contracts decoupled from tool execution; resolution hierarchy (CapabilityId -> SkillId -> ToolId) preserving ADR-0001 registry authority; authoritative SkillRegistry (validated/registered skills) decoupled from ephemeral runtime discovery cache; declarative NVIDIA ecosystem adapters (TensorRT, Model Optimizer PTQ/QAT, TAO, DeepStream, DALI, Triton, Jetson, CUDA Agent) per [P§15]; typed 7-state availability machine (KNOWN, DISCOVERED, CONFIGURED, AVAILABLE, EXECUTABLE, UNAVAILABLE, KNOWN_BUT_UNAVAILABLE); known-but-unavailable constraint enforcing zero silent CPU fallback on unconfigured NVIDIA targets (D-022); execution target binding aligned with ADR-0005 (LOCAL, REMOTE, MCP_ENDPOINT); approval execution requirement declaration decoupled from ADR-0003/APPROVALS.md policy authority; and proposed Phase 3 interface contracts (SkillRegistry, SkillResolver, SkillExecutor).
- **D-026:** ADR-0008 Reasoning Subsystem Architecture is accepted as the authoritative decision; cognitive engineering judgment decoupled from orchestration (ADR-0003), execution (ADR-0005), and retrieval (ADR-0006); specialized stateless provider-agnostic reasoning components (ProblemFramingNode, BaselineSelectionNode, ExperimentDiagnosisNode, StrategyFormulationNode); provider-isolated LLM Gateway consumption with zero direct provider SDK imports per [P§20]; passive grounded evidence consumption with dedicated GroundingValidator detecting evidence contradictions and unsupported speculative inferences; capability intent formulation (CapabilityId) strictly decoupled from downstream capability validation and skill mapping (ADR-0007 SkillResolver resolving against ADR-0001); standardized Pydantic v2 schemas with ConfigDict(frozen=True); mandatory DecisionPayload[T] audit envelope containing DecisionAuditRecord; approval recommendation flagging (recommends_approval = True) deferring policy evaluation to ADR-0003/APPROVALS.md; and implementation targeted for Phase 4 (DISCOVER / DEFINE workflow per ROADMAP.md).
- **D-027:** single-trunk development flow: short-lived work branches → PR → `main`; `dev-munna` is permanently retired; the project owner is the Official PM who reviews and approves all merges into `main` (supersedes D-010 per R-003).
- **D-028:** Q10 dataset storage architecture accepted: manifest-first content-addressed storage with pluggable backends (local_fs, s3, gcs, minio); Git-tracked manifests authoritative; raw payloads addressed by content hash externally; DVC/Git LFS not core runtime dependencies; ADR-0009 unblocked.
- **D-029:** ADR-0009 Dataset Subsystem Architecture is accepted as the authoritative decision; manifest-first content-addressed storage with pluggable backends (local_fs, s3, gcs, minio); authoritative Git-tracked manifests in .cv_agent/datasets/ covering 16 mandatory fields from docs/DATA.md; raw media payloads addressed by SHA-256 ContentHash externally; zero core runtime dependency on DVC or Git LFS; independent AnnotationManifest versioning; explicit immutable split memberships with 5-point leakage auditing; lineage DAG; remote worker retrieval without Git cloning; and mutation approval gates per docs/APPROVALS.md.
- **D-030:** ADR-0010 Training Subsystem Architecture is accepted as the authoritative decision: immutable TrainingPlan contract; cognitive reasoning strategy proposal (ADR-0008) vs training materialization/lifecycle boundary; TrainingLauncher dispatch contract delegating transport execution to ADR-0005 (LOCAL, REMOTE, MCP_ENDPOINT); mandatory structured cost estimation and human approval preconditions per docs/APPROVALS.md and [P§24]; explicit 11-state training job lifecycle state machine; real-time TrainingTelemetryRecord streaming; CheckpointManifest with content hashes, storage binding, and retention policies; online failure diagnostics (NaN/Inf, loss divergence, early stopping, CUDA OOM recovery); TrainingEnvironmentRecord reproducibility metadata per [P§25]; and clean decoupling from future Evaluation Subsystem (ADR-0011).
- **D-031:** ADR-0011 Evaluation Subsystem Architecture is accepted as the authoritative decision: co-equal measurement of model quality metrics (MetricResult) and hardware systems performance (BenchmarkResult); immutable bindings to ADR-0009 dataset splits and ADR-0010 model checkpoints; mandatory baseline comparison (BaselineComparisonResult) prohibiting unsubstantiated claims per [P§29.2], [P§29.3]; extensible versioned MetricDefinition and stateless MetricCalculator contracts; hardware-aware benchmarking with warmup/repetitions on target accelerators per [P§13], [P§29.4]; structured diagnostic ErrorAnalysisRecord per [P§27]; EvaluationRunner dispatch delegating physical transport to ADR-0005 (LOCAL, REMOTE, MCP_ENDPOINT); immutable result handoff to ADR-0004 experiment ledger (experiments.sqlite); complete EvaluationEnvironmentRecord per [P§25]; profiling approval checks per docs/APPROVALS.md; and 12 codified architectural invariants.
- **D-032:** ADR-0012 Optimization and Deployment Subsystem Architecture is accepted as the authoritative decision: model optimization pipeline (OptimizationPlan) covering ONNX export, TensorRT compilation, and precision quantization (FP32/FP16/BF16/FP8/INT8 PTQ/QAT) with dynamic shape profiles; immutable calibration bindings (CalibrationDatasetBinding) to ADR-0009 manifests; cryptographic transformation lineage DAG (OptimizedModelArtifact); mandatory post-optimization verification via ADR-0011 against unoptimized baselines (accuracy_retention_ratio, speedup_ratio); runtime-agnostic deployment architecture (DeploymentRuntime: Standalone TensorRT, DeepStream, Triton, ONNX Runtime); hardware-locked target profiles (DeploymentTarget) per [P§13], [P§29.4], D-014, D-022; production deployment human approval gates per docs/APPROVALS.md and [P§24]; automated health/readiness probing (HealthCheckSpec) with zero-downtime rollback; and 12 codified architectural invariants.

## Blocking questions

Q1–Q5, Q7–Q8, Q10 are resolved. ADR-0001 through ADR-0012 are accepted. Downstream implementation work is gated on Phase 1 ADR acceptance.

## Next session

1. Await Official PM instruction establishing scope and requirements for ADR-0013 (Monitoring & Active Learning Subsystem Architecture).
2. Open GitHub Issue and cut short-lived branch for ADR-0013 upon authorization.
