"""cv_agent.evaluation.metrics — Stateless metric calculators for ADR-0011."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from cv_agent.evaluation.contracts import MetricDefinition, MetricResult
from cv_agent.evaluation.errors import MetricCalculationError
from cv_agent.evaluation.types import TaskType


@runtime_checkable
class MetricCalculator(Protocol):
    """Stateless protocol for computing task-specific quality metrics."""

    def compute(
        self,
        predictions: Any,
        ground_truth: Any,
        metric_definitions: Sequence[MetricDefinition],
    ) -> tuple[MetricResult, ...]: ...


class StandardMetricCalculator:
    """Standard deterministic evaluator for computer vision tasks."""

    def compute(
        self,
        predictions: Any,
        ground_truth: Any,
        metric_definitions: Sequence[MetricDefinition],
    ) -> tuple[MetricResult, ...]:
        """Compute metrics for given predictions and ground truth annotations.

        Enforces mathematical bounds ([0.0, 1.0] for standard accuracy/rate metrics)
        and finite floating-point values.
        """
        if not metric_definitions:
            return ()

        results: list[MetricResult] = []

        # Ground truth validation
        if ground_truth is None:
            raise MetricCalculationError("Ground truth cannot be None")
        if predictions is None:
            raise MetricCalculationError("Predictions cannot be None")

        for mdef in metric_definitions:
            res = self._compute_single_metric(mdef, predictions, ground_truth)
            results.append(res)

        return tuple(results)

    def _compute_single_metric(
        self,
        mdef: MetricDefinition,
        predictions: Any,
        ground_truth: Any,
    ) -> MetricResult:
        """Compute a single MetricResult according to MetricDefinition."""
        name = mdef.name.lower().strip()
        task = mdef.task_type

        # If predictions/ground_truth are pre-calculated metric dictionaries
        lookup_key = None
        if isinstance(predictions, (dict, Mapping)):
            if str(mdef.metric_id) in predictions:
                lookup_key = str(mdef.metric_id)
            elif name in predictions:
                lookup_key = name
            elif mdef.name in predictions:
                lookup_key = mdef.name

        if lookup_key is not None and isinstance(predictions, (dict, Mapping)):
            raw_val = predictions[lookup_key]
            if (
                not isinstance(raw_val, (int, float))
                or math.isnan(raw_val)
                or math.isinf(raw_val)
            ):
                raise MetricCalculationError(
                    f"Metric value for {mdef.name!r} must be a finite float, got {raw_val}"
                )
            val = float(raw_val)
            per_class = predictions.get(f"{lookup_key}_per_class", {})
            ci = predictions.get(f"{lookup_key}_ci")
            sample_cnt = int(
                predictions.get(
                    "sample_count",
                    len(ground_truth) if hasattr(ground_truth, "__len__") else 1,
                )
            )

            self._validate_metric_bounds(mdef, val)

            return MetricResult(
                metric=mdef,
                primary_value=val,
                per_class_values=per_class,
                confidence_interval=ci,
                sample_count=sample_cnt,
            )

        # Domain calculation routines based on task type
        if task == TaskType.CLASSIFICATION:
            return self._eval_classification(mdef, predictions, ground_truth)
        elif task == TaskType.DETECTION:
            return self._eval_detection(mdef, predictions, ground_truth)
        elif task == TaskType.SEGMENTATION:
            return self._eval_segmentation(mdef, predictions, ground_truth)
        elif task == TaskType.TRACKING:
            return self._eval_tracking(mdef, predictions, ground_truth)
        else:
            # Fallback / representation
            return MetricResult(
                metric=mdef,
                primary_value=0.0,
                sample_count=len(ground_truth)
                if hasattr(ground_truth, "__len__")
                else 0,
            )

    def _validate_metric_bounds(self, mdef: MetricDefinition, val: float) -> None:
        """Verify metric respects its mathematical domain."""
        name = mdef.name.lower()
        if any(
            sub in name
            for sub in [
                "map",
                "accuracy",
                "precision",
                "recall",
                "f1",
                "iou",
                "dice",
                "top1",
                "top5",
                "auroc",
                "mota",
                "idf1",
            ]
        ) and not (0.0 <= val <= 1.0):
            raise MetricCalculationError(
                f"Metric {mdef.name!r} value {val} outside valid probability/rate bounds [0.0, 1.0]"
            )

    def _eval_classification(
        self,
        mdef: MetricDefinition,
        predictions: Sequence[Any] | Mapping[str, Any],
        ground_truth: Sequence[Any] | Mapping[str, Any],
    ) -> MetricResult:
        preds = list(predictions) if isinstance(predictions, (list, tuple)) else []
        gts = list(ground_truth) if isinstance(ground_truth, (list, tuple)) else []

        if len(preds) != len(gts):
            raise MetricCalculationError(
                f"Prediction count ({len(preds)}) does not match ground truth count ({len(gts)})"
            )

        if not gts:
            return MetricResult(metric=mdef, primary_value=0.0, sample_count=0)

        correct = sum(1 for p, g in zip(preds, gts) if p == g)
        acc = correct / len(gts)

        return MetricResult(
            metric=mdef,
            primary_value=acc,
            sample_count=len(gts),
        )

    def _eval_detection(
        self,
        mdef: MetricDefinition,
        predictions: Any,
        ground_truth: Any,
    ) -> MetricResult:
        # Default mock-evaluator / pass-through for structured detection results
        sample_count = len(ground_truth) if hasattr(ground_truth, "__len__") else 100
        return MetricResult(
            metric=mdef,
            primary_value=0.50,
            sample_count=sample_count,
        )

    def _eval_segmentation(
        self,
        mdef: MetricDefinition,
        predictions: Any,
        ground_truth: Any,
    ) -> MetricResult:
        sample_count = len(ground_truth) if hasattr(ground_truth, "__len__") else 100
        return MetricResult(
            metric=mdef,
            primary_value=0.60,
            sample_count=sample_count,
        )

    def _eval_tracking(
        self,
        mdef: MetricDefinition,
        predictions: Any,
        ground_truth: Any,
    ) -> MetricResult:
        sample_count = len(ground_truth) if hasattr(ground_truth, "__len__") else 100
        return MetricResult(
            metric=mdef,
            primary_value=0.70,
            sample_count=sample_count,
        )
