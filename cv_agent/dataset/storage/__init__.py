"""cv_agent.dataset.storage — Pluggable storage abstraction for Dataset Subsystem (ADR-0009)."""

from __future__ import annotations

from cv_agent.dataset.storage.base import DatasetStore
from cv_agent.dataset.storage.local import LocalFSStore
from cv_agent.dataset.storage.remote import GCSStore, MinioStore, S3Store

__all__ = [
    "DatasetStore",
    "GCSStore",
    "LocalFSStore",
    "MinioStore",
    "S3Store",
]
