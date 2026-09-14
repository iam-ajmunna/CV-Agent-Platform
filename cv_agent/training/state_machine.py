"""cv_agent.training.state_machine — Pure domain lifecycle transition validator for ADR-0010.

Enforces valid domain state transitions across the 11-state TrainingJobStatus machine.
This module is strictly a pure domain validator: it does not own SQLite persistence,
fencing leases, event monotonic sequencing, or session recovery (which remain the sole
authority of ADR-0003 Orchestration).
"""

from __future__ import annotations

from types import MappingProxyType

from cv_agent.training.errors import InvalidTrainingJobTransitionError
from cv_agent.training.types import TrainingJobStatus

_ALLOWED_TRANSITIONS: MappingProxyType[
    TrainingJobStatus, frozenset[TrainingJobStatus]
] = MappingProxyType(
    {
        TrainingJobStatus.DRAFT: frozenset({TrainingJobStatus.VALIDATED}),
        TrainingJobStatus.VALIDATED: frozenset(
            {TrainingJobStatus.ESTIMATED, TrainingJobStatus.DRAFT}
        ),
        TrainingJobStatus.ESTIMATED: frozenset(
            {TrainingJobStatus.AWAITING_APPROVAL, TrainingJobStatus.VALIDATED}
        ),
        TrainingJobStatus.AWAITING_APPROVAL: frozenset(
            {TrainingJobStatus.APPROVED, TrainingJobStatus.REJECTED}
        ),
        TrainingJobStatus.APPROVED: frozenset(
            {TrainingJobStatus.LAUNCHING, TrainingJobStatus.ABORTED}
        ),
        TrainingJobStatus.LAUNCHING: frozenset(
            {
                TrainingJobStatus.RUNNING,
                TrainingJobStatus.FAILED,
                TrainingJobStatus.ABORTED,
            }
        ),
        TrainingJobStatus.RUNNING: frozenset(
            {
                TrainingJobStatus.COMPLETED,
                TrainingJobStatus.FAILED,
                TrainingJobStatus.ABORTED,
            }
        ),
        # Terminal states have no valid outgoing transitions
        TrainingJobStatus.COMPLETED: frozenset(),
        TrainingJobStatus.FAILED: frozenset(),
        TrainingJobStatus.ABORTED: frozenset(),
        TrainingJobStatus.REJECTED: frozenset(),
    }
)


def validate_training_job_transition(
    current: TrainingJobStatus,
    target: TrainingJobStatus,
) -> None:
    """Validate that a training job domain status transition is legal.

    Raises:
        InvalidTrainingJobTransitionError: If the transition is prohibited or current is terminal.
    """
    if not isinstance(current, TrainingJobStatus) or not isinstance(
        target, TrainingJobStatus
    ):
        raise InvalidTrainingJobTransitionError(
            f"Expected TrainingJobStatus enum values, got current={current!r}, target={target!r}"
        )

    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidTrainingJobTransitionError(
            f"Illegal training job state transition from {current.value!r} to {target.value!r}. "
            f"Allowed transitions from {current.value!r}: {sorted(s.value for s in allowed) if allowed else 'None (terminal state)'}"
        )
