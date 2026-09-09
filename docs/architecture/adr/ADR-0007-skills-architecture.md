# ADR-0007: Skills Architecture & NVIDIA Capability Discovery

- **Status:** Accepted
- **Amendment Status:** Frozen
- **Date:** 2026-09-03
- **Last Reviewed:** 2026-09-10
- **Layer:** skills / execution
- **Canon:** `[P§15]`, `[P§23]`, `[P§29.9]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #22

## 1. Context

In the CV Engineering Agent architecture, the system operates under a strict four-layer resolution hierarchy `[P§23]`, `docs/architecture/OVERVIEW.md`:

$$\text{User Task} \longrightarrow \text{Capability} \longrightarrow \text{Skill} \longrightarrow \text{Tool} \longrightarrow \text{Execution Target}$$

1. **Capability (ADR-0001):** The typed catalogue of *what* the system can achieve (e.g., `model.optimize.quantization`).
2. **Tool (ADR-0005):** The typed executable interface and transport boundary that performs discrete actions on execution targets (e.g., executing a command or invoking an MCP tool).
3. **Knowledge (ADR-0006):** The passive indexed evidence base providing external domain facts, APIs, and literature.
4. **Skill (ADR-0007):** The specialized procedural knowledge explaining *how* to accomplish a specific capability using one or more tools.

Without an explicit Skills architecture, agents collapse into two well-documented anti-patterns `[P§31]`:
- **The "Prompt Pile" Failure:** Procedural instructions, command flags, and optimization recipes are hard-coded into large, unwieldy reasoning prompts.
- **Layer Leakage (`[P§19]`, `[P§34]`):** Reasoning nodes attempt to own tool execution parameters directly, or tools attempt to own domain-specific procedural logic.

Furthermore, computer vision engineering on edge and production targets heavily relies on specialized vendor ecosystems, predominantly **NVIDIA** (TensorRT, Model Optimizer, DeepStream, TAO, Triton, Jetson tools). The canon mandates:

> **Don't reinvent existing NVIDIA/CUDA/vendor expertise — discover and invoke `[P§15]`.**

Per decision `D-022` (the factual NVIDIA capability inventory), the host development environment is Apple Silicon (macOS arm64) with general development tools only; zero local NVIDIA GPUs, CUDA toolkits, or drivers exist. Therefore, the Skills architecture must declaratively represent NVIDIA capabilities, distinguish local vs. remote compute availability, and strictly enforce the **known-but-unavailable** invariant without silent fallback.

## 2. Responsibility (`[P§34]`)

- **This owns:**
  - The definition and schema of skills (`SkillId`, `SkillDescriptor`, procedural recipes, and parameter contracts).
  - The declarative representation and metadata of internal and external skills (including NVIDIA ecosystem skills per `[P§15]`).
  - The architectural contracts for skill resolution and registry management (`SkillRegistry`, `SkillResolver`, `SkillExecutor`).
  - The resolution chain: mapping a `CapabilityId` to candidate `SkillId`s, and validating required `ToolId`s and execution targets.
  - The skill availability state machine (`KNOWN`, `DISCOVERED`, `CONFIGURED`, `AVAILABLE`, `EXECUTABLE`, `UNAVAILABLE`, `KNOWN_BUT_UNAVAILABLE`).
  - The **known-but-unavailable** constraint and prohibition of silent fallback for external vendor skills.
  - Discovery lifecycle contracts for local procedural skills, remote execution targets, and MCP-advertised skills.
- **This does NOT own:**
  - Capability catalogue authority, identity types, or stage definitions → Capability Model (ADR-0001) `[P§23]`;
  - Physical tool execution, subprocess launching, transport protocols, or SSH/MCP connections → Tools & MCP Boundary (ADR-0005) `[P§22]`;
  - External documentation, paper retrieval, RAG, and credibility weighting → Knowledge Subsystem (ADR-0006) `[P§16]`–`[P§19]`;
  - Task framing, CV problem decomposition, elicitation, and judgment → Reasoning Subsystem (ADR-0008) `[P§4]`, `[P§5]`;
  - Workflow graph transitions, checkpointing, and approval policy enforcement → Orchestration (ADR-0003, `docs/APPROVALS.md`) `[P§21]`, `[P§24]`;
  - Implementation or reimplementation of NVIDIA algorithms, CUDA kernels, or compiler fusions → External Vendor Runtimes `[P§15]`.
- **Why this responsibility does not belong elsewhere:**
  A capability declares *what* is needed, but cannot encode *how* without bloated monolithic schemas. A tool executes commands, but cannot understand multi-step engineering procedures without polluting the transport boundary. Reasoning decides *which* strategy to follow, but must not hard-code vendor-specific CLI recipes. Isolating Skills provides a modular, pluggable substrate for domain expertise.

## 3. Decision

We establish an explicit **Skills Architecture and NVIDIA Capability Discovery Boundary** based on declarative skill contracts, dynamic capability-to-tool resolution, multi-tier availability tracking, and strict external vendor invocation boundaries.

### 3.1. Architectural Conceptual Model

```text
       Reasoning Nodes (ADR-0008)
                  │
                  │ requests resolution for CapabilityId
                  ▼
       ┌─────────────────────────────────────────────────────────┐
       │                     SkillResolver                       │
       │   Resolves: CapabilityId ──► SkillId ──► ToolId(s)     │
       │   Filters by: ExecutionTarget requirements & policy     │
       └──────────┬───────────────────────────────┬──────────────┘
                  │ queries catalogue             │ checks targets
                  ▼                               ▼
       ┌─────────────────────┐         ┌─────────────────────────┐
       │    SkillRegistry    │         │  ADR-0005 Target Probe  │
       │  Authoritative vs.  │         │   LOCAL | REMOTE | MCP  │
       │  Discovered Cache   │         └──────────┬──────────────┘
       └─────────────────────┘                    │
                  │                               │
                  │ returns bound SkillDescriptor │
                  ▼                               ▼
       ┌─────────────────────────────────────────────────────────┐
       │                     SkillExecutor                       │
       │  1. Validates prerequisites & parameters                │
       │  2. Checks approval requirements (docs/APPROVALS.md)    │
       │  3. Dispatches structured tool invocations via ADR-0005  │
       └──────────────────────────┬──────────────────────────────┘
                                  │ ToolDescriptor invocation
                                  ▼
                     Tools Layer (ADR-0005)
                     on LOCAL / REMOTE / MCP
