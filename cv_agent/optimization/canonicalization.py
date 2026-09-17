"""cv_agent.optimization.canonicalization — Canonical payload hashing for ADR-0012.

Extracts functional optimization and deployment intent for ADR-0003 canonicalization,
cryptographic lineage DAG tracking, and human approval bindings.
"""

from __future__ import annotations

import hashlib
from typing import Any

from cv_agent.dataset import ContentHash
from cv_agent.optimization.contracts import DeploymentPlan, OptimizationPlan
from cv_agent.orchestration.canonicalization import (
    canonicalize_payload,
    compute_payload_hash,
)


def extract_optimization_operation_payload(plan: OptimizationPlan) -> dict[str, Any]:
    """Extract functional fields constituting the canonical optimization operation payload.

    Excludes transient execution tokens or local timestamps.
    """
    shapes_list = [
        {
            "tensor_name": s.tensor_name,
            "min_shape": list(s.min_shape),
            "opt_shape": list(s.opt_shape),
            "max_shape": list(s.max_shape),
        }
        for s in plan.shape_profiles
    ]

    calib_dict: dict[str, Any] | None = None
    if plan.calibration_binding is not None:
        calib_dict = {
            "dataset_id": str(plan.calibration_binding.dataset_id),
            "dataset_version": str(plan.calibration_binding.dataset_version),
            "dataset_manifest_hash": str(
                plan.calibration_binding.dataset_manifest_hash
            ),
            "split_name": plan.calibration_binding.split_name,
            "num_calibration_samples": plan.calibration_binding.num_calibration_samples,
            "calibration_algorithm": plan.calibration_binding.calibration_algorithm.value,
        }

    return {
        "plan_id": str(plan.plan_id),
        "source_checkpoint_id": str(plan.source_checkpoint_id),
        "source_weights_hash": str(plan.source_weights_hash),
        "technique": plan.technique.value,
        "target_precision": plan.target_precision.value,
        "target_platform": plan.target_platform,
        "target_compute_capability": plan.target_compute_capability,
        "shape_profiles": shapes_list,
        "calibration_binding": calib_dict,
        "verification_dataset_binding": {
            "dataset_id": str(plan.verification_dataset_binding.dataset_id),
            "dataset_version": str(plan.verification_dataset_binding.dataset_version),
            "dataset_manifest_hash": str(
                plan.verification_dataset_binding.dataset_manifest_hash
            ),
            "split_name": plan.verification_dataset_binding.split_name,
            "slice_filters": dict(plan.verification_dataset_binding.slice_filters),
        },
        "max_accuracy_drop_tolerance": plan.max_accuracy_drop_tolerance,
        "parameters": dict(plan.parameters),
    }


def compute_optimization_payload_hash(plan: OptimizationPlan) -> str:
    """Compute deterministic SHA-256 hash of the canonical optimization payload."""
    payload = extract_optimization_operation_payload(plan)
    return compute_payload_hash(payload)


def extract_deployment_operation_payload(plan: DeploymentPlan) -> dict[str, Any]:
    """Extract functional fields constituting the canonical deployment operation payload.

    Captures target endpoints, runtime packaging, health checks, and rollback predecessor
    to bind approval credentials strictly to the deployment configuration.
    """
    health_dict: dict[str, Any] = {
        "startup_timeout_sec": plan.health_check.startup_timeout_sec,
        "readiness_probe_samples": plan.health_check.readiness_probe_samples,
        "max_latency_threshold_ms": plan.health_check.max_latency_threshold_ms,
        "memory_headroom_mb": plan.health_check.memory_headroom_mb,
        "expected_output_shape": (
            list(plan.health_check.expected_output_shape)
            if plan.health_check.expected_output_shape is not None
            else None
        ),
    }

    return {
        "deployment_id": str(plan.deployment_id),
        "artifact_id": str(plan.artifact_id),
        "target": {
            "target_id": str(plan.target.target_id),
            "hostname_or_ip": plan.target.hostname_or_ip,
            "platform_type": plan.target.platform_type,
            "gpu_device_name": plan.target.gpu_device_name,
            "compute_capability": plan.target.compute_capability,
            "cuda_runtime_version": plan.target.cuda_runtime_version,
            "driver_version": plan.target.driver_version,
            "max_memory_allocation_gb": plan.target.max_memory_allocation_gb,
            "power_mode": plan.target.power_mode,
        },
        "runtime": plan.runtime.value,
        "runtime_config": dict(plan.runtime_config),
        "health_check": health_dict,
        "rollback_target_id": (
            str(plan.rollback_target_id)
            if plan.rollback_target_id is not None
            else None
        ),
    }


def compute_deployment_payload_hash(plan: DeploymentPlan) -> str:
    """Compute deterministic SHA-256 hash of the canonical deployment payload."""
    payload = extract_deployment_operation_payload(plan)
    return compute_payload_hash(payload)


def compute_artifact_lineage_hash(
    plan: OptimizationPlan,
    additional_metadata: dict[str, Any] | None = None,
) -> ContentHash:
    """Compute cryptographic lineage DAG hash for OptimizedModelArtifact per ADR-0012 §3.1.

    Parent Checkpoint Hash + Optimization Config Hash + Calibration Manifest Hash -> Artifact Hash.
    """
    payload = extract_optimization_operation_payload(plan)
    if additional_metadata:
        payload["_additional"] = additional_metadata

    raw_json = canonicalize_payload(payload)
    artifact_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    return ContentHash(f"sha256:{artifact_hash}")
