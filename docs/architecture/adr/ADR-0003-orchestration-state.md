# ADR-0003: Orchestration State Machine, Checkpointing, and Persistent Approvals

- **Status:** Accepted
- **Amendment Status:** Frozen
- **Date:** 2026-09-02
- **Last Reviewed:** 2026-09-09
- **Layer:** orchestration
- **Canon:** `[P§6]`, `[P§21]`, `[P§24]`, `[P§25]`, `[P§29.8]`, `[P§33]`, `[P§34]`
- **Issue:** #18 (freeze review)

## 1. Context

`[P§21]` establishes LangGraph as the authoritative orchestration layer for workflow state,
branching, iteration, approvals, retries, checkpoints, and multi-step coordination. ADR-0003
uses durable SQLite checkpoints at `.cv_agent/state/checkpoints.sqlite`, separate from the
permanent experiment ledger at `.cv_agent/state/experiments.sqlite` owned by ADR-0004.

A checkpoint transaction cannot be atomic with a remote process, cloud service, LLM provider,
or MCP execution. Effectful work therefore requires stable operation identity, explicit approval
binding, and reconciliation rather than blind replay.

## 2. Responsibility (`[P§34]`)

**Owns:** workflow topology, orchestration state, lifecycle transitions, checkpoint persistence
and restoration, approval interrupt/resume, operation identity, session ownership, event ordering,
and orchestration-level retry/recovery policy.

**Does not own:** LLM providers (ADR-0002), capability identity (ADR-0001), procedural skills
(ADR-0007), tool transport/execution (ADR-0005), permanent memory (ADR-0004), dataset storage
(ADR-0009), training (ADR-0010), evaluation (ADR-0011), optimization/deployment (ADR-0012), or
production monitoring/active learning (ADR-0013).

## 3. Decision

Use a LangGraph `StateGraph` with durable SQLite checkpoint persistence. `MemorySaver` is
permitted only for isolated tests. Public orchestration semantics MUST remain independent of
the checkpoint backend so a future server-side checkpointer can replace SQLite without changing
operation, approval, or state semantics.

### 3.1 Persistence separation

```text
.cv_agent/state/
├── checkpoints.sqlite   ← ADR-0003 runtime orchestration state
└── experiments.sqlite   ← ADR-0004 permanent experiment ledger
```

Checkpoint state is runtime state. Experiment facts are handed to ADR-0004 by stable references.
Non-terminal or recoverable sessions MUST NOT be pruned.

### 3.2 Authoritative state model

Persisted orchestration records MUST be immutable value objects and carry a
`checkpoint_schema_version`. Optional values are explicit nullable fields. Arbitrary
`dict[str, Any]` state is prohibited; extension data MUST use a versioned schema envelope.
Timestamps are timezone-aware UTC instants. Large artifacts, secrets, SDK objects, sockets,
clients, and file handles MUST NOT be persisted in `AgentState`.

### 3.3 Session ownership and fencing

`SessionId` identifies a user-visible session and `ThreadId` identifies its LangGraph thread.
A session retains one stable thread identity across recovery.

A running session is protected by `ExecutionLease` containing `lease_id`, session/thread IDs,
`owner_instance_id`, expiration timestamps, and a monotonically increasing `fencing_epoch`.
Every state mutation MUST carry the current lease and fencing epoch. A stale epoch MUST be
rejected. Lease renewal is compare-and-swap against the current lease/epoch. A worker that
loses fencing MUST stop mutating state.

SQLite file locking is a storage mechanism, not the application-level concurrency contract.
Independent sessions may run concurrently; mutation of one session is serialized by fencing.

### 3.4 Canonical operation identity

Every effectful action receives an immutable logical `OperationId` before dispatch. The
executable identity is:

```text
OperationIdentity =
    operation_id + operation_version + canonical_payload_hash + target_identity
```

Canonicalization MUST be deterministic and versioned. The canonical form MUST define UTF-8
encoding, Unicode normalization, object-key ordering, representation of null/default/numeric
values, deterministic sequence/set representation, inclusion of all execution-relevant
parameters and artifact references, exclusion of observational metadata, a canonicalization
algorithm version, and a cryptographic hash algorithm/version.

Equivalent executable requests MUST produce the same canonical representation and hash.
Materially different executable intent MUST produce a different version and/or hash.

### 3.5 Typed operation payloads

Authoritative operation payloads use a versioned schema envelope rather than an unconstrained
mapping:

```python
class OperationPayloadEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_id: str
    schema_version: int
    canonicalization_version: str
    payload_hash: str
    payload: object
```

Concrete payload schemas remain owned by the subsystem performing the operation. ADR-0003 owns
identity, versioning, canonicalization, approval binding, persistence, and recovery semantics.

### 3.6 External side-effect protocol

Effectful operations follow:

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

Exactly-once execution is never claimed universally. If a target lacks idempotency or
reconciliation, orchestration exposes explicit `UNKNOWN` semantics.

### 3.7 Recovery and reconciliation authority

Orchestration owns the recovery state; the owning execution subsystem supplies target-specific
status/query mechanisms.

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

`UNRESOLVED` MUST NOT be silently mapped to `FAILED`, `COMPLETED`, or a fresh dispatch.
Repeated reconciliation failures are bounded and preserve the last known evidence.

### 3.8 Approval validity and evidence

An approval binds to one operation version, canonical payload hash, policy version, and resource
scope. It is valid only while all bound values match.

Approval evidence MUST preserve request identity, operation identity/version/hash, risk/policy
classification, requested resource scope/cost, authenticated principal reference,
authorization-context reference, creation/expiry/resolution timestamps, and final decision.

`MODIFIED`, expired, superseded, rejected, or hash-mismatched approvals MUST fail closed.
Approval policy remains owned by `docs/APPROVALS.md` and the governing subsystem.

