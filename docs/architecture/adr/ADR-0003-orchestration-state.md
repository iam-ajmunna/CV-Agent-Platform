# ADR-0003: Orchestration State Machine, Checkpointing, and Persistent Approvals

- **Status:** Accepted
- **Amendment Status:** Frozen
- **Date:** 2026-09-02
- **Last Reviewed:** 2026-09-09
- **Layer:** orchestration
- **Canon:** `[P§6]`, `[P§21]`, `[P§24]`, `[P§25]`, `[P§29.8]`, `[P§33]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #18 (freeze review)

## 1. Context

The CV Engineering Agent operates across multi-step engineering stages
(DISCOVER → DEFINE → DEVELOP → BENCHMARK → DEPLOY → MONITOR per `[P§6]`, `[P§35]`).
`[P§21]` designates LangGraph as the authoritative orchestration layer responsible for
state, workflow, branching, iteration, human approval, retries, checkpoints, and
multi-step execution coordination.

The orchestration layer requires durable state across process termination, workspace-scoped
isolation, external asynchronous job references, replay-safe recovery, strict typing, and
persistent approval interrupts. ADR-0003 establishes local SQLite checkpoint persistence at
`.cv_agent/state/checkpoints.sqlite`, distinct from the permanent experiment ledger at
`.cv_agent/state/experiments.sqlite` owned by ADR-0004.

A local SQLite transaction cannot be atomic with an external process, remote host, cloud
service, LLM provider, or MCP execution. Therefore orchestration models effectful operations
as versioned intents with stable identities and explicit reconciliation rather than assuming
checkpoint commit and external side effect form one transaction.

## 2. Responsibility (`[P§34]`)

- **Owns:** workflow graph topology, orchestration state, lifecycle transitions,
  node-to-node propagation, checkpoint persistence/restoration, approval interrupt/resume,
  operation identity and recovery protocol, session ownership, execution-event sequencing,
  and orchestration-level retry/recovery policy.
- **Does NOT own:** LLM provider management (ADR-0002); domain reasoning (ADR-0008);
  capability identity (ADR-0001); procedural skills (ADR-0007); tool execution/transport
  (ADR-0005); permanent memory/experiment ledger (ADR-0004); dataset storage (ADR-0009);
  training (ADR-0010); evaluation (ADR-0011); optimization/deployment (ADR-0012); or
  production monitoring/active learning (ADR-0013).

## 3. Decision

Use a LangGraph `StateGraph` with durable SQLite checkpoint persistence at
`.cv_agent/state/checkpoints.sqlite`. `MemorySaver` is permitted only for isolated tests.
The public orchestration semantics are independent of the checkpoint backend so future
migration to a server-side checkpointer does not alter operation, approval, or state semantics.

### 3.1 Dual-SQLite separation

```text
.cv_agent/state/
├── checkpoints.sqlite   ← ADR-0003 runtime checkpoint persistence
└── experiments.sqlite   ← ADR-0004 permanent experiment ledger
```

Checkpoint state is runtime state, not permanent domain memory. Experiment facts are handed
to ADR-0004 through stable references. Checkpoints needed by non-terminal or recoverable
sessions MUST NOT be pruned.

### 3.2 Authoritative state model

`AgentState` is a transport/state envelope and MUST NOT contain provider SDK objects, clients,
sockets, file handles, raw secrets, model artifacts, or large domain payloads.

All authoritative persisted records MUST be immutable value objects. Every checkpoint carries
`checkpoint_schema_version`. Optional values are explicit nullable fields. Arbitrary
`dict[str, Any]` state is prohibited; extension data MUST use a versioned schema envelope.
Timestamps are timezone-aware UTC instants. Large artifacts are referenced, never embedded.
Persistent history is append-only and bounded/redacted.

### 3.3 Session and execution ownership

`SessionId` identifies the user-visible orchestration session. `ThreadId` identifies its
LangGraph execution thread. A session has exactly one stable thread identity for its lifetime.
Recovery MUST reuse that identity.

A running session is protected by an `ExecutionLease`:

- `lease_id` — unique lease token;
- `session_id` and `thread_id`;
- `owner_instance_id`;
- `acquired_at` and `expires_at`;
- monotonically increasing `fencing_epoch`.

Every state mutation MUST carry the lease identity and fencing epoch. The persisted current
epoch MUST equal the mutation epoch; otherwise the mutation is rejected as stale. Lease expiry
is evaluated by the store using a monotonic-clock/lease-expiration mechanism; wall-clock time
is retained only as audit metadata. Renewal is compare-and-swap against the current lease and
fencing epoch. A worker that loses renewal or fencing MUST stop mutating state and enter stale
worker shutdown/recovery handling.

SQLite file locking is only a storage mechanism, not the application-level concurrency
contract. Independent sessions may execute concurrently; a single session is serialized by
its lease/fencing contract.

### 3.4 Canonical operation identity and hashing

Every externally effectful action receives an immutable logical `OperationId` before dispatch.
The executable identity is:

```text
OperationIdentity =
    operation_id
    + operation_version
    + canonical_payload_hash
    + target_identity
