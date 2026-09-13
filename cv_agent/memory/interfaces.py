"""cv_agent.memory.interfaces — Contracts for Git-tracked structured project memory.

Defines:
- HardwareConstraints dataclass mapping to hardware_constraints.yaml [P§29.4].
- ArchitecturalDecision dataclass for high-level model decisions.
- LearnedHeuristic dataclass for validated empirical heuristics.
- ProjectMemoryReader and ProjectMemoryWriter protocols.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HardwareConstraints:
    """Hardware resource budgets and deployment target specifications [P§29.4]."""

    target_device: str
    max_vram_mb: int
    max_ram_mb: int
    power_budget_watts: float | None
    latency_ceiling_ms: float | None
    supported_precisions: tuple[str, ...]


@dataclass(frozen=True)
class ArchitecturalDecision:
    """Documented high-level model or pipeline architecture decision."""

    decision_id: str
    timestamp: str
    title: str
    rationale: str
    linked_experiment_ids: tuple[str, ...]


@dataclass(frozen=True)
class LearnedHeuristic:
    """Empirical engineering rule of thumb discovered during project execution."""

    heuristic_id: str
    context: str
    observation: str
    recommendation: str
    evidence_experiment_ids: tuple[str, ...]


class ProjectMemoryReader(Protocol):
    """Read-only interface to Git-tracked structured project memory [P§33]."""

    def get_hardware_constraints(self) -> HardwareConstraints:
        """Retrieve target deployment hardware budgets."""
        ...

    def get_architectural_decisions(self) -> tuple[ArchitecturalDecision, ...]:
        """Retrieve documented architectural decisions and justifications."""
        ...

    def get_learned_heuristics(self) -> tuple[LearnedHeuristic, ...]:
        """Retrieve empirical engineering heuristics."""
        ...


class ProjectMemoryWriter(Protocol):
    """Controlled mutation interface for Git-tracked structured project memory."""

    def update_hardware_constraints(self, constraints: HardwareConstraints) -> None:
        """Record or update hardware resource budgets."""
        ...

    def record_architectural_decision(self, decision: ArchitecturalDecision) -> None:
        """Append an architectural decision with justification and experiment links."""
        ...

    def append_learned_heuristic(self, heuristic: LearnedHeuristic) -> None:
        """Append a newly validated empirical engineering heuristic."""
        ...