```

### 3.2. The Skill Contract (`SkillDescriptor`)

A Skill is a declarative specification of procedural expertise. Every skill in the system MUST adhere to the `SkillDescriptor` contract:

1. **Identity & Metadata:**
   - `skill_id`: Unique typed identifier (`SkillId`, e.g., `nvidia.model_optimizer.ptq`).
   - `name`: Human-readable display name.
   - `description`: Summary of what the skill does and when it is applicable.
   - `version`: SemVer string of the skill specification.
   - `provenance`: `internal` (in-repo recipe) or `external` (vendor tool adapter per `[P§15]`).
   - `vendor`: Vendor identifier (e.g. `"nvidia"`, `"ultralytics"`, `"internal"`).
2. **Capability Binding:**
   - `satisfies_capabilities`: Tuple of `CapabilityId`s that this skill can satisfy.
3. **Tool Dependencies & Ownership Invariant:**
   - `required_tools`: Tuple of authoritative `ToolId`s necessary to execute the skill's procedure.
   - **Ownership Invariant:** ADR-0001 owns `ToolId` identity and catalogue authority; ADR-0005 owns `ToolDescriptor` execution semantics and target binding.
4. **Execution-Target Requirements (`TargetRequirements`):**
   - `allowed_targets`: Permitted execution target types (`LOCAL`, `REMOTE`, `MCP_ENDPOINT` per ADR-0005).
   - `required_hardware`: Target hardware constraints (e.g., `"nvidia_gpu"`, `"jetson_orin"`, `"any"`).
   - `min_cuda_compute`: Minimum CUDA compute capability required (e.g. `"8.7"` for Orin, `"none"` for CPU).
   - `required_env_vars`: System environment variables required for execution.
5. **Procedural Recipe & Approval Invariant:**
   - `procedural_recipe`: Structured declarative instructions or recipe template defining parameter mappings, command sequences, and artifact outputs.
   - `parameter_schema`: JSON schema defining typed input parameters.
   - `prerequisites`: Verification checks that must pass before invocation (e.g., input model artifact exists).
   - `timeout_seconds`: Bounded maximum execution duration for the skill procedure (default 600s).
   - `deprecated_in_favor_of`: Optional replacement `SkillId` for superseded skills.
   - `requires_approval`: Boolean flag indicating if execution involves expensive/destructive operations.
   - **Approval Invariant:** `requires_approval` is a skill-level execution requirement declaration; approval policy, gate evaluation, and ticket/token validation remain strictly owned by Orchestration (ADR-0003) and `docs/APPROVALS.md`.

### 3.3. Skill Registry & Resolution (`CapabilityId → SkillId → ToolId`)

Resolution follows a strict 3-stage chain while preserving ADR-0001 registry authority:

```text
CapabilityId (ADR-0001)
     │
     ▼ (SkillResolver queries SkillRegistry)
