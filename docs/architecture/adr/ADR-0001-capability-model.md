# ADR-0001: Capability / Skill / Tool / Agent as four distinct registries

- **Status:** Accepted
- **Date:** 2026-08-30
- **Amended:** 2026-09-09
- **Layer:** cross-cutting
- **Canon:** `[P§23]`, `[P§15]`, `[P§22]`, `[P§34]`, `[P§32]`
- **Issue:** #12
- **Amendment:** D-034

## 1. Context

`[P§23]` states that capability, skill, tool, and agent are not interchangeable and defines the resolution chain `task → capability → skill → tools → execution agent`. `[P§32]` records the existing capability registry with type-safe identities. `[P§15]` requires the system to discover and invoke existing NVIDIA/vendor capability rather than reimplement it.

The original ADR established the four identities but under-specified the boundaries between declaration, resolution, compatibility, availability, discovery, and execution. Those omissions would permit a registry entry to be mistaken for an executable implementation, make multi-skill resolution nondeterministic, weaken reproducibility, and create overlap with ADR-0007's already-accepted discovery and skill-resolution contracts.

This amendment strengthens the foundational registry contract while preserving ownership boundaries established by ADR-0003, ADR-0005, ADR-0007, and later ADRs.

## 2. Responsibility `[P§34]`

- **Owns:** stable typed identities for capabilities, skills, tools, and agents; authoritative catalogue semantics; capability-to-skill binding semantics; resolution contracts; compatibility constraints; resolution-result semantics; and immutable resolution snapshots.
- **Does NOT own:**
  - deciding whether a capability should be invoked → reasoning layer;
  - approval policy or authorization → ADR-0003 / `docs/APPROVALS.md`;
  - procedural skill implementation, discovery mechanisms, or vendor adapters → ADR-0007;
  - physical tool execution, transport, subprocesses, SSH, or MCP invocation → ADR-0005;
  - workflow sequencing and checkpointing → ADR-0003;
  - experiment persistence → ADR-0004;
  - dataset, training, evaluation, optimization, or monitoring semantics → ADR-0009 through ADR-0013.
- **Why:** the capability model defines semantic identity and resolution without absorbing procedural, execution, governance, or workflow concerns.

## 3. Decision

The platform retains four distinct typed entity classes and registries:

```text
task
  │
  ▼
CapabilityRegistry
  │ CapabilityId
  ▼
CapabilityResolver
  │
  ├── compatibility filtering
  ├── availability filtering
  ├── policy/governance filtering
  └── deterministic suitability ordering
  │
  ▼
SkillBinding → SkillRegistry (ADR-0007)
  │
  ▼
ToolBinding → ToolRegistry (ADR-0005)
  │
  ▼
AgentBinding → ExecutionTarget (ADR-0005)
  │
  ▼
ResolutionSnapshot
```

### 3.1 Stable identity

The four entity identities remain distinct and stable:

```python
CapabilityId = NewType("CapabilityId", str)
SkillId      = NewType("SkillId", str)
ToolId       = NewType("ToolId", str)
AgentId      = NewType("AgentId", str)
```

Identity MUST NOT encode implementation version. Versions are separate typed metadata and are resolved through compatibility constraints.

An **Agent** is an execution-capable actor identified by `AgentId`. It is not synonymous with a Tool, Skill, process, runtime, or `ExecutionTarget`. An `ExecutionTarget` describes where execution occurs; ADR-0005 owns its transport and physical execution semantics.

### 3.2 Entity semantics

| Entity | Answers | Owns | Does not own |
|---|---|---|---|
| Capability | **What** can be achieved? | semantic capability identity, stage, risk metadata | procedure or execution |
| Skill | **How** can the capability be accomplished? | procedural expertise and capability bindings | physical transport |
| Tool | **What discrete action** can be invoked? | executable interface/transport contract | domain procedure |
| Agent | **Which actor** can execute? | execution-actor identity/contract | physical target lifecycle |

