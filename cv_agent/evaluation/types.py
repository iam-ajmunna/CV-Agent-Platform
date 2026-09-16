"""cv_agent.evaluation.types — Canonical types, enums, and identifiers for ADR-0011.

Identifier syntax rules conform to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import NewType

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$")

EvaluationId = NewType("EvaluationId", str)
MetricId = NewType("MetricId", str)
BenchmarkId = NewType("BenchmarkId", str)
ExperimentId = NewType("ExperimentId", str)


def validate_evaluation_id(value: str) -> EvaluationId:
    """Validate EvaluationId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"EvaluationId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return EvaluationId(value)


def validate_metric_id(value: str) -> MetricId:
    """Validate MetricId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"MetricId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return MetricId(value)


def validate_benchmark_id(value: str) -> BenchmarkId:
    """Validate BenchmarkId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"BenchmarkId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return BenchmarkId(value)


def validate_experiment_id(value: str) -> ExperimentId:
    """Validate ExperimentId conforms to canonical identifier syntax."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"ExperimentId {value!r} does not conform to canonical syntax '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'"
        )
    return ExperimentId(value)


class TaskType(str, Enum):
    """Computer vision task category."""

    CLASSIFICATION = "classification"
    DETECTION = "detection"
    SEGMENTATION = "segmentation"
    TRACKING = "tracking"
    REPRESENTATION = "representation"


class HardwareClass(str, Enum):
    """Hardware target class for execution benchmarking."""

    CPU = "cpu"
    NVIDIA_GPU = "nvidia_gpu"
    JETSON = "jetson"
    OTHER_ACCELERATOR = "other_accelerator"


class EvaluationVerdict(str, Enum):
    """Domain empirical measurement verdict for an evaluation experiment.

    Denotes domain measurement outcome, NOT orchestration lifecycle status.
    """

    PASSED = "passed"
    FAILED_QUALITY = "failed_quality"
    FAILED_SYSTEMS = "failed_systems"
    INCONCLUSIVE = "inconclusive"


class StatisticalTestType(str, Enum):
    """Statistical methodology for baseline comparison significance."""

    PAIRED_T_TEST = "paired_t_test"
    WILCOXON_SIGNED_RANK = "wilcoxon_signed_rank"
    BOOTSTRAP_RESAMPLING = "bootstrap_resampling"
    MCNEMAR_TEST = "mcnemar_test"
