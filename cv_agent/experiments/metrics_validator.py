"""cv_agent.experiments.metrics_validator — Metric completeness validation for ADR-0004.

Enforces ADR-0004 §2.4.C:
- training: Validation accuracy metrics (map_50, map_50_95, precision, recall) +
  training resource metrics (gpu_hours, training_time_seconds, peak_vram_mb).
- evaluation: Task-specific accuracy metrics (precision, recall, mAP, per-class metrics);
  on-target system metrics when evaluating deployability [P§12].
- optimization: Post-optimization accuracy/tolerance metrics + on-target system metrics
  (latency_inference_ms, latency_e2e_ms, throughput_fps, peak_vram_mb, power_draw_watts).
- benchmark: On-target benchmark/system metrics under production stream conditions
  (throughput_fps, latency_inference_ms, latency_e2e_ms, peak_vram_mb, power_draw_watts) +
  relevant accuracy verification.

Preserves [P§29.4]: "Hardware-aware: accuracy without deployability is incomplete".
Enforces strict metric applicability contracts: no arbitrary OR semantics.
"""

from __future__ import annotations

from collections.abc import Sequence

from cv_agent.experiments.interfaces import (
    AccuracyMetrics,
    SystemMetrics,
    WorkloadType,
)


class IncompleteMetricsError(Exception):
    """Raised when experiment completion evidence violates ADR-0004 §2.4.C."""


def _is_metric_present(accuracy: AccuracyMetrics, metric_name: str) -> bool:
    """Check whether a declared accuracy metric is populated and non-None."""
    if metric_name == "map_50":
        return accuracy.map_50 is not None
    if metric_name == "map_50_95":
        return accuracy.map_50_95 is not None
    if metric_name == "precision":
        return accuracy.precision is not None
    if metric_name == "recall":
        return accuracy.recall is not None
    if metric_name == "f1_score":
        return accuracy.f1_score is not None
    if accuracy.additional_metrics and metric_name in accuracy.additional_metrics:
        return accuracy.additional_metrics[metric_name] is not None
    return False


def validate_metric_completeness(
    workload_type: WorkloadType,
    accuracy: AccuracyMetrics,
    system: SystemMetrics,
    is_deployability_eval: bool = True,
    accuracy_metric_requirements: tuple[str, ...] | Sequence[str] | None = None,
) -> None:
    """Validates metric completeness for terminal experiment completion per ADR-0004 §2.4.C.

    Requires explicit representation of applicable/relevant accuracy evidence for
    evaluation, optimization, and benchmark workloads (zero arbitrary OR semantics).
    Training strictly requires all four validation accuracy metrics and all three
    training resource metrics.

    Raises:
        IncompleteMetricsError: If mandatory or declared metrics for the workload are missing.
    """
    missing: list[str] = []

    if workload_type == WorkloadType.TRAINING:
        # Validation accuracy metrics: (map_50, map_50_95, precision, recall)
        if accuracy.map_50 is None:
            missing.append("accuracy.map_50")
        if accuracy.map_50_95 is None:
            missing.append("accuracy.map_50_95")
        if accuracy.precision is None:
            missing.append("accuracy.precision")
        if accuracy.recall is None:
            missing.append("accuracy.recall")

        # Also validate any additional declared accuracy requirements
        if accuracy_metric_requirements:
            for req in accuracy_metric_requirements:
                if not _is_metric_present(accuracy, req):
                    missing.append(f"accuracy.{req}")

        # Training resource metrics: (gpu_hours, training_time_seconds, peak_vram_mb)
        if system.gpu_hours is None or system.gpu_hours < 0:
            missing.append("system.gpu_hours")
        if system.training_time_seconds is None or system.training_time_seconds < 0:
            missing.append("system.training_time_seconds")
        if system.peak_vram_mb is None or system.peak_vram_mb <= 0:
            missing.append("system.peak_vram_mb")

    elif workload_type == WorkloadType.EVALUATION:
        # Task-specific accuracy metrics: must be explicitly declared and all present (no arbitrary OR)
        if not accuracy_metric_requirements:
            raise IncompleteMetricsError(
                "Workload 'evaluation' completion evidence incomplete [P§29.4]. "
                "No task-specific accuracy evidence declared in accuracy_metric_requirements."
            )

        for req in accuracy_metric_requirements:
            if not _is_metric_present(accuracy, req):
                missing.append(f"accuracy.{req}")

        # On-target system metrics when evaluating deployability [P§12]
        if is_deployability_eval:
            if system.throughput_fps is None or system.throughput_fps <= 0:
                missing.append("system.throughput_fps")
            if system.latency_inference_ms is None or system.latency_inference_ms <= 0:
                missing.append("system.latency_inference_ms")
            if system.peak_vram_mb is None or system.peak_vram_mb <= 0:
                missing.append("system.peak_vram_mb")

    elif workload_type == WorkloadType.OPTIMIZATION:
        # Post-optimization accuracy/tolerance metrics: must be explicitly declared and all present (no arbitrary OR)
        if not accuracy_metric_requirements:
            raise IncompleteMetricsError(
                "Workload 'optimization' completion evidence incomplete [P§29.4]. "
                "No post-optimization accuracy/tolerance evidence declared in accuracy_metric_requirements."
            )

        for req in accuracy_metric_requirements:
            if not _is_metric_present(accuracy, req):
                missing.append(f"accuracy.{req}")

        # On-target system metrics: latency_inference_ms, latency_e2e_ms, throughput_fps, peak_vram_mb, power_draw_watts
        if system.latency_inference_ms is None or system.latency_inference_ms <= 0:
            missing.append("system.latency_inference_ms")
        if system.latency_e2e_ms is None or system.latency_e2e_ms <= 0:
            missing.append("system.latency_e2e_ms")
        if system.throughput_fps is None or system.throughput_fps <= 0:
            missing.append("system.throughput_fps")
        if system.peak_vram_mb is None or system.peak_vram_mb <= 0:
            missing.append("system.peak_vram_mb")
        if system.power_draw_watts is None or system.power_draw_watts <= 0:
            missing.append("system.power_draw_watts")

    elif workload_type == WorkloadType.BENCHMARK:
        # On-target benchmark/system metrics under production stream conditions:
        # (throughput_fps, latency_inference_ms, latency_e2e_ms, peak_vram_mb, power_draw_watts)
        if system.throughput_fps is None or system.throughput_fps <= 0:
            missing.append("system.throughput_fps")
        if system.latency_inference_ms is None or system.latency_inference_ms <= 0:
            missing.append("system.latency_inference_ms")
        if system.latency_e2e_ms is None or system.latency_e2e_ms <= 0:
            missing.append("system.latency_e2e_ms")
        if system.peak_vram_mb is None or system.peak_vram_mb <= 0:
            missing.append("system.peak_vram_mb")
        if system.power_draw_watts is None or system.power_draw_watts <= 0:
            missing.append("system.power_draw_watts")

        # Relevant accuracy verification: must be explicitly declared and all present (no arbitrary OR)
        if not accuracy_metric_requirements:
            raise IncompleteMetricsError(
                "Workload 'benchmark' completion evidence incomplete [P§29.4]. "
                "No relevant accuracy verification evidence declared in accuracy_metric_requirements."
            )

        for req in accuracy_metric_requirements:
            if not _is_metric_present(accuracy, req):
                missing.append(f"accuracy.{req}")

    if missing:
        raise IncompleteMetricsError(
            f"Workload {workload_type.value!r} completion evidence incomplete [P§29.4]. "
            f"Missing required metrics: {', '.join(missing)}"
        )
