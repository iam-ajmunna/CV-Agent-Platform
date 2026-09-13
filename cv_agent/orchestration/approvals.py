"""cv_agent.orchestration.approvals — Persistent human approval gate and evidence binding.

Implements ADR-0003 §3.8 and PM Gate 5:
- Approvals bind strictly to the 4-tuple:
  (operation_version, canonical_payload_hash, policy_version, resource_scope).
- Hash equality alone is insufficient: scope, policy, and version must also match.
- Modified, expired, rejected, or mismatched approvals fail closed with ApprovalSecurityError.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from cv_agent.orchestration.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ExecutionLease,
    OperationIntent,
)
from cv_agent.orchestration.store import SQLiteOrchestrationStore, _parse_iso


class ApprovalSecurityError(Exception):
    """Raised when an approval check breaches security or evidence binding constraints."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class ApprovalManager:
    """Manages persistent approval requests and enforces the 4-tuple security binding."""

    def __init__(self, store: SQLiteOrchestrationStore) -> None:
        self.store = store

    def create_request(
        self,
        operation: OperationIntent,
        action_type: str,
        risk_level: str,
        policy_version: str,
        resource_scope: str = "default",
        lease: ExecutionLease | None = None,
        expires_at: datetime | None = None,
    ) -> ApprovalRequest:
        """Create and persist a new approval request bound to operation intent."""
        if lease is None:
            raise ApprovalSecurityError(
                "LEASE_REQUIRED",
                "An active ExecutionLease is required to create an approval request.",
            )

        request_id = f"appr-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)

        req = ApprovalRequest(
            request_id=request_id,
            session_id=operation.session_id,
            operation_id=operation.operation_id,
            operation_version=operation.operation_version,
            canonical_payload_hash=operation.canonical_payload.payload_hash,
            policy_version=policy_version,
            action_type=action_type,
            risk_level=risk_level,
            resource_scope=resource_scope,
            created_at=now,
            expires_at=expires_at,
        )

        self.store.record_approval_request(req, lease)
        return req

    def record_decision(
        self,
        request_id: str,
        decision: ApprovalStatus,
        principal: str,
        lease: ExecutionLease,
        decision_metadata: Mapping[str, Any] | None = None,
    ) -> ApprovalDecision:
        """Record an approval resolution bound to the exact security tuple."""
        raw = self.store.find_approval(request_id)
        if raw is None:
            raise ApprovalSecurityError(
                "APPROVAL_NOT_FOUND",
                f"Approval request {request_id!r} does not exist.",
            )

        now = datetime.now(timezone.utc)
        resolved = ApprovalDecision(
            request_id=request_id,
            decision=decision,
            principal=principal,
            resolved_at=now,
            bound_payload_hash=str(raw["canonical_payload_hash"]),
            bound_resource_scope=str(raw["resource_scope"]),
            bound_policy_version=str(raw["policy_version"]),
            decision_metadata=decision_metadata or {},
        )

        self.store.record_approval_decision(resolved, lease)
        return resolved

    def verify_authorization(
        self,
        request_id: str,
        current_operation: OperationIntent,
        policy_version: str,
        resource_scope: str = "default",
    ) -> None:
        """PM Gate 5: Authenticate that current execution strictly satisfies the 4-tuple security binding.

        Tuple:
        1. operation_version
        2. canonical_payload_hash
        3. policy_version
        4. resource_scope

        Fails closed on any mismatch.
        """
        raw = self.store.find_approval(request_id)
        if raw is None:
            raise ApprovalSecurityError(
                "APPROVAL_NOT_FOUND",
                f"Approval request {request_id!r} does not exist.",
            )

        # 1. Verify resolution status
        decision = raw["decision"]
        if decision == ApprovalStatus.PENDING.value or decision is None:
            raise ApprovalSecurityError(
                "APPROVAL_PENDING",
                f"Approval request {request_id!r} is still pending authorization.",
            )
        if decision == ApprovalStatus.REJECTED.value:
            raise ApprovalSecurityError(
                "APPROVAL_REJECTED",
                f"Approval request {request_id!r} was rejected by principal {raw['principal']!r}.",
            )
        if decision != ApprovalStatus.APPROVED.value:
            raise ApprovalSecurityError(
                "APPROVAL_INVALID_STATE",
                f"Approval request {request_id!r} has invalid state {decision!r}.",
            )

        # 2. Check expiration
        if raw["expires_at"]:
            exp = _parse_iso(str(raw["expires_at"]))
            if exp < datetime.now(timezone.utc):
                raise ApprovalSecurityError(
                    "APPROVAL_EXPIRED",
                    f"Approval request {request_id!r} expired at {exp.isoformat()}.",
                )

        # 3. Security tuple component 1: operation_version
        req_version = int(raw["operation_version"])
        if current_operation.operation_version != req_version:
            raise ApprovalSecurityError(
                "APPROVAL_VERSION_MISMATCH",
                f"Approval version mismatch for {request_id!r}: approved version={req_version}, current={current_operation.operation_version}.",
            )

        # 4. Security tuple component 2: canonical_payload_hash
        bound_hash = str(raw["bound_payload_hash"] or raw["canonical_payload_hash"])
        current_hash = current_operation.canonical_payload.payload_hash
        if current_hash != bound_hash:
            raise ApprovalSecurityError(
                "APPROVAL_HASH_MISMATCH",
                f"Approval payload hash mismatch for {request_id!r}: approved hash={bound_hash!r}, current={current_hash!r}. "
                "Payload was modified after approval. Execution rejected fail-closed.",
            )

        # 5. Security tuple component 3: policy_version
        bound_policy = str(raw["bound_policy_version"] or raw["policy_version"])
        if policy_version != bound_policy:
            raise ApprovalSecurityError(
                "APPROVAL_POLICY_MISMATCH",
                f"Approval policy version mismatch for {request_id!r}: approved policy={bound_policy!r}, current={policy_version!r}.",
            )

        # 6. Security tuple component 4: resource_scope
        bound_scope = str(raw["bound_resource_scope"] or raw["resource_scope"])
        if resource_scope != bound_scope:
            raise ApprovalSecurityError(
                "APPROVAL_SCOPE_MISMATCH",
                f"Approval resource scope mismatch for {request_id!r}: approved scope={bound_scope!r}, requested={resource_scope!r}. "
                "Cross-scope authorization is strictly forbidden.",
            )