```

`operation_id` is a stable logical identifier. `operation_version` identifies a materially
changed intent. `target_identity` identifies the execution destination. `canonical_payload_hash`
is the digest of the canonical executable payload.

Canonicalization MUST be deterministic and versioned. The canonical form MUST specify:

1. UTF-8 encoding;
2. Unicode normalization policy;
3. deterministic object-key ordering;
4. explicit representation of null, omitted, default, boolean, integer, decimal, and string
   values;
5. normalized representation of sequences and sets (sets MUST be represented as explicitly
   sorted sequences before hashing);
6. inclusion of every execution-relevant parameter, selected capability/tool, target,
   dataset/model/artifact reference, and policy-relevant configuration;
7. exclusion of observational metadata such as timestamps, retry counters, worker identity,
   and log text unless those values change execution semantics;
8. a versioned canonicalization algorithm identifier; and
9. SHA-256 or a future approved cryptographic hash algorithm identified by algorithm/version.

Two semantically equivalent requests MUST produce the same canonical representation and hash.
Materially different executable intent MUST produce a different operation version and/or hash.
A changed payload MUST invalidate any prior approval.

### 3.5 Typed operation payloads

Authoritative operation payloads MUST be represented by a versioned schema envelope rather than
an unconstrained `Mapping[str, object]`:

```python
class OperationPayloadEnvelope(Protocol):
    schema_id: str
    schema_version: int
    payload_hash: str
    canonicalization_version: str
    payload: object
```

Concrete operation payload schemas are owned by the subsystem performing the operation. ADR-0003
owns their identity, versioning, canonicalization, approval binding, persistence, and recovery
semantics. The architectural interface may use `object`/mapping types as conceptual transport
placeholders, but an implementation MUST NOT persist an unvalidated arbitrary mapping as
authoritative state.

### 3.6 External side-effect protocol

SQLite checkpoint atomicity does not imply external atomicity. Effectful actions use:

```text
CREATE INTENT
  → VALIDATE + POLICY CHECK
  → APPROVAL (if required)
  → PERSIST DISPATCH-READY INTENT
  → DISPATCH with OperationIdentity/idempotency key
  → PERSIST external reference/result
  → RECONCILE after crash/timeout
  → TERMINAL STATE
```

The dispatch boundary MUST accept the operation identity where the target supports idempotency.
Exactly-once execution is never claimed universally. If the target cannot provide idempotency or
reconciliation, orchestration exposes explicit `UNKNOWN` semantics rather than claiming safety
that the target cannot provide.

### 3.7 Recovery authority and reconciliation

Reconciliation is an orchestration responsibility for establishing operation state; the owning
execution subsystem supplies the target-specific status/query mechanism.

The authoritative recovery state machine is:

```text
UNKNOWN
  ↓
RECONCILIATION_REQUIRED
  ↓
RECONCILING
  ├── CONFIRMED_COMPLETED → COMPLETED
  ├── CONFIRMED_NOT_SUBMITTED → DISPATCHABLE
  ├── CONFIRMED_FAILED → FAILED
  └── UNRESOLVED → OPERATOR_OR_POLICY_DECISION
