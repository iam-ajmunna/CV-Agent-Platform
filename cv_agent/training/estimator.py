"""cv_agent.training.estimator — Deterministic resource and cost estimation for ADR-0010."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Protocol, runtime_checkable

from cv_agent.training.contracts import CostEstimate, TrainingPlan
from cv_agent.training.errors import CostEstimationError


@runtime_checkable
class TrainingEstimator(Protocol):
    """Estimates resource requirements and financial cost for a TrainingPlan."""

    def estimate(
        self,
        plan: TrainingPlan,
        train_items_count: int,
        sample_step_latency_sec: float = 0.1,
        target_hourly_rate_usd: Decimal = Decimal("2.50"),
        model_size_gb: float = 0.5,
        checkpoints_per_epoch: int = 1,
    ) -> CostEstimate:
        """Calculate a deterministic CostEstimate for the plan."""
        ...


class DeterministicTrainingEstimator:
    """Authoritative deterministic implementation of training cost estimation per ADR-0010 §3.4."""

    def estimate(
        self,
        plan: TrainingPlan,
        train_items_count: int,
        sample_step_latency_sec: float = 0.1,
        target_hourly_rate_usd: Decimal = Decimal("2.50"),
        model_size_gb: float = 0.5,
        checkpoints_per_epoch: int = 1,
    ) -> CostEstimate:
        """Calculate deterministic CostEstimate.

        Formulas:
            effective_batch = batch_size_per_device * num_devices * gradient_accumulation_steps
            steps_per_epoch = ceil(train_items_count / effective_batch)
            total_steps = steps_per_epoch * epochs
            wall_clock_hours = (total_steps * step_latency_sec) / 3600.0
            gpu_hours = wall_clock_hours * num_devices
            cost_usd = gpu_hours * hourly_rate
            storage_gb = (epochs * checkpoints_per_epoch) * model_size_gb
        """
        if not isinstance(train_items_count, int) or train_items_count <= 0:
            raise CostEstimationError(
                f"train_items_count must be a positive integer, got {train_items_count!r}"
            )
        if (
            not isinstance(sample_step_latency_sec, (int, float))
            or not math.isfinite(sample_step_latency_sec)
            or sample_step_latency_sec <= 0.0
        ):
            raise CostEstimationError(
                f"sample_step_latency_sec must be a positive finite number, got {sample_step_latency_sec!r}"
            )
        if (
            not isinstance(target_hourly_rate_usd, Decimal)
            or target_hourly_rate_usd.is_nan()
            or target_hourly_rate_usd.is_infinite()
            or target_hourly_rate_usd < Decimal("0.00")
        ):
            raise CostEstimationError(
                f"target_hourly_rate_usd must be a non-negative finite Decimal, got {target_hourly_rate_usd!r}"
            )
        if (
            not isinstance(model_size_gb, (int, float))
            or not math.isfinite(model_size_gb)
            or model_size_gb < 0.0
        ):
            raise CostEstimationError(
                f"model_size_gb must be a non-negative finite number, got {model_size_gb!r}"
            )
        if not isinstance(checkpoints_per_epoch, int) or checkpoints_per_epoch < 1:
            raise CostEstimationError(
                f"checkpoints_per_epoch must be an integer >= 1, got {checkpoints_per_epoch!r}"
            )

        batch_per_device = plan.batch_size_per_device
        num_devices = plan.compute_requirements.num_devices
        grad_accum = plan.gradient_accumulation_steps
        epochs = plan.epochs

        if batch_per_device <= 0 or num_devices <= 0 or grad_accum <= 0 or epochs <= 0:
            raise CostEstimationError(
                f"Invalid plan hyperparameters: batch={batch_per_device}, devices={num_devices}, "
                f"grad_accum={grad_accum}, epochs={epochs}"
            )

        effective_batch = batch_per_device * num_devices * grad_accum
        steps_per_epoch = math.ceil(train_items_count / effective_batch)
        total_steps = steps_per_epoch * epochs

        wall_clock_hours = round((total_steps * sample_step_latency_sec) / 3600.0, 4)
        gpu_hours = round(wall_clock_hours * num_devices, 4)
        cost_usd = (Decimal(str(gpu_hours)) * target_hourly_rate_usd).quantize(
            Decimal("0.01")
        )
        storage_gb = round((epochs * checkpoints_per_epoch) * model_size_gb, 4)

        basis_desc = (
            f"Effective batch size: {effective_batch} (per_device={batch_per_device}, "
            f"devices={num_devices}, accum={grad_accum}); Steps/epoch: {steps_per_epoch} "
            f"(items={train_items_count}); Total steps: {total_steps} ({epochs} epochs); "
            f"Step latency: {sample_step_latency_sec}s; Hourly rate: ${target_hourly_rate_usd}/hr; "
            f"Model size: {model_size_gb}GB ({checkpoints_per_epoch} ckpts/epoch)"
        )

        return CostEstimate(
            estimated_gpu_hours=gpu_hours,
            estimated_wall_clock_hours=wall_clock_hours,
            estimated_cost_usd=cost_usd,
            estimated_storage_gb=storage_gb,
            basis_description=basis_desc,
        )
