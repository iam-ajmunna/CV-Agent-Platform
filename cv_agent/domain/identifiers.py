"""Validated, semantically distinct identifiers for ADR-0001 entities."""

from __future__ import annotations

import re

from typing_extensions import Self

from cv_agent.domain.errors import DomainValidationError, ValidationCode

_IDENTIFIER_PATTERN = re.compile(r"[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*", re.ASCII)


class _Identifier(str):
    """Validated base for stable, namespaced ADR-0001 identifiers."""

    def __new__(cls, value: str) -> Self:
        if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
            raise DomainValidationError(
                ValidationCode.INVALID_IDENTIFIER,
                cls.__name__,
                "must match [a-z0-9_.-]+(/[a-z0-9_.-]+)*",
                value,
            )
        return super().__new__(cls, value)


class CapabilityId(_Identifier):
    """Identity of what the platform can accomplish."""


class SkillId(_Identifier):
    """Identity of declarative procedural expertise."""


class ToolId(_Identifier):
    """Identity of an executable interface, not its transport."""


class AgentId(_Identifier):
    """Identity of an execution-capable actor, not an execution target."""