```

Repeated reconciliation failures use bounded retry policy and preserve the last known evidence.
`UNRESOLVED` MUST NOT be silently converted into `FAILED`, `COMPLETED`, or a fresh dispatch.
Only explicit target evidence, an authoritative owning subsystem result, or an authorized
operator/policy decision may resolve it.

### 3.8 Approval evidence and validity

An `ApprovalRequest` binds to exactly one operation version, canonical payload hash, policy
context, and resource scope. Approval is valid only when all bound values still match.

Approval evidence MUST preserve:

- request identity;
- operation identity/version/hash;
- policy/risk classification and policy version;
- requested resource scope and cost estimate;
- authenticated principal reference and authorization-context reference;
- creation, expiry, and resolution timestamps;
- final decision and decision rationale/reference.

`MODIFIED` is never executable approval. It creates a new operation version/hash and requires
new approval. Expired, superseded, rejected, or hash-mismatched approval MUST fail closed.
Approval policy itself remains owned by `docs/APPROVALS.md` and the governing subsystem.

### 3.9 Authoritative state-transition protocol

Every session and operation transition MUST be validated as:

```text
(CurrentState, Event, Preconditions, Authority) → NextState
```

Illegal transitions MUST be rejected. A persisted transition records the prior state identity,
event identity, resulting state, sequence number, actor/lease, and schema version. State may
only advance through the authoritative transition function; nodes MUST NOT mutate lifecycle
status through ad-hoc field assignment.

The session and external-operation state machines are independent:

```text
SESSION:
INITIALIZING → READY → PLANNING → AWAITING_APPROVAL → EXECUTING → EVALUATING
                                                          ↓
                                               COMPLETED / FAILED / PAUSED

OPERATION:
CREATED → VALIDATED → APPROVAL_REQUIRED → DISPATCHING → SUBMITTED → RUNNING
                                                               ↓
                    COMPLETED / FAILED / CANCELLED
                                                               ↓
                                      UNKNOWN → RECONCILING → terminal/decision
