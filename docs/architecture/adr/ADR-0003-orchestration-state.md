# ADR-0003: Orchestration State Machine, Checkpointing, and Persistent Approvals

- **Status:** Accepted
- **Amendment Status:** Proposed
- **Date:** 2026-09-02
- **Layer:** orchestration
- **Canon:** `[P§6]`, `[P§21]`, `[P§24]`, `[P§25]`, `[P§29.8]`, `[P§33]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #16 (amendment)

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

Deep architectural review identified an additional distributed-systems boundary: a local
SQLite transaction cannot be atomic with an external process, remote host, cloud service,
LLM provider, or MCP execution. Therefore orchestration must model effectful operations as
versioned intents with stable identities and explicit reconciliation rather than assuming
checkpoint commit and external side effect form one transaction.

## 2. Responsibility (`[P§34]`)

- **This owns:** workflow graph topology, orchestration state, lifecycle transitions,
  node-to-node propagation, checkpoint persistence/restoration, approval interrupt/resume,
  operation identity and recovery protocol, session ownership, execution-event sequencing,
  and orchestration-level retry/recovery policy.
- **This does NOT own:**
  - LLM provider management or provider APIs → ADR-0002;
  - domain reasoning and hypothesis generation → ADR-0008;
  - capability catalogue and authoritative capability/tool identities → ADR-0001;
  - procedural domain instructions → ADR-0007;
  - executable tool semantics and transport → ADR-0005;
  - permanent project memory and experiment ledger → ADR-0004;
  - dataset storage/versioning → ADR-0009;
  - training execution → ADR-0010;
  - offline model/system evaluation → ADR-0011;
  - optimization/deployment runtime → ADR-0012;
  - production monitoring/active learning → ADR-0013.

## 3. Decision

We use a LangGraph `StateGraph` with durable SQLite checkpoint persistence at
`.cv_agent/state/checkpoints.sqlite`. `MemorySaver` is permitted only for isolated tests.
The architecture is deliberately portable above the checkpoint backend so future migration
to a server-side checkpointer does not alter operation, approval, or state semantics.

### 3.1 Dual-SQLite separation

```text
.cv_agent/state/
├── checkpoints.sqlite   ← ADR-0003 runtime checkpoint persistence
│                           retention governed; survives process termination
└── experiments.sqlite   ← ADR-0004 permanent experiment ledger
                            immutable historical experiment record
```

Checkpoint state is runtime state, not permanent domain memory. Experiment facts are handed
to ADR-0004 and retained only through stable references in orchestration state.

Checkpoint retention is policy-driven. Non-terminal or recoverable sessions MUST NOT be
pruned. Pruning is permitted only when the session is terminal, no unresolved approval or
external operation depends on the checkpoint, and the configured retention policy allows it.

### 3.2 State model invariants

`AgentState` is a transport/state envelope for LangGraph and MUST NOT contain arbitrary
provider SDK objects, tool clients, sockets, file handles, raw secrets, model artifacts, or
domain payloads.

The implementation contract is:

- all persisted domain records are immutable value objects (`frozen=True` Pydantic v2 models
  or an equivalent explicitly immutable representation);
- every persisted state has a `checkpoint_schema_version`;
- optional fields are explicit nullable fields rather than `TypedDict(total=False)` semantics;
- unbounded `dict[str, Any]` is prohibited in authoritative state; extension data must use a
  versioned, schema-validated envelope;
- execution history is append-only and sequence-numbered;
- state contains references to large artifacts, never artifact payloads;
- timestamps are timezone-aware UTC instants;
- serialized state is bounded and JSON/msgpack-compatible according to the selected
  LangGraph checkpointer contract.

### 3.3 Session and ownership identity

`SessionId` identifies the user-visible orchestration session. `ThreadId` identifies the
LangGraph execution thread. A session has exactly one stable thread identity for its lifetime;
recovery MUST reuse it.

A running session has an `ExecutionLease` containing:

- `lease_id` — unique ownership token;
- `session_id` / `thread_id`;
- `owner_instance_id`;
- `acquired_at` and `expires_at`;
- monotonically increasing `fencing_epoch`.

Only the current lease holder may advance a session. SQLite file locking is an implementation
mechanism, not the application-level concurrency contract. Stale workers MUST be rejected by
fencing checks. Independent sessions may execute concurrently.

### 3.4 Operation identity and idempotency

Every externally effectful action receives an immutable `OperationId` before dispatch.
An operation is identified by:

```text
OperationId
 + operation_version
 + canonical_payload_hash
 + target_identity
```

The canonical payload hash covers every execution-relevant parameter, selected tool/capability,
target, dataset/model/artifact references, and policy-relevant configuration. Equivalent
requests produce the same canonical representation; materially different requests produce a
different operation version/hash.

An operation MUST progress through an explicit orchestration record rather than relying on
node replay to infer whether the external side effect occurred.

### 3.5 External side-effect protocol and crash recovery

SQLite checkpoint atomicity does not imply atomicity with external execution. Effectful actions
therefore use this protocol:

```text
CREATE OperationIntent
        ↓
