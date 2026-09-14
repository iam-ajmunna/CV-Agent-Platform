"""cv_agent.reasoning.errors — Authoritative ADR-0008 Error Hierarchy.

Normalized failure taxonomy for reasoning nodes, budget exhaustion,
grounding failures, and bounded schema repairs.
"""

from __future__ import annotations


class ReasoningError(Exception):
    """Base exception for all Reasoning Subsystem failures."""

    def __init__(self, message: str, *, code: str = "REASONING_ERROR") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CognitiveBudgetExceededError(ReasoningError):
    """Raised when the workflow cognitive hop budget is exhausted."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="COGNITIVE_BUDGET_EXCEEDED")


class SchemaRepairExhaustedError(ReasoningError):
    """Raised when schema repair attempts (maximum 2 repairs, 3 total completions) fail."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="SCHEMA_REPAIR_EXHAUSTED")


class UnanchoredCitationError(ReasoningError):
    """Raised when a cited URI is absent from the verified context evidence."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="UNANCHORED_CITATION")


class UncitedEmpiricalClaimError(ReasoningError):
    """Raised when an empirical quantitative claim is asserted without any supporting citations."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="UNCITED_EMPIRICAL_CLAIM")


class EvidenceContradictionError(ReasoningError):
    """Raised when an empirical claim contradicts the quantitative bounds of cited evidence."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="EVIDENCE_CONTRADICTION")


class UnsupportedSpeculationError(ReasoningError):
    """Raised when a high-confidence hypothesis lacks supporting citations or evidence."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="UNSUPPORTED_SPECULATION")


class ReasoningTransportError(ReasoningError):
    """Raised when the underlying LLM Gateway encounters an unhandled transport error."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="REASONING_TRANSPORT_ERROR")
