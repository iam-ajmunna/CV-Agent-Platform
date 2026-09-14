"""cv_agent.dataset — Authoritative Dataset Subsystem (ADR-0009).

Delivers manifest-first content-addressed storage, independent annotation versioning,
explicit immutable split memberships, 5-dimension leakage auditing, and geometry validation.
"""

from __future__ import annotations

from cv_agent.dataset.contracts import (
    AnnotationManifest,
    ClassDefinition,
    CollectionConditions,
    DatasetLineage,
    DatasetManifest,
    ItemMetadata,
    LeakageCheckResult,
    OperationIdentity,
    SplitMembership,
)
from cv_agent.dataset.errors import (
    DatasetApprovalRequiredError,
    DatasetError,
    DatasetNotFoundError,
    DatasetVersionConflictError,
    InvalidGeometryError,
    PathTraversalError,
    PayloadCorruptionError,
    PayloadIntegrityError,
    PayloadNotFoundError,
    SplitContaminationError,
    SplitLeakageError,
)
from cv_agent.dataset.leakage import (
    LeakageAuditor,
    StandardLeakageAuditor,
)
from cv_agent.dataset.registry import (
    DatasetRegistry,
    FileSystemDatasetRegistry,
)
from cv_agent.dataset.storage import (
    DatasetStore,
    GCSStore,
    LocalFSStore,
    MinioStore,
    S3Store,
)
from cv_agent.dataset.types import (
    AnnotationVersion,
    ContentHash,
    DatasetId,
    DatasetVersion,
    GuidelineRef,
    LeakageType,
    SplitName,
    StorageBackendType,
    validate_annotation_version,
    validate_content_hash,
    validate_dataset_id,
    validate_dataset_version,
    validate_guideline_ref,
)
from cv_agent.dataset.validation import (
    compute_payload_root_checksum,
    validate_annotation_geometry,
    verify_payload_root_checksum,
)

__all__ = [
    "AnnotationManifest",
    "AnnotationVersion",
    "ClassDefinition",
    "CollectionConditions",
    "ContentHash",
    "DatasetApprovalRequiredError",
    "DatasetError",
    "DatasetId",
    "DatasetLineage",
    "DatasetManifest",
    "DatasetNotFoundError",
    "DatasetRegistry",
    "DatasetStore",
    "DatasetVersion",
    "DatasetVersionConflictError",
    "FileSystemDatasetRegistry",
    "GCSStore",
    "GuidelineRef",
    "InvalidGeometryError",
    "ItemMetadata",
    "LeakageAuditor",
    "LeakageCheckResult",
    "LeakageType",
    "LocalFSStore",
    "MinioStore",
    "OperationIdentity",
    "PathTraversalError",
    "PayloadCorruptionError",
    "PayloadIntegrityError",
    "PayloadNotFoundError",
    "S3Store",
    "SplitContaminationError",
    "SplitLeakageError",
    "SplitMembership",
    "SplitName",
    "StandardLeakageAuditor",
    "StorageBackendType",
    "compute_payload_root_checksum",
    "validate_annotation_geometry",
    "validate_annotation_version",
    "validate_content_hash",
    "validate_dataset_id",
    "validate_dataset_version",
    "validate_guideline_ref",
    "verify_payload_root_checksum",
]
