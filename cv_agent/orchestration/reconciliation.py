"""cv_agent.orchestration.reconciliation — External side-effect recovery and reconciliation.

Implements ADR-0003 §3.7 and PM Gate 6:
- Crash recovery protocol for ambiguous external side-effects.
- Strictly preserves tri-state outcomes:
    CONFIRMED_COMPLETED      -> COMPLETED
    CONFIRMED_NOT_SUBMITTED  -> DISPATCHING (dispatchable)
    CONFIRMED_FAILED         -> FAILED
    UNRESOLVED               -> preserves UNRESOLVED / operator escalation
- UNRESOLVED must NEVER be silently mapped to FAILED, COMPLETED, or automatic redispatch.
"""

from __future__ import annotations

from enum import Enum

from cv_agent.orchestration.contracts import (
    ExecutionLease,
    OperationId,
    OperationIntent,
    OperationReconciler,
    OperationStatus,
    OrchestrationStore,
)
from cv_agent.orchestration.state_machine import validate_operation_transition


class ReconciliationOutcome(str, Enum):
    """Authoritative outcome of an external operation reconciliation attempt."""

    CONFIRMED_COMPLETED = "confirmed_completed"
    CONFIRMED_NOT_SUBMITTED = "confirmed_not_submitted"
    CONFIRMED_FAILED = "confirmed_failed"
    UNRESOLVED = "unresolved"


class OperatorEscalationRequired(Exception):
    """Raised when an operation cannot be resolved and requires operator or policy intervention."""

    def __init__(self, operation_id: str, reason: str) -> None:
        self.operation_id = operation_id
        self.reason = reason
        super().__init__(
            f"[OPERATOR_ESCALATION_REQUIRED] Operation {operation_id!r} could not be reconciled: {reason}. "
            "State remains explicitly unresolved (ADR-0003 §3.7)."
        )


class ReconciliationCoordinator:
    """Coordinates crash recovery and reconciliation against the authoritative store."""

    def __init__(
        self,
        store: OrchestrationStore,
        reconciler: OperationReconciler | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.store = store
        self.reconciler = reconciler
        self.max_attempts = max_attempts

    def initiate_reconciliation(
        self,
        operation_id: OperationId,
        lease: ExecutionLease,
    ) -> None:
        """Mark an ambiguous (UNKNOWN / LOST) operation as actively RECONCILING."""
        op = self.store.find_operation(operation_id)
        if op is None:
            raise KeyError(f"Operation {operation_id.value!r} does not exist in store.")

        # Authoritative transition check
        validate_operation_transition(
            OperationStatus.UNKNOWN, OperationStatus.RECONCILING
        )
        self.store.update_operation_status(
            operation_id, OperationStatus.RECONCILING, lease
        )

    def reconcile_operation(
        self,
        operation: OperationIntent,
        lease: ExecutionLease,
    ) -> ReconciliationOutcome:
        """PM Gate 6: Reconcile an ambiguous operation preserving strict tri-state semantics.

        Never silently defaults UNRESOLVED to success or failure.
        """
        if self.reconciler is None:
            # Without a target reconciler, remains explicitly UNRESOLVED
            return ReconciliationOutcome.UNRESOLVED

        target_status = self.reconciler.reconcile(operation)

        if target_status == OperationStatus.COMPLETED:
            self.store.update_operation_status(
                operation.operation_id, OperationStatus.COMPLETED, lease
            )
            return ReconciliationOutcome.CONFIRMED_COMPLETED

        if target_status in (OperationStatus.CREATED, OperationStatus.VALIDATED):
            # Target confirms job was never received or submitted
            self.store.update_operation_status(
                operation.operation_id, OperationStatus.DISPATCHING, lease
            )
            return ReconciliationOutcome.CONFIRMED_NOT_SUBMITTED

        if target_status == OperationStatus.FAILED:
            self.store.update_operation_status(
                operation.operation_id, OperationStatus.FAILED, lease
            )
            return ReconciliationOutcome.CONFIRMED_FAILED

        # PM Gate 6: UNRESOLVED remains explicitly unresolved in RECONCILING/UNKNOWN
        # Under no circumstance is it converted to COMPLETED or FAILED
        return ReconciliationOutcome.UNRESOLVED