VALIDATE + policy checks
        ↓
APPROVAL (if required)
        ↓
PERSIST dispatch-ready operation
        ↓
DISPATCH using OperationId / idempotency key
        ↓
PERSIST external reference / dispatch result
        ↓
RECONCILE external state after crash or timeout
        ↓
TERMINAL operation state
```

If the process crashes after dispatch but before checkpointing the result, recovery MUST first
reconcile the external target using `OperationId`, external reference, or target-specific
idempotency semantics. It MUST NOT blindly redispatch.

If reconciliation cannot establish whether the side effect occurred, the operation enters
`UNKNOWN` and requires target-specific reconciliation or an explicit policy/operator decision.

Exactly-once execution is NOT claimed where the external target cannot provide idempotency or
reconciliation. The orchestration guarantee is **at-most-once dispatch attempt when the target
supports idempotency**, otherwise replay-safe recovery with explicit `UNKNOWN` semantics.

### 3.6 Approval contract

An `ApprovalRequest` is bound to exactly one operation version and canonical payload hash.
Approval is valid only when the request, operation version, payload hash, policy context, and
resource scope still match.

`MODIFIED` is never executable approval. It creates a new operation version and payload hash,
invalidates the previous approval, and returns the operation to the approval-required state.

Approval records contain:

- `request_id`;
- `operation_id` and `operation_version`;
- `canonical_payload_hash`;
- policy/risk classification;
- requested resources and cost estimate;
- creation/expiry timestamps;
- authenticated approval principal reference;
- final decision and decision timestamp.

Approvals are single-use for the bound operation version unless an explicit policy says
otherwise. Expired, superseded, or hash-mismatched approvals MUST fail closed.

### 3.7 Lifecycle state machine

Session lifecycle remains:

```text
INITIALIZING → READY → PLANNING → AWAITING_APPROVAL → EXECUTING → EVALUATING
                                                        ↓
                                             COMPLETED / FAILED / PAUSED
```

External operation lifecycle is independent:

```text
CREATED → VALIDATED → APPROVAL_REQUIRED → DISPATCHING → SUBMITTED → RUNNING
                                                               ↓
                         ┌────────────── COMPLETED / FAILED / CANCELLED
                         │
                         └── UNKNOWN → RECONCILING → terminal state
