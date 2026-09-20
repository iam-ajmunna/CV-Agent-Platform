"""cv_agent.monitoring.drift — Pure-Python statistical drift and anomaly engine for ADR-0013."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from cv_agent.monitoring.contracts import (
    BaselineReferenceBinding,
    DriftAnalysisRecord,
    DriftMetricResult,
    UnifiedTelemetrySnapshot,
)
from cv_agent.monitoring.errors import BaselineMismatchError, DriftEvaluationError
from cv_agent.monitoring.types import (
    DriftAnalysisId,
    DriftMetricType,
    DriftSeverity,
    TelemetryDimension,
)


def _gammp(s: float, x: float) -> float:
    """Regularized lower incomplete gamma function P(s, x) = gamma(s, x) / Gamma(s)."""
    if x < 0.0 or s <= 0.0:
        raise ValueError(f"Invalid gamma parameters: s={s}, x={x}")
    if x == 0.0:
        return 0.0
    if x < s + 1.0:
        # Series expansion
        ap = s
        summ = 1.0 / s
        delta = summ
        for _ in range(1, 250):
            ap += 1.0
            delta *= x / ap
            summ += delta
            if abs(delta) < abs(summ) * 1e-15:
                break
        return summ * math.exp(-x + s * math.log(x) - math.lgamma(s))
    else:
        # Continued fraction representation of Q(s, x) = 1 - P(s, x)
        b = x + 1.0 - s
        c = 1.0 / 1e-30
        d = 1.0 / b
        h = d
        for i in range(1, 250):
            an = -i * (i - s)
            b += 2.0
            d = an * d + b
            if abs(d) < 1e-30:
                d = 1e-30
            c = b + an / c
            if abs(c) < 1e-30:
                c = 1e-30
            d = 1.0 / d
            del_val = d * c
            h *= del_val
            if abs(del_val - 1.0) < 1e-15:
                break
        gammq = math.exp(-x + s * math.log(x) - math.lgamma(s)) * h
        return 1.0 - gammq


def _chi2_sf(x: float, df: int) -> float:
    """Survival function (1 - CDF) for Chi-Square distribution with df degrees of freedom."""
    if x <= 0.0:
        return 1.0
    if df <= 0:
        raise ValueError(f"Degrees of freedom must be positive, got {df}")
    s = df / 2.0
    x_half = x / 2.0
    p = _gammp(s, x_half)
    return max(0.0, min(1.0, 1.0 - p))


def _kolmogorov_sf(z: float) -> float:
    """Asymptotic survival function for the two-sample Kolmogorov-Smirnov test."""
    if z <= 0.0:
        return 1.0
    if z >= 3.5:
        return 0.0
    term_sum = 0.0
    for k in range(1, 101):
        sign = -1.0 if (k % 2 == 0) else 1.0
        exponent = -2.0 * (k * k) * (z * z)
        if exponent < -50.0:
            break
        term_sum += sign * math.exp(exponent)
    p = 2.0 * term_sum
    return max(0.0, min(1.0, p))


@runtime_checkable
class StatisticalDriftDetector(Protocol):
    """Contract for calculating distribution divergence against the validated baseline."""

    def evaluate_drift(
        self,
        telemetry_window: UnifiedTelemetrySnapshot,
        baseline: BaselineReferenceBinding,
    ) -> DriftAnalysisRecord:
        """Compute statistical drift metrics against the ADR-0011 baseline."""
        ...


class DefaultStatisticalDriftDetector:
    """Statistically rigorous drift and anomaly detection engine implementing ADR-0013.

    Calculates exact two-sample Kolmogorov-Smirnov distance, 1D Wasserstein distance,
    binned Population Stability Index (PSI), Chi-Square independence test,
    prediction entropy divergence, and system SLA violation checks.
    Fails closed when baseline distributions are absent or incomplete.
    """

    def __init__(
        self,
        ks_statistic_threshold: float = 0.15,
        ks_p_value_threshold: float = 0.05,
        psi_warning_threshold: float = 0.10,
        psi_critical_threshold: float = 0.25,
        wasserstein_threshold: float = 0.10,
        chi2_p_value_threshold: float = 0.05,
        entropy_shift_threshold: float = 0.20,
    ) -> None:
        self.ks_statistic_threshold = ks_statistic_threshold
        self.ks_p_value_threshold = ks_p_value_threshold
        self.psi_warning_threshold = psi_warning_threshold
        self.psi_critical_threshold = psi_critical_threshold
        self.wasserstein_threshold = wasserstein_threshold
        self.chi2_p_value_threshold = chi2_p_value_threshold
        self.entropy_shift_threshold = entropy_shift_threshold

    def evaluate_drift(
        self,
        telemetry_window: UnifiedTelemetrySnapshot,
        baseline: BaselineReferenceBinding,
    ) -> DriftAnalysisRecord:
        """Evaluate live multi-dimensional telemetry window against authoritative ADR-0011 baseline."""
        # 1. Validate baseline completeness per Baseline Primacy Invariant [P§29.2]
        if baseline is None or not isinstance(baseline, BaselineReferenceBinding):
            raise BaselineMismatchError(
                "Authoritative BaselineReferenceBinding is mandatory for statistical drift evaluation per [P§29.2]"
            )
        if not baseline.evaluation_id or not str(baseline.evaluation_id).strip():
            raise BaselineMismatchError(
                "BaselineReferenceBinding must link to an authoritative ADR-0011 evaluation_id"
            )
        if (
            not baseline.baseline_distribution_ref
            or not str(baseline.baseline_distribution_ref).strip()
        ):
            raise BaselineMismatchError(
                "BaselineReferenceBinding must declare baseline_distribution_ref"
            )
        if baseline.distribution_profile is None:
            raise BaselineMismatchError(
                "BaselineReferenceBinding lacks authoritative BaselineDistributionProfile per ADR-0013"
            )
        if (
            baseline.baseline_distribution_ref
            != baseline.distribution_profile.distribution_ref
        ):
            raise BaselineMismatchError(
                f"baseline_distribution_ref ({baseline.baseline_distribution_ref!r}) does not match "
                f"distribution_profile.distribution_ref ({baseline.distribution_profile.distribution_ref!r})"
            )

        model_t = telemetry_window.model_telemetry
        system_t = telemetry_window.system_telemetry
        data_t = telemetry_window.data_telemetry
        base_prof = baseline.distribution_profile

        # Validate that distribution evidence is present (no approximation of absent distributions)
        if not base_prof.confidence_histogram:
            raise DriftEvaluationError(
                "Authoritative baseline confidence_histogram is missing; cannot approximate distribution per [P§29.2]"
            )
        if not model_t.confidence_histogram:
            raise DriftEvaluationError(
                "Live model confidence_histogram is missing; cannot evaluate distribution drift"
            )
        if not base_prof.class_distribution:
            raise DriftEvaluationError(
                "Authoritative baseline class_distribution is missing; cannot approximate categorical balance"
            )

        metric_results: list[DriftMetricResult] = []

        # ---------------------------------------------------------------------
        # 1. Model Dimension: Kolmogorov-Smirnov Test on Confidence Distribution
        # ---------------------------------------------------------------------
        ks_result = self._compute_confidence_ks(
            model_t.confidence_histogram,
            base_prof.confidence_histogram,
            model_t.total_inferences,
            base_prof.sample_count,
        )
        metric_results.append(ks_result)

        # ---------------------------------------------------------------------
        # 2. Model Dimension: Wasserstein Distance on Confidence Distribution
        # ---------------------------------------------------------------------
        wass_result = self._compute_confidence_wasserstein(
            model_t.confidence_histogram, base_prof.confidence_histogram
        )
        metric_results.append(wass_result)

        # ---------------------------------------------------------------------
        # 3. Model Dimension: Population Stability Index (PSI) on Confidence
        # ---------------------------------------------------------------------
        psi_result = self._compute_confidence_psi(
            model_t.confidence_histogram, base_prof.confidence_histogram
        )
        metric_results.append(psi_result)

        # ---------------------------------------------------------------------
        # 4. Model Dimension: Chi-Square Test on Class Distribution
        # ---------------------------------------------------------------------
        chi2_result = self._compute_class_chi2(
            model_t.class_counts, base_prof.class_distribution
        )
        metric_results.append(chi2_result)

        # ---------------------------------------------------------------------
        # 5. Model Dimension: Prediction Entropy & JS Divergence
        # ---------------------------------------------------------------------
        entropy_result = self._compute_entropy_divergence(
            live_entropy_mean=model_t.prediction_entropy_mean,
            base_entropy_mean=base_prof.prediction_entropy_mean,
            live_class_props=model_t.class_proportions,
            base_class_props=base_prof.class_distribution,
        )
        metric_results.append(entropy_result)

        # ---------------------------------------------------------------------
        # 6. System Dimension: SLA Latency & Throughput Violations
        # ---------------------------------------------------------------------
        latency_result = self._evaluate_latency_sla(system_t, baseline)
        metric_results.append(latency_result)

        throughput_result = self._evaluate_throughput_sla(system_t, baseline)
        metric_results.append(throughput_result)

        # ---------------------------------------------------------------------
        # 7. Data Dimension: Optical Stream Health & Image Degradation
        # ---------------------------------------------------------------------
        data_quality_results = self._evaluate_data_quality(data_t)
        metric_results.extend(data_quality_results)

        # ---------------------------------------------------------------------
        # Aggregate Severity & Overall Verdict
        # ---------------------------------------------------------------------
        overall_drift = any(r.drift_detected for r in metric_results)
        severities = [r.severity for r in metric_results if r.drift_detected]

        if DriftSeverity.CRITICAL in severities:
            highest_sev = DriftSeverity.CRITICAL
        elif DriftSeverity.WARNING in severities:
            highest_sev = DriftSeverity.WARNING
        else:
            highest_sev = DriftSeverity.INFO

        # Synthesize clear, grounded diagnostic summary
        drift_metrics = [r.metric_name for r in metric_results if r.drift_detected]
        if drift_metrics:
            summary = (
                f"Statistical drift detected in {len(drift_metrics)} metric(s): {', '.join(drift_metrics)}. "
                f"Highest severity: {highest_sev.value.upper()}."
            )
        else:
            summary = (
                f"All {len(metric_results)} monitored metrics are within acceptable operational tolerances "
                f"relative to ADR-0011 baseline ({baseline.evaluation_id})."
            )

        analysis_id = DriftAnalysisId(
            f"drift-{telemetry_window.deployment_id.replace('/', '-')}-{int(telemetry_window.timestamp.timestamp())}"
        )

        return DriftAnalysisRecord(
            analysis_id=analysis_id,
            deployment_id=telemetry_window.deployment_id,
            baseline_binding=baseline,
            timestamp=datetime.now(timezone.utc),
            window_start=model_t.window_start,
            window_end=model_t.window_end,
            sample_count=model_t.total_inferences
            if model_t.total_inferences > 0
            else 1,
            metric_results=tuple(metric_results),
            overall_drift_detected=overall_drift,
            highest_severity=highest_sev,
            diagnostic_summary=summary,
        )

    # -------------------------------------------------------------------------
    # Helper Computations
    # -------------------------------------------------------------------------

    def _compute_confidence_ks(
        self,
        live_hist: Mapping[str, int],
        base_hist: Mapping[str, int],
        live_n: int,
        base_n: int,
    ) -> DriftMetricResult:
        """Compute Kolmogorov-Smirnov distance between live and baseline confidence histograms."""
        keys = sorted(set(live_hist.keys()) | set(base_hist.keys()))
        total_live = max(1, live_n if live_n > 0 else sum(live_hist.values()))
        total_base = max(1, base_n if base_n > 0 else sum(base_hist.values()))

        cum_live = 0.0
        cum_base = 0.0
        max_d = 0.0

        for k in keys:
            cum_live += live_hist.get(k, 0) / total_live
            cum_base += base_hist.get(k, 0) / total_base
            diff = abs(cum_live - cum_base)
            max_d = max(max_d, diff)

        # Asymptotic effective sample size
        n_eff = (total_live * total_base) / (total_live + total_base)
        z = max_d * math.sqrt(n_eff)
        p_val = _kolmogorov_sf(z)

        drift_detected = (max_d >= self.ks_statistic_threshold) and (
            p_val < self.ks_p_value_threshold
        )
        if max_d >= 0.25 and p_val < 0.01:
            severity = DriftSeverity.CRITICAL
        elif drift_detected:
            severity = DriftSeverity.WARNING
        else:
            severity = DriftSeverity.INFO

        return DriftMetricResult(
            dimension=TelemetryDimension.MODEL,
            metric_name="confidence_ks",
            drift_type=DriftMetricType.KOLMOGOROV_SMIRNOV,
            statistic_value=round(max_d, 4),
            p_value=round(p_val, 6),
            statistic_threshold=self.ks_statistic_threshold,
            p_value_threshold=self.ks_p_value_threshold,
            drift_detected=drift_detected,
            severity=severity,
            details={"effective_sample_size": n_eff, "z_statistic": round(z, 4)},
        )

    def _compute_confidence_wasserstein(
        self,
        live_hist: Mapping[str, int],
        base_hist: Mapping[str, int],
    ) -> DriftMetricResult:
        """Compute Earth Mover's Distance between normalized cumulative histograms."""
        keys = sorted(set(live_hist.keys()) | set(base_hist.keys()))
        sum_live = max(1, sum(live_hist.values()))
        sum_base = max(1, sum(base_hist.values()))

        bin_width = 1.0 / max(1, len(keys))
        cum_live = 0.0
        cum_base = 0.0
        w1 = 0.0

        for k in keys:
            cum_live += live_hist.get(k, 0) / sum_live
            cum_base += base_hist.get(k, 0) / sum_base
            w1 += abs(cum_live - cum_base) * bin_width

        drift_detected = w1 >= self.wasserstein_threshold
        if w1 >= self.wasserstein_threshold * 2.0:
            severity = DriftSeverity.CRITICAL
        elif drift_detected:
            severity = DriftSeverity.WARNING
        else:
            severity = DriftSeverity.INFO

        return DriftMetricResult(
            dimension=TelemetryDimension.MODEL,
            metric_name="confidence_wasserstein",
            drift_type=DriftMetricType.WASSERSTEIN_DISTANCE,
            statistic_value=round(w1, 4),
            p_value=None,
            statistic_threshold=self.wasserstein_threshold,
            p_value_threshold=None,
            drift_detected=drift_detected,
            severity=severity,
            details={"bin_count": len(keys), "bin_width": bin_width},
        )

    def _compute_confidence_psi(
        self,
        live_hist: Mapping[str, int],
        base_hist: Mapping[str, int],
    ) -> DriftMetricResult:
        """Compute Population Stability Index between live and baseline confidence histograms."""
        keys = sorted(set(live_hist.keys()) | set(base_hist.keys()))
        sum_live = max(1, sum(live_hist.values()))
        sum_base = max(1, sum(base_hist.values()))
        eps = 1e-4

        psi = 0.0
        for k in keys:
            p_live = (live_hist.get(k, 0) / sum_live) + eps
            p_base = (base_hist.get(k, 0) / sum_base) + eps
            psi += (p_live - p_base) * math.log(p_live / p_base)

        drift_detected = psi >= self.psi_warning_threshold
        if psi >= self.psi_critical_threshold:
            severity = DriftSeverity.CRITICAL
        elif drift_detected:
            severity = DriftSeverity.WARNING
        else:
            severity = DriftSeverity.INFO

        return DriftMetricResult(
            dimension=TelemetryDimension.MODEL,
            metric_name="confidence_psi",
            drift_type=DriftMetricType.POPULATION_STABILITY_INDEX,
            statistic_value=round(psi, 4),
            p_value=None,
            statistic_threshold=self.psi_warning_threshold,
            p_value_threshold=None,
            drift_detected=drift_detected,
            severity=severity,
            details={"critical_threshold": self.psi_critical_threshold},
        )

    def _compute_class_chi2(
        self,
        live_counts: Mapping[str, int],
        base_proportions: Mapping[str, float],
    ) -> DriftMetricResult:
        """Compute Chi-Square independence test comparing observed class detections to baseline expectation."""
        classes = sorted(set(live_counts.keys()) | set(base_proportions.keys()))
        total_live = sum(live_counts.values())

        if total_live == 0 or len(classes) <= 1:
            return DriftMetricResult(
                dimension=TelemetryDimension.MODEL,
                metric_name="class_chi2",
                drift_type=DriftMetricType.CHI_SQUARE_INDEPENDENCE,
                statistic_value=0.0,
                p_value=1.0,
                statistic_threshold=15.0,
                p_value_threshold=self.chi2_p_value_threshold,
                drift_detected=False,
                severity=DriftSeverity.INFO,
                details={"reason": "insufficient_detections"},
            )

        eps = 1e-4
        chi2 = 0.0
        for c in classes:
            observed = float(live_counts.get(c, 0))
            expected = total_live * base_proportions.get(c, eps)
            if expected <= 0.0:
                expected = eps
            chi2 += ((observed - expected) ** 2) / expected

        df = max(1, len(classes) - 1)
        p_val = _chi2_sf(chi2, df)

        drift_detected = p_val < self.chi2_p_value_threshold
        if p_val < 0.001:
            severity = DriftSeverity.CRITICAL
        elif drift_detected:
            severity = DriftSeverity.WARNING
        else:
            severity = DriftSeverity.INFO

        return DriftMetricResult(
            dimension=TelemetryDimension.MODEL,
            metric_name="class_chi2",
            drift_type=DriftMetricType.CHI_SQUARE_INDEPENDENCE,
            statistic_value=round(chi2, 4),
            p_value=round(p_val, 6),
            statistic_threshold=15.0,
            p_value_threshold=self.chi2_p_value_threshold,
            drift_detected=drift_detected,
            severity=severity,
            details={"degrees_of_freedom": df, "total_detections": total_live},
        )

    def _compute_entropy_divergence(
        self,
        live_entropy_mean: float,
        base_entropy_mean: float,
        live_class_props: Mapping[str, float],
        base_class_props: Mapping[str, float],
    ) -> DriftMetricResult:
        """Compute prediction entropy shift and class Jensen-Shannon divergence."""
        entropy_delta = abs(live_entropy_mean - base_entropy_mean)

        classes = sorted(set(live_class_props.keys()) | set(base_class_props.keys()))
        eps = 1e-5
        js_div = 0.0

        if classes:
            p_live = [live_class_props.get(c, 0.0) + eps for c in classes]
            p_base = [base_class_props.get(c, 0.0) + eps for c in classes]
            sum_l = sum(p_live)
            sum_b = sum(p_base)
            p_live = [x / sum_l for x in p_live]
            p_base = [x / sum_b for x in p_base]
            m = [0.5 * (pl + pb) for pl, pb in zip(p_live, p_base)]

            kl_pm = sum(pl * math.log(pl / mi) for pl, mi in zip(p_live, m))
            kl_qm = sum(pb * math.log(pb / mi) for pb, mi in zip(p_base, m))
            js_div = 0.5 * (kl_pm + kl_qm)

        combined_stat = max(entropy_delta, js_div)
        drift_detected = combined_stat >= self.entropy_shift_threshold
        if combined_stat >= self.entropy_shift_threshold * 2.0:
            severity = DriftSeverity.CRITICAL
        elif drift_detected:
            severity = DriftSeverity.WARNING
        else:
            severity = DriftSeverity.INFO

        return DriftMetricResult(
            dimension=TelemetryDimension.MODEL,
            metric_name="entropy_divergence",
            drift_type=DriftMetricType.ENTROPY_DIVERGENCE,
            statistic_value=round(combined_stat, 4),
            p_value=None,
            statistic_threshold=self.entropy_shift_threshold,
            p_value_threshold=None,
            drift_detected=drift_detected,
            severity=severity,
            details={
                "entropy_delta": round(entropy_delta, 4),
                "js_divergence": round(js_div, 4),
                "live_entropy_mean": live_entropy_mean,
                "base_entropy_mean": base_entropy_mean,
            },
        )

    def _evaluate_latency_sla(
        self,
        system_t: Any,
        baseline: BaselineReferenceBinding,
    ) -> DriftMetricResult:
        """Evaluate online latency percentiles against baseline SLA ceiling."""
        observed_p95 = system_t.latency_p95_ms
        p95_ceiling = baseline.baseline_latency_p95_ms
        p99_ceiling = baseline.baseline_latency_p99_ms

        drift_detected = observed_p95 > p95_ceiling
        if observed_p95 > p99_ceiling:
            severity = DriftSeverity.CRITICAL
        elif drift_detected:
            severity = DriftSeverity.WARNING
        else:
            severity = DriftSeverity.INFO

        # Calculate deviation in milliseconds beyond threshold
        deviation_ms = max(0.0, observed_p95 - p95_ceiling)

        return DriftMetricResult(
            dimension=TelemetryDimension.SYSTEM,
            metric_name="latency_sla_breach",
            drift_type=DriftMetricType.Z_SCORE_DEVIATION,
            statistic_value=round(deviation_ms, 2),
            p_value=None,
            statistic_threshold=0.0,
            p_value_threshold=None,
            drift_detected=drift_detected,
            severity=severity,
            details={
                "observed_latency_p95_ms": observed_p95,
                "baseline_latency_p95_ms": p95_ceiling,
                "baseline_latency_p99_ms": p99_ceiling,
            },
        )

    def _evaluate_throughput_sla(
        self,
        system_t: Any,
        baseline: BaselineReferenceBinding,
    ) -> DriftMetricResult:
        """Evaluate throughput FPS against baseline lower bound."""
        observed_fps = system_t.throughput_fps
        floor_fps = baseline.baseline_fps_p05
        nominal_fps = baseline.baseline_fps_p50

        drift_detected = observed_fps < floor_fps
        if observed_fps < (floor_fps * 0.75):
            severity = DriftSeverity.CRITICAL
        elif drift_detected:
            severity = DriftSeverity.WARNING
        else:
            severity = DriftSeverity.INFO

        deficit = max(0.0, floor_fps - observed_fps)

        return DriftMetricResult(
            dimension=TelemetryDimension.SYSTEM,
            metric_name="throughput_fps_deficit",
            drift_type=DriftMetricType.Z_SCORE_DEVIATION,
            statistic_value=round(deficit, 2),
            p_value=None,
            statistic_threshold=0.0,
            p_value_threshold=None,
            drift_detected=drift_detected,
            severity=severity,
            details={
                "observed_fps": observed_fps,
                "baseline_fps_p05": floor_fps,
                "baseline_fps_p50": nominal_fps,
            },
        )

    def _evaluate_data_quality(self, data_t: Any) -> list[DriftMetricResult]:
        """Evaluate optical quality, occlusion, and network packet loss metrics."""
        results: list[DriftMetricResult] = []

        # 1. Blur / Laplacian variance
        blur_val = data_t.blur_laplacian_variance
        blur_threshold = 40.0
        blur_drift = blur_val < blur_threshold
        results.append(
            DriftMetricResult(
                dimension=TelemetryDimension.DATA,
                metric_name="optical_blur",
                drift_type=DriftMetricType.Z_SCORE_DEVIATION,
                statistic_value=round(blur_val, 2),
                p_value=None,
                statistic_threshold=blur_threshold,
                p_value_threshold=None,
                drift_detected=blur_drift,
                severity=DriftSeverity.WARNING if blur_drift else DriftSeverity.INFO,
                details={"blur_laplacian_variance": blur_val},
            )
        )

        # 2. Camera occlusion
        occlusion_drift = bool(data_t.occlusion_detected)
        results.append(
            DriftMetricResult(
                dimension=TelemetryDimension.DATA,
                metric_name="camera_occlusion",
                drift_type=DriftMetricType.Z_SCORE_DEVIATION,
                statistic_value=1.0 if occlusion_drift else 0.0,
                p_value=None,
                statistic_threshold=1.0,
                p_value_threshold=None,
                drift_detected=occlusion_drift,
                severity=DriftSeverity.CRITICAL
                if occlusion_drift
                else DriftSeverity.INFO,
                details={"occlusion_detected": occlusion_drift},
            )
        )

        # 3. Packet loss
        packet_loss = data_t.packet_loss_ratio or 0.0
        loss_threshold = 0.05
        loss_drift = packet_loss > loss_threshold
        results.append(
            DriftMetricResult(
                dimension=TelemetryDimension.DATA,
                metric_name="stream_packet_loss",
                drift_type=DriftMetricType.Z_SCORE_DEVIATION,
                statistic_value=round(packet_loss, 4),
                p_value=None,
                statistic_threshold=loss_threshold,
                p_value_threshold=None,
                drift_detected=loss_drift,
                severity=DriftSeverity.CRITICAL
                if packet_loss > 0.15
                else (DriftSeverity.WARNING if loss_drift else DriftSeverity.INFO),
                details={"packet_loss_ratio": packet_loss},
            )
        )

        return results
