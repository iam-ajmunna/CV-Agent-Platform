"""cv_agent.optimization.verifier — Post-optimization empirical verification for ADR-0012."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cv_agent.evaluation.comparison import BaselineComparator, DefaultBaselineComparator
from cv_agent.evaluation.contracts import EvaluationResult
from cv_agent.evaluation.types import ExperimentId
from cv_agent.optimization.contracts import (
    OptimizationPlan,
    OptimizationResult,
    OptimizedModelArtifact,
)
from cv_agent.optimization.errors import (
    OptimizationValidationError,
    OptimizationVerificationError,
)


@runtime_checkable
class OptimizationVerifier(Protocol):
    """Verifies optimization accuracy retention and systems speedup via ADR-0011."""

    def verify(
        self,
        artifact: OptimizedModelArtifact,
        plan: OptimizationPlan,
        baseline_evaluation: EvaluationResult,
        candidate_evaluation: EvaluationResult,
        *,
        baseline_comparator: BaselineComparator | None = None,
        raise_on_failure: bool = False,
    ) -> OptimizationResult: ...


class DefaultOptimizationVerifier:
    """Standard post-optimization verifier enforcing accuracy tolerance guardrails per [P§14]."""

    def __init__(self, baseline_comparator: BaselineComparator | None = None) -> None:
        self.baseline_comparator = baseline_comparator or DefaultBaselineComparator()

    def verify(
        self,
        artifact: OptimizedModelArtifact,
        plan: OptimizationPlan,
        baseline_evaluation: EvaluationResult,
        candidate_evaluation: EvaluationResult,
        *,
        baseline_comparator: BaselineComparator | None = None,
        raise_on_failure: bool = False,
    ) -> OptimizationResult:
        """Perform quantitative verification of candidate optimized artifact against unoptimized baseline."""
        # 1. Artifact-to-Plan binding validation
        if str(artifact.plan_id) != str(plan.plan_id):
            raise OptimizationValidationError(
                f"Artifact plan_id {artifact.plan_id!r} does not match OptimizationPlan {plan.plan_id!r}"
            )

        # 2. Statistical Baseline Comparison via ADR-0011
        comparator = baseline_comparator or self.baseline_comparator

        # 3. Primary metric accuracy retention calculation
        cand_map = {
            m.metric.name: m.primary_value for m in candidate_evaluation.quality_metrics
        }
        base_map = {
            m.metric.name: m.primary_value for m in baseline_evaluation.quality_metrics
        }

        # Select primary metric (e.g. "map50", "accuracy", "f1", or first available common metric)
        common_metrics = [name for name in cand_map if name in base_map]
        if not common_metrics:
            raise OptimizationVerificationError(
                "No overlapping quality metrics between candidate and baseline evaluation"
            )

        primary_metric_name = common_metrics[0]
        for candidate_key in ["map50", "mAP50", "mAP", "accuracy", "f1"]:
            if candidate_key in common_metrics:
                primary_metric_name = candidate_key
                break

        cand_val = cand_map[primary_metric_name]
        base_val = base_map[primary_metric_name]

        accuracy_drop = base_val - cand_val
        retention_ratio = (cand_val / base_val) if base_val > 0.0 else 1.0

        # 4. Statistical Baseline Comparison via ADR-0011
        comparison = comparator.compare(
            candidate_metrics=candidate_evaluation.quality_metrics,
            baseline_metrics=baseline_evaluation.quality_metrics,
            candidate_benchmarks=candidate_evaluation.system_benchmarks,
            baseline_benchmarks=baseline_evaluation.system_benchmarks,
            candidate_exp_id=ExperimentId(str(candidate_evaluation.evaluation_id)),
            baseline_exp_id=ExperimentId(str(baseline_evaluation.evaluation_id)),
        )

        # 5. Systems speedup and memory reduction calculation
        speedup_ratio = 1.0
        if (
            candidate_evaluation.system_benchmarks
            and baseline_evaluation.system_benchmarks
        ):
            cand_fps = candidate_evaluation.system_benchmarks[0].throughput_fps
            base_fps = baseline_evaluation.system_benchmarks[0].throughput_fps
            if base_fps > 0.0:
                speedup_ratio = cand_fps / base_fps

        memory_reduction_ratio = 0.0
        if (
            candidate_evaluation.system_benchmarks
            and baseline_evaluation.system_benchmarks
        ):
            cand_vram = candidate_evaluation.system_benchmarks[0].peak_vram_gb
            base_vram = baseline_evaluation.system_benchmarks[0].peak_vram_gb
            if (
                cand_vram is not None
                and base_vram is not None
                and base_vram > 0.0
                and cand_vram <= base_vram
            ):
                memory_reduction_ratio = round(1.0 - (cand_vram / base_vram), 4)

        # 6. Evaluate accuracy drop tolerance guardrail
        verification_passed = accuracy_drop <= plan.max_accuracy_drop_tolerance

        if not verification_passed:
            diag = (
                f"Optimization verification FAILED accuracy tolerance gate for {primary_metric_name!r}: "
                f"drop of {accuracy_drop:.4f} ({base_val:.4f} -> {cand_val:.4f}) exceeds "
                f"maximum allowable tolerance {plan.max_accuracy_drop_tolerance:.4f}."
            )
            if raise_on_failure:
                raise OptimizationVerificationError(diag)
        else:
            diag = (
                f"Optimization verification PASSED for {primary_metric_name!r}: "
                f"accuracy drop {accuracy_drop:.4f} <= tolerance {plan.max_accuracy_drop_tolerance:.4f}; "
                f"retention={retention_ratio:.2%}, speedup={speedup_ratio:.2f}x, "
                f"memory_reduction={memory_reduction_ratio:.2%}."
            )

        return OptimizationResult(
            plan=plan,
            artifact=artifact,
            evaluation_result=candidate_evaluation,
            baseline_comparison=comparison,
            accuracy_retention_ratio=round(max(0.0, retention_ratio), 4),
            speedup_ratio=round(max(0.001, speedup_ratio), 4),
            memory_reduction_ratio=round(max(0.0, memory_reduction_ratio), 4),
            verification_passed=verification_passed,
            diagnostics=diag,
        )
