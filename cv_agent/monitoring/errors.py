"""cv_agent.monitoring.errors — Domain exception hierarchy for ADR-0013."""

from __future__ import annotations


class MonitoringError(Exception):
    """Base exception for all monitoring and active learning domain errors."""


class TelemetryValidationError(MonitoringError):
    """Raised when telemetry records contain invalid values, ranges, or inconsistencies."""


class BaselineMismatchError(MonitoringError):
    """Raised when live telemetry lacks an authoritative ADR-0011 baseline binding or distribution profile."""


class DriftEvaluationError(MonitoringError):
    """Raised when statistical drift evaluation encounters degenerate, missing, or incompatible distribution data."""


class ActiveLearningBufferError(MonitoringError):
    """Raised when candidate selection buffer operations encounter capacity, eviction, or state errors."""


class ExpiredCandidatePayloadError(ActiveLearningBufferError):
    """Raised when attempting to curate, assemble, or stage a candidate sample whose storage TTL has expired."""


class UnauthorizedRetrainingError(MonitoringError):
    """Raised when retraining is triggered without explicit, verified human authorization per docs/APPROVALS.md."""