A declaration of any entity does not imply that it is executable.

### 3.3 Typed bindings

Unstructured string lists such as `relevant_skills`, `relevant_tools`, and `relevant_agents` are not authoritative architectural bindings. Bindings MUST carry explicit relationship semantics and, where relevant, compatibility and provenance metadata.

Conceptually:

```python
class SkillBinding(Protocol):
    capability_id: CapabilityId
    skill_id: SkillId
    compatibility: "CompatibilityConstraint"
    priority: int
    provenance: "Provenance"

class ToolBinding(Protocol):
    skill_id: SkillId
    tool_id: ToolId
    compatibility: "CompatibilityConstraint"

class AgentBinding(Protocol):
    tool_id: ToolId
    agent_id: AgentId
```

These are architectural contracts; concrete storage remains an implementation concern.

### 3.4 Compatibility contract

Resolution MUST support compatibility constraints without hard-coding today's vendor versions into ADR-0001.

A compatibility constraint may express requirements over:

- software/runtime versions;
- operating system and architecture;
- framework and model/data formats;
- hardware class and accelerator availability;
- CUDA/driver capability where applicable;
- execution-target class;
- capability/skill/tool specification versions.

Compatibility is evaluated against the actual execution environment and target metadata. Unsupported constraints result in a deterministic rejection reason rather than silent fallback.

### 3.5 Availability semantics

The architecture distinguishes these facts:

```text
DECLARED
  ≠ VALIDATED
  ≠ COMPATIBLE
  ≠ AVAILABLE
  ≠ EXECUTABLE
  ≠ AUTHORIZED
```

At minimum, resolution MUST be able to represent:

- `KNOWN` — authoritative declaration exists;
- `DISCOVERED` — runtime candidate exists but is not authoritative;
- `CONFIGURED` — compatible target/binding has been configured;
- `AVAILABLE` — prerequisites and target health have been verified;
- `EXECUTABLE` — required tool bindings and invocation prerequisites are verified;
- `UNAVAILABLE` — known binding currently cannot execute;
- `KNOWN_BUT_UNAVAILABLE` — capability/skill is known but no compatible target is currently available.

ADR-0007 owns the detailed Skill lifecycle and discovery mechanics. ADR-0001 consumes those states for capability resolution and MUST NOT duplicate discovery logic.

### 3.6 Deterministic resolution

For a requested `CapabilityId`, resolution MUST be deterministic for an identical registry snapshot, target inventory, policy context, and resolver-policy version.

Candidate resolution follows:

```text
CapabilityId
    ↓
Candidate SkillBindings
    ↓
Schema/contract validation
    ↓
Compatibility filtering
    ↓
Availability filtering
    ↓
Governance/policy eligibility
    ↓
Deterministic suitability ordering
    ↓
Selected binding OR explicit unresolved result
```

Suitability ordering MUST be explicit and versioned. It may consider declared priority, compatibility specificity, availability, target suitability, provenance/trust class, and other policy-approved criteria. Ties MUST resolve deterministically; the resolver MUST NOT depend on unordered map/set iteration.

Resolution SHOULD retain rejected candidates and machine-readable rejection reasons for diagnostics. A missing executable binding is a valid resolution outcome, not a lookup exception.

### 3.7 Approval boundary

`requires_approval` MAY exist as capability or binding risk metadata, but it is never an authorization decision. The authoritative approval policy and approval record validation remain in ADR-0003 and `docs/APPROVALS.md`.

Therefore:

```text
Capability risk metadata
        ↓
approval required/recommended
        ↓
ADR-0003 / APPROVALS.md evaluates policy
        ↓
authorization result
```

ADR-0001 MUST NOT encode duplicated or competing approval policy.

### 3.8 Provenance

Provenance is a first-class extensible property rather than a binary internal/external flag. The model MUST be able to distinguish at least:

