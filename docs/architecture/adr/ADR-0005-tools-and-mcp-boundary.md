# ADR-0005: Tools and MCP Execution Boundary

- **Status:** Accepted
- **Amendment Status:** Frozen
- **Date:** 2026-09-03
- **Last Reviewed:** 2026-09-10
- **Layer:** execution / tools
- **Canon:** `[P§10]`, `[P§15]`, `[P§22]`, `[P§23]`, `[P§24]`, `[P§29.4]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #14

## 1. Context

In the CV Engineering Agent architecture, the system must interact with external binaries, scripts, profiling tools, and remote hardware infrastructure `[P§22]`. However, allowing the reasoning layer (ADR-0008) or orchestration graph (ADR-0003) to execute ad-hoc shell commands or directly manage host processes creates severe architectural failure modes:
1. **Layer Leakage (`[P§34]`):** If reasoning prompts or graph nodes directly construct command strings or import platform SDKs, execution semantics leak into decision-making and graph state, compromising testability, sandboxing, and reproducibility `[P§25]`.
2. **Hardware & Platform Separation (`[P§15]`, `[P§29.4]`, `D-022`):** The primary agent workstation is macOS Darwin `arm64` (Apple Silicon), which possesses zero local NVIDIA GPUs, drivers, CUDA runtimes, or TensorRT libraries (`D-022`). Assuming tools can be executed locally as host processes breaks NVIDIA ecosystem integration (`[P§15]`) and conflicts with `D-014`, which establishes that GPU training and heavy compute execute on external targets out-of-process.
3. **The Four-Identity Invariant (`[P§23]`, ADR-0001):** Collapsing capabilities, skills, and tools into a flat dictionary of functions makes `[P§15]` unenforceable because there is no mechanism to represent that a capability is known and selected, but its execution requires external tools and remote targets.
4. **Standardized Protocol Boundary (`[P§22]`):** The Model Context Protocol (MCP) is designated to provide a standardized boundary between the agent and external capabilities (filesystem, profiling, NVIDIA tools, research, training infrastructure). The architecture requires a clean contract that accommodates local execution adapters, remote targets, and MCP-hosted tools without conflating transport protocols with logical tool semantics.

Phase 1 requires establishing the architectural boundary, interface contracts, discovery model, and security constraints for tools and MCP integration before Phase 2 implementation begins.

## 2. Responsibility (`[P§34]`)

- **This owns:**
  - The architectural definition and typed contract for executable tools (`ToolDescriptor`).
  - The abstraction across execution environments (`ExecutionTarget`).
  - Transport adapters mediating between the agent and execution endpoints (local process, remote compute target, and MCP invocation endpoint connections).
  - Dynamic tool availability probing and validation against target environments.
  - Parameter schema validation and output normalization (`ToolResult`).
  - Tool-level execution safety, workspace path containment verification, and enforcement of approval preconditions established by orchestration (`docs/APPROVALS.md`, ADR-0003).
- **This does NOT own:**
  - The typed catalogue of system capabilities or capability-to-tool resolution → Capability Registry (ADR-0001) `[P§23]`;
  - Authoritative creation of logical `ToolId` identities → Capability Registry (ADR-0001);
  - Model inference, provider routing, or structured prompt completion → LLM Gateway (ADR-0002) `[P§20]`;
  - Workflow sequencing, graph state, checkpointing, and approval interrupt lifecycle → Orchestration State Machine (ADR-0003) `[P§21]`, `[P§24]`;
  - Permanent experiment records, metrics ledger, or Git-tracked domain memory → Project Memory & Experiment Ledger (ADR-0004) `[P§25]`, `[P§33]`;
  - Procedural instructions, domain recipes, and prompt templates → Skills (ADR-0007) `[P§15]`, `[P§23]`.
- **Why this responsibility does not belong elsewhere:**
  Reasoning decides *what* needs to be done; Skills define *how* to apply domain knowledge; Orchestration coordinates *when* actions occur. None of these layers should know socket protocols, SSH mechanics, JSON-RPC envelopes, exit codes, or process signals. The Tool/MCP layer isolates executable interfaces behind a stable, transport-independent contract.

## 3. Decision

We establish an explicit **Tool and MCP Execution Boundary** centered on logical tool contracts, pluggable execution target abstractions, transport adapters, and normalized invocation results.

### 3.1. Architectural Conceptual Model

```text
                  Capability Registry (ADR-0001)
                         │
                         │ authoritative ToolId / logical tool identity
                         ▼
                  ┌───────────────┐
                  │     Tool      │
                  │   Contract    │
                  └───────┬───────┘
                          │
                   target compatibility
                          │
                          ▼
                  ┌───────────────┐
                  │ Execution     │
                  │    Target     │
                  └───────┬───────┘
                          │
                       transport
                          │
             ┌────────────┼────────────┐
             ▼            ▼            ▼
          Local       Remote       MCP Endpoint
          Adapter     Adapter        Adapter
             │            │            │
             ▼            ▼            ▼
          Process      Remote       MCP-hosted
          /CLI        Compute          Tool
                      Target           │
                         │             ▼
                         │     (runtime candidate)
                         │     must resolve to an
                         │     authoritative ToolId
                         │
                         │ (asynchronous dispatch)
                         ▼
                  ActiveJobHandoff
                         │
                         ▼
                  ADR-0003 Orchestrator
                  (ActiveJobRef in state)


