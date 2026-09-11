"""Structured validation errors for immutable domain contracts."""

from __future__ import annotations

from enum import StrEnum


class ValidationCode(StrEnum):
    """Machine-readable categories for domain input validation failures."""

    INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
    INVALID_VALUE = "INVALID_VALUE"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    INVALID_SERIALIZATION = "INVALID_SERIALIZATION"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"


class DomainValidationError(ValueError):
    """Validation failure with stable code, field, and rejected value metadata."""

    def __init__(
        self,
        code: ValidationCode,
        field: str,
        message: str,
        value: object | None = None,
    ) -> None:
        self.code = code
        self.field = field
        self.value = value
        super().__init__(f"{code}: {field}: {message}")
