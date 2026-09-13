"""cv_agent.experiments.state_machine — Lifecycle state machine for ADR-0004.

Enforces ADR-0004 §2.4.B transition matrix:
- proposed -> queued, running, cancelled, failed
- queued -> running, cancelled, failed
- running -> completed, failed, cancelled
- terminal states (completed, failed, cancelled) -> NO transitions allowed.
"""

from __future__ import annotations

from cv_agent.experiments.interfaces import ExperimentStatus


class ExperimentStateError(Exception):
    """Base exception for experiment lifecycle errors."""


class InvalidExperimentTransitionError(ExperimentStateError):
    """Raised when an illegal lifecycle transition is attempted."""


class ExperimentImmutabilityError(ExperimentStateError):
    """Raised when attempting to modify a terminal experiment."""


LEGAL_EXPERIMENT_TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.PROPOSED: frozenset(
        {
            ExperimentStatus.QUEUED,
            ExperimentStatus.RUNNING,
            ExperimentStatus.CANCELLED,
            ExperimentStatus.FAILED,
        }
    ),
    ExperimentStatus.QUEUED: frozenset(
        {
            ExperimentStatus.RUNNING,
            ExperimentStatus.CANCELLED,
            ExperimentStatus.FAILED,
        }
    ),
    ExperimentStatus.RUNNING: frozenset(
        {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.COMPLETED: frozenset(),
    ExperimentStatus.FAILED: frozenset(),
    ExperimentStatus.CANCELLED: frozenset(),
}


def validate_experiment_transition(
    current: ExperimentStatus, target: ExperimentStatus
) -> None:
    """Validates an experiment status transition per ADR-0004 §2.4.B.

    Raises:
        ExperimentImmutabilityError: If current state is terminal.
        InvalidExperimentTransitionError: If the transition is illegal.
    """
    if current in (
        ExperimentStatus.COMPLETED,
        ExperimentStatus.FAILED,
        ExperimentStatus.CANCELLED,
    ):
        raise ExperimentImmutabilityError(
            f"Experiment in terminal status {current.value!r} is strictly immutable [P§25]. "
            f"Cannot transition to {target.value!r}."
        )

    allowed = LEGAL_EXPERIMENT_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidExperimentTransitionError(
            f"Illegal experiment transition: {current.value!r} -> {target.value!r}. "
            f"Allowed transitions: {[s.value for s in allowed]}"
        )
