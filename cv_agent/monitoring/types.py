"""cv_agent.monitoring.types — Canonical types, enums, and identifiers for ADR-0013.

Identifier syntax rules conform to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated

from pydantic import AfterValidator

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$")


def validate_drift_analysis_id(value: str) -> str:
    """Validate DriftAnalysisId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"DriftAnalysisId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return value


def validate_candidate_batch_id(value: str) -> str:
    """Validate CandidateBatchId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"CandidateBatchId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return value


def validate_candidate_sample_id(value: str) -> str:
    """Validate CandidateSampleId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"CandidateSampleId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return value


def validate_alert_id(value: str) -> str:
    """Validate AlertId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"AlertId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return value


def validate_trigger_proposal_id(value: str) -> str:
    """Validate TriggerProposalId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"TriggerProposalId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return value


def validate_snapshot_id(value: str) -> str:
    """Validate SnapshotId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"SnapshotId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return value


DriftAnalysisId = Annotated[str, AfterValidator(validate_drift_analysis_id)]
CandidateBatchId = Annotated[str, AfterValidator(validate_candidate_batch_id)]
CandidateSampleId = Annotated[str, AfterValidator(validate_candidate_sample_id)]
AlertId = Annotated[str, AfterValidator(validate_alert_id)]
TriggerProposalId = Annotated[str, AfterValidator(validate_trigger_proposal_id)]
SnapshotId = Annotated[str, AfterValidator(validate_snapshot_id)]


class TelemetryDimension(str, Enum):
    """The three canonical observation dimensions defined in [P§28]."""

    MODEL = "model"
    SYSTEM = "system"
    DATA = "data"


class DriftMetricType(str, Enum):
    """Statistical distribution distance and hypothesis test algorithms."""

    KOLMOGOROV_SMIRNOV = (
        "kolmogorov_smirnov"  # Continuous distributions (confidence, latency)
    )
    POPULATION_STABILITY_INDEX = (
        "population_stability_index"  # Binned feature/confidence drift
    )
    WASSERSTEIN_DISTANCE = "wasserstein_distance"  # Earth Mover's Distance
    CHI_SQUARE_INDEPENDENCE = (
        "chi_square_independence"  # Discrete class distribution drift
    )
    ENTROPY_DIVERGENCE = (
        "entropy_divergence"  # KL/JS divergence of prediction uncertainty
    )
    Z_SCORE_DEVIATION = "z_score_deviation"  # Scalar system metrics (FPS, temperature)


class DriftSeverity(str, Enum):
    """Operational alert severity tiers."""

    INFO = "info"  # Detectable variation within acceptable operational tolerance
    WARNING = (
        "warning"  # Statistically significant drift approaching operational SLA limit
    )
    CRITICAL = "critical"  # Severe distribution shift violating performance invariants


class ActiveLearningStrategy(str, Enum):
    """Canonical active learning sampling heuristics [P§26], [P§29.7]."""

    UNCERTAINTY_LEAST_CONFIDENT = (
        "uncertainty_least_confident"  # Lowest max-class confidence
    )
    UNCERTAINTY_MARGIN = "uncertainty_margin"  # Smallest difference between top-1 and top-2 probabilities
    UNCERTAINTY_ENTROPY = (
        "uncertainty_entropy"  # Highest output probability distribution entropy
    )
    FEATURE_DIVERSITY_CLUSTER = (
        "feature_diversity_cluster"  # Core-set / k-means in visual embedding space
    )
    OUT_OF_DISTRIBUTION_ANOMALY = (
        "out_of_distribution_anomaly"  # High reconstruction or density error
    )
    ENVIRONMENTAL_SHIFT = (
        "environmental_shift"  # Low-light, blur, weather, or scene change trigger
    )
    EMPIRICAL_FAILURE_FEEDBACK = (
        "empirical_failure_feedback"  # Operator-flagged false alarms / misses
    )


class TriggerReason(str, Enum):
    """Root cause triggering the retraining proposal."""

    MODEL_ACCURACY_DRIFT = (
        "model_accuracy_drift"  # Statistical confidence/entropy shift
    )
    CLASS_IMBALANCE_SHIFT = (
        "class_imbalance_shift"  # Production class frequencies diverged from training
    )
    OUT_OF_DISTRIBUTION_SPIKE = (
        "out_of_distribution_spike"  # High density of novel visual concepts
    )
    CAMERA_ENVIRONMENTAL_SHIFT = (
        "camera_environmental_shift"  # Persistent optical or scene change
    )
    ACTIVE_LEARNING_QUOTA_REACHED = (
        "active_learning_quota_reached"  # Curated candidate batch threshold met
    )
    SYSTEM_SLA_VIOLATION = (
        "system_sla_violation"  # Latency / throughput degraded below SLA limit
    )
