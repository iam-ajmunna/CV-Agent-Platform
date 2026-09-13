"""cv_agent.orchestration.state_machine — Authoritative lifecycle transition validation.

Implements ADR-0003 §3.9, Invariant 17, and PM Gate 6:
- Enforces strict (CurrentState, TargetState) transition validation.
- Rejects illegal transitions with InvalidStateTransitionError.
- Enforces Invariant 17: EVALUATING transitions exclusively to COMPLETED, FAILED, or PAUSED.
  Transition from EVALUATING back to EXECUTING is strictly prohibited.
- Validates external operation lifecycle progression, ensuring UNKNOWN and RECONCILING
  cannot bypass reconciliation.
"""

from __future__ import annotations

from cv_agent.orchestration.contracts import LifecycleStatus, OperationStatus


class InvalidStateTransitionError(Exception):
    """Raised when an illegal lifecycle or operation status transition is attempted."""

    def __init__(
        self, entity_type: str, current: str, target: str, reason: str | None = None
    ) -> None:
        self.entity_type = entity_type
        self.current = current
        self.target = target
        self.reason = (
            reason
            or "Transition is not permitted by authoritative state machine (ADR-0003 §3.9)"
        )
        super().__init__(
            f"[{entity_type.upper()}_TRANSITION_DENIED] Cannot transition from {current!r} to {target!r}: {self.reason}"
        )


# Authoritative session transitions (ADR-0003 §3.9 & Invariant 17)
_VALID_LIFECYCLE_TRANSITIONS: dict[LifecycleStatus, frozenset[LifecycleStatus]] = {
    LifecycleStatus.INITIALIZING: frozenset(
        {
            LifecycleStatus.READY,
            LifecycleStatus.FAILED,
            LifecycleStatus.RECOVERY_REQUIRED,
        }
    ),
    LifecycleStatus.READY: frozenset(
        {
            LifecycleStatus.PLANNING,
            LifecycleStatus.PAUSED,
            LifecycleStatus.FAILED,
            LifecycleStatus.RECOVERY_REQUIRED,
        }
    ),
    LifecycleStatus.PLANNING: frozenset(
        {
            LifecycleStatus.AWAITING_APPROVAL,
            LifecycleStatus.EXECUTING,
            LifecycleStatus.PAUSED,
            LifecycleStatus.FAILED,
            LifecycleStatus.RECOVERY_REQUIRED,
        }
    ),
    LifecycleStatus.AWAITING_APPROVAL: frozenset(
        {
            LifecycleStatus.EXECUTING,
            LifecycleStatus.PLANNING,
            LifecycleStatus.PAUSED,
            LifecycleStatus.FAILED,
            LifecycleStatus.RECOVERY_REQUIRED,
        }
    ),
    LifecycleStatus.EXECUTING: frozenset(
        {
            LifecycleStatus.EVALUATING,
            LifecycleStatus.PAUSED,
            LifecycleStatus.FAILED,
            LifecycleStatus.RECOVERY_REQUIRED,
        }
    ),
    # Invariant 17: EVALUATING is terminal-adjacent. Transition back to EXECUTING is strictly prohibited.
    LifecycleStatus.EVALUATING: frozenset(
        {
            LifecycleStatus.COMPLETED,
            LifecycleStatus.FAILED,
            LifecycleStatus.PAUSED,
            LifecycleStatus.RECOVERY_REQUIRED,
        }
    ),
    LifecycleStatus.PAUSED: frozenset(
        {
            LifecycleStatus.READY,
            LifecycleStatus.PLANNING,
            LifecycleStatus.EXECUTING,
            LifecycleStatus.FAILED,
            LifecycleStatus.RECOVERY_REQUIRED,
        }
    ),
    LifecycleStatus.RECOVERY_REQUIRED: frozenset(
        {
            LifecycleStatus.INITIALIZING,
            LifecycleStatus.READY,
            LifecycleStatus.PAUSED,
            LifecycleStatus.FAILED,
        }
    ),
    LifecycleStatus.COMPLETED: frozenset(),  # Terminal state
    LifecycleStatus.FAILED: frozenset(),  # Terminal state
}


# Authoritative external operation transitions (ADR-0003 §3.9)
_VALID_OPERATION_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.CREATED: frozenset(
        {OperationStatus.VALIDATED, OperationStatus.FAILED}
    ),
    OperationStatus.VALIDATED: frozenset(
        {
            OperationStatus.APPROVAL_REQUIRED,
            OperationStatus.DISPATCHING,
            OperationStatus.FAILED,
        }
    ),
    OperationStatus.APPROVAL_REQUIRED: frozenset(
        {
            OperationStatus.DISPATCHING,
            OperationStatus.CANCELLED,
            OperationStatus.FAILED,
        }
    ),
    OperationStatus.DISPATCHING: frozenset(
        {
            OperationStatus.SUBMITTED,
            OperationStatus.RUNNING,
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.UNKNOWN,
        }
    ),
    OperationStatus.SUBMITTED: frozenset(
        {
            OperationStatus.RUNNING,
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.CANCEL_REQUESTED,
            OperationStatus.UNKNOWN,
        }
    ),
    OperationStatus.RUNNING: frozenset(
        {
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.CANCEL_REQUESTED,
            OperationStatus.UNKNOWN,
            OperationStatus.LOST,
        }
    ),
    OperationStatus.CANCEL_REQUESTED: frozenset(
        {
            OperationStatus.CANCELLED,
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.UNKNOWN,
        }
    ),
    # Recovery & reconciliation states
    OperationStatus.UNKNOWN: frozenset({OperationStatus.RECONCILING}),
    OperationStatus.LOST: frozenset({OperationStatus.RECONCILING}),
    OperationStatus.RECONCILING: frozenset(
        {
            OperationStatus.COMPLETED,
            OperationStatus.DISPATCHING,
            OperationStatus.FAILED,
            OperationStatus.UNKNOWN,  # Preserves explicit UNRESOLVED/UNKNOWN state
        }
    ),
    # Terminal states
    OperationStatus.COMPLETED: frozenset(),
    OperationStatus.FAILED: frozenset(),
    OperationStatus.CANCELLED: frozenset(),
}


def validate_lifecycle_transition(
    current: LifecycleStatus, target: LifecycleStatus
) -> None:
    """Validate a session lifecycle transition.

    Enforces:
    - Target must be a permissible successor of Current.
    - Invariant 17: EVALUATING cannot transition to EXECUTING.
    - Terminal states cannot transition to any other state.
    """
    if current == target:
        return

    allowed = _VALID_LIFECYCLE_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        if (
            current == LifecycleStatus.EVALUATING
            and target == LifecycleStatus.EXECUTING
        ):
            raise InvalidStateTransitionError(
                "session_lifecycle",
                current.value,
                target.value,
                "Invariant 17 violation: EVALUATING is terminal-adjacent and must transition exclusively "
                "to COMPLETED, FAILED, or PAUSED. Re-entry to EXECUTING is strictly prohibited.",
            )
        raise InvalidStateTransitionError(
            "session_lifecycle", current.value, target.value
        )


def validate_operation_transition(
    current: OperationStatus, target: OperationStatus
) -> None:
    """Validate an external operation status transition.

    Enforces legal operation progression and prevents bypassing the reconciliation lifecycle.
    """
    if current == target:
        return

    allowed = _VALID_OPERATION_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidStateTransitionError(
            "operation_status", current.value, target.value
        )
