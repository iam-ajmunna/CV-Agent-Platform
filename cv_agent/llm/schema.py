"""cv_agent.llm.schema — Pure Python deterministic JSON Schema validator.

Enforces structural and type validation for structured output schemas without
requiring external third-party dependencies.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class SchemaValidationError(ValueError):
    """Raised when structured output fails JSON schema validation."""

    def __init__(self, message: str, path: str = "$") -> None:
        self.path = path
        super().__init__(f"Validation failed at '{path}': {message}")


def _matches_type(instance: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(instance, str) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "array":
        return isinstance(instance, (list, tuple))
    if expected == "object":
        return isinstance(instance, (dict, Mapping))
    if expected == "null":
        return instance is None
    return True


def validate_json_schema(
    instance: Any,
    schema: Mapping[str, Any],
    path: str = "$",
) -> None:
    """Validate a JSON-compatible Python object against a JSON schema specification.

    Raises:
        SchemaValidationError: If instance does not conform to schema constraints.
    """
    if not isinstance(schema, Mapping):
        return

    # 1. Type validation
    if "type" in schema:
        expected_types = schema["type"]
        if isinstance(expected_types, str):
            expected_types = [expected_types]

        if not any(_matches_type(instance, exp) for exp in expected_types):
            raise SchemaValidationError(
                f"expected type {schema['type']!r}, got '{type(instance).__name__}'",
                path=path,
            )

    # 2. Enum validation
    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaValidationError(
            f"value {instance!r} is not in allowed enum {schema['enum']!r}",
            path=path,
        )

    # 3. Const validation
    if "const" in schema and instance != schema["const"]:
        raise SchemaValidationError(
            f"value {instance!r} does not match expected const {schema['const']!r}",
            path=path,
        )

    # 4. Object validation
    if isinstance(instance, (dict, Mapping)):
        # Required properties
        if "required" in schema:
            req_keys = schema["required"]
            if isinstance(req_keys, (list, tuple, set, frozenset)):
                for key in req_keys:
                    if key not in instance:
                        raise SchemaValidationError(
                            f"missing required property {key!r}",
                            path=path,
                        )

        # Property validation
        props = schema.get("properties", {})
        if isinstance(props, Mapping):
            for prop_name, prop_schema in props.items():
                if prop_name in instance and isinstance(prop_schema, Mapping):
                    validate_json_schema(
                        instance[prop_name],
                        prop_schema,
                        path=f"{path}.{prop_name}",
                    )

        # Additional properties
        if "additionalProperties" in schema:
            add_props = schema["additionalProperties"]
            allowed_props = set(props.keys()) if isinstance(props, Mapping) else set()
            extra_props = set(instance.keys()) - allowed_props

            if add_props is False and extra_props:
                raise SchemaValidationError(
                    f"unexpected additional properties: {sorted(extra_props)}",
                    path=path,
                )
            elif isinstance(add_props, Mapping):
                for extra_key in extra_props:
                    validate_json_schema(
                        instance[extra_key],
                        add_props,
                        path=f"{path}.{extra_key}",
                    )

    # 5. Array validation
    if isinstance(instance, (list, tuple)):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaValidationError(
                f"array length {len(instance)} is less than minItems {schema['minItems']}",
                path=path,
            )
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise SchemaValidationError(
                f"array length {len(instance)} is greater than maxItems {schema['maxItems']}",
                path=path,
            )
        if "items" in schema and isinstance(schema["items"], Mapping):
            item_schema = schema["items"]
            for idx, item in enumerate(instance):
                validate_json_schema(item, item_schema, path=f"{path}[{idx}]")

    # 6. String constraints
    if isinstance(instance, str) and not isinstance(instance, bool):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaValidationError(
                f"string length {len(instance)} is less than minLength {schema['minLength']}",
                path=path,
            )
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise SchemaValidationError(
                f"string length {len(instance)} is greater than maxLength {schema['maxLength']}",
                path=path,
            )
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            raise SchemaValidationError(
                f"string does not match pattern {schema['pattern']!r}",
                path=path,
            )

    # 7. Numeric constraints
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaValidationError(
                f"value {instance} is less than minimum {schema['minimum']}",
                path=path,
            )
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaValidationError(
                f"value {instance} is greater than maximum {schema['maximum']}",
                path=path,
            )