### 3.9 Authoritative state transitions

Every lifecycle transition is validated as:

```text
(CurrentState, Event, Preconditions, Authority) → NextState
```

Illegal transitions MUST be rejected. Persisted transitions record prior state identity, event
identity, resulting state, sequence number, actor/lease, and schema version. Nodes MUST NOT
mutate lifecycle state through ad-hoc field assignment.

Session lifecycle:

```text
INITIALIZING → READY → PLANNING → AWAITING_APPROVAL → EXECUTING → EVALUATING
                                                          ↓
                                               COMPLETED / FAILED / PAUSED
```

External operation lifecycle:

```text
CREATED → VALIDATED → APPROVAL_REQUIRED → DISPATCHING → SUBMITTED → RUNNING
                                                               ↓
                    COMPLETED / FAILED / CANCELLED
                                                               ↓
                                      UNKNOWN → RECONCILING → terminal/decision
```

`CANCEL_REQUESTED`, `UNKNOWN`, `RECONCILING`, `LOST`, and `RECOVERY_REQUIRED` are explicit
non-success/recovery states and MUST never be silently treated as success.

### 3.10 Event ordering

Execution events are append-only and ordered per session. Each session has one authoritative
sequence domain, and sequence allocation is transactional with the state transition. Lease
fencing prevents concurrent workers from allocating conflicting sequence numbers.

Cross-session global ordering is not guaranteed. Wall-clock timestamps are audit metadata only
and MUST NOT define authoritative event order.

### 3.11 Replay and nondeterminism

The architecture guarantees deterministic checkpoint deserialization, deterministic transition
validation, replay-safe existing operation identities, reconciliation before retry, and immutable
per-session event ordering.

It does NOT claim byte-for-byte deterministic re-execution of LLM calls, remote tools, wall-clock
observations, or external systems. Nondeterministic observations are retained by reference or
redacted metadata sufficient for the applicable audit/reproduction policy.

### 3.12 Retry semantics

- Deterministic validation/configuration errors: no automatic retry.
- Transient transport errors: bounded exponential backoff with jitter.
- Ambiguous effectful dispatch: reconcile first; never blind retry.
- Remote job failure: defer to the owning subsystem's policy.
- Approval rejection/expiry: no automatic retry.
- Stale lease/fencing failure: terminate stale worker and reacquire ownership.
- Repeated failure beyond policy: terminal `FAILED` with structured diagnostics.

Retrying does not create a new semantic operation unless executable parameters change.

### 3.13 Checkpoint durability and migration

SQLite checkpoints SHOULD use WAL where supported and an explicitly configured durability mode.
WAL is not treated as universal durability. Every checkpoint carries `checkpoint_schema_version`.
Supported retained versions require explicit migration rules; unsupported versions fail closed.

Integrity failures MUST NOT silently restore arbitrary data. Recovery selects the newest valid
checkpoint supported by the integrity contract; if none is valid, the session enters
`RECOVERY_REQUIRED` with diagnostic evidence preserved outside the corrupt record where possible.

### 3.14 Safe retention

`prune_checkpoints()` MUST NOT delete checkpoints required by approvals, active or ambiguous
operations, unresolved recovery, migration, or audit procedures. Pruning MUST be idempotent and
MUST never modify `experiments.sqlite`.

Retention duration is configurable policy, not an architectural invariant.

## 4. Interface

```python
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

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
    risk_level: str
    created_at: datetime
    expires_at: datetime | None = None


class OrchestrationStore(Protocol):
    def acquire_lease(
        self, session_id: str, owner_instance_id: str
    ) -> ExecutionLease: ...

    def release_lease(self, lease: ExecutionLease) -> None: ...

    def load_state(self, thread_id: str) -> object | None: ...

    def save_state(self, state: object, lease: ExecutionLease) -> None: ...

    def find_operation(self, operation_id: OperationId) -> OperationIntent | None: ...


class OperationReconciler(Protocol):
    def reconcile(self, operation: OperationIntent) -> OperationStatus: ...


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
```

## 5. Architectural invariants

1. Runtime checkpoints and permanent experiment records are separate stores.
2. Authoritative persisted state is immutable and schema-versioned.
3. Only the current lease/fencing epoch may mutate a session.
4. Operation identity and canonical hashing are deterministic and versioned.
5. Authoritative operation payloads are typed schema envelopes.
6. Approval is bound to exact operation/policy context and fails closed on mismatch.
7. No universal exactly-once guarantee is claimed for external effects.
8. Ambiguous effects are reconciled before retry or redispatch.
9. Unresolved external state remains explicitly unresolved.
10. Lifecycle changes use an authoritative transition function.
11. Event order is authoritative per session, not globally.
12. Replay claims cover state recovery, not nondeterministic external re-execution.
13. Persistent history is bounded and redacted.
14. Retry and step policies prevent unbounded orchestration loops.
15. Pruning cannot remove state required for recovery, approval, execution, migration, or audit.
16. ADR-0003 does not absorb ownership from the other accepted ADRs.

## 6. Acceptance criteria

Phase 2 implementation MUST prove deterministic canonical hashing, stale-fencing rejection,
reconciliation-first recovery, approval invalidation on modification, illegal-transition
rejection, monotonic per-session event ordering, safe checkpoint migration/corruption handling,
retention safety, and bounded/redacted persistent history.

## 7. Consequences and revisit triggers

The design increases metadata and validation overhead in exchange for reproducibility,
auditability, and crash-safe recovery. Future execution targets may provide stronger
idempotency/reconciliation without changing this contract.

Revisit only if the authoritative orchestration engine, persistence/concurrency model, or
external-effect semantics materially change. Additional providers, tools, GPUs, or workflows
alone do not justify weakening these invariants.
