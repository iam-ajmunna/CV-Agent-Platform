"""cv_agent.skills.errors — Normalized failure taxonomy for ADR-0007 Skills Architecture.

Implements ADR-0007 §5:
- A. Resolution & Discovery Failures:
  1. UNKNOWN_CAPABILITY
  2. NO_SATISFYING_SKILL
  3. KNOWN_BUT_UNAVAILABLE
  4. PREREQUISITE_FAILED
- B. Execution & Binding Failures:
  5. MISSING_TOOL_DEPENDENCY
  6. TARGET_MISMATCH
  7. PARAMETER_VALIDATION_FAILED
  8. APPROVAL_REQUIRED
  9. TIMEOUT
"""

from __future__ import annotations


class SkillError(Exception):
    """Base exception for all Skills subsystem failures."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class UnknownCapabilityError(SkillError):
    """Raised when a requested capability ID is not recognized by ADR-0001."""

    def __init__(self, message: str) -> None:
        super().__init__("UNKNOWN_CAPABILITY", message)


class NoSatisfyingSkillError(SkillError):
    """Raised when a capability is valid, but no registered skill declares support for it."""

    def __init__(self, message: str) -> None:
        super().__init__("NO_SATISFYING_SKILL", message)


class KnownButUnavailableError(SkillError):
    """Raised when a skill contract is known, but no compatible execution target exists (D-022)."""

    def __init__(self, message: str) -> None:
        super().__init__("KNOWN_BUT_UNAVAILABLE", message)


class PrerequisiteFailedError(SkillError):
    """Raised when environment or artifact prerequisites fail verification."""

    def __init__(self, message: str) -> None:
        super().__init__("PREREQUISITE_FAILED", message)


class MissingToolDependencyError(SkillError):
    """Raised when a skill requires a ToolId that is not configured or available."""

    def __init__(self, message: str) -> None:
        super().__init__("MISSING_TOOL_DEPENDENCY", message)


class TargetMismatchError(SkillError):
    """Raised when a candidate execution target does not satisfy skill target requirements."""

    def __init__(self, message: str) -> None:
        super().__init__("TARGET_MISMATCH", message)


class ParameterValidationError(SkillError):
    """Raised when input parameters fail JSON schema validation."""

    def __init__(self, message: str) -> None:
        super().__init__("PARAMETER_VALIDATION_FAILED", message)


class ApprovalRequiredError(SkillError):
    """Raised when a gated skill requires approval, but none was authorized."""

    def __init__(self, message: str) -> None:
        super().__init__("APPROVAL_REQUIRED", message)


class SkillTimeoutError(SkillError):
    """Raised when skill execution exceeds declared timeout_seconds."""

    def __init__(self, message: str) -> None:
        super().__init__("TIMEOUT", message)
