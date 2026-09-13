"""cv_agent.orchestration.canonicalization — Deterministic operation canonicalization engine.

Implements ADR-0003 §3.4 and PM Gate 4:
- Canonical payload represents purely effectful intent.
- Filters out volatile observational metadata (timestamps, trace/execution IDs, worker IDs, retry counters, telemetry).
- Deterministic UTF-8 encoding with Unicode NFC normalization.
- Lexicographically sorted dictionary keys.
- Stable set representation.
- Canonical SHA-256 payload hashing.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any

from pydantic import BaseModel

from cv_agent.orchestration.contracts import OperationPayloadEnvelope

CANONICALIZATION_VERSION: str = "cv-agent-canonical-v1"

# PM Gate 4: Strictly exclude observational/runtime-only keys from canonical payload
_OBSERVATIONAL_KEYS: frozenset[str] = frozenset(
    {
        "timestamp",
        "timestamps",
        "created_at",
        "updated_at",
        "executed_at",
        "started_at",
        "finished_at",
        "trace_id",
        "execution_id",
        "attempt_id",
        "worker_id",
        "owner_instance_id",
        "instance_id",
        "retry_count",
        "retries",
        "telemetry",
        "latency_ms",
        "duration_ms",
        "wall_clock",
        "observations",
        "runtime_metadata",
    }
)


def _canonicalize_value(value: Any) -> Any:
    """Recursively normalize values into a deterministic, observational-free representation."""
    if value is None or isinstance(value, (bool, int)):
        return value

    if isinstance(value, float):
        # Format floats consistently to avoid cross-platform float precision artifact strings
        return f"{value:.10g}"

    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)

    if isinstance(value, BaseModel):
        return _canonicalize_value(value.model_dump())

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _canonicalize_value(dataclasses.asdict(value))

    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            key_str = unicodedata.normalize("NFC", str(k))
            if key_str.lower() in _OBSERVATIONAL_KEYS:
                continue
            cleaned[key_str] = _canonicalize_value(v)
        # Return sorted dictionary
        return {k: cleaned[k] for k in sorted(cleaned.keys())}

    if isinstance(value, AbstractSet):
        # Sets must be deterministically sorted by their canonical json serialization
        canon_elements = [_canonicalize_value(elem) for elem in value]
        return sorted(
            canon_elements,
            key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False),
        )

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonicalize_value(item) for item in value]

    return unicodedata.normalize("NFC", str(value))


def canonicalize_payload(payload: object) -> str:
    """Convert an arbitrary payload into its authoritative canonical JSON string.

    Guarantees:
    1. Unicode NFC normalization.
    2. Removal of observational/runtime metadata.
    3. Deterministic key ordering and set sorting.
    4. Compact UTF-8 formatting (separators=(',', ':')).
    """
    normalized = _canonicalize_value(payload)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_payload_hash(payload: object) -> str:
    """Compute the authoritative SHA-256 digest of the canonicalized payload."""
    canonical_json = canonicalize_payload(payload)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_operation_envelope(
    schema_id: str,
    schema_version: int,
    payload: object,
) -> OperationPayloadEnvelope:
    """Construct an authoritative OperationPayloadEnvelope carrying canonical hash and version."""
    payload_hash = compute_payload_hash(payload)
    return OperationPayloadEnvelope(
        schema_id=schema_id,
        schema_version=schema_version,
        canonicalization_version=CANONICALIZATION_VERSION,
        payload_hash=payload_hash,
        payload=payload,
    )
