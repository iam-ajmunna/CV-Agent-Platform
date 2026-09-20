"""cv_agent.monitoring.pipeline — Telemetry ingestion pipeline and coordinator for ADR-0013."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from cv_agent.monitoring.contracts import (
    BaselineReferenceBinding,
    DataTelemetryRecord,
    DriftAnalysisRecord,
    DriftMetricResult,
    ModelTelemetryRecord,
    MonitoringAlertRecord,
    RetrainingTriggerProposal,
    SystemTelemetryRecord,
    UnifiedTelemetrySnapshot,
)
from cv_agent.monitoring.drift import (
    DefaultStatisticalDriftDetector,
    StatisticalDriftDetector,
)
from cv_agent.monitoring.errors import (
    TelemetryValidationError,
    UnauthorizedRetrainingError,
)
from cv_agent.monitoring.types import (
    AlertId,
    CandidateBatchId,
    DriftAnalysisId,
    DriftSeverity,
    SnapshotId,
    TelemetryDimension,
    TriggerProposalId,
    TriggerReason,
)
from cv_agent.optimization.types import DeploymentId


@runtime_checkable
class TelemetryIngestionSink(Protocol):
    """Contract for consuming continuous runtime telemetry streams."""

    def ingest_model_telemetry(self, record: ModelTelemetryRecord) -> None:
        """Ingest a model telemetry window from a deployed runtime."""
        ...

    def ingest_system_telemetry(self, record: SystemTelemetryRecord) -> None:
        """Ingest hardware and throughput telemetry from an execution host."""
        ...

    def ingest_data_telemetry(self, record: DataTelemetryRecord) -> None:
        """Ingest optical stream health telemetry from an active camera feed."""
        ...


@runtime_checkable
class MonitoringCoordinator(Protocol):
    """Top-level monitoring coordinator interface."""

    def emit_alert(self, alert: MonitoringAlertRecord) -> None:
        """Dispatch immediate operational alerts to logging or orchestration."""
        ...

    def submit_retraining_proposal(
        self,
        proposal: RetrainingTriggerProposal,
    ) -> None:
        """Submit an evidence-backed retraining proposal to ADR-0008 Reasoning."""
        ...


class DefaultTelemetryPipeline:
    """Production telemetry ingestion pipeline and proposal coordinator.

    Maintains rolling buffers of incoming telemetry, synchronizes tri-dimensional snapshots,
    invokes statistical drift detection, dispatches operational alerts, and formulates
    advisory retraining proposals with strict human approval invariants [P§24].
    """

    def __init__(
        self,
        drift_detector: StatisticalDriftDetector | None = None,
        alert_sink: Callable[[MonitoringAlertRecord], None] | None = None,
        proposal_sink: Callable[[RetrainingTriggerProposal], None] | None = None,
    ) -> None:
        self.drift_detector = drift_detector or DefaultStatisticalDriftDetector()
        self._alert_sink = alert_sink
        self._proposal_sink = proposal_sink

        # Buffered records keyed by deployment_id
        self._model_buffer: dict[DeploymentId, list[ModelTelemetryRecord]] = {}
        self._system_buffer: dict[DeploymentId, list[SystemTelemetryRecord]] = {}
        self._data_buffer: dict[
            str, list[DataTelemetryRecord]
        ] = {}  # keyed by stream_id

        # Emitted history for audit
        self._emitted_alerts: list[MonitoringAlertRecord] = []
        self._emitted_proposals: list[RetrainingTriggerProposal] = []

    @property
    def emitted_alerts(self) -> tuple[MonitoringAlertRecord, ...]:
        """Audit log of emitted operational alerts."""
        return tuple(self._emitted_alerts)

    @property
    def emitted_proposals(self) -> tuple[RetrainingTriggerProposal, ...]:
        """Audit log of emitted retraining trigger proposals."""
        return tuple(self._emitted_proposals)

    def ingest_model_telemetry(self, record: ModelTelemetryRecord) -> None:
        """Ingest model telemetry record into rolling buffer."""
        if not isinstance(record, ModelTelemetryRecord):
            raise TelemetryValidationError(
                f"Expected ModelTelemetryRecord, got {type(record)}"
            )
        self._model_buffer.setdefault(record.deployment_id, []).append(record)

    def ingest_system_telemetry(self, record: SystemTelemetryRecord) -> None:
        """Ingest system hardware/throughput telemetry record into rolling buffer."""
        if not isinstance(record, SystemTelemetryRecord):
            raise TelemetryValidationError(
                f"Expected SystemTelemetryRecord, got {type(record)}"
            )
        self._system_buffer.setdefault(record.deployment_id, []).append(record)

    def ingest_data_telemetry(self, record: DataTelemetryRecord) -> None:
        """Ingest optical stream health telemetry record into rolling buffer."""
        if not isinstance(record, DataTelemetryRecord):
            raise TelemetryValidationError(
                f"Expected DataTelemetryRecord, got {type(record)}"
            )
        self._data_buffer.setdefault(record.stream_id, []).append(record)

    def create_snapshot(
        self,
        deployment_id: DeploymentId,
        stream_id: str,
        timestamp: datetime | None = None,
    ) -> UnifiedTelemetrySnapshot:
        """Synthesize a unified tri-dimensional telemetry snapshot from latest buffered records."""
        model_records = self._model_buffer.get(deployment_id)
        if not model_records:
            raise TelemetryValidationError(
                f"No buffered ModelTelemetryRecord found for deployment {deployment_id!r}"
            )
        system_records = self._system_buffer.get(deployment_id)
        if not system_records:
            raise TelemetryValidationError(
                f"No buffered SystemTelemetryRecord found for deployment {deployment_id!r}"
            )
        data_records = self._data_buffer.get(stream_id)
        if not data_records:
            raise TelemetryValidationError(
                f"No buffered DataTelemetryRecord found for stream {stream_id!r}"
            )

        ts = timestamp or datetime.now(timezone.utc)
        snap_id = SnapshotId(
            f"snap-{deployment_id.replace('/', '-')}-{stream_id}-{int(ts.timestamp())}"
        )

        return UnifiedTelemetrySnapshot(
            snapshot_id=snap_id,
            deployment_id=deployment_id,
            stream_id=stream_id,
            timestamp=ts,
            model_telemetry=model_records[-1],
            system_telemetry=system_records[-1],
            data_telemetry=data_records[-1],
        )

    def process_and_evaluate(
        self,
        snapshot: UnifiedTelemetrySnapshot,
        baseline: BaselineReferenceBinding,
    ) -> DriftAnalysisRecord:
        """Evaluate drift for a synchronized snapshot and emit operational alerts for breaches."""
        analysis = self.drift_detector.evaluate_drift(
            telemetry_window=snapshot,
            baseline=baseline,
        )

        # Check for immediate alerts on critical/warning breaches
        for result in analysis.metric_results:
            if result.drift_detected:
                mitigation = self._derive_mitigation(result, snapshot.deployment_id)
                alert = MonitoringAlertRecord(
                    alert_id=AlertId(
                        f"alert-{snapshot.deployment_id}-{result.metric_name}-{int(analysis.timestamp.timestamp())}"
                    ),
                    deployment_id=snapshot.deployment_id,
                    severity=result.severity,
                    timestamp=analysis.timestamp,
                    dimension=result.dimension,
                    message=f"Drift detected in {result.metric_name}: statistic={result.statistic_value} (threshold={result.statistic_threshold})",
                    observed_value=result.statistic_value,
                    threshold_value=result.statistic_threshold,
                    recommended_mitigation=mitigation,
                )
                self.emit_alert(alert)

        return analysis

    def _derive_mitigation(
        self, result: DriftMetricResult, deployment_id: DeploymentId
    ) -> str:
        """Derive actionable mitigation guidance based on anomaly dimension and type."""
        if (
            result.metric_name == "latency_sla_breach"
            and result.severity == DriftSeverity.CRITICAL
        ):
            return "Severe latency SLA ceiling breach. Recommend immediate traffic rollback to predecessor deployment via ADR-0012."
        if (
            result.metric_name == "throughput_fps_deficit"
            and result.severity == DriftSeverity.CRITICAL
        ):
            return "Hardware throughput degraded below operational floor. Check accelerator thermal conditions or rollback via ADR-0012."
        if result.metric_name == "camera_occlusion":
            return "Camera lens occlusion detected. Dispatch field maintenance to inspect physical sensor."
        if result.metric_name == "optical_blur":
            return "Optical defocus or lens contamination detected. Clean and recalibrate camera optics."
        if result.dimension == TelemetryDimension.MODEL:
            return "Distribution drift detected in model inference. Formulate retraining proposal for ADR-0008."
        return f"Investigate telemetry anomaly in deployment {deployment_id}."

    def emit_alert(self, alert: MonitoringAlertRecord) -> None:
        """Dispatch operational alert to logging sink and audit ledger."""
        self._emitted_alerts.append(alert)
        if self._alert_sink is not None:
            self._alert_sink(alert)

    def formulate_retraining_proposal(
        self,
        deployment_id: DeploymentId,
        trigger_reason: TriggerReason,
        severity: DriftSeverity,
        evidence_summary: str,
        estimated_new_samples: int,
        drift_analysis_id: DriftAnalysisId | None = None,
        candidate_batch_id: CandidateBatchId | None = None,
    ) -> RetrainingTriggerProposal:
        """Formulate an evidence-backed retraining trigger proposal.

        Enforces Invariant 3 (Zero Autonomous Retraining) and Invariant 4 (Mandatory Human Approval).
        """
        now = datetime.now(timezone.utc)
        proposal_id = TriggerProposalId(
            f"prop-{deployment_id.replace('/', '-')}-{trigger_reason.value}-{int(now.timestamp())}"
        )

        proposal = RetrainingTriggerProposal(
            proposal_id=proposal_id,
            deployment_id=deployment_id,
            created_at=now,
            trigger_reason=trigger_reason,
            severity=severity,
            drift_analysis_id=drift_analysis_id,
            candidate_batch_id=candidate_batch_id,
            evidence_summary=evidence_summary,
            estimated_new_samples=estimated_new_samples,
            requires_human_approval=True,
            approval_id=None,
            approved=False,
            approved_by=None,
            approval_timestamp=None,
        )

        self.submit_retraining_proposal(proposal)
        return proposal

    def submit_retraining_proposal(
        self,
        proposal: RetrainingTriggerProposal,
    ) -> None:
        """Submit retraining proposal to Reasoning (ADR-0008). Fails closed if human approval is bypassed."""
        if proposal.requires_human_approval is not True:
            raise UnauthorizedRetrainingError(
                "Proposal must require human approval per [P§24]. Autonomous retraining is strictly forbidden."
            )
        self._emitted_proposals.append(proposal)
        if self._proposal_sink is not None:
            self._proposal_sink(proposal)