Skills ───────────────► select/declare required Tools
Orchestration ────────► controls workflow and approval lifecycle
LLM Gateway ──────────► performs model inference
Memory/Ledger ────────► owns persistent project/experiment records
```

### 3.2. Architectural Terminology

To preserve conceptual clarity across layers (`[P§23]`, `[P§34]`):
- **`Capability` (`[P§23]`, ADR-0001):** What the system can accomplish (e.g., `model.optimize.quantization`). Owned by the Capability Registry.
- **`Skill` (`[P§23]`, `[P§15]`, ADR-0007):** Procedural know-how and instructions detailing how to fulfill a capability using tools.
- **`Tool` (`[P§22]`, ADR-0005):** An executable interface with a typed input schema, output schema, and operational constraints.
- **`ToolId` (ADR-0001, ADR-0005):** Distinct typed identifier for a tool (e.g., `trt-build`, `nvidia-smi`, `git`). The Capability Registry (ADR-0001) is the sole authoritative source of truth for `ToolId`.
- **`ExecutionTarget` (`D-014`, `D-022`, ADR-0005):** A declared compute or invocation environment where a tool executes (classified into `LOCAL`, `REMOTE`, or `MCP_ENDPOINT`).
- **`Transport` (ADR-0005):** The communication channel or protocol adapter used to dispatch an invocation to an execution target (e.g., host subprocess, SSH, MCP protocol transport).
- **`MCP Server` (`[P§22]`, ADR-0005):** An external invocation endpoint that advertises and executes tools/resources according to the Model Context Protocol boundary.
- **`MCP-Hosted Tool` (ADR-0005):** A tool whose execution endpoint is mediated by an MCP server.

### 3.3. Decoupling Tool Identity from Transport

A logical tool (`ToolDescriptor`) is defined by:
- A stable, typed identity (`ToolId`) defined by ADR-0001;
- Descriptive metadata (name, description, purpose);
- An input parameter contract (`input_schema`);
- A structured output contract (`output_schema`);
- Operational constraints (risk level, approval requirement, default timeout, idempotency);
- Compatible target classes (`compatible_target_types`).

**Transport Decoupling Invariant:**
A tool is **not** bound to a single transport protocol. The logical tool describes *what* interface it exposes and *which* target classes it supports. Transport resolution is handled separately: the Tool Layer matches the tool to an available `ExecutionTarget`, and the target specifies the appropriate `Transport` adapter (e.g., local subprocess, SSH client, or MCP client). The logical identity of a tool remains invariant even if its transport changes.

### 3.4. Execution Target Abstraction (`D-014`, `D-022`)

Tools execute against an explicit `ExecutionTarget`:
1. **`LOCAL` (Local Workstation Target):** The developer machine hosting the agent process. Restricted to local development tasks (e.g., git version control, local workspace file validation, Python package inspection).
2. **`REMOTE` (Remote Compute Target — `D-014`):** An external machine (e.g., Linux workstation, Jetson board, or cloud GPU instance) where hardware-dependent workloads (training, TensorRT compilation, hardware profiling) run out-of-process.
3. **`MCP_ENDPOINT` (MCP Invocation Endpoint — `[P§22]`):** An external invocation endpoint exposing tools over an MCP protocol transport. An MCP endpoint is an invocation boundary, not an ambient compute environment; it handles execution only to the extent exposed by the backing MCP server.

**Target Constraint Invariant (`D-014`, `D-022`):**
Because the local workstation lacks NVIDIA hardware and runtimes (`D-022`), tools requiring NVIDIA drivers, CUDA, or TensorRT must declare compatibility exclusively with compatible external targets (`REMOTE` or `MCP_ENDPOINT`). They must never default to host-local execution.

### 3.5. Layered Discovery & "Known but Unavailable" Semantics (`D-009`, `D-022`)

Tool discovery maintains a strict distinction between architectural declaration and operational availability, preserving ADR-0001 as the sole source of truth for tool identities:
1. **Capability Registry (Architectural Declaration — ADR-0001):** Authoritatively defines which `ToolId` tokens exist conceptually and which capabilities/skills reference them. Registration does **not** imply that a tool is installed, configured, or executable (`D-009`). Operational discovery does not invent or declare new authoritative tool identities.
2. **Tool Discovery (Operational Discovery — ADR-0005):** Discovers runtime descriptors and target bindings for authoritative `ToolId` tokens.
3. **Target Verification (Target Availability — ADR-0005):** Probes configured execution targets and active MCP endpoints to determine whether an invocation environment is reachable and equipped with required runtimes.
4. **Tool Availability:** Evaluates whether an authoritatively declared tool can actually be invoked at the present time:
   - **`available = True`:** The tool is registered, its required target is configured and verified reachable, and operational prerequisites are satisfied.
   - **`available = False` ("Known but Unavailable" — `D-022`):** The tool is declared, but its required target is unconfigured, unreachable, or missing required runtime software. The resolver reports a structured reason (e.g., `"Target 'remote_gpu' unconfigured; NVIDIA tool 'trt-build' cannot execute on local macOS workstation"`) rather than raising a system crash or unhandled exception.

### 3.6. MCP Boundary & Registry Authority (`[P§22]`, ADR-0001)

MCP integration is defined as an external invocation boundary, not an authoritative catalogue or compute environment:
- **Server Identity:** Each MCP connection is identified by a unique `MCPServerId`.
- **Discovery vs. Authority Boundary:**
  MCP tools may be discovered dynamically by querying the MCP endpoint. Discovery produces runtime metadata and candidate descriptors; **it does not create authoritative project tool identities**. An MCP-discovered tool becomes invocable through the agent only when it resolves to an authoritative `ToolId` defined by ADR-0001 and satisfies the ADR-0005 contract and target/policy checks. Unresolved MCP tools remain discovered but unavailable to the agent, preventing external endpoints from silently extending or mutating the project's capability/tool catalogue.
- **Trust Boundary:** MCP endpoints are external, untrusted processes. All outputs from MCP tools must be validated against declared schemas before being accepted into agent state.
- **Current State:** Zero MCP servers currently exist in the repository. MCP servers are an architectural integration mechanism, not assumed existing infrastructure.

### 3.7. Security and Trust Model

The Tool Layer enforces execution safety before dispatching any invocation. Security requirements are categorized across three governance tiers:

#### A. Existing Project Constraints
- Actions subject to an approval requirement (training runs, cloud GPU allocation, destructive dataset mutations, writing outside the workspace) require explicit human approval (`docs/APPROVALS.md`, `[P§24]`).
- The agent runs locally on the workstation; GPU training executes externally out-of-process (`D-014`).
- Single-project workspace containment: execution state and operations are isolated to the repository workspace root (`D-013`).

#### B. New ADR-0005 Architectural Decisions
- **Prohibition of Raw Shell Interpretation:** Local tool invocation disallows raw shell string execution. Local command execution MUST use structured argument vectors (`argv: list[str]`) and MUST NOT rely on shell interpretation. This eliminates command and shell injection vulnerabilities at the architectural boundary.
- **Workspace Path Containment:** Local file path parameters must be validated to reside within the workspace directory before execution. Attempts to reference paths outside the workspace without explicit authorization are blocked as policy violations.
- **Secret Isolation:** Authentication tokens, SSH keys, and credentials must not be passed as plain command-line parameters or recorded in invocation logs.
- **Approval Precondition Enforcement:** When invoking a tool subject to an approval requirement, the Tool Layer verifies that the orchestration layer has authorized the action. If no valid authorization is present, the invocation is refused.

#### C. Phase 2+ Implementation Hardening (Deferred)
- Concrete subprocess execution APIs, process group management, and signal forwarding mechanisms.
- Specific sandboxing mechanisms (OS containers, user namespace isolation, cgroups).
- Transport-level encryption policies, credential keychain integration, and SSH client library selection.
- Host-level network firewalling and egress filtering.

### 3.8. Orchestration & Asynchronous Execution Boundary (ADR-0003, `docs/APPROVALS.md`)

ADR-0005 interfaces with the orchestration, approval, and asynchronous execution model established by ADR-0003:
- **Orchestration Authority:** Orchestration (ADR-0003) owns workflow sequencing, state persistence (`checkpoints.sqlite`), and approval interrupt lifecycles (`ApprovalRequest` / `ApprovalResponse`).
- **Approval Enforcement Boundary:**
  ```text
  Orchestration / Approval Subsystem (ADR-0003)
          │
          │ authoritative approval authorization
          ▼
  Tool Execution Policy (ADR-0005)
          │
          ├── permitted (dispatch to target)
          └── denied (PERMISSION_DENIED)
  ```
  The Tool Layer does **not** create a competing approval system or invent synthetic approval tokens. It receives the orchestration execution context and checks whether the required approval state is authorized per `docs/APPROVALS.md`. `ToolDescriptor.requires_approval` declares that invocation is subject to an approval precondition; it does not define or replace the authoritative approval policy or approval state owned by the orchestration/approval subsystem. Invocations lacking required authorization fail fast with `PERMISSION_DENIED`.
- **Synchronous vs. Asynchronous Invariant:**
  - A synchronous invocation produces a terminal `ToolResult` directly to the calling graph node (e.g., inspection tools, dataset file checks, local git operations), where `ToolResult.status` represents the terminal outcome of execution.
  - An accepted asynchronous invocation (e.g., multi-hour training, hyperparameter sweeps, complex video profiling) produces a `ToolResult` containing an `ActiveJobHandoff` reference (`active_job_ref`). For asynchronous invocations, `ToolResult.status == SUCCESS` denotes that the tool invocation/dispatch itself succeeded (the job was accepted by the target), not that the underlying job has finished. `active_job_ref.status` represents the underlying job's initial lifecycle state (`submitted` or `running`).
  - **`ActiveJobHandoff`** mirrors the externally visible fields of ADR-0003's `ActiveJobRef` (`job_id`, `job_type`, `target`, `submitted_at`, `status`, `metadata`) for the asynchronous handoff boundary. ADR-0003 converts and records it as the authoritative `ActiveJobRef` in `AgentState` and owns all subsequent lifecycle monitoring, state transitions, restart recovery, and checkpointing. ADR-0005 does not own or duplicate asynchronous job state machines.

### 3.9. Result Normalization and Memory Handoff (ADR-0004)

Tool execution results are normalized to eliminate CLI-specific bias:
- **`ToolResult` Structure:** Every execution returns a typed result containing execution identity, tool identity, target identity, execution/dispatch status (`status`), structured output, diagnostics dictionary (holding optional stdout, stderr, or protocol logs), generated artifact references, execution metadata (duration, timestamps), and an optional asynchronous `active_job_ref` (`ActiveJobHandoff`).
- **Handoff to Experiment Ledger (ADR-0004):**
  - Ephemeral diagnostics remain in execution logs.
  - When tool execution produces quantitative experimental facts (accuracy metrics, latency, VRAM consumption, model weights), these facts are formatted into structured dictionaries and committed to `experiments.sqlite` via ADR-0004. ADR-0005 does not own experiment persistence.

## 4. Proposed Interface Contracts (Phase 2 Implementation Target)

*(Note: These interfaces represent the proposed type contracts for Phase 2 implementation. They do not exist in the repository today).*

```python
# module: cv_agent.tools.types (Proposed — Phase 2 implementation target)

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping, NewType, Protocol, runtime_checkable