SkillId (Candidate SkillDescriptor)
     │
     ▼ (Target & Environment Validation)
AvailabilityState (Available / Unavailable / Known-but-unavailable)
     │
     ▼ (Tool Dependency Resolution)
ToolId(s) (ADR-0005 ToolDescriptors bound to ExecutionTarget)
```

**Registry Authority & Storage Boundaries:**
- **Authoritative Registered Skills:** Static, verified skill descriptors packaged with the agent or explicitly added via project governance. Owned exclusively by `SkillRegistry`.
- **Runtime-Discovered Candidate Skills:** Ephemeral descriptors discovered dynamically via MCP endpoints or remote target probes. Held in a separate discovery cache; they do **not** become authoritative registry entries unless explicitly registered and validated against ADR-0001.
- **`SkillResolver` (Matchmaker):** Evaluates a `CapabilityId` against candidate skills and available execution targets (`ADR-0005`). Returns a `SkillResolution`.
- **`SkillExecutor` (Orchestrator):** Binds validated parameters to tool descriptors, verifies approval preconditions (`docs/APPROVALS.md`), and invokes dependent tools through the ADR-0005 interface.

### 3.4. NVIDIA Capability Discovery & Declarative Expertise (`[P§15]`, `D-022`)

The agent incorporates external NVIDIA capabilities via declarative adapters. The agent does **not** reimplement NVIDIA algorithms, CUDA kernels, or compiler logic.

**Declarative NVIDIA Skill Inventory:**

| SkillId | Satisfies Capability | Required Tools | Target Requirements |
|---|---|---|---|
| `nvidia.tensorrt.inference_optimization` | `model.optimize.tensorrt` | `tool.nvidia.trtexec` | `REMOTE`, GPU Compute $\ge 7.0$ |
| `nvidia.model_optimizer.ptq` | `model.optimize.quantization` | `tool.nvidia.modelopt` | `REMOTE`, GPU Compute $\ge 8.0$ |
| `nvidia.model_optimizer.qat` | `model.optimize.quantization` | `tool.nvidia.modelopt` | `REMOTE`, GPU Compute $\ge 8.0$ |
| `nvidia.tao.transfer_learning` | `model.train.fine_tune` | `tool.nvidia.tao` | `REMOTE`, Docker + GPU |
| `nvidia.deepstream.pipeline` | `stream.pipeline.deepstream` | `tool.nvidia.deepstream_app` | `REMOTE`, Jetson or dGPU |
| `nvidia.dali.data_pipeline` | `dataset.preprocess.dali` | `tool.nvidia.dali` | `REMOTE`, CUDA Toolkit |
| `nvidia.triton.model_serving` | `model.deploy.triton` | `tool.nvidia.tritonserver` | `REMOTE` / `MCP_ENDPOINT` |
| `nvidia.cuda_agent.kernel_profiling` | `hardware.profile.cuda` | `tool.nvidia.nsys` | `REMOTE`, CUDA Toolkit |
| `nvidia.jetson.hardware_profiling` | `hardware.profile.jetson` | `tool.nvidia.tegrastats` | `REMOTE`, Jetson target |
| `nvidia.model_inspection.visualize` | `model.inspect.architecture` | `tool.netron`, `tool.onnx` | `LOCAL` or `REMOTE` |

### 3.5. Availability State Machine & The Known-but-Unavailable Invariant

Skills move through an explicit state machine with formally typed states.

**Lifecycle Paths & Registration Boundary:**
- `DISCOVERED` is a non-authoritative candidate state and is not a mandatory predecessor of `KNOWN`. A discovered candidate becomes `KNOWN` only after explicit registration/validation; otherwise it remains ephemeral in the discovery cache and may be discarded.
- Statically declared skills (internal recipes or authoritative external vendor adapters) enter the system directly as `KNOWN`.

| State | Definition & Semantics | Allowed Next Transitions |
|---|---|---|
| **`KNOWN`** | Skill descriptor is declared/recognized by the system (external contract known). | `CONFIGURED`, `KNOWN_BUT_UNAVAILABLE` |
| **`DISCOVERED`** | Ephemeral runtime candidate detected via MCP or remote probe (non-authoritative). | `KNOWN` (upon validation/registration), or discarded |
| **`CONFIGURED`** | Execution target (`LOCAL`, `REMOTE`, `MCP_ENDPOINT`) has been assigned to skill. | `AVAILABLE`, `UNAVAILABLE` |
| **`AVAILABLE`** | Target is reachable and environment prerequisites (GPU, CUDA arch) are verified. | `EXECUTABLE`, `UNAVAILABLE` |
| **`EXECUTABLE`** | Target verified, tool dependencies confirmed, credentials valid; ready to dispatch. | (Terminal executable state) |
| **`UNAVAILABLE`** | Target unreachable, environment incompatible, or required tools uninstalled. | `CONFIGURED` (upon remediation) |
| **`KNOWN_BUT_UNAVAILABLE`** | Skill contract is known, but no compatible execution target exists (`D-022`). | `CONFIGURED` (when target added) |

**Transitions and Causes:**
1. `KNOWN` $\rightarrow$ `KNOWN_BUT_UNAVAILABLE`: Occurs when a known skill (e.g. NVIDIA GPU adapter) is evaluated, but no compatible compute target is configured in the environment.
2. `KNOWN` $\rightarrow$ `CONFIGURED`: Occurs when a compatible execution target satisfying `TargetRequirements` is bound to a known skill.
3. `DISCOVERED` $\rightarrow$ `KNOWN`: Occurs when a runtime-discovered candidate skill passes schema validation and is explicitly registered in the `SkillRegistry`.
4. `CONFIGURED` $\rightarrow$ `AVAILABLE`: Occurs when target probing confirms host reachability and hardware/driver version compatibility.
5. `AVAILABLE` $\rightarrow$ `EXECUTABLE`: Occurs when dependent `ToolDescriptor`s are confirmed present and invocable on the target.
6. `CONFIGURED` / `AVAILABLE` $\rightarrow$ `UNAVAILABLE`: Occurs if target is offline, hardware is incompatible, or a tool dependency is missing.

**The Known-but-Unavailable Invariant (`D-022`):**
When a task requests a capability requiring an external NVIDIA skill, but no compatible execution target (`REMOTE` GPU box or `MCP_ENDPOINT`) is configured:
1. The resolution MUST formally return `AvailabilityState.KNOWN_BUT_UNAVAILABLE`.
2. **Strict Prohibition of Silent CPU Fallback:** The agent MUST NOT silently fall back to generic CPU scripts or mock runtimes when an NVIDIA capability is requested. The system must explicitly report `KNOWN_BUT_UNAVAILABLE`, requiring human approval and capability reassignment to proceed with an alternative baseline.

### 3.6. Execution-Target Binding (Alignment with ADR-0005)

Skill resolution binds directly to ADR-0005 execution targets:
- `LOCAL`: Workstation host (macOS arm64). Suitable for inspection, dataset verification, local git operations, and model architecture visualization.
- `REMOTE`: Configured external compute target (remote Linux GPU server, cloud VM, or Jetson board via SSH). Mandatory for all NVIDIA GPU execution.
- `MCP_ENDPOINT`: Invocation boundary for external tools exposed over Model Context Protocol.

A skill cannot declare execution transports independently; it MUST express execution requirements via `TargetRequirements`, which the `SkillResolver` validates against available ADR-0005 targets.

### 3.7. Discovery Lifecycle & Authority Boundaries

Discovery operates across three distinct mechanisms:

1. **Local Procedural Skills:** Discovered by scanning packaged skill specifications (`cv_agent/skills/`) and local workspace definitions (`.cv_agent/skills/`).
2. **Remote Execution Skills:** Discovered by probing active remote compute targets (`ADR-0005`) for installed tools (`nvcc`, `trtexec`, `tao`, `tegrastats`).
3. **MCP-Advertised Skills:** Discovered by querying connected MCP servers (`ADR-0005`).

**Authority Invariant:**
Discovery of an external skill or tool via MCP or remote probing **never creates or mutates authoritative capability identities** in ADR-0001. Runtime-discovered skills are recorded as `DISCOVERED` candidates; they become invocable only if they satisfy an existing authoritative `CapabilityId` and pass validation.

## 4. Proposed Interface Contracts (Phase 3 Implementation Target)

*(Note: These interfaces represent the proposed type contracts for Phase 3 implementation. They do not exist in the repository today).*

```python
# module: cv_agent.skills.types (Proposed — Phase 3 implementation target)

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, NewType, Protocol, runtime_checkable

