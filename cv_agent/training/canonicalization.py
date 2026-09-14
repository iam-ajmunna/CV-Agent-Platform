"""cv_agent.training.canonicalization — Functional operation payload adapter for ADR-0010.

Extracts purely functional training plan intent for ADR-0003 canonicalization and payload hashing.
Zero operation-version, operation-id, or target-identity synthesis.
"""

from __future__ import annotations

from typing import Any

from cv_agent.orchestration.canonicalization import compute_payload_hash
from cv_agent.training.contracts import TrainingPlan


def extract_training_operation_payload(plan: TrainingPlan) -> dict[str, Any]:
    """Extract functional fields constituting the canonical training operation payload.

    Excludes external/informational approval metadata (`approval_id`).
    Includes functional hyperparameters, dataset bindings, and pre-execution `cost_estimate`
    so any change in proposed cost invalidates prior authorization.
    """
    cost_dict: dict[str, Any] | None = None
    if plan.cost_estimate is not None:
        cost_dict = {
            "estimated_gpu_hours": plan.cost_estimate.estimated_gpu_hours,
            "estimated_wall_clock_hours": plan.cost_estimate.estimated_wall_clock_hours,
            "estimated_cost_usd": str(plan.cost_estimate.estimated_cost_usd),
            "estimated_storage_gb": plan.cost_estimate.estimated_storage_gb,
            "basis_description": plan.cost_estimate.basis_description,
        }

    return {
        "plan_id": str(plan.plan_id),
        "experiment_id": plan.experiment_id,
        "capability_id": str(plan.capability_id),
        "dataset_id": str(plan.dataset_id),
        "dataset_version": str(plan.dataset_version),
        "dataset_manifest_hash": str(plan.dataset_manifest_hash),
        "train_split": plan.train_split,
        "val_split": plan.val_split,
        "architecture_name": plan.architecture_name,
        "num_classes": plan.num_classes,
        "pretrained_weights_ref": plan.pretrained_weights_ref,
        "optimizer": {
            "name": plan.optimizer.name,
            "lr": plan.optimizer.lr,
            "weight_decay": plan.optimizer.weight_decay,
            "momentum": plan.optimizer.momentum,
            "betas": plan.optimizer.betas,
            "gradient_clip_norm": plan.optimizer.gradient_clip_norm,
        },
        "scheduler": {
            "policy": plan.scheduler.policy,
            "warmup_epochs": plan.scheduler.warmup_epochs,
            "min_lr": plan.scheduler.min_lr,
            "step_size": plan.scheduler.step_size,
            "gamma": plan.scheduler.gamma,
        },
        "losses": [
            {
                "loss_type": l.loss_type,
                "weight": l.weight,
                "params": dict(l.params),
            }
            for l in plan.losses
        ],
        "augmentation": {
            "pipeline_name": plan.augmentation.pipeline_name,
            "image_size": list(plan.augmentation.image_size),
            "horizontal_flip_prob": plan.augmentation.horizontal_flip_prob,
            "mosaic_prob": plan.augmentation.mosaic_prob,
            "mixup_prob": plan.augmentation.mixup_prob,
            "color_jitter": plan.augmentation.color_jitter,
            "custom_params": dict(plan.augmentation.custom_params),
        },
        "batch_size_per_device": plan.batch_size_per_device,
        "gradient_accumulation_steps": plan.gradient_accumulation_steps,
        "epochs": plan.epochs,
        "precision": plan.precision.value,
        "compute_requirements": {
            "min_vram_gb": plan.compute_requirements.min_vram_gb,
            "min_cuda_compute_capability": plan.compute_requirements.min_cuda_compute_capability,
            "num_devices": plan.compute_requirements.num_devices,
            "distributed_strategy": plan.compute_requirements.distributed_strategy.value,
        },
        "cost_estimate": cost_dict,
    }


def compute_training_payload_hash(plan: TrainingPlan) -> str:
    """Compute the authoritative SHA-256 canonical payload hash via ADR-0003."""
    payload = extract_training_operation_payload(plan)
    return compute_payload_hash(payload)
