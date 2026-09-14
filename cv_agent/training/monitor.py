"""cv_agent.training.monitor — Online divergence and convergence monitoring for ADR-0010."""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

from cv_agent.training.contracts import TrainingTelemetryRecord
from cv_agent.training.types import CompletionReason, FailureReason


@runtime_checkable
class TrainingMonitor(Protocol):
    """Online diagnostic monitor analyzing streaming telemetry events."""

    def on_step(
        self, telemetry: TrainingTelemetryRecord
    ) -> tuple[bool, FailureReason | CompletionReason, str | None]:
        """Analyze a telemetry event.

        Returns:
            (should_terminate, reason, details)
        """
        ...

    def reset(self) -> None:
        """Reset internal accumulator state."""
        ...


class OnlineDivergenceMonitor:
    """Standard implementation of online divergence and early stopping monitoring.

    Enforces fail-safe abort on NaN/Inf loss or gradient explosion, while treating
    early stopping as a successful convergence completion event.
    """

    def __init__(
        self,
        window_size: int = 5,
        min_observations: int = 10,
        divergence_multiplier: float = 3.0,
        patience_epochs: int = 5,
        min_delta: float = 1e-4,
    ) -> None:
        if window_size < 1:
            raise ValueError(f"window_size must be >= 1, got {window_size}")
        if min_observations < 1:
            raise ValueError(f"min_observations must be >= 1, got {min_observations}")
        if divergence_multiplier <= 1.0:
            raise ValueError(
                f"divergence_multiplier must be > 1.0, got {divergence_multiplier}"
            )
        if patience_epochs < 1:
            raise ValueError(f"patience_epochs must be >= 1, got {patience_epochs}")
        if min_delta < 0.0:
            raise ValueError(f"min_delta must be >= 0.0, got {min_delta}")

        self.window_size = window_size
        self.min_observations = min_observations
        self.divergence_multiplier = divergence_multiplier
        self.patience_epochs = patience_epochs
        self.min_delta = min_delta

        self._initial_loss: float | None = None
        self._recent_losses: list[float] = []
        self._observations_count: int = 0
        self._best_val_loss: float = float("inf")
        self._stagnation_epochs: int = 0
        self._last_evaluated_epoch: int = -1

    def reset(self) -> None:
        """Reset all accumulator tracking."""
        self._initial_loss = None
        self._recent_losses.clear()
        self._observations_count = 0
        self._best_val_loss = float("inf")
        self._stagnation_epochs = 0
        self._last_evaluated_epoch = -1

    def on_step(
        self, telemetry: TrainingTelemetryRecord
    ) -> tuple[bool, FailureReason | CompletionReason, str | None]:
        """Evaluate a point-in-time telemetry record against divergence and stagnation rules."""
        # 1. NaN / Infinite check
        if not math.isfinite(telemetry.train_loss):
            return (
                True,
                FailureReason.NAN_LOSS,
                f"Non-finite train_loss detected at step {telemetry.step}: {telemetry.train_loss}",
            )

        for comp_name, comp_val in telemetry.loss_components.items():
            if not math.isfinite(comp_val):
                return (
                    True,
                    FailureReason.NAN_LOSS,
                    f"Non-finite loss component {comp_name!r} detected at step {telemetry.step}: {comp_val}",
                )

        if telemetry.val_loss is not None and not math.isfinite(telemetry.val_loss):
            return (
                True,
                FailureReason.NAN_LOSS,
                f"Non-finite val_loss detected at step {telemetry.step}: {telemetry.val_loss}",
            )

        # 2. Loss Divergence check
        if self._initial_loss is None:
            self._initial_loss = telemetry.train_loss

        self._recent_losses.append(telemetry.train_loss)
        if len(self._recent_losses) > self.window_size:
            self._recent_losses.pop(0)

        self._observations_count += 1

        if self._observations_count >= self.min_observations:
            recent_avg = sum(self._recent_losses) / len(self._recent_losses)
            # Guard against negative or zero initial loss
            baseline_loss = max(abs(self._initial_loss), 1e-4)
            if recent_avg > self.divergence_multiplier * baseline_loss:
                return (
                    True,
                    FailureReason.LOSS_DIVERGENCE,
                    (
                        f"Loss divergence detected at step {telemetry.step}: recent smoothed loss "
                        f"{recent_avg:.4f} exceeds {self.divergence_multiplier}x baseline loss {baseline_loss:.4f}"
                    ),
                )

        # 3. Early Stopping (stagnation) check
        if (
            telemetry.val_loss is not None
            and telemetry.epoch != self._last_evaluated_epoch
        ):
            self._last_evaluated_epoch = telemetry.epoch
            current_val = telemetry.val_loss

            if current_val < self._best_val_loss - self.min_delta:
                self._best_val_loss = current_val
                self._stagnation_epochs = 0
            else:
                self._stagnation_epochs += 1
                if self._stagnation_epochs >= self.patience_epochs:
                    return (
                        True,
                        CompletionReason.EARLY_STOPPING,
                        (
                            f"Early stopping triggered at epoch {telemetry.epoch}: val_loss failed to improve "
                            f"by at least {self.min_delta} over best {self._best_val_loss:.4f} for {self.patience_epochs} consecutive epochs"
                        ),
                    )

        return (False, FailureReason.NONE, None)