from cv_agent.capabilities.registry import CapabilityId
from cv_agent.tools.types import ExecutionTarget, TargetType, ToolId

SkillId = NewType("SkillId", str)

# Identifier syntax rule: SkillId MUST be a non-empty ASCII string
# conforming to [a-z0-9_.-]+(/[a-z0-9_.-]+)* per ADR-0001 §3.1.


class AvailabilityState(str, Enum):
    """Lifecycle state of a skill on target compute environments."""

    KNOWN = "known"
    DISCOVERED = "discovered"
    CONFIGURED = "configured"
    AVAILABLE = "available"
    EXECUTABLE = "executable"
    UNAVAILABLE = "unavailable"
    KNOWN_BUT_UNAVAILABLE = "known_but_unavailable"


class SkillProvenance(str, Enum):
    """Provenance origin of skill procedural instructions."""

    INTERNAL = "internal"  # Packaged or workspace procedural recipe
    EXTERNAL = "external"  # Vendor adapter (NVIDIA, PyTorch, etc.) per [P§15]


@dataclass(frozen=True)
class TargetRequirements:
    """Hardware and environment prerequisites required by a skill."""

    allowed_targets: tuple[TargetType, ...] = (TargetType.REMOTE,)
    required_hardware: str = "any"  # e.g. "nvidia_gpu", "jetson_orin"
    min_cuda_compute: str | None = None  # e.g. "8.7"
    required_env_vars: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillDescriptor:
    """Architectural definition and procedural contract of a skill."""

    skill_id: SkillId
    name: str
    description: str
    version: str
    provenance: SkillProvenance
    satisfies_capabilities: tuple[CapabilityId, ...]
    required_tools: tuple[ToolId, ...]
    target_requirements: TargetRequirements
    parameter_schema: Mapping[str, Any]
    procedural_recipe: str  # Structured workflow / recipe template
    vendor: str | None = None  # e.g. "nvidia"
    prerequisites: tuple[str, ...] = ()
    timeout_seconds: int = 600
    deprecated_in_favor_of: SkillId | None = None
    requires_approval: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillResolution:
    """Result of resolving a CapabilityId against candidate skills."""

    capability_id: CapabilityId
    candidate_skills: tuple[SkillDescriptor, ...]
    selected_skill: SkillDescriptor | None
    availability_state: AvailabilityState
    missing_prerequisites: tuple[str, ...] = ()
    unavailability_reason: str | None = None

    @property
    def is_executable(self) -> bool:
        return (
            self.selected_skill is not None
            and self.availability_state == AvailabilityState.EXECUTABLE
        )


