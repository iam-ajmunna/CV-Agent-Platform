"""cv_agent.evaluation.canonicalization — Functional operation payload adapter for ADR-0011.

Extracts purely functional evaluation plan intent for ADR-0003 canonicalization and payload hashing.
Zero operation-version, operation-id, or target-identity synthesis.
"""

from __future__ import annotations

from typing import Any

from cv_agent.evaluation.contracts import EvaluationPlan
from cv_agent.orchestration.canonicalization import compute_payload_hash


def extract_evaluation_operation_payload(plan: EvaluationPlan) -> dict[str, Any]:
    """Extract functional fields constituting the canonical evaluation operation payload.

    Excludes external transient execution tokens.
    Includes dataset bindings, model artifact references, metric definitions,
    and benchmark configurations so any modification invalidates prior authorization.
    """
    bench_dict: dict[str, Any] | None = None
    if plan.benchmark_config is not None:
        bench_dict = {
            "benchmark_id": str(plan.benchmark_config.benchmark_id),
            "batch_sizes": list(plan.benchmark_config.batch_sizes),
            "warmup_iterations": plan.benchmark_config.warmup_iterations,
            "measured_iterations": plan.benchmark_config.measured_iterations,
            "precision": plan.benchmark_config.precision.value,
            "measure_power": plan.benchmark_config.measure_power,
            "measure_memory": plan.benchmark_config.measure_memory,
            "target_requirements": {
                "required_hardware_class": (
                    plan.benchmark_config.target_requirements.required_hardware_class.value
                    if plan.benchmark_config.target_requirements.required_hardware_class
                    else None
                ),
                "min_cuda_compute_capability": (
                    list(
                        plan.benchmark_config.target_requirements.min_cuda_compute_capability
                    )
                    if plan.benchmark_config.target_requirements.min_cuda_compute_capability
                    else None
                ),
            },
        }

    return {
        "evaluation_id": str(plan.evaluation_id),
        "experiment_id": str(plan.experiment_id),
        "capability_id": str(plan.capability_id),
        "dataset_binding": {
            "dataset_id": str(plan.dataset_binding.dataset_id),
            "dataset_version": str(plan.dataset_binding.dataset_version),
            "dataset_manifest_hash": str(plan.dataset_binding.dataset_manifest_hash),
            "split_name": plan.dataset_binding.split_name,
            "slice_filters": dict(plan.dataset_binding.slice_filters),
        },
        "model_artifact": {
            "checkpoint_id": str(plan.model_artifact.checkpoint_id),
            "architecture_name": plan.model_artifact.architecture_name,
            "weights_hash": str(plan.model_artifact.weights_hash),
            "weights_uri": plan.model_artifact.weights_uri,
            "precision": plan.model_artifact.precision.value,
            "input_resolution": list(plan.model_artifact.input_resolution),
        },
        "task_type": plan.task_type.value,
        "metrics": [
            {
                "metric_id": str(m.metric_id),
                "name": m.name,
                "task_type": m.task_type.value,
                "version": m.version,
                "higher_is_better": m.higher_is_better,
                "parameters": dict(m.parameters),
            }
            for m in plan.metrics
        ],
        "benchmark_config": bench_dict,
        "baseline_experiment_id": (
            str(plan.baseline_experiment_id)
            if plan.baseline_experiment_id is not None
            else None
        ),
        "target_requirements": {
            "required_hardware_class": (
                plan.target_requirements.required_hardware_class.value
                if plan.target_requirements.required_hardware_class
                else None
            ),
            "min_cuda_compute_capability": (
                list(plan.target_requirements.min_cuda_compute_capability)
                if plan.target_requirements.min_cuda_compute_capability
                else None
            ),
        },
        "evaluation_requirements": dict(plan.evaluation_requirements),
        "reproducibility_requirements": dict(plan.reproducibility_requirements),
    }


def compute_evaluation_payload_hash(plan: EvaluationPlan) -> str:
    """Compute the authoritative SHA-256 canonical payload hash via ADR-0003."""
    payload = extract_evaluation_operation_payload(plan)
    return compute_payload_hash(payload)