```text
internal
official_vendor
official_repository
community
remote_service
generated
discovered
user_registered
```

Provenance metadata SHOULD identify publisher/source, specification version, discovery/registration origin, and verification/trust state. Exact trust policy belongs to the relevant governance/discovery subsystem.

This permits NVIDIA/vendor capability declarations without embedding or reimplementing vendor algorithms.

### 3.9 Discovery boundary

ADR-0001 defines **what may be registered and how it participates in resolution**. It does not define how entities are discovered.

Discovery providers may include packaged metadata, local plugins, MCP endpoints, remote target probes, or future registry services. ADR-0007 owns skill discovery and vendor capability discovery; ADR-0005 owns execution-target discovery/probing. A discovered candidate is not authoritative merely because it was observed.

A runtime-discovered entity becomes authoritative only through the applicable registration and validation process.

### 3.10 Resolution snapshot and reproducibility

Every execution-relevant resolution MUST be reproducible through an immutable `ResolutionSnapshot` containing, conceptually:

```python
class ResolutionSnapshot(Protocol):
    snapshot_id: str
    registry_version: str
    resolver_policy_version: str
    capability_id: CapabilityId
    capability_version: str
    skill_id: SkillId | None
    skill_version: str | None
    tool_ids: tuple[ToolId, ...]
    tool_versions: tuple[str, ...]
    agent_id: AgentId | None
    target_reference: str | None
    compatibility_result: str
    availability_state: str
    created_at: str
```

The snapshot is a historical resolution fact, not mutable live registry state. ADR-0004 may persist it as part of experiment/workflow lineage.

### 3.11 External vendor invariant

External vendor capabilities, including NVIDIA ecosystems, MUST be represented through declarative capability/skill/tool contracts and invocation boundaries rather than reimplemented inside the capability model.

If a requested vendor capability is known but no compatible execution target exists, resolution MUST return `KNOWN_BUT_UNAVAILABLE` (or the equivalent authoritative unavailable state) and MUST NOT silently substitute a semantically different CPU/mock implementation.

An alternative capability may be proposed only through the appropriate reasoning/governance path.

### 3.12 Architectural invariants

1. **Identity invariant:** Capability, Skill, Tool, and Agent identities are distinct typed identities.
2. **Declaration invariant:** Registry declaration never implies executability.
3. **Binding invariant:** Capability satisfaction is represented by typed bindings, not ambiguous string references.
4. **Version invariant:** Stable identity is independent of implementation/specification version.
5. **Compatibility invariant:** Executability requires successful compatibility evaluation against the actual target/environment.
6. **Availability invariant:** Resolution distinguishes known, unavailable, available, executable, and authorized states.
7. **Determinism invariant:** Equivalent resolution inputs and snapshot state produce the same resolution result.
8. **Explainability invariant:** Failed candidate resolution produces explicit machine-readable reasons.
9. **Discovery authority invariant:** Runtime discovery cannot silently mutate authoritative registry identity or semantics.
10. **Approval invariant:** Capability risk metadata cannot bypass ADR-0003 / `docs/APPROVALS.md`.
11. **Execution boundary invariant:** ADR-0001 never executes tools or owns transport.
12. **Reproducibility invariant:** Execution-relevant resolution is capturable as an immutable snapshot.
13. **Vendor invariant:** Known vendor capability must not be silently replaced by an unrelated implementation.
14. **Layer invariant:** ADR-0001 defines semantic identity/resolution; ADR-0007 defines skill procedure/discovery; ADR-0005 defines tool execution/targets.

## 4. Alternatives considered