```

`CANCEL_REQUESTED`, `UNKNOWN`, `RECONCILING`, and `LOST` are explicit non-success states and
must never be silently mapped to `FAILED` or `COMPLETED`.

### 3.8 Replay and recovery semantics

The architecture does **not** claim deterministic re-execution of LLM calls, remote tools,
wall-clock observations, or external systems. It guarantees:

1. deterministic deserialization of a valid checkpoint;
2. deterministic state-transition validation;
3. replay-safe handling of already-created operation identities;
4. reconciliation before retrying unresolved external effects;
5. immutable event ordering through sequence numbers.

LLM responses and other nondeterministic external observations are recorded by reference or
redacted metadata when needed for audit/reproduction; replay may reproduce the orchestration
state without reproducing the original external response byte-for-byte.

### 3.9 Event history and step limits

Every node execution and operation transition emits an append-only `ExecutionEvent` with a
monotonic sequence number. `step_count` is derived from the authoritative sequence rather than
maintained as an independent mutable counter.

`max_step_limit` counts orchestration node/super-step executions, not remote job duration.
Retries consume steps unless explicitly classified as transport-level retry events. Exhaustion
transitions the session to a defined terminal/recovery state and prevents an infinite loop.

Inputs and outputs stored in event summaries are bounded, structured, and automatically
redacted. Secrets, credentials, authorization tokens, raw prompts containing secrets, and large
binary payloads MUST NOT enter persistent orchestration history.

### 3.10 Retry semantics

Retry policy is explicit by failure class:

- deterministic validation/configuration errors: no automatic retry;
- transient transport errors: bounded retry with exponential backoff and jitter;
- effectful operation after ambiguous dispatch: reconcile first, never blind retry;
- remote job failure: defer to the owning subsystem's job policy;
- approval rejection/expiry: no automatic retry;
- stale lease/fencing failure: abort the stale worker and reacquire through the session owner;
- repeated failure beyond policy limit: terminal `FAILED` with structured diagnostics.

Retry policy MUST be represented independently from operation identity so retrying an operation
does not create a semantically different operation unless parameters change.

### 3.11 Checkpoint durability, versioning, and corruption

SQLite checkpoints use WAL where supported and a configured durability mode appropriate to the
local reliability requirement. The architecture does not equate WAL with universal durability;
filesystem/storage failures remain possible.

Each checkpoint carries `checkpoint_schema_version`. Version migrations are explicit and
backward compatibility is required for all supported retained checkpoints. Unsupported schema
versions fail closed and require migration or operator intervention.

Checkpoint integrity failures MUST NOT silently restore arbitrary data. Recovery attempts the
latest valid checkpoint according to the checkpointer's integrity guarantees; otherwise the
session becomes `RECOVERY_REQUIRED`/`FAILED` with diagnostic evidence preserved outside the
corrupt record where possible.

### 3.12 Safe retention and pruning

`prune_checkpoints()` MUST NOT delete checkpoints needed by:

- `AWAITING_APPROVAL` or expired-but-unresolved approvals;
- `RUNNING`, `SUBMITTED`, `DISPATCHING`, `UNKNOWN`, `LOST`, or `RECONCILING` operations;
- sessions whose recovery lease/state has not reached a safe terminal condition;
- migrations or audit procedures explicitly retaining a checkpoint.

The default retention duration is a configurable policy, not an architectural invariant.
Pruning MUST be idempotent and MUST never modify `experiments.sqlite`.

## 4. Alternatives considered

| Alternative | Why rejected |
|---|---|
| MemorySaver only | Cannot survive process termination or persistent approvals. |
| Single SQLite database | Blurs runtime checkpoint and permanent experiment-ledger lifecycles. |
| Custom FSM/Celery/Airflow | Conflicts with the mandated LangGraph orchestration boundary and adds unnecessary infrastructure for the local agent. |
| PostgreSQL/Redis as default | Enterprise concurrency is useful but violates the current local, repository-scoped zero-service baseline. It remains a future migration option. |
| Blind retry after crash | Cannot distinguish dispatch success from checkpoint failure and can duplicate expensive/destructive external effects. |

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


class OperationIntent(BaseModel):
    model_config = ConfigDict(frozen=True)
    operation_id: OperationId
    operation_version: int = Field(ge=1)
    operation_type: str = Field(min_length=1)
    canonical_payload_hash: str = Field(min_length=1)
    target_identity: str = Field(min_length=1)
    payload: Mapping[str, object]
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
    modified_parameters: Mapping[str, object] | None = None
    resolved_at: datetime
    approval_principal_ref: str


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

## 6. Consequences

- Durable orchestration survives process termination while remaining separate from permanent
  experiment history.
- External side effects have explicit identity, approval binding, and crash reconciliation.
- Stale workers cannot mutate sessions after lease ownership changes.
- Approval modification cannot accidentally reuse authorization for a different operation.
- Replay semantics are honest about nondeterministic external systems and LLM responses.
- State is versioned and bounded, making future checkpointer/backend migration safer.
- The additional operation/lease/event machinery increases implementation complexity, but this
  complexity is necessary at the orchestration boundary for expensive, remote, and destructive
  operations.

## 7. Complete Acceptance Criteria

1. **State schema:** persisted state validates against a versioned immutable schema; arbitrary
   `Any` and mutable authoritative records are rejected.
2. **Lifecycle:** invalid lifecycle and operation transitions are rejected explicitly.
3. **Persistence:** checkpoints survive process termination and preserve schema version.
4. **Approval:** approval requires exact operation/version/hash matching; modification creates
   a new version and requires re-approval; expired approvals fail closed.
5. **Idempotency:** replay of a checkpoint containing an existing `OperationId` does not blindly
   dispatch the same external effect again.
6. **Crash recovery:** a crash between external dispatch and checkpoint persistence triggers
   reconciliation before retry; unresolved status becomes `UNKNOWN` rather than guessed success.
7. **Lease fencing:** a stale execution owner cannot commit state after lease loss.
8. **Concurrency:** one active owner exists per session while independent sessions remain
   concurrently executable.
9. **Event history:** sequence numbers are monotonic and execution history is append-only.
10. **Redaction:** secrets and oversized payloads cannot enter persistent execution history.
11. **Retry:** transient failures retry within bounded policy; ambiguous effectful failures
    reconcile before retry.
12. **Checkpoint integrity:** unsupported schema versions and corruption fail closed with
    recovery diagnostics.
13. **Retention:** non-terminal/recoverable sessions are never pruned; pruning is idempotent
    and isolated from ADR-0004's experiment ledger.
14. **Boundary compliance:** orchestration imports neither provider SDKs nor domain execution
    implementations and delegates executable work to ADR-0005/subsystems.
15. **Portability:** orchestration contracts remain valid if the SQLite checkpointer is later
    replaced by a server-side backend.

## 8. Revisit triggers

Reopen this decision if:

1. shared multi-user/cloud orchestration becomes a baseline requirement;
2. checkpoint volume exceeds the practical local SQLite envelope;
3. the external execution ecosystem cannot provide sufficient idempotency/reconciliation and a
   durable operation broker becomes necessary;
4. LangGraph changes break the required state/checkpoint/interrupt contracts;
5. regulatory or security requirements require tamper-evident external audit storage;
6. orchestration becomes distributed across independent agent processes and requires a durable
   coordinator beyond local session leases.

## 9. Amendment freeze rule

This amendment is **not frozen** until the PM explicitly approves the concrete contract changes.
Once approved, the amendment must be merged as a separate architectural change, CI-validated,
and recorded in the decision ledger. Phase 2 runtime implementation remains paused until the
amended ADR is frozen.