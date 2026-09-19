"""cv_agent.optimization.verifier — Post-optimization empirical verification for ADR-0012."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cv_agent.evaluation.comparison import BaselineComparator, DefaultBaselineComparator
from cv_agent.evaluation.contracts import EvaluationResult
from cv_agent.evaluation.types import EvaluationVerdict, ExperimentId
from cv_agent.optimization.contracts import (
    OptimizationPlan,
    OptimizationResult,
    OptimizedArtifactEvaluationBinding,
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
        candidate_artifact_binding: OptimizedArtifactEvaluationBinding | None = None,
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
        candidate_artifact_binding: OptimizedArtifactEvaluationBinding | None = None,
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
        if str(artifact.source_checkpoint_id) != str(plan.source_checkpoint_id):
            raise OptimizationValidationError(
                f"Artifact source_checkpoint_id {artifact.source_checkpoint_id!r} does not match plan {plan.source_checkpoint_id!r}"
            )

        # 2. Admissible Evaluation Verdicts (B9: Fail-closed on non-passed verdicts)
        if baseline_evaluation.verdict != EvaluationVerdict.PASSED:
            raise OptimizationVerificationError(
                f"Baseline evaluation verdict must be PASSED, got {baseline_evaluation.verdict.value!r}"
            )
        if candidate_evaluation.verdict != EvaluationVerdict.PASSED:
            raise OptimizationVerificationError(
                f"Candidate evaluation verdict must be PASSED, got {candidate_evaluation.verdict.value!r}"
            )

        # 3. Verification Dataset Binding equality across baseline, candidate, and plan (B9)
        target_ds = plan.verification_dataset_binding
        for eval_label, eval_obj in (
            ("baseline", baseline_evaluation),
            ("candidate", candidate_evaluation),
        ):
            ds = eval_obj.plan.dataset_binding
            if str(ds.dataset_id) != str(target_ds.dataset_id):
                raise OptimizationVerificationError(
                    f"{eval_label.capitalize()} evaluation dataset_id {ds.dataset_id!r} does not match "
                    f"OptimizationPlan verification_dataset_binding {target_ds.dataset_id!r}"
                )
            if str(ds.dataset_version) != str(target_ds.dataset_version):
                raise OptimizationVerificationError(
                    f"{eval_label.capitalize()} evaluation dataset_version {ds.dataset_version!r} does not match "
                    f"OptimizationPlan verification_dataset_binding {target_ds.dataset_version!r}"
                )
            if str(ds.split_name) != str(target_ds.split_name):
                raise OptimizationVerificationError(
                    f"{eval_label.capitalize()} evaluation split_name {ds.split_name!r} does not match "
                    f"OptimizationPlan verification_dataset_binding {target_ds.split_name!r}"
                )
            if str(ds.dataset_manifest_hash) != str(target_ds.dataset_manifest_hash):
                raise OptimizationVerificationError(
                    f"{eval_label.capitalize()} evaluation dataset_manifest_hash {ds.dataset_manifest_hash!r} does not match "
                    f"OptimizationPlan verification_dataset_binding {target_ds.dataset_manifest_hash!r}"
                )

        # 4. Mandatory Candidate Artifact Binding (B14)
        if candidate_artifact_binding is None:
            raise OptimizationVerificationError(
                "candidate_artifact_binding is mandatory for candidate verification. "
                "Candidate evaluation must bind explicitly to the optimized artifact identity and hash."
            )
        if str(candidate_artifact_binding.candidate_artifact_id) != str(
            artifact.artifact_id
        ):
            raise OptimizationVerificationError(
                f"Candidate artifact binding ID {candidate_artifact_binding.candidate_artifact_id!r} "
                f"does not match artifact {artifact.artifact_id!r}"
            )
        if str(candidate_artifact_binding.candidate_artifact_hash) != str(
            artifact.artifact_hash
        ):
            raise OptimizationVerificationError(
                f"Candidate artifact binding hash {candidate_artifact_binding.candidate_artifact_hash!r} "
                f"does not match artifact hash {artifact.artifact_hash!r}"
            )
        if candidate_artifact_binding.evaluation_id is not None and str(
            candidate_artifact_binding.evaluation_id
        ) != str(candidate_evaluation.evaluation_id):
            raise OptimizationVerificationError(
                f"Candidate artifact binding evaluation_id {candidate_artifact_binding.evaluation_id!r} "
                f"does not match candidate evaluation {candidate_evaluation.evaluation_id!r}"
            )

        # 5. ADR-0011 Model / Checkpoint Lineage Verification
        base_model = baseline_evaluation.plan.model_artifact
        if str(base_model.checkpoint_id) != str(plan.source_checkpoint_id):
            raise OptimizationVerificationError(
                f"Baseline evaluation model checkpoint {base_model.checkpoint_id!r} does not match "
                f"plan source_checkpoint_id {plan.source_checkpoint_id!r}"
            )
        if str(base_model.weights_hash) != str(plan.source_weights_hash):
            raise OptimizationVerificationError(
                f"Baseline evaluation weights hash {base_model.weights_hash!r} does not match "
                f"plan source_weights_hash {plan.source_weights_hash!r}"
            )

        cand_model = candidate_evaluation.plan.model_artifact
        if str(cand_model.checkpoint_id) != str(plan.source_checkpoint_id):
            raise OptimizationVerificationError(
                f"Candidate evaluation model checkpoint lineage {cand_model.checkpoint_id!r} does not match "
                f"source checkpoint {plan.source_checkpoint_id!r}"
            )
        if str(cand_model.weights_hash) != str(plan.source_weights_hash):
            raise OptimizationVerificationError(
                f"Candidate evaluation weights hash lineage {cand_model.weights_hash!r} does not match "
                f"source weights hash {plan.source_weights_hash!r}"
            )

        # 6. Hardware / Target environment binding verification
        base_hw = baseline_evaluation.environment.target_hardware
        cand_hw = candidate_evaluation.environment.target_hardware
        if base_hw != cand_hw:
            raise OptimizationVerificationError(
                f"Hardware environment mismatch: baseline executed on {base_hw!r}, "
                f"candidate executed on {cand_hw!r}"
            )
        target_platform_str = str(plan.target_platform).strip()
        norm_plan_platform = (
            target_platform_str.lower().replace("_", " ").replace("-", " ")
        )
        norm_base_hw = base_hw.lower().replace("_", " ").replace("-", " ")
        if (
            target_platform_str
            and norm_plan_platform not in norm_base_hw
            and norm_base_hw not in norm_plan_platform
        ):
            raise OptimizationVerificationError(
                f"Evaluation hardware {base_hw!r} does not match plan target_platform {plan.target_platform!r}"
            )

        # 6. Benchmark timing synchronization check (B9)
        for b in baseline_evaluation.system_benchmarks:
            if not b.timing_synchronized:
                raise OptimizationVerificationError(
                    f"Baseline benchmark {b.benchmark_id} timing is not hardware-synchronized. "
                    "Synchronized timing is mandatory per ADR-0011 §3.13."
                )
        for b in candidate_evaluation.system_benchmarks:
            if not b.timing_synchronized:
                raise OptimizationVerificationError(
                    f"Candidate benchmark {b.benchmark_id} timing is not hardware-synchronized. "
                    "Synchronized timing is mandatory per ADR-0011 §3.13."
                )

        # 7. Deterministic primary metric selection (B9)
        cand_map = {
            m.metric.name: m.primary_value for m in candidate_evaluation.quality_metrics
        }
        base_map = {
            m.metric.name: m.primary_value for m in baseline_evaluation.quality_metrics
        }
        common_metrics = set(cand_map.keys()).intersection(base_map.keys())
        if not common_metrics:
            raise OptimizationVerificationError(
                "No overlapping quality metrics between candidate and baseline evaluation"
            )

        primary_metric_name: str
        if "primary_metric_name" in plan.parameters:
            req_metric = str(plan.parameters["primary_metric_name"])
            if req_metric not in common_metrics:
                raise OptimizationVerificationError(
                    f"Required primary_metric_name {req_metric!r} declared in plan.parameters "
                    f"is not present in both evaluations. Overlapping: {sorted(common_metrics)}"
                )
            primary_metric_name = req_metric
        else:
            standard_candidates = ["map50", "mAP50", "mAP", "accuracy", "f1"]
            primary_metric_name = min(common_metrics)
            for cand_name in standard_candidates:
                if cand_name in common_metrics:
                    primary_metric_name = cand_name
                    break

        cand_val = cand_map[primary_metric_name]
        base_val = base_map[primary_metric_name]

        accuracy_drop = base_val - cand_val
        retention_ratio = (cand_val / base_val) if base_val > 0.0 else 1.0

        # 8. Statistical Baseline Comparison via ADR-0011
        comparator = baseline_comparator or self.baseline_comparator
        comparison = comparator.compare(
            candidate_metrics=candidate_evaluation.quality_metrics,
            baseline_metrics=baseline_evaluation.quality_metrics,
            candidate_benchmarks=candidate_evaluation.system_benchmarks,
            baseline_benchmarks=baseline_evaluation.system_benchmarks,
            candidate_exp_id=ExperimentId(str(candidate_evaluation.evaluation_id)),
            baseline_exp_id=ExperimentId(str(baseline_evaluation.evaluation_id)),
        )

        # 9. Systems speedup and memory reduction calculation (B9: Deterministic batch matching)
        speedup_ratio = 1.0
        memory_reduction_ratio = 0.0

        base_bench_by_batch = {
            b.batch_size: b for b in baseline_evaluation.system_benchmarks
        }
        cand_bench_by_batch = {
            b.batch_size: b for b in candidate_evaluation.system_benchmarks
        }
        common_batches = sorted(
            set(base_bench_by_batch.keys()).intersection(cand_bench_by_batch.keys())
        )

        if common_batches:
            matched_batch = 1 if 1 in common_batches else common_batches[0]
            b_base = base_bench_by_batch[matched_batch]
            b_cand = cand_bench_by_batch[matched_batch]
            if b_base.throughput_fps > 0.0:
                speedup_ratio = b_cand.throughput_fps / b_base.throughput_fps
            if (
                b_base.peak_vram_gb is not None
                and b_cand.peak_vram_gb is not None
                and b_base.peak_vram_gb > 0.0
                and b_cand.peak_vram_gb <= b_base.peak_vram_gb
            ):
                memory_reduction_ratio = round(
                    1.0 - (b_cand.peak_vram_gb / b_base.peak_vram_gb), 4
                )

        # 10. Evaluate accuracy drop tolerance guardrail
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
            candidate_artifact_binding=candidate_artifact_binding,
        )