| Alternative | Evidence for | Evidence against | Why not chosen |
|---|---|---|---|
| Single tool registry | Simple; common ecosystem pattern | Loses semantic capability and known-but-unavailable state | Violates `[P§23]` and weakens `[P§15]` |
| Capability + tool only | Fewer entities | Procedural knowledge leaks into capability definitions | Violates capability/skill separation |
| Registry per layer with no resolver | Loose coupling | Duplicated resolution and inconsistent availability decisions | No authoritative answer to capability availability |
| Capability registry owns discovery | Centralized | Couples semantic identity to vendor/MCP/host discovery | Discovery belongs to specialized boundaries, especially ADR-0007/0005 |
| Dynamic unversioned plugin resolution | Flexible | Non-reproducible and potentially non-deterministic | Cannot guarantee experiment lineage or stable behavior |

## 5. Interface

The canonical public surface is conceptual and implementation-neutral:

```python
class Capability(Protocol):
    id: CapabilityId
    version: str
    stage: Stage
    description: str
    risk_level: str
    requires_approval: bool

class Resolution(NamedTuple):
    capability: Capability
    skill: Skill | None
    tools: tuple[ToolId, ...]
    agent: AgentId | None
    availability: str
    compatible: bool
    authorized: bool
    unavailable_reason: str | None
    rejected_candidates: tuple[str, ...]
    snapshot: "ResolutionSnapshot | None"

class CapabilityResolver(Protocol):
    def resolve(
        self,
        capability: CapabilityId,
        *,
        context: object | None = None,
    ) -> Resolution: ...

class CapabilityRegistry(Protocol):
    def capabilities(
        self,
        *,
        stage: Stage | None = None,
    ) -> tuple[Capability, ...]: ...

    def describe(self, capability: CapabilityId) -> Capability: ...
```

The concrete runtime implementation MUST preserve the invariants above. The resolver MUST NOT itself execute the selected tool.

## 6. Consequences

- **Enables:** deterministic and explainable capability resolution, honest unavailable states, vendor integration without reimplementation, and reproducible experiment lineage.
- **Requires:** explicit compatibility metadata, typed bindings, resolution policy/versioning, and snapshot capture.
- **Preserves:** ADR-0007 as the authority for skill discovery/lifecycle and ADR-0005 as the authority for tool execution/transport.
- **Makes harder:** ad-hoc tools or implicit fallbacks. This friction is intentional under `[P§34]`.
- **Operational cost:** modest registry/resolver metadata and validation overhead; materially lower architectural ambiguity and future migration cost.

## 7. Acceptance tests

The implementation of this ADR MUST eventually prove at least:

- a known capability with no satisfying skill resolves without exception and reports an explicit unavailable state/reason;
- a discovered but unregistered candidate cannot mutate authoritative registry semantics;
- an external vendor skill preserves extensible provenance;
- stable entity IDs remain independent of versions;
- incompatible candidates are rejected with machine-readable reasons;
- multiple eligible skills resolve deterministically under an identical resolver policy and snapshot;
- unordered registration order cannot change the selected result;
- resolution records preserve rejected candidates/reasons;
- a capability marked as requiring approval cannot be authorized by the capability registry itself;
- resolution can be serialized into an immutable snapshot sufficient to reproduce the selected binding;
- `CapabilityId`, `SkillId`, `ToolId`, and `AgentId` remain statically distinct under type checking;
- a known NVIDIA capability with no compatible target resolves as known-but-unavailable and does not silently fall back to an unrelated implementation.

## 8. Interim correction

The pre-existing `cv_agent/capabilities/registry.py` predates the resolver contract and does not implement the full `CapabilityResolver`/`Resolution` architecture. Its current semantics were corrected so declared-but-unbound entries are not falsely reported as executable. This amendment does not authorize premature runtime implementation; the resolver, typed binding model, discovery integration, and snapshot implementation remain Phase 2 work governed by this contract and ADR-0007/ADR-0005.

## 9. Revisit trigger

This ADR should be reconsidered if the platform changes the semantic distinction between capability, skill, tool, agent, or execution target; if the authoritative discovery/resolution ownership changes; or if future execution architecture makes the current four-entity model materially obsolete. A mere increase in the number of implementations or vendors is not grounds for collapsing the model.
