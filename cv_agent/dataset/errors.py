"""cv_agent.dataset.errors — Normalized exception hierarchy for Dataset Subsystem (ADR-0009)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cv_agent.dataset.types import ContentHash


class DatasetError(Exception):
    """Base exception for all dataset subsystem errors."""


class DatasetNotFoundError(DatasetError):
    """Raised when a requested dataset manifest cannot be found."""

    def __init__(self, dataset_id: str, version: str) -> None:
        super().__init__(f"Dataset '{dataset_id}' version '{version}' not found")
        self.dataset_id = dataset_id
        self.version = version


class DatasetVersionConflictError(DatasetError):
    """Raised when attempting to overwrite an existing immutable dataset version."""

    def __init__(self, dataset_id: str, version: str) -> None:
        super().__init__(
            f"Dataset '{dataset_id}' version '{version}' is already registered and immutable"
        )
        self.dataset_id = dataset_id
        self.version = version


class SplitContaminationError(DatasetError):
    """Raised when partition sets overlap (violating split disjointness)."""

    def __init__(
        self,
        overlapping_hashes: set[str] | set[ContentHash] | set[Any],
        split_pair: tuple[str, str],
    ) -> None:
        sorted_samples = sorted(str(h) for h in overlapping_hashes)[:3]
        super().__init__(
            f"Split contamination detected between '{split_pair[0]}' and '{split_pair[1]}': "
            f"{len(overlapping_hashes)} overlapping hashes (e.g. {sorted_samples})"
        )
        self.overlapping_hashes = {str(h) for h in overlapping_hashes}
        self.split_pair = split_pair


class SplitLeakageError(DatasetError):
    """Raised when a dataset split fails the mandatory 5-point leakage audit."""

    def __init__(self, failed_checks: list[str]) -> None:
        super().__init__(
            f"Dataset split failed mandatory leakage audit on dimensions: {failed_checks}"
        )
        self.failed_checks = failed_checks


class PayloadNotFoundError(DatasetError):
    """Raised when a content-addressed payload blob does not exist in the store."""

    def __init__(self, content_hash: str, uri: str | None = None) -> None:
        msg = f"Payload with content hash '{content_hash}' not found"
        if uri:
            msg += f" at URI '{uri}'"
        super().__init__(msg)
        self.content_hash = content_hash
        self.uri = uri


class PayloadCorruptionError(DatasetError):
    """Raised when retrieved or stored payload bytes fail SHA-256 integrity verification."""

    def __init__(
        self, expected_hash: str, actual_hash: str, uri: str | None = None
    ) -> None:
        msg = (
            f"Payload byte corruption detected: expected SHA-256 '{expected_hash}', "
            f"computed '{actual_hash}'"
        )
        if uri:
            msg += f" from URI '{uri}'"
        super().__init__(msg)
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        self.uri = uri


class PayloadIntegrityError(DatasetError):
    """Raised when manifest root checksum does not match payload collection."""

    def __init__(self, expected_root: str, computed_root: str) -> None:
        super().__init__(
            f"Payload root checksum mismatch: manifest declares '{expected_root}', "
            f"computed '{computed_root}'"
        )
        self.expected_root = expected_root
        self.computed_root = computed_root


class InvalidGeometryError(DatasetError):
    """Raised when annotation primitives fail geometric validity checks (AC #8)."""

    def __init__(self, reason: str, details: dict[str, object] | None = None) -> None:
        super().__init__(f"Invalid annotation geometry: {reason}")
        self.reason = reason
        self.details = details or {}


class PathTraversalError(DatasetError):
    """Raised when a storage path escapes the configured root store directory."""

    def __init__(self, path: str, store_root: str) -> None:
        super().__init__(
            f"Security violation: path '{path}' escapes store root '{store_root}'"
        )
        self.path = path
        self.store_root = store_root


class DatasetApprovalRequiredError(DatasetError):
    """Raised when a gated mutating action is attempted without valid human approval."""

    def __init__(self, action: str, dataset_id: str, version: str) -> None:
        super().__init__(
            f"Action '{action}' on dataset '{dataset_id}:{version}' requires human approval per docs/APPROVALS.md"
        )
        self.action = action
        self.dataset_id = dataset_id
        self.version = version


class BaselineEstablishmentError(DatasetError):
    """Base exception for dataset baseline establishment failures."""


class BaselineValidationError(BaselineEstablishmentError):
    """Raised when empirical baseline evidence, target, or timing violates invariants."""
