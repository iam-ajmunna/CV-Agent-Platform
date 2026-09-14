"""cv_agent.training.types — Canonical types, enums, and identifiers for ADR-0010.

Identifier syntax rules conform to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import NewType

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$")

PlanId = NewType("PlanId", str)
TrainingJobId = NewType("TrainingJobId", str)
CheckpointId = NewType("CheckpointId", str)
ApprovalId = NewType("ApprovalId", str)


def validate_plan_id(value: str) -> PlanId:
    """Validate PlanId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"PlanId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return PlanId(value)


def validate_training_job_id(value: str) -> TrainingJobId:
    """Validate TrainingJobId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"TrainingJobId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return TrainingJobId(value)


def validate_checkpoint_id(value: str) -> CheckpointId:
    """Validate CheckpointId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"CheckpointId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return CheckpointId(value)


def validate_approval_id(value: str) -> ApprovalId:
    """Validate ApprovalId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"ApprovalId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return ApprovalId(value)


class TrainingPrecision(str, Enum):
    """Supported training computation precisions."""

    FP32 = "fp32"
    FP16_AMP = "fp16_amp"
    BF16_AMP = "bf16_amp"
    INT8_PTQ_AWARE = "int8_ptq_aware"


class DistributedStrategy(str, Enum):
    """Supported distributed training architectures."""

    NONE = "none"
    DDP = "ddp"
    FSDP = "fsdp"
    DEEPSPEED = "deepspeed"


class TrainingJobStatus(str, Enum):
    """Domain lifecycle states for a training job specification and execution."""

    DRAFT = "draft"
    VALIDATED = "validated"
    ESTIMATED = "estimated"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    LAUNCHING = "launching"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class FailureReason(str, Enum):
    """Failure classifications for non-successful training termination."""

    NONE = "none"
    NAN_LOSS = "nan_loss"
    LOSS_DIVERGENCE = "loss_divergence"
    CUDA_OOM = "cuda_oom"
    HARDWARE_FAULT = "hardware_fault"
    USER_CANCELLED = "user_cancelled"
    TIMEOUT = "timeout"


class CompletionReason(str, Enum):
    """Successful termination classifications for training jobs."""

    ALL_EPOCHS_COMPLETED = "all_epochs_completed"
    EARLY_STOPPING = "early_stopping"