@runtime_checkable
class SkillRegistry(Protocol):
    """Authoritative catalogue of validated, registered skill descriptors."""

    def register_skill(self, descriptor: SkillDescriptor) -> None:
        """Register a validated skill descriptor into the registry."""
        ...

    def get_skill(self, skill_id: SkillId) -> SkillDescriptor | None:
        """Retrieve a skill descriptor by ID."""
        ...

    def list_skills_for_capability(
        self, capability_id: CapabilityId
    ) -> tuple[SkillDescriptor, ...]:
        """List all skills satisfying the specified capability."""
        ...


@runtime_checkable
class SkillResolver(Protocol):
    """Resolves capabilities to skills bound to concrete execution targets."""

    def resolve(
        self,
        capability_id: CapabilityId,
        available_targets: tuple[ExecutionTarget, ...],
    ) -> SkillResolution:
        """Resolve a capability to candidate skills and evaluate availability."""
        ...


@runtime_checkable
class SkillExecutor(Protocol):
    """Coordinates procedural execution of a bound skill via ADR-0005 tools."""

    async def execute(
        self,
        skill: SkillDescriptor,
        parameters: Mapping[str, Any],
        target: ExecutionTarget,
    ) -> Mapping[str, Any]:
        """Validate parameters, enforce approvals, and invoke dependent tools."""
        ...