```

`CANCEL_REQUESTED`, `UNKNOWN`, `RECONCILING`, `LOST`, and `RECOVERY_REQUIRED` are explicit
non-success/recovery states and MUST never be silently mapped to success.

### 3.10 Event ordering and concurrency

Execution events are append-only and ordered **per session**. Each session has exactly one
authoritative sequence domain. Sequence allocation occurs transactionally with the state
transition. Concurrent workers cannot independently allocate the same sequence because lease
fencing serializes session mutation.

Cross-session global ordering is explicitly not guaranteed. Wall-clock timestamps are audit
metadata only and MUST NOT be used to infer authoritative event order.

`step_count` is derived from authoritative event/transition records. `max_step_limit` counts
orchestration node/super-step executions, not remote job duration. Retry attempts are counted
according to the declared retry class. Exhaustion produces a defined recovery/terminal state.

### 3.11 Replay and nondeterminism

The architecture guarantees:

1. deterministic deserialization of a valid checkpoint;
2. deterministic validation of state transitions;
3. replay-safe handling of existing operation identities;
4. reconciliation before retrying unresolved external effects; and
5. immutable per-session event ordering.

It does **not** claim deterministic byte-for-byte re-execution of LLM calls, remote tools,
wall-clock observations, or external systems. Nondeterministic observations are retained by
reference or redacted metadata sufficient for audit and reproduction policy.

### 3.12 Retry semantics

Retry policy is explicit by failure class:

- deterministic validation/configuration errors: no automatic retry;
- transient transport errors: bounded retry with exponential backoff and jitter;
- ambiguous effectful dispatch: reconcile first, never blind retry;
- remote job failure: defer to the owning subsystem's job policy;
- approval rejection/expiry: no automatic retry;
- stale lease/fencing failure: terminate stale worker and reacquire through session ownership;
- repeated failure beyond policy limit: terminal `FAILED` with structured diagnostics.

Retrying an operation does not create a new semantic operation unless executable parameters change.

### 3.13 Checkpoint durability, migration, and corruption

SQLite checkpoints SHOULD use WAL where supported and an explicitly configured durability mode.
WAL is not treated as universal durability. Filesystem/storage failures remain possible.

Each checkpoint carries `checkpoint_schema_version`. Supported retained versions require explicit
migration rules. Unsupported versions fail closed and require migration or operator intervention.

Integrity failures MUST NOT silently restore arbitrary data. Recovery selects the newest valid
checkpoint supported by the integrity contract. If none is valid, the session enters
`RECOVERY_REQUIRED` and preserves diagnostic evidence outside the corrupt record where possible.

### 3.14 Safe retention and pruning

`prune_checkpoints()` MUST NOT delete checkpoints required by approvals, running/submitted/
dispatching/unknown/lost/reconciling operations, unresolved recovery state, migration, or audit
procedures. Pruning MUST be idempotent and MUST never modify `experiments.sqlite`.

Retention duration is configurable policy, not an architectural invariant.

## 4. Alternatives considered

| Alternative | Why rejected |
|---|---|
| MemorySaver only | Cannot survive process termination or persistent approvals. |
| Single SQLite database | Blurs runtime checkpoint and permanent experiment-ledger lifecycles. |
| Custom FSM/Celery/Airflow | Conflicts with the mandated LangGraph orchestration boundary and adds unnecessary infrastructure for the current local agent. |
| PostgreSQL/Redis as default | Enterprise concurrency is useful but violates the current local, repository-scoped zero-service baseline. It remains a future migration option. |
| Blind retry after crash | Cannot distinguish dispatch success from checkpoint failure and can duplicate expensive/destructive effects. |

## 5. Interface

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field


class LifecycleStatus(StrEnum):
    INITIALIZING = "initializing"
    READY = "ready"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    EVALUATING = "evaluating"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


class OperationStatus(StrEnum):
    CREATED = "created"
    VALIDATED = "validated"
    APPROVAL_REQUIRED = "approval_required"
    DISPATCHING = "dispatching"
    SUBMITTED = "submitted"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    LOST = "lost"
    RECONCILING = "reconciling"


class ApprovalAction(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"


class ApprovalRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class StageName(StrEnum):
    DISCOVER = "discover"
    DEFINE = "define"
    DEVELOP = "develop"
    BENCHMARK = "benchmark"
    DEPLOY = "deploy"
    MONITOR = "monitor"


class OperationId(BaseModel):
    model_config = ConfigDict(frozen=True)
    value: str = Field(min_length=1)


class OperationPayloadEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_id: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    canonicalization_version: str = Field(min_length=1)
    payload_hash: str = Field(min_length=1)
    payload: object


class OperationIntent(BaseModel):
    model_config = ConfigDict(frozen=True)
    operation_id: OperationId
    operation_version: int = Field(ge=1)
    operation_type: str = Field(min_length=1)
    canonical_payload: OperationPayloadEnvelope
    target_identity: str = Field(min_length=1)
    created_at: datetime


class ExecutionLease(BaseModel):
    model_config = ConfigDict(frozen=True)
    lease_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    owner_instance_id: str = Field(min_length=1)
    acquired_at: datetime
    expires_at: datetime
    fencing_epoch: int = Field(ge=1)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    request_id: str
    operation_id: OperationId
    operation_version: int = Field(ge=1)
    canonical_payload_hash: str
    policy_version: str
    action_type: str
    description: str
    risk_level: ApprovalRiskLevel
    estimated_cost_usd: float | None = None
    estimated_gpu_hours: float | None = None
    affected_paths: tuple[str, ...] = ()
    created_at: datetime
    expires_at: datetime | None = None


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    request_id: str
    operation_id: OperationId
    operation_version: int
    canonical_payload_hash: str
    action: ApprovalAction
    feedback: str | None = None
    modified_parameters: object | None = None
    resolved_at: datetime
    approval_principal_ref: str
    authorization_context_ref: str | None = None


class ExternalReference(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider: str
    reference: str


class ActiveJobRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    job_id: str
    operation_id: OperationId
    job_type: str
    target: str
    submitted_at: datetime
    status: OperationStatus
    submission_attempt: int = Field(ge=1)
    external_reference: ExternalReference | None = None


class ExecutionEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    sequence: int = Field(ge=1)
    event_type: str
    node: str
    operation_id: OperationId | None = None
    stage: StageName | None = None
    timestamp: datetime
    duration_ms: float | None = Field(default=None, ge=0)
    summary: Mapping[str, object] = {}
    redaction_version: str
    error_code: str | None = None


class AgentState(BaseModel):
    model_config = ConfigDict(frozen=True)
    checkpoint_schema_version: int = Field(default=1, ge=1)
    session_id: str
    thread_id: str
    status: LifecycleStatus
    current_stage: StageName | None = None
    task: str | None = None
    task_type: str | None = None
    parameters: Mapping[str, object] = {}
    selected_capabilities: tuple[str, ...] = ()
    active_skill: str | None = None
    active_provider: str | None = None
    active_model: str | None = None
    total_tokens_used: int = Field(default=0, ge=0)
    pending_approval: ApprovalRequest | None = None
    approval_history: tuple[ApprovalResponse, ...] = ()
    active_jobs: tuple[ActiveJobRef, ...] = ()
    active_operation: OperationIntent | None = None
    execution_lease: ExecutionLease | None = None
    sequence: int = Field(default=0, ge=0)
    events: tuple[ExecutionEvent, ...] = ()
    memory_ref: str | None = None
    experiment_ref: str | None = None


@dataclass(frozen=True)
class OrchestratorConfig:
    workspace_root: Path
    checkpoint_db_path: Path
    enable_persistence: bool = True
    max_step_limit: int = 100
    checkpoint_retention_days: int = 30
    wal_mode: bool = True
    lease_ttl_seconds: int = 60
    max_retries: int = 3


class OperationReconciler(Protocol):
    def reconcile(self, operation: OperationIntent) -> OperationStatus: ...


class OrchestrationStore(Protocol):
    def acquire_lease(self, session_id: str, owner_instance_id: str) -> ExecutionLease: ...
    def release_lease(self, lease: ExecutionLease) -> None: ...
    def load_state(self, thread_id: str) -> AgentState | None: ...
    def save_state(self, state: AgentState, lease: ExecutionLease) -> None: ...
    def find_operation(self, operation_id: OperationId) -> OperationIntent | None: ...


def get_checkpointer(config: OrchestratorConfig): ...


def build_graph(config: OrchestratorConfig): ...


def resume_session(
    graph,
    session_id: str,
    approval_response: ApprovalResponse | None = None,
) -> AgentState: ...


def prune_checkpoints(
    config: OrchestratorConfig,
    older_than_days: int | None = None,
) -> int: ...
```

