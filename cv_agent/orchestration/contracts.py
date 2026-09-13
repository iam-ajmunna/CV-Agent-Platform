"""cv_agent.orchestration.contracts — Authoritative contracts for ADR-0003.

Defines:
- LifecycleStatus and OperationStatus enums.
- OperationId, OperationPayloadEnvelope, OperationIntent.
- ExecutionLease with fencing_epoch.
- ApprovalRequest and ApprovalDecision with security tuple binding (ADR-0003 §3.8).
- OrchestrationStore and OperationReconciler protocols.
- OrchestratorConfig frozen configuration dataclass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class LifecycleStatus(str, Enum):
    """Authoritative session lifecycle states per ADR-0003 §4."""

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


class OperationStatus(str, Enum):
    """Authoritative external operation execution states per ADR-0003 §4."""

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


class SessionId(BaseModel):
    """Validated user-visible session identifier."""

    model_config = ConfigDict(frozen=True)
    value: str = Field(min_length=1, pattern=r"^[a-z0-9_.-]+$")

    def __str__(self) -> str:
        return self.value


class ThreadId(BaseModel):
    """Validated LangGraph thread identifier."""

    model_config = ConfigDict(frozen=True)
    value: str = Field(min_length=1, pattern=r"^[a-z0-9_.-]+$")

    def __str__(self) -> str:
        return self.value


class OperationId(BaseModel):
    """Immutable unique logical identity of an executable operation."""

    model_config = ConfigDict(frozen=True)
    value: str = Field(min_length=1)

    def __str__(self) -> str:
        return self.value


class OperationPayloadEnvelope(BaseModel):
    """Versioned schema envelope wrapping typed operation intent."""

    model_config = ConfigDict(frozen=True)
    schema_id: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    canonicalization_version: str = Field(min_length=1)
    payload_hash: str = Field(min_length=1)
    payload: object


class OperationIntent(BaseModel):
    """Authoritative, dispatch-ready operation intent."""

    model_config = ConfigDict(frozen=True)
    operation_id: OperationId
    operation_version: int = Field(ge=1)
    operation_type: str = Field(min_length=1)
    canonical_payload: OperationPayloadEnvelope
    target_identity: str = Field(min_length=1)
    created_at: datetime


class ExecutionLease(BaseModel):
    """Session ownership lease with monotonically increasing CAS fencing epoch."""

    model_config = ConfigDict(frozen=True)
    lease_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    owner_instance_id: str = Field(min_length=1)
    acquired_at: datetime
    expires_at: datetime
    fencing_epoch: int = Field(ge=1)


class ApprovalStatus(str, Enum):
    """Status of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class ApprovalRequest(BaseModel):
    """Persistent approval request bound to operation identity, hash, and policy scope."""

    model_config = ConfigDict(frozen=True)
    request_id: str = Field(min_length=1)
    operation_id: OperationId
    operation_version: int = Field(ge=1)
    canonical_payload_hash: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    action_type: str = Field(min_length=1)
    risk_level: str = Field(min_length=1)
    resource_scope: str = Field(default="default", min_length=1)
    created_at: datetime
    expires_at: datetime | None = None


class ApprovalDecision(BaseModel):
    """Resolved decision cryptographically bound to the approval security tuple."""

    model_config = ConfigDict(frozen=True)
    request_id: str = Field(min_length=1)
    decision: ApprovalStatus
    principal: str = Field(min_length=1)
    resolved_at: datetime
    bound_payload_hash: str = Field(min_length=1)
    bound_resource_scope: str = Field(min_length=1)
    bound_policy_version: str = Field(min_length=1)
    decision_metadata: Mapping[str, Any] = Field(default_factory=dict)


class OrchestrationEvent(BaseModel):
    """Monotonically ordered, immutable per-session audit event."""

    model_config = ConfigDict(frozen=True)
    session_id: str = Field(min_length=1)
    sequence_number: int = Field(ge=1)
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    timestamp: datetime
    payload: Mapping[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class OrchestratorConfig:
    """Runtime configuration parameters for ADR-0003 Orchestration Subsystem."""

    workspace_root: Path
    checkpoint_db_path: Path
    enable_persistence: bool = True
    max_step_limit: int = 100
    checkpoint_retention_days: int = 30
    wal_mode: bool = True
    lease_ttl_seconds: int = 60
    max_retries: int = 3


class OrchestrationStore(Protocol):
    """Durable persistence contract for leases, checkpoints, events, operations, and approvals."""

    def acquire_lease(
        self, session_id: str, thread_id: str, owner_instance_id: str
    ) -> ExecutionLease: ...

    def renew_lease(self, lease: ExecutionLease) -> ExecutionLease: ...

    def release_lease(self, lease: ExecutionLease) -> None: ...

    def load_state(self, thread_id: str) -> object | None: ...

    def save_state(self, state: object, lease: ExecutionLease) -> None: ...

    def find_operation(self, operation_id: OperationId) -> OperationIntent | None: ...

    def save_operation(
        self, operation: OperationIntent, lease: ExecutionLease
    ) -> None: ...

    def update_operation_status(
        self, operation_id: OperationId, status: OperationStatus, lease: ExecutionLease
    ) -> None: ...

    def record_event(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        lease: ExecutionLease,
    ) -> OrchestrationEvent: ...

    def list_events(
        self, session_id: str, since_sequence: int | None = None
    ) -> Sequence[OrchestrationEvent]: ...

    def prune_checkpoints(self, older_than: datetime) -> int: ...


class OperationReconciler(Protocol):
    """Target-specific protocol for querying external side-effect status."""

    def reconcile(self, operation: OperationIntent) -> OperationStatus: ...