from cv_agent.capabilities import ToolId

TargetId = NewType("TargetId", str)
ExecutionId = NewType("ExecutionId", str)
MCPServerId = NewType("MCPServerId", str)

# Identifier syntax rule: TargetId, ExecutionId, MCPServerId, and ToolId MUST be
# non-empty ASCII strings conforming to [a-z0-9_.-]+(/[a-z0-9_.-]+)* per ADR-0001 §3.1.


class TargetType(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"
    MCP_ENDPOINT = "mcp_endpoint"


class ToolRiskLevel(str, Enum):
    """Operational risk and resource intensity classification."""

    READ_ONLY = "read_only"  # Non-mutating inspection, query, or local check
    RESOURCE_INTENSIVE = "resource_intensive"  # Heavy GPU/CPU compute, profiling
    DESTRUCTIVE = (
        "destructive"  # Data mutations, deletions, system configuration changes
    )


class ToolExecutionStatus(str, Enum):
    """Unified status vocabulary representing tool resolution, validation, and execution/dispatch outcomes."""

    SUCCESS = "success"
    TOOL_NOT_FOUND = "tool_not_found"
    TARGET_UNAVAILABLE = "target_unavailable"
    TOOL_UNAVAILABLE = "tool_unavailable"
    INVALID_PARAMETERS = "invalid_parameters"
    PERMISSION_DENIED = "permission_denied"
    POLICY_VIOLATION = "policy_violation"
    EXECUTION_TIMEOUT = "execution_timeout"
    EXECUTION_CANCELLED = "execution_cancelled"
    EXECUTION_FAILED = "execution_failed"
    TRANSPORT_ERROR = "transport_error"


@dataclass(frozen=True)
class ExecutionTargetDescriptor:
    """Represents a declared compute or invocation environment."""

    target_id: TargetId
    target_type: TargetType
    is_available: bool
    description: str = ""
    unavailable_reason: str | None = None
    target_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDescriptor:
    """Logical architectural specification of an executable tool."""

    tool_id: ToolId  # Authoritative identity defined in ADR-0001 Capability Registry
    name: str
    description: str
    compatible_target_types: tuple[TargetType, ...]
    input_schema: Mapping[str, Any]  # JSON Schema for parameters
    output_schema: Mapping[str, Any]  # JSON Schema for structured output
    risk_level: ToolRiskLevel
    requires_approval: bool  # Declares an approval precondition; does not replace authoritative orchestration policy
    default_timeout_seconds: int = 300
    is_idempotent: bool = False


@dataclass(frozen=True)
class ActiveJobHandoff:
    """Transport-neutral handoff contract mirroring ADR-0003's ActiveJobRef.

    ADR-0003 converts and records this as the authoritative ActiveJobRef in AgentState
    and owns all subsequent lifecycle monitoring, restart recovery, and checkpointing.
    """

    job_id: str
    job_type: Literal["training", "profiling", "evaluation", "export"]
    target: Literal["local_gpu", "remote_gpu", "cloud"]
    submitted_at: str
    status: Literal["submitted", "running", "completed", "failed"]
    heartbeat_timeout_seconds: int = 3600
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolInvocation:
    """A concrete request to invoke a tool on a target."""

    tool_id: ToolId
    parameters: Mapping[str, Any]
    target_id: TargetId
    execution_id: ExecutionId
    timeout_seconds: int
    approval_authorized: bool = False  # Verified against ADR-0003 approval state
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """Normalized, transport-independent output from a tool invocation."""

    tool_id: ToolId
    execution_id: ExecutionId
    target_id: TargetId
    status: ToolExecutionStatus  # Terminal outcome for sync tools; dispatch outcome for async tools
    structured_output: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)  # stdout, stderr, logs
    artifacts: tuple[str, ...] = ()
    duration_ms: int = 0
    error_message: str | None = None
    active_job_ref: ActiveJobHandoff | None = (
        None  # Handoff to ADR-0003 ActiveJobRef when invocation is asynchronous
    )