## 6. Architectural invariants

1. **Persistence separation:** runtime checkpoints and permanent experiment records use separate stores and lifecycles.
2. **State immutability:** authoritative persisted records are immutable and schema-versioned.
3. **Session ownership:** only the current lease/fencing epoch may mutate a session.
4. **Canonical identity:** executable operation identity is stable, deterministic, and hash-versioned.
5. **Typed payloads:** authoritative operation payloads are schema envelopes, never unconstrained mappings.
6. **Approval binding:** approval is valid only for the exact operation version/hash/policy context it authorizes.
7. **No false exactly-once:** orchestration never claims guarantees unsupported by the external target.
8. **Reconciliation first:** ambiguous external effects are reconciled before retry or redispatch.
9. **Explicit unknown:** unresolved external state remains explicit and cannot be silently treated as success/failure.
10. **Transition authority:** lifecycle changes occur only through validated state transitions.
11. **Per-session ordering:** event sequence is authoritative per session; cross-session order is not implied.
12. **Replay honesty:** state recovery is deterministic where specified; external/LLM side effects are not claimed deterministic.
13. **Redaction:** secrets, credentials, raw sensitive prompts, and large binary payloads never enter persistent history.
14. **Bounded execution:** step/retry policies prevent unbounded orchestration loops.
15. **Safe retention:** pruning cannot remove state required for approval, execution, reconciliation, migration, or audit.
16. **Boundary integrity:** ADR-0003 does not absorb provider, tool, dataset, training, evaluation, deployment, or monitoring ownership.

## 7. Acceptance criteria

The eventual runtime implementation MUST demonstrate at least:

- identical canonical operation payloads generate identical hashes across repeated processes;
- semantically different executable payloads cannot collide except at cryptographic-hash failure assumptions;
- stale lease/fencing mutations are rejected;
- an ambiguous dispatch enters reconciliation and cannot blindly redispatch;
- unresolved reconciliation reaches an explicit operator/policy decision state;
- modified approval requests invalidate previous approvals;
- illegal lifecycle transitions are rejected;
- per-session event ordering remains monotonic under concurrent worker attempts;
- checkpoint schema migration rejects unsupported versions safely;
- pruning never removes a checkpoint required by an active or recoverable operation;
- persistent history is bounded and redacted;
- no runtime dependency on a specific future checkpointer is required by the architectural contract.

## 8. Consequences

- Durable orchestration survives process termination while remaining separate from permanent experiment history.
- External effects have explicit identity, approval binding, and crash reconciliation semantics.
- Stale workers cannot mutate sessions after lease ownership changes.
- Future execution targets may provide stronger idempotency/reconciliation without changing the orchestration contract.
- The architecture deliberately imposes more metadata and validation than an in-memory workflow; this cost is required for reproducibility, auditability, and safe recovery.

## 9. Revisit triggers

Revisit this ADR only if the platform changes its authoritative orchestration engine, requires a materially different persistence/concurrency model, adopts a distributed execution architecture that invalidates the local-session ownership assumptions, or introduces a target class whose effect semantics cannot be represented by the operation/reconciliation contract. A larger number of providers, tools, GPUs, or workflows alone is not grounds for weakening these invariants.
