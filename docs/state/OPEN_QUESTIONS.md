# OPEN QUESTIONS

> Questions the canon does not answer that must be answered before certain work can
> proceed. **Answered questions are struck through, not deleted** — the answer and its
> date stay, because "why is it like this" is asked more often than "what is it."
>
> An agent that hits an unanswered question adds it here and stops; it does not invent
> the answer `[P§35]`.

## Blocking — work cannot proceed until answered

~~**Q1. What is the unit of a "project"?** `[P§25]`, `[P§33]` — Does the agent handle one
CV project per repository/workspace, or many projects with isolated memory? This determines
the shape of project memory and whether experiment IDs are globally or project-scoped.~~
**Answered 2026-09-01:** one CV Engineering Agent project per repository/workspace;
project memory and experiment records are isolated to that project, with experiment
identifiers scoped to the project.

~~**Q2. Where does the agent run, and where does training run?** `[P§10]`, `[P§13]`,
`[P§24]` — Local workstation, remote GPU box, cloud, or all three? Does the agent submit
jobs or execute them in-process?~~ **Answered 2026-09-01:** the agent runs on the local
workstation. Training is submitted as an external job to a configured execution target
(local, remote, or cloud GPU) and is not executed in-process by the agent.

~~**Q3. What is the human-approval transport?** `[P§24]` — CLI prompt only, or must
approvals survive process restart (a queued request answered hours later)? The latter
makes approvals a persisted entity, not an interrupt.~~ **Answered 2026-09-01:** human
approvals are persistent workflow entities. The initial interaction surface may be CLI-based,
but approval requests and state survive process restarts and support asynchronous approval.

~~**Q4. Is the first target a real project or a reference project?** `[P§30]` — Building
against the prison/garment examples as a real deliverable versus as a test fixture changes
Phase-1 scope substantially.~~ **Answered 2026-09-01:** Phase 1 architecture targets a
real CV project. The prison/garment examples are reference workloads and validation
fixtures, not the architectural scope itself.

~~**Q5. Which NVIDIA capabilities are actually installed and invocable today?** `[P§15]` —
The design says "discover and invoke, do not duplicate." Discovery mechanism depends on
whether these are MCP servers, CLI tools, Python SDKs, or agent skills.
*Blocks: ADR-0005, ADR-0007.*~~
**Answered 2026-09-02:** Factual audit confirms: (1) Verified local capabilities: host is macOS arm64 with general development tools only; zero local NVIDIA GPUs, drivers, CUDA toolkits, or NVIDIA Python runtimes exist. (2) Documented/planned capabilities: 20 capabilities reference NVIDIA tools in the registry, all currently `status: "planned"` with no executable bindings wired (D-009). (3) Absent capabilities: all local NVIDIA execution tools (`nvidia-smi`, `nvcc`, `trtexec`, `tegrastats`, TensorRT, Triton) are absent on the host. (4) Remote execution targets: per D-014, GPU workloads require external execution targets, but no remote GPU, cluster, or NVIDIA MCP server is currently configured. NVIDIA capabilities are currently known but unavailable until a compatible execution target is configured; ADR-0005 and ADR-0007 are unblocked. See D-022.

## Soon — needed within one or two phases

~~**Q7.** Which LLM providers are actually available with keys, and what is the routing
policy per task class? `[P§20]`~~ — **Answered 2026-09-01:** initial gateway provider
classes are Anthropic, OpenAI, and Local/Ollama; routing is configuration-driven;
transient rate-limit/server/timeout failures may fall back sequentially; authentication,
invalid-request, context-length, and unsupported-model failures fail fast. See D-016 /
ADR-0002.

~~**Q8.** What is the persistence backend for project memory and the experiment ledger —
files in-repo, SQLite, or a service? Reproducibility `[P§29.5]` favors in-repo; scale
favors otherwise.~~ — **Answered 2026-09-01:** dual-layer persistence: Git-tracked
structured project memory plus local SQLite at `.cv_agent/state/experiments.sqlite`
for high-volume experiment rows. See D-017 / ADR-0004.

**Q6.** What are the default cost thresholds for approval gates (GPU-hours, $, dataset
mutation scope)? `docs/APPROVALS.md` has placeholders. `[P§24]`

**Q9.** LinkedIn as a research source `[P§17]` — what is the actual access mechanism, and
what are the terms-of-service constraints? The requirement is clear; the mechanism is
not.

~~**Q10.** Dataset storage and versioning: DVC, Git LFS, or external object store? `[P§26]`~~ — **Answered 2026-09-08:** Manifest-first content-addressed dataset storage with pluggable payload backends (`local_fs`, `s3`, `gcs`, `minio`). Git-tracked manifests provide authoritative schema, identity, splits, lineage, and checksums; raw media payloads are stored externally by content hash. DVC and Git LFS are not core runtime dependencies (DVC adapter deferred). See D-028 / `docs/DATA.md` / ADR-0009.

## Deferrable

**Q11.** Multi-camera / multi-stream orchestration model. `[P§9]`
**Q12.** Monitoring backend and alerting surface. `[P§28]`
**Q13.** Does the agent ever fine-tune or serve its own models, or only orchestrate?
**Q14.** Multi-user / team usage, or single-operator? Affects memory and approvals.

## Answered

~~**Q0.** Should the canonical document be edited into the repository docs, or kept
verbatim?~~ — **Answered 2026-08-30:** kept verbatim and frozen as `docs/PROJECT.md`;
derived files cite it as `[P§n]`. See D-001.
