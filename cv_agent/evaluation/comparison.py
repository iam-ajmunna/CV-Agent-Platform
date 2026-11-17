"""cv_agent.evaluation.comparison — Reproducible baseline comparison for ADR-0011."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast, runtime_checkable

from cv_agent.evaluation.contracts import (
    BaselineComparisonResult,
    BenchmarkResult,
    MetricResult,
)
from cv_agent.evaluation.errors import BaselineComparisonError
from cv_agent.evaluation.types import ExperimentId, MetricId, StatisticalTestType


def _betacf(
    a: float, b: float, x: float, max_iter: int = 200, eps: float = 3.0e-12
) -> float:
    """Continued fraction evaluation for regularized incomplete beta function."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        # Even step
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c

        # Odd step
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        del_val = d * c
        h *= del_val
        if abs(del_val - 1.0) <= eps:
            break
    return h


def _incbeta(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x < 0.0 or x > 1.0:
        raise ValueError(f"x must be in [0, 1], got {x}")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    else:
        return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_test_p_value(t: float, df: int) -> float:
    """Two-tailed p-value for Student's t-distribution with df degrees of freedom."""
    if df <= 0:
        raise ValueError("Degrees of freedom must be strictly positive")
    x = df / (df + t * t)
    p = _incbeta(0.5 * df, 0.5, x)
    return max(0.0, min(1.0, float(p)))


@runtime_checkable
class BaselineComparator(Protocol):
    """Protocol for comparative statistical evaluation against an authoritative baseline."""

    def compare(
        self,
        candidate_metrics: Sequence[MetricResult],
        baseline_metrics: Sequence[MetricResult],
        candidate_benchmarks: Sequence[BenchmarkResult],
        baseline_benchmarks: Sequence[BenchmarkResult],
        candidate_exp_id: ExperimentId,
        baseline_exp_id: ExperimentId,
        *,
        test_type: StatisticalTestType = StatisticalTestType.PAIRED_T_TEST,
        confidence_level: float = 0.95,
        paired_observations: (
            Mapping[MetricId, Sequence[tuple[float, float]]]
            | Sequence[tuple[float, float]]
            | None
        ) = None,
    ) -> BaselineComparisonResult: ...


class DefaultBaselineComparator:
    """Standard statistical baseline comparison engine adhering to [P§29.2] and [P§29.3]."""

    def compare(
        self,
        candidate_metrics: Sequence[MetricResult],
        baseline_metrics: Sequence[MetricResult],
        candidate_benchmarks: Sequence[BenchmarkResult],
        baseline_benchmarks: Sequence[BenchmarkResult],
        candidate_exp_id: ExperimentId,
        baseline_exp_id: ExperimentId,
        *,
        test_type: StatisticalTestType = StatisticalTestType.PAIRED_T_TEST,
        confidence_level: float = 0.95,
        paired_observations: (
            Mapping[MetricId, Sequence[tuple[float, float]]]
            | Sequence[tuple[float, float]]
            | None
        ) = None,
    ) -> BaselineComparisonResult:
        """Perform quantitative comparison, calculate deltas, test significance, and assess Pareto dominance."""
        if not candidate_metrics:
            raise BaselineComparisonError(
                "Candidate evaluation has no quality metrics to compare"
            )
        if not baseline_metrics:
            raise BaselineComparisonError(
                "Baseline evaluation has no quality metrics to compare"
            )
        if not (0.0 < confidence_level < 1.0):
            raise BaselineComparisonError(
                f"confidence_level must be in (0.0, 1.0), got {confidence_level}"
            )

        cand_map = {m.metric.metric_id: m for m in candidate_metrics}
        base_map = {m.metric.metric_id: m for m in baseline_metrics}

        shared_metric_ids = set(cand_map.keys()) & set(base_map.keys())
        if not shared_metric_ids:
            raise BaselineComparisonError(
                f"No overlapping metrics between candidate ({sorted(cand_map.keys())}) "
                f"and baseline ({sorted(base_map.keys())})"
            )

        quality_deltas: dict[MetricId, float] = {}
        improved_flags: list[bool] = []

        for mid in sorted(shared_metric_ids):
            c_res = cand_map[mid]
            b_res = base_map[mid]
            delta = c_res.primary_value - b_res.primary_value
            quality_deltas[mid] = round(delta, 5)

            if c_res.metric.higher_is_better:
                improved_flags.append(delta > 0.0)
            else:
                improved_flags.append(delta < 0.0)

        quality_improved = any(improved_flags)

        # Extract paired observation samples
        pairs: list[tuple[float, float]] | None = None
        if paired_observations is not None:
            if isinstance(paired_observations, Mapping):
                obs_map = cast(
                    Mapping[Any, Sequence[tuple[float, float]]], paired_observations
                )
                for mid in sorted(shared_metric_ids):
                    if mid in obs_map:
                        pairs = list(obs_map[mid])
                        break
                    elif str(mid) in obs_map:
                        pairs = list(obs_map[str(mid)])
                        break
            else:
                pairs = list(paired_observations)
        else:
            # Check metric metadata for observations
            for mid in sorted(shared_metric_ids):
                c_res = cand_map[mid]
                b_res = base_map[mid]
                if "paired_observations" in c_res.metadata:
                    pairs = list(c_res.metadata["paired_observations"])
                    break
                if (
                    "observations" in c_res.metadata
                    and "observations" in b_res.metadata
                ):
                    c_obs = list(c_res.metadata["observations"])
                    b_obs = list(b_res.metadata["observations"])
                    if len(c_obs) == len(b_obs) and len(c_obs) >= 2:
                        pairs = list(zip(c_obs, b_obs))
                        break

        # Rigorous statistical significance calculation over paired samples
        alpha = 1.0 - confidence_level
        p_val: float | None = None
        statistically_sig: bool | None = None
        sample_count = 0

        if pairs is not None and len(pairs) >= 2:
            sample_count = len(pairs)
            p_val, statistically_sig = self._compute_significance(
                pairs, test_type, alpha
            )
        elif pairs is not None:
            sample_count = len(pairs)

        # Systems deltas
        cand_b_map = {b.batch_size: b for b in candidate_benchmarks}
        base_b_map = {b.batch_size: b for b in baseline_benchmarks}
        shared_batches = set(cand_b_map.keys()) & set(base_b_map.keys())

        sys_deltas: dict[str, float] = {}
        latency_delta = 0.0
        throughput_delta = 0.0

        if shared_batches:
            primary_b = min(shared_batches)
            c_bench = cand_b_map[primary_b]
            b_bench = base_b_map[primary_b]

            latency_delta = round(c_bench.latency_mean_ms - b_bench.latency_mean_ms, 3)
            throughput_delta = round(c_bench.throughput_fps - b_bench.throughput_fps, 2)
            sys_deltas[f"latency_mean_ms_b{primary_b}"] = latency_delta
            sys_deltas[f"throughput_fps_b{primary_b}"] = throughput_delta
            if c_bench.peak_vram_gb is not None and b_bench.peak_vram_gb is not None:
                memory_delta = round(c_bench.peak_vram_gb - b_bench.peak_vram_gb, 3)
                sys_deltas[f"peak_vram_gb_b{primary_b}"] = memory_delta

        # Pareto dominance: quality improved and no systems regression
        pareto_dominant = (
            quality_improved and (latency_delta <= 0.0) and (throughput_delta >= 0.0)
        )

        if p_val is not None and statistically_sig is not None:
            summary = (
                f"Comparison vs baseline {baseline_exp_id}: quality_improved={quality_improved}, "
                f"p_value={p_val:.4f} (significant={statistically_sig}), "
                f"latency_delta={latency_delta:+.2f}ms, throughput_delta={throughput_delta:+.2f}fps, "
                f"pareto_dominant={pareto_dominant}"
            )
        else:
            summary = (
                f"Comparison vs baseline {baseline_exp_id}: quality_improved={quality_improved}, "
                "statistical_significance=inconclusive (insufficient paired samples), "
                f"latency_delta={latency_delta:+.2f}ms, throughput_delta={throughput_delta:+.2f}fps, "
                f"pareto_dominant={pareto_dominant}"
            )

        return BaselineComparisonResult(
            baseline_experiment_id=baseline_exp_id,
            candidate_experiment_id=candidate_exp_id,
            comparable_evidence_ref=f"comp-{candidate_exp_id}-vs-{baseline_exp_id}",
            test_type=test_type,
            test_version="v2.0",
            sample_count=sample_count,
            confidence_level=confidence_level,
            p_value=round(p_val, 4) if p_val is not None else None,
            statistically_significant=statistically_sig,
            quality_metric_deltas=quality_deltas,
            system_metric_deltas=sys_deltas,
            quality_improved=quality_improved,
            pareto_dominant=pareto_dominant,
            summary=summary,
        )

    def _compute_significance(
        self,
        pairs: Sequence[tuple[float, float]],
        test_type: StatisticalTestType,
        alpha: float,
    ) -> tuple[float | None, bool | None]:
        """Compute statistical test statistic and two-tailed p-value from paired observation samples."""
        n = len(pairs)
        if n < 2:
            return None, None

        if test_type == StatisticalTestType.PAIRED_T_TEST:
            diffs = [float(c) - float(b) for c, b in pairs]
            mean_d = sum(diffs) / n
            var_d = sum((d - mean_d) ** 2 for d in diffs) / (n - 1)
            if var_d <= 1e-15:
                if abs(mean_d) < 1e-12:
                    p_val = 1.0
                else:
                    p_val = 0.0
            else:
                se = math.sqrt(var_d / n)
                t = mean_d / se
                p_val = _t_test_p_value(t, n - 1)
            return p_val, bool(p_val < alpha)

        elif test_type == StatisticalTestType.WILCOXON_SIGNED_RANK:
            diffs = [float(c) - float(b) for c, b in pairs]
            non_zeros = [d for d in diffs if abs(d) > 1e-12]
            if not non_zeros:
                return 1.0, False
            nr = len(non_zeros)
            if nr < 2:
                return None, None

            # Rank absolute differences with tie averaging
            abs_sorted = sorted(enumerate(non_zeros), key=lambda x: abs(x[1]))
            ranks = [0.0] * nr
            i = 0
            tie_correction = 0.0
            while i < nr:
                j = i
                val = abs(abs_sorted[i][1])
                while j < nr and abs(abs(abs_sorted[j][1]) - val) < 1e-12:
                    j += 1
                tie_count = j - i
                avg_rank = (i + 1 + j) / 2.0
                for k in range(i, j):
                    orig_idx = abs_sorted[k][0]
                    ranks[orig_idx] = avg_rank
                if tie_count > 1:
                    tie_correction += (tie_count**3 - tie_count) / 2.0
                i = j

            pos_rank_sum = sum(ranks[idx] for idx, d in enumerate(non_zeros) if d > 0)
            mu_w = nr * (nr + 1) / 4.0
            var_w = (nr * (nr + 1) * (2 * nr + 1) - tie_correction) / 24.0
            if var_w <= 1e-15:
                p_val = 1.0 if abs(pos_rank_sum - mu_w) < 1e-12 else 0.0
            else:
                sigma_w = math.sqrt(var_w)
                diff_w = pos_rank_sum - mu_w
                if diff_w > 0:
                    z = (diff_w - 0.5) / sigma_w
                elif diff_w < 0:
                    z = (diff_w + 0.5) / sigma_w
                else:
                    z = 0.0
                p_val = math.erfc(abs(z) / math.sqrt(2.0))
            p_val = max(0.0, min(1.0, p_val))
            return p_val, bool(p_val < alpha)

        elif test_type == StatisticalTestType.BOOTSTRAP_RESAMPLING:
            diffs = [float(c) - float(b) for c, b in pairs]
            mean_d = sum(diffs) / n
            d_centered = [d - mean_d for d in diffs]
            b_reps = 2000
            rng = random.Random(42)
            exceed_count = 0
            for _ in range(b_reps):
                resample = [d_centered[rng.randint(0, n - 1)] for _ in range(n)]
                resample_mean = sum(resample) / n
                if abs(resample_mean) >= abs(mean_d):
                    exceed_count += 1
            p_val = (exceed_count + 1) / (b_reps + 1)
            p_val = max(0.0, min(1.0, p_val))
            return p_val, bool(p_val < alpha)

        elif test_type == StatisticalTestType.MCNEMAR_TEST:
            # Pairs are binary outcomes (1 for correct/success, 0 for failure)
            b_cases = sum(1 for c, b in pairs if c >= 0.5 and b < 0.5)
            c_cases = sum(1 for c, b in pairs if c < 0.5 and b >= 0.5)
            total_discordant = b_cases + c_cases
            if total_discordant < 2:
                if b_cases == 0 and c_cases == 0:
                    return 1.0, False
                return None, None
            chi2 = (
                ((abs(b_cases - c_cases) - 1.0) ** 2) / total_discordant
                if abs(b_cases - c_cases) > 0
                else 0.0
            )
            p_val = math.erfc(math.sqrt(chi2 / 2.0))
            p_val = max(0.0, min(1.0, p_val))
            return p_val, bool(p_val < alpha)

        return None, None