@dataclass(frozen=True)
class ToolAvailabilityInfo:
    """Structured availability evaluation returned by tool probing."""

    is_available: bool
    status: ToolExecutionStatus
    reason: str | None = None
    target_reachable: bool = False
    runtime_verified: bool = False


@runtime_checkable
class ToolExecutor(Protocol):
    """Protocol for executing tool invocations across transport adapters."""

    async def execute(self, invocation: ToolInvocation) -> ToolResult: ...

    async def probe(
        self, tool: ToolDescriptor, target: ExecutionTargetDescriptor
    ) -> ToolAvailabilityInfo:
        """Evaluate operational availability against a target."""
        ...


@runtime_checkable
class ToolRegistry(Protocol):
    """Runtime operational registry/view for authoritative executable tool descriptors and their availability.

    Acts as an operational cache and resolver; does NOT create or redefine authoritative
    ToolId identities, which remain strictly owned by ADR-0001.
    """

    def register(self, tool: ToolDescriptor) -> None:
        """Bind an operational descriptor to an existing authoritative ADR-0001 ToolId."""
        ...

    def get(self, tool_id: ToolId) -> ToolDescriptor | None: ...

    def list_tools(
        self, *, target_type: TargetType | None = None
    ) -> tuple[ToolDescriptor, ...]: ...

    def check_availability(
        self, tool_id: ToolId, target_id: TargetId | None = None
    ) -> ToolAvailabilityInfo: ...
