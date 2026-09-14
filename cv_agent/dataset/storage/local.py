"""cv_agent.dataset.storage.local — Local filesystem content-addressed store implementation (ADR-0009)."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from cv_agent.dataset.errors import (
    DatasetError,
    PathTraversalError,
    PayloadCorruptionError,
    PayloadNotFoundError,
)
from cv_agent.dataset.types import (
    ContentHash,
    StorageBackendType,
    validate_content_hash,
)

_CHUNK_SIZE = 64 * 1024  # 64 KB stream buffer


def _compute_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file in streaming chunks."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


class LocalFSStore:
    """Zero-dependency local filesystem content-addressed store.

    Stores immutable media payloads in a directory tree indexed by SHA-256:
    `<root_dir>/blobs/<hash[:2]>/<hash[2:4]>/<hash>`
    """

    def __init__(self, root_dir: Path | str) -> None:
        self._root_dir = Path(root_dir).resolve()
        self._blobs_dir = self._root_dir / "blobs"
        self._tmp_dir = self._blobs_dir / ".tmp"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

    @property
    def backend_type(self) -> StorageBackendType:
        return StorageBackendType.LOCAL_FS

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    def _blob_path(self, content_hash: ContentHash) -> Path:
        """Resolve the canonical filesystem path for a content hash."""
        h = str(content_hash)
        path = self._blobs_dir / h[:2] / h[2:4] / h
        # Ensure path does not escape blobs_dir (containment invariant I6)
        resolved = path.resolve()
        try:
            resolved.relative_to(self._blobs_dir)
        except ValueError:
            raise PathTraversalError(str(path), str(self._blobs_dir)) from None
        return resolved

    def put_payload(self, local_path: Path | str) -> ContentHash:
        """Compute SHA-256, store blob by content hash, and return hash."""
        src = Path(local_path).resolve()
        if not src.is_file():
            raise FileNotFoundError(f"Source payload file not found: '{src}'")

        # Stream copy to tmp directory while computing SHA-256
        tmp_id = f"put_{uuid.uuid4().hex}.tmp"
        tmp_file = self._tmp_dir / tmp_id

        h = hashlib.sha256()
        try:
            with src.open("rb") as f_in, tmp_file.open("wb") as f_out:
                while chunk := f_in.read(_CHUNK_SIZE):
                    h.update(chunk)
                    f_out.write(chunk)
                f_out.flush()
                os.fsync(f_out.fileno())

            computed_hash = ContentHash(h.hexdigest())
            dest = self._blob_path(computed_hash)

            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_file, dest)
                # Enforce write-once immutability via read-only permissions
                try:
                    os.chmod(dest, 0o444)
                except OSError as err:
                    try:
                        os.chmod(dest, 0o644)
                    except OSError:
                        pass
                    dest.unlink(missing_ok=True)
                    raise DatasetError(
                        f"Failed to enforce read-only permissions (0o444) on stored payload {dest}: {err}"
                    ) from err
            else:
                # Deduplication: already stored with identical content hash
                tmp_file.unlink(missing_ok=True)

            return computed_hash
        except Exception:
            tmp_file.unlink(missing_ok=True)
            raise

    def get_payload(
        self, content_hash: ContentHash, destination_path: Path | str
    ) -> Path:
        """Retrieve payload matching content hash to destination with byte-integrity verification."""
        content_hash = validate_content_hash(str(content_hash))
        blob = self._blob_path(content_hash)

        if not blob.exists():
            raise PayloadNotFoundError(content_hash, self.get_uri(content_hash))

        dest = Path(destination_path).resolve()

        # If destination already exists, verify its byte integrity
        if dest.is_file():
            existing_hash = _compute_sha256(dest)
            if existing_hash == content_hash:
                return dest  # Verified no-op

        # Stream copy to temporary destination file while verifying SHA-256
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.parent / f".tmp_{dest.name}_{os.getpid()}_{uuid.uuid4().hex}"

        h = hashlib.sha256()
        try:
            with blob.open("rb") as f_in, tmp_dest.open("wb") as f_out:
                while chunk := f_in.read(_CHUNK_SIZE):
                    h.update(chunk)
                    f_out.write(chunk)
                f_out.flush()
                os.fsync(f_out.fileno())

            computed_hash = h.hexdigest()
            if computed_hash != content_hash:
                tmp_dest.unlink(missing_ok=True)
                raise PayloadCorruptionError(
                    expected_hash=content_hash,
                    actual_hash=computed_hash,
                    uri=self.get_uri(content_hash),
                )

            os.replace(tmp_dest, dest)
            try:
                os.chmod(dest, 0o444)
            except OSError as err:
                try:
                    os.chmod(dest, 0o644)
                except OSError:
                    pass
                dest.unlink(missing_ok=True)
                raise DatasetError(
                    f"Failed to enforce read-only permissions (0o444) on retrieved payload {dest}: {err}"
                ) from err
            return dest
        except Exception:
            tmp_dest.unlink(missing_ok=True)
            raise

    def exists(self, content_hash: ContentHash) -> bool:
        """Check whether a content hash blob exists in the store."""
        try:
            content_hash = validate_content_hash(str(content_hash))
            return self._blob_path(content_hash).is_file()
        except ValueError:
            return False

    def get_uri(self, content_hash: ContentHash) -> str:
        """Return canonical file:// storage URI."""
        content_hash = validate_content_hash(str(content_hash))
        return f"file://{self._blob_path(content_hash)}"
