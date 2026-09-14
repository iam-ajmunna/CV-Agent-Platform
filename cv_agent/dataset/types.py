"""cv_agent.dataset.types — Authoritative identifiers and enumeration types (ADR-0009).

Cross-ADR binding module cited by ADR-0010 §4, ADR-0011 §4, ADR-0012 §4, and ADR-0013 §4.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import NewType

# --- Canonical Identifier Regexes (ADR-0001 §3.1, ADR-0009 §4, Gate B1) ---

# DatasetId: conforms to [a-z0-9_.-]+(/[a-z0-9_.-]+)* per ADR-0001 §3.1
_DATASET_ID_REGEX = re.compile(r"^[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*$")

# DatasetVersion: canonical SemVer 2.0.0 with mandatory 'v' prefix (Gate B1)
_DATASET_VERSION_REGEX = re.compile(
    r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

# ContentHash: lowercase 64-character hex string (SHA-256 digest)
_CONTENT_HASH_REGEX = re.compile(r"^[a-f0-9]{64}$")

# AnnotationVersion: conforms to [a-z0-9_.-]+
_ANNOTATION_VERSION_REGEX = re.compile(r"^[a-z0-9_.-]+$")

# GuidelineRef: URI or commit SHA conforming to [a-z0-9_./:-]+
_GUIDELINE_REF_REGEX = re.compile(r"^[a-z0-9_./:-]+$")


DatasetId = NewType("DatasetId", str)
"""Authoritative dataset identifier, e.g. 'cctv.perimeter.intrusion'."""

DatasetVersion = NewType("DatasetVersion", str)
"""Canonical SemVer 2.0.0 version string with mandatory 'v' prefix, e.g. 'v1.0.0'."""

ContentHash = NewType("ContentHash", str)
"""Cryptographic SHA-256 hex digest of payload bytes."""

AnnotationVersion = NewType("AnnotationVersion", str)
"""Authoritative annotation version string, e.g. 'ann-v1.0.0'."""

GuidelineRef = NewType("GuidelineRef", str)
"""URI or commit SHA pointing to the governing annotation guideline."""


def validate_dataset_id(value: str) -> DatasetId:
    """Validate and return a DatasetId conforming to [a-z0-9_.-]+(/[a-z0-9_.-]+)*."""
    if not isinstance(value, str) or not _DATASET_ID_REGEX.match(value):
        raise ValueError(
            f"DatasetId {value!r} does not match canonical format '^[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*$'"
        )
    return DatasetId(value)


def validate_dataset_version(value: str) -> DatasetVersion:
    """Validate and return a DatasetVersion conforming to strict SemVer 2.0.0 with 'v' prefix."""
    if not isinstance(value, str) or not _DATASET_VERSION_REGEX.match(value):
        raise ValueError(
            f"DatasetVersion {value!r} does not match canonical SemVer format 'vMAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]'"
        )
    return DatasetVersion(value)


def validate_content_hash(value: str) -> ContentHash:
    """Validate and return a ContentHash conforming to lowercase 64-hex SHA-256."""
    if not isinstance(value, str) or not _CONTENT_HASH_REGEX.match(value):
        raise ValueError(
            f"ContentHash {value!r} does not match canonical format '^[a-f0-9]{{64}}$'"
        )
    return ContentHash(value)


def validate_annotation_version(value: str) -> AnnotationVersion:
    """Validate and return an AnnotationVersion conforming to [a-z0-9_.-]+."""
    if not isinstance(value, str) or not _ANNOTATION_VERSION_REGEX.match(value):
        raise ValueError(
            f"AnnotationVersion {value!r} does not match canonical format '^[a-z0-9_.-]+$'"
        )
    return AnnotationVersion(value)


def validate_guideline_ref(value: str) -> GuidelineRef:
    """Validate and return a GuidelineRef conforming to [a-z0-9_./:-]+."""
    if not isinstance(value, str) or not _GUIDELINE_REF_REGEX.match(value):
        raise ValueError(
            f"GuidelineRef {value!r} does not match canonical format '^[a-z0-9_./:-]+$'"
        )
    return GuidelineRef(value)


# --- Enums (ADR-0009 §4) ---


class StorageBackendType(str, Enum):
    """Supported storage backend protocol types."""

    LOCAL_FS = "local_fs"
    S3 = "s3"
    GCS = "gcs"
    MINIO = "minio"


class SplitName(str, Enum):
    """Standard split partition identifiers."""

    TRAIN = "train"
    VAL = "val"
    TEST = "test"
    CALIBRATION = "calibration"


class LeakageType(str, Enum):
    """Taxonomy of dataset split leakage checks per docs/DATA.md."""

    TEMPORAL = "temporal"
    CAMERA_SITE = "camera_site"
    NEAR_DUPLICATE = "near_duplicate"
    SUBJECT_IDENTITY = "subject_identity"
    ANNOTATION_ROUND = "annotation_round"