```

## 5. Normalized Failure Taxonomy

The Skills subsystem categorizes failures into distinct operational types:

### A. Resolution & Discovery Failures
1. **`UNKNOWN_CAPABILITY`:** Requested capability ID does not exist in ADR-0001 registry.
2. **`NO_SATISFYING_SKILL`:** Capability is valid, but zero registered skills declare support for it.
3. **`KNOWN_BUT_UNAVAILABLE`:** Skill contract is known, but no compatible execution target satisfies its requirements (`D-022`).
4. **`PREREQUISITE_FAILED`:** Environment, input artifacts, or software versions fail skill preconditions.

### B. Execution & Binding Failures
5. **`MISSING_TOOL_DEPENDENCY`:** Skill requires a `ToolId` that is not installed or configured on the target.
6. **`TARGET_MISMATCH`:** Target does not meet `TargetRequirements` (e.g. attempting to run TensorRT on `LOCAL` macOS).
7. **`PARAMETER_VALIDATION_FAILED`:** Input parameters violate the skill's declared `parameter_schema`.
8. **`APPROVAL_REQUIRED`:** Skill requires approval (`requires_approval=True`) but no valid approval token was provided.

## 6. Alternatives Considered

| Alternative | Evidence For | Evidence Against | Why Not Chosen |
|---|---|---|---|
| **Merge Skills into Capabilities (Flat Capability Model)** | Fewer files and type definitions. | Folds procedural code and CLI recipes into capability definitions; causes monolithic hard-coded prompts `[P§31]`. | Violates `[P§23]`, `[P§34]`, and ADR-0001. |
| **Merge Skills into Tools (LangChain / Semantic Kernel Model)** | Standard ecosystem pattern; simple tool registry. | Tools become bloated with high-level workflows; cannot represent "capability known but tool unavailable"; violates `[P§15]`. | Violates `[P§15]`, `[P§22]`, `[P§23]`. |
| **Reimplement NVIDIA functionality in Python (e.g. custom quantization)** | Local self-contained execution on Mac host. | Millions of engineering hours in TensorRT/ModelOpt wasted; slow and un-deployable on edge targets `[P§29.4]`. | Violates `[P§15]` ("discover and invoke, do not duplicate"). |
| **Silent CPU Fallback when NVIDIA targets missing** | Prevents agent workflow from halting. | Masquerades slow CPU emulation as valid GPU edge engineering; produces meaningless benchmark metrics. | Violates `[P§29.3]` (evidence over hype) and `[P§29.4]` (hardware-aware). |

## 7. Consequences

### Positive
- **Clear Identity Separation (`[P§23]`, `[P§34]`):** Enforces `Capability ≠ Skill ≠ Tool ≠ Agent/Runtime`.
- **Ecosystem Leverage without Duplication (`[P§15]`):** NVIDIA expertise is invoked through declarative adapters, keeping the agent lightweight and deployable.
- **Hardware-Aware Integrity (`[P§29.4]`, `D-022`):** Explicit `KNOWN_BUT_UNAVAILABLE` reporting prevents hallucinated or misleading local execution on Apple Silicon hosts.
- **Pluggable Discovery:** Skills can be added locally, remotely, or via MCP without mutating core capability contracts.

### Negative / Costs
- **Multi-Hop Indirection:** Resolving a task requires traversing `Capability → Skill → Tool`.
- **Target Dependency:** NVIDIA skills cannot execute locally on development hosts and require configured remote targets (`D-014`).

## 8. Acceptance Criteria

1. **Identity Separation Preserved:** Type contracts strictly maintain distinct identities for `CapabilityId`, `SkillId`, and `ToolId`.
2. **Capability → Skill → Tool Resolution:** `SkillResolver` accepts a `CapabilityId`, queries `SkillRegistry`, and resolves required `ToolId`s without mutating ADR-0001 definitions.
3. **Tool Ownership Decoupled:** `required_tools` references `ToolId` (owned by ADR-0001 for identity authority), while execution semantics are delegated exclusively to ADR-0005 `ToolDescriptor`s.
4. **Approval Policy Decoupled:** `requires_approval` in `SkillDescriptor` declares an execution requirement; approval policy and ticket validation remain owned by ADR-0003 and `docs/APPROVALS.md`.
5. **NVIDIA Declarative Invocation:** NVIDIA skills (TensorRT, Model Optimizer, TAO, DeepStream, DALI, Triton, Jetson tools) are modeled declaratively as external skills (`provenance: EXTERNAL`) without reimplementing vendor logic `[P§15]`.
6. **Availability State Machine:** Skills support the formally typed lifecycle states (`KNOWN`, `DISCOVERED`, `CONFIGURED`, `AVAILABLE`, `EXECUTABLE`, `UNAVAILABLE`, `KNOWN_BUT_UNAVAILABLE`).
7. **Known-but-Unavailable Enforcement:** When an NVIDIA skill lacks a compatible GPU execution target, resolution explicitly returns `AvailabilityState.KNOWN_BUT_UNAVAILABLE`; silent fallback to CPU is strictly prohibited.
8. **Execution-Target Alignment:** `SkillDescriptor` target requirements align with ADR-0005 (`LOCAL`, `REMOTE`, `MCP_ENDPOINT`).
9. **Discovery Authority Boundary:** Dynamically discovered skills from MCP or remote probes are stored as non-authoritative candidates and do not mutate authoritative capability identities in ADR-0001.
10. **Bounded Execution & Deprecation Lineage:** `SkillDescriptor` defines an explicit `timeout_seconds` enforced by the executor and an optional `deprecated_in_favor_of` pointer when procedural skills are superseded.

## 9. References

- `[P§15]` Don't reinvent existing NVIDIA/CUDA/vendor expertise — discover and invoke
- `[P§23]` Vocabulary that must not blur: Capability vs. Skill vs. Tool vs. Agent
- `[P§24]` Human-in-the-loop approvals
- `[P§29.3]` Evidence over hype: quantitative comparison or no claim
- `[P§29.4]` Hardware-aware: accuracy without deployability is incomplete
- `[P§29.9]` Don't reinvent what NVIDIA already solved
- `[P§34]` Architecture boundary test: what does this own, and why not elsewhere?
- `docs/architecture/OVERVIEW.md` Architecture boundaries and design sequence
- `docs/APPROVALS.md` Human approval policies and trigger thresholds
- `ADR-0001` Capability Model
- `ADR-0002` LLM Gateway and Provider Abstraction
- `ADR-0003` Orchestration State Machine, Checkpointing, and Persistent Approvals
- `ADR-0004` Project Memory and Experiment Ledger Persistence
- `ADR-0005` Tools and MCP Execution Boundary
- `ADR-0006` Knowledge and RAG Subsystem
- `D-022` Factual NVIDIA capability inventory
