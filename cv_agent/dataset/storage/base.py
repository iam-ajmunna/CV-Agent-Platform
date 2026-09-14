"""cv_agent.dataset.storage.base — Pluggable content-addressed storage protocol (ADR-0009 §4)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from cv_agent.dataset.types import ContentHash, StorageBackendType


class DatasetStore(Protocol):
    """Pluggable content-addressed payload storage backend abstraction."""

    @property
    def backend_type(self) -> StorageBackendType:
        """Return the concrete storage backend type."""
        ...

    def put_payload(self, local_path: Path) -> ContentHash:
        """Compute SHA-256, store blob by content hash, and return hash."""
        ...

    def get_payload(self, content_hash: ContentHash, destination_path: Path) -> Path:
        """Retrieve payload matching content hash to local destination path with byte verification."""
        ...

    def exists(self, content_hash: ContentHash) -> bool:
        """Check whether a content hash blob exists in the store."""
        ...

    def get_uri(self, content_hash: ContentHash) -> str:
        """Return canonical storage URI (e.g. 'file:///var/data/blobs/ab/cd/abcd1234')."""
        ...
