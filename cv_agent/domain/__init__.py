"""Immutable domain contracts shared by Phase 2 runtime subsystems.

This package owns value validation, semantic identifiers, and serialization
boundaries. It intentionally does not discover, execute, or persist anything.
"""

from cv_agent.domain.contracts import AvailabilityState, Stage
from cv_agent.domain.identifiers import AgentId, CapabilityId, SkillId, ToolId

__all__ = [
    "AgentId",
    "AvailabilityState",
    "CapabilityId",
    "SkillId",
    "Stage",
    "ToolId",
]
