"""Deterministic immutable JSON-compatible value helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType

from cv_agent.domain.errors import DomainValidationError, ValidationCode

JsonScalar = str | int | float | bool | None
FrozenJsonValue = (
    JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]
)


def freeze_json_mapping(
    value: Mapping[str, object], *, field: str
) -> Mapping[str, FrozenJsonValue]:
    """Copy a JSON-compatible mapping into an immutable, recursively frozen mapping."""
    if not isinstance(value, Mapping):
        raise DomainValidationError(
            ValidationCode.INVALID_VALUE, field, "must be a mapping", value
        )
    frozen: dict[str, FrozenJsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise DomainValidationError(
                ValidationCode.INVALID_VALUE, field, "mapping keys must be strings", key
            )
        frozen[key] = freeze_json_value(item, field=field)
    return MappingProxyType(frozen)


def freeze_json_value(value: object, *, field: str) -> FrozenJsonValue:
    """Validate and recursively freeze a JSON-compatible external-boundary value."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return freeze_json_mapping(value, field=field)
    if isinstance(value, tuple | list):
        return tuple(freeze_json_value(item, field=field) for item in value)
    raise DomainValidationError(
        ValidationCode.INVALID_VALUE,
        field,
        "must contain only JSON-compatible values",
        value,
    )


def to_json_value(value: FrozenJsonValue) -> object:
    """Convert immutable values into ordinary deterministic JSON primitives."""
    if isinstance(value, Mapping):
        return {key: to_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [to_json_value(item) for item in value]
    return value


def canonical_json(value: Mapping[str, object]) -> str:
    """Produce a deterministic JSON representation for a validated mapping."""
    frozen = freeze_json_mapping(value, field="serialization")
    return json.dumps(
        to_json_value(frozen),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