```

## 5. Normalized Failure Taxonomy

The Tool Layer normalizes failures into distinct architectural categories, cleanly separating resolution/availability failures from validation, policy enforcement, and post-invocation execution failures. The taxonomy maps 1:1 to the `ToolExecutionStatus` vocabulary:

### A. Resolution & Availability Failures (Pre-Execution / Discovery)
1. **`TOOL_NOT_FOUND`:** Requested `ToolId` is not registered in the Tool Layer catalogue.
2. **`TARGET_UNAVAILABLE`:** The required `ExecutionTarget` is unconfigured, unreachable, or offline.
3. **`TOOL_UNAVAILABLE`:** The tool is registered and the target exists, but required runtime binaries or environments are missing on the target.

### B. Invocation & Policy Failures (Pre-Execution / Validation)
4. **`INVALID_PARAMETERS`:** The invocation parameter payload violates the tool's declared `input_schema`.
5. **`PERMISSION_DENIED`:** A tool subject to an approval requirement (`requires_approval=True`) was invoked without verified approval authorization from orchestration.
6. **`POLICY_VIOLATION`:** An execution policy constraint was breached (e.g., attempt to access paths outside the workspace directory without authorization).

### C. Execution Failures (Post-Launch)
7. **`EXECUTION_TIMEOUT`:** Execution exceeded the configured `timeout_seconds`.
8. **`EXECUTION_CANCELLED`:** Execution was aborted by orchestrator cancellation or process signal.
9. **`EXECUTION_FAILED`:** The tool process or endpoint completed with an error (details captured in `diagnostics` and `error_message`).
10. **`TRANSPORT_ERROR`:** Physical transport disruption (e.g., SSH connection dropped, MCP socket disconnected) during execution.

## 6. Alternatives Considered

| Alternative | Evidence For | Evidence Against | Why Not Chosen |
|---|---|---|---|
| **Direct shell execution from LangGraph nodes** | Simple; zero abstraction; fast to prototype. | Leaks bash strings into graph state; no schema validation; impossible to run on remote GPU targets safely. | Violates `[P§34]` layer boundaries, `[P§22]`, and `D-014`. |
| **Direct tool execution from LLM Gateway** | Standard LangChain/OpenAI function calling. | Couples LLM provider to local execution; model decides execution without orchestration approval. | Violates `[P§20]` (ADR-0002) and `[P§24]` (ADR-0003). |
| **Local-only tool execution assumption** | Simplifies process management. | Completely breaks on macOS arm64 workstations lacking NVIDIA hardware (`D-022`). | Incompatible with `[P§15]`, `D-014`, and `D-022`. |
| **MCP-only execution architecture** | Single protocol for all operations. | Overcomplicates simple local workspace operations (e.g., `git status` or local path checks); forces lightweight tools into heavyweight client-server lifecycles. | Adds unnecessary overhead for trivial local tasks. MCP is used where appropriate (`[P§22]`). |
| **Coupling Tool identity to transport** | Simpler descriptor model. | The same logical tool (e.g., `nvidia-smi`) requires separate tool identities depending on whether it runs via SSH or MCP. | Violates tool identity stability; bloats capability registry. |
| **Dynamic MCP tool registration without ADR-0001 resolution** | Discovered MCP tools immediately available to agent. | Creates two competing sources of truth for tool identities; allows external servers to silently mutate project capabilities without governance. | Violates `[P§23]`, `[P§34]`, and ADR-0001 registry authority. |

## 7. Consequences

### Positive
- **Hardware Portability (`D-014`, `D-022`):** Enables the agent to run reliably on non-NVIDIA developer workstations while targeting external GPU clusters, Jetson devices, or cloud VMs.
- **Honest Capability Reporting (`[P§15]`, ADR-0001):** Unconfigured tools cleanly report `available=False` ("known but unavailable") without crashes.
- **Single Source of Truth:** Tool identities remain authoritatively defined by the Capability Registry (ADR-0001); MCP discovery cannot bypass architectural governance.
- **Protocol Neutrality:** The logical tool contract remains stable whether invoked locally, over SSH, or via an MCP client.
- **Security & Sandboxing:** Prevents shell injection, enforces workspace containment, and protects against unauthorized expensive operations.
- **Standardized MCP Integration (`[P§22]`):** Provides a clear path to integrate external MCP servers for DeepStream, TensorRT, or research tools as they become available.

### Negative / Costs
- **Abstraction Overhead:** Requires explicit schemas, descriptors, and transport adapters rather than raw process calls.
- **Configuration Management:** Requires configuring execution targets (SSH keys, endpoints, or MCP server configs) for remote tool operations.

## 8. Acceptance Criteria

1. **Typed Identity Integrity:** Tools are identified by type-safe `ToolId` tokens matching ADR-0001. Passing a `SkillId` or `CapabilityId` where a `ToolId` is expected fails static typing.
2. **Registry Authority Preserved:** Operational discovery (including MCP) does not create authoritative `ToolId` identities. Only tools corresponding to an authoritative ADR-0001 `ToolId` can be invoked.
3. **Explicit Target Binding:** Every tool execution targets an explicit `TargetId`. Host-local execution is rejected for tools requiring NVIDIA or CUDA environments.
4. **Representation of Unconfigured NVIDIA Tools:** Probing an NVIDIA tool on an unconfigured target evaluates to `available=False` with a structured reason string; it does **not** raise an unhandled exception or crash the process.
5. **Approval Enforcement:** Invoking a tool subject to an approval requirement (`requires_approval=True`) without verified orchestration approval authorization fails fast with `PERMISSION_DENIED` and prevents execution.
6. **Prohibition of Shell Interpretation:** Local command execution MUST use structured argument vectors and MUST NOT rely on shell interpretation.
7. **Provider Isolation:** LLM Gateway classes and imports (`cv_agent.llm`) remain completely isolated from Tool Layer adapters.
8. **Transport-Independent Results:** Tool outputs adhere to the structured `ToolResult` specification, separating structured output and artifacts from transport-specific diagnostics (`stderr`, protocol logs).
9. **Asynchronous Execution Handoff:** An accepted asynchronous tool invocation returns a `ToolResult` carrying an `ActiveJobHandoff` contract whose fields mechanically mirror ADR-0003's `ActiveJobRef`, where `ToolResult.status == SUCCESS` denotes successful dispatch rather than completion of the underlying job, leaving job persistence, lifecycle transitions, restart recovery, and observation strictly to orchestration.

## 9. References

- `[P§10]` Autonomous training requirements
- `[P§15]` NVIDIA ecosystem integration — discover and invoke, do not duplicate
- `[P§20]` LLM Gateway & provider isolation
- `[P§22]` MCP's role as standardized tool boundary
- `[P§23]` Skill & capability four-identity architecture
- `[P§24]` Human approval gates for expensive/destructive operations
- `[P§29.4]` Hardware-aware deployment & accuracy
- `[P§34]` Layer boundaries and ownership
- `docs/APPROVALS.md` Gated actions and thresholds
- `ADR-0001` Capability / Skill / Tool / Agent as distinct registries
- `ADR-0002` LLM Gateway and provider abstraction
- `ADR-0003` Orchestration state machine, checkpointing, and persistent approvals
- `ADR-0004` Project memory and experiment ledger persistence
- `D-014` Out-of-process training on external GPU targets
- `D-022` Factual NVIDIA capability inventory on local macOS workstation
