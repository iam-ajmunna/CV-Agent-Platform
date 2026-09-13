"""cv_agent.experiments.metrics_validator — Metric completeness validation for ADR-0004.

Enforces ADR-0004 §2.4.C:
- training: Validation accuracy metrics (map_50, map_50_95, precision, recall) +
  training resource metrics (gpu_hours, training_time_seconds, peak_vram_mb).
- evaluation: Task-specific accuracy metrics (precision, recall, map_50, map_50_95);
  on-target system metrics when evaluating deployability [P§12].
- optimization: Post-optimization accuracy/tolerance metrics + on-target system metrics
  (latency_inference_ms, latency_e2e_ms, throughput_fps, peak_vram_mb, power_draw_watts).
- benchmark: On-target benchmark/system metrics under production stream conditions
  (throughput_fps, latency_inference_ms, latency_e2e_ms, peak_vram_mb, power_draw_watts) +
  relevant accuracy verification.

Preserves [P§29.4]: "Hardware-aware: accuracy without deployability is incomplete".
"""

from __future__ import annotations

from cv_agent.experiments.interfaces import (
    AccuracyMetrics,
    SystemMetrics,
    WorkloadType,
)


class IncompleteMetricsError(Exception):
    """Raised when experiment completion evidence violates ADR-0004 §2.4.C."""


def validate_metric_completeness(
    workload_type: WorkloadType,
    accuracy: AccuracyMetrics,
    system: SystemMetrics,
    is_deployability_eval: bool = True,
) -> None:
    """Validates metric completeness for terminal experiment completion.

    Raises:
        IncompleteMetricsError: If mandatory metrics for the workload are missing.
    """
    missing: list[str] = []

    if workload_type == WorkloadType.TRAINING:
        # Validation accuracy metrics
        if accuracy.map_50 is None:
            missing.append("accuracy.map_50")
        if accuracy.map_50_95 is None:
            missing.append("accuracy.map_50_95")
        if accuracy.precision is None:
            missing.append("accuracy.precision")
        if accuracy.recall is None:
            missing.append("accuracy.recall")

        # Training resource metrics
        if system.training_time_seconds is None and system.gpu_hours is None:
            missing.append("system.training_time_seconds or system.gpu_hours")
        if system.peak_vram_mb is None or system.peak_vram_mb <= 0:
            missing.append("system.peak_vram_mb")

    elif workload_type == WorkloadType.EVALUATION:
        # Task-specific accuracy metrics
        if (
            accuracy.map_50 is None
            and accuracy.precision is None
            and accuracy.recall is None
            and accuracy.f1_score is None
            and not accuracy.additional_metrics
        ):
            missing.append(
                "at least one task-specific accuracy metric (map_50, precision, recall, f1_score)"
            )

        # System metrics when evaluating deployability [P§12]
        if (
            is_deployability_eval
            and system.throughput_fps is None
            and system.latency_inference_ms is None
        ):
            missing.append(
                "system.throughput_fps or system.latency_inference_ms (deployability evaluation)"
            )

    elif workload_type == WorkloadType.OPTIMIZATION:
        # Post-optimization accuracy/tolerance metrics
        if (
            accuracy.map_50 is None
            and accuracy.precision is None
            and not accuracy.additional_metrics
        ):
            missing.append(
                "post-optimization accuracy metric (accuracy.map_50 or accuracy.precision)"
            )

        # On-target system metrics
        if system.latency_inference_ms is None and system.latency_e2e_ms is None:
            missing.append("system.latency_inference_ms or system.latency_e2e_ms")
        if system.throughput_fps is None:
            missing.append("system.throughput_fps")
        if system.peak_vram_mb is None or system.peak_vram_mb <= 0:
            missing.append("system.peak_vram_mb")
        if system.power_draw_watts is None:
            missing.append("system.power_draw_watts")

    elif workload_type == WorkloadType.BENCHMARK:
        # On-target system metrics under production stream conditions
        if system.throughput_fps is None:
            missing.append("system.throughput_fps")
        if system.latency_inference_ms is None and system.latency_e2e_ms is None:
            missing.append("system.latency_inference_ms or system.latency_e2e_ms")
        if system.peak_vram_mb is None or system.peak_vram_mb <= 0:
            missing.append("system.peak_vram_mb")
        if system.power_draw_watts is None:
            missing.append("system.power_draw_watts")

        # Relevant accuracy verification
        if (
            accuracy.map_50 is None
            and accuracy.precision is None
            and not accuracy.additional_metrics
        ):
            missing.append(
                "accuracy verification metric (accuracy.map_50 or accuracy.precision)"
            )

    if missing:
        raise IncompleteMetricsError(
            f"Workload {workload_type.value!r} completion evidence incomplete [P§29.4]. "
            f"Missing required metrics: {', '.join(missing)}"
        )
