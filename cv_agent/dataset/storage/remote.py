"""cv_agent.dataset.storage.remote — Remote storage adapters for S3, GCS, and MinIO (ADR-0009).

Lazy-import adapters designed for remote GPU worker retrieval without core SDK dependencies,
enforcing streaming byte-integrity verification, atomic retrieval, and corruption detection (Gate B4).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from cv_agent.dataset.errors import (
    DatasetError,
    PayloadCorruptionError,
    PayloadNotFoundError,
)
from cv_agent.dataset.types import (
    ContentHash,
    StorageBackendType,
    validate_content_hash,
)

_CHUNK_SIZE = 64 * 1024  # 64 KB streaming buffer


def _compute_stream_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file in streaming chunks."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


class S3Store:
    """AWS S3 content-addressed store adapter."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "blobs",
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint_url = endpoint_url
        self.region_name = region_name

    @property
    def backend_type(self) -> StorageBackendType:
        return StorageBackendType.S3

    def _object_key(self, content_hash: ContentHash) -> str:
        h = str(content_hash)
        return f"{self.prefix}/{h[:2]}/{h[2:4]}/{h}"

    def get_uri(self, content_hash: ContentHash) -> str:
        content_hash = validate_content_hash(str(content_hash))
        return f"s3://{self.bucket}/{self._object_key(content_hash)}"

    def exists(self, content_hash: ContentHash) -> bool:
        content_hash = validate_content_hash(str(content_hash))
        try:
            import boto3  # type: ignore[import-untyped,import-not-found]
        except ImportError:
            raise ImportError(
                "boto3 package is required to interact with S3Store at runtime"
            ) from None
        s3 = boto3.client(
            "s3", endpoint_url=self.endpoint_url, region_name=self.region_name
        )
        try:
            s3.head_object(Bucket=self.bucket, Key=self._object_key(content_hash))
            return True
        except Exception:  # noqa: BLE001 - catch client-specific remote probe failures
            return False

    def put_payload(self, local_path: Path | str) -> ContentHash:
        """Streamingly compute SHA-256, upload blob to S3 by content hash, and return hash."""
        path = Path(local_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Local payload not found: {path}")

        computed_hex = _compute_stream_sha256(path)
        content_hash = validate_content_hash(computed_hex)
        key = self._object_key(content_hash)

        try:
            import boto3  # type: ignore[import-untyped,import-not-found]
        except ImportError:
            raise ImportError(
                "boto3 package is required to interact with S3Store at runtime"
            ) from None

        s3 = boto3.client(
            "s3", endpoint_url=self.endpoint_url, region_name=self.region_name
        )
        # Check if already exists in bucket for deduplication
        try:
            s3.head_object(Bucket=self.bucket, Key=key)
            resp = s3.get_object(Bucket=self.bucket, Key=key)
            hasher = hashlib.sha256()
            body = resp.get("Body")
            if body is not None:
                for chunk in iter(lambda: body.read(_CHUNK_SIZE), b""):
                    hasher.update(chunk)
            if hasher.hexdigest().lower() == content_hash:
                return content_hash
        except Exception:  # noqa: BLE001, S110 - object does not exist yet or verification failed
            pass

        try:
            s3.upload_file(
                str(path),
                self.bucket,
                key,
                ExtraArgs={"Metadata": {"sha256": str(content_hash)}},
            )
        except Exception as err:
            raise DatasetError(
                f"Failed uploading payload {content_hash} to S3 bucket '{self.bucket}': {err}"
            ) from err

        # Post-upload remote-side integrity verification (fail-closed)
        try:
            resp = s3.get_object(Bucket=self.bucket, Key=key)
            hasher = hashlib.sha256()
            body = resp.get("Body")
            if body is not None:
                for chunk in iter(lambda: body.read(_CHUNK_SIZE), b""):
                    hasher.update(chunk)
            remote_hex = hasher.hexdigest().lower()
            if remote_hex != content_hash:
                try:
                    s3.delete_object(Bucket=self.bucket, Key=key)
                except Exception:  # noqa: BLE001, S110 - best-effort deletion of corrupted remote object
                    pass
                raise PayloadCorruptionError(
                    expected_hash=str(content_hash),
                    actual_hash=str(remote_hex),
                    uri=self.get_uri(content_hash),
                )
        except PayloadCorruptionError:
            raise
        except Exception as err:
            raise DatasetError(
                f"Post-upload remote verification failed for S3 payload {content_hash}: {err}"
            ) from err

        return content_hash

    def get_payload(
        self, content_hash: ContentHash, destination_path: Path | str
    ) -> Path:
        """Retrieve payload from S3 with atomic staging, streaming SHA-256 verification, and 0o444 permissions."""
        content_hash = validate_content_hash(str(content_hash))
        dest = Path(destination_path).resolve()

        if dest.is_file():
            existing_hash = _compute_stream_sha256(dest)
            if existing_hash == content_hash:
                return dest

        try:
            import boto3  # type: ignore[import-untyped,import-not-found]
        except ImportError:
            raise ImportError(
                "boto3 package is required to interact with S3Store at runtime"
            ) from None

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.parent / f".tmp_{dest.name}_{os.getpid()}_{uuid.uuid4().hex}"

        s3 = boto3.client(
            "s3", endpoint_url=self.endpoint_url, region_name=self.region_name
        )
        key = self._object_key(content_hash)

        try:
            s3.download_file(self.bucket, key, str(tmp_dest))
        except Exception as err:
            tmp_dest.unlink(missing_ok=True)
            err_msg = str(err).lower()
            if "404" in err_msg or "not found" in err_msg or "nosuchkey" in err_msg:
                raise PayloadNotFoundError(
                    content_hash, self.get_uri(content_hash)
                ) from err
            raise DatasetError(
                f"Failed downloading payload {content_hash} from S3: {err}"
            ) from err

        # Verify byte integrity
        actual_hash = _compute_stream_sha256(tmp_dest)
        if actual_hash != content_hash:
            tmp_dest.unlink(missing_ok=True)
            raise PayloadCorruptionError(
                expected_hash=content_hash,
                actual_hash=actual_hash,
                uri=self.get_uri(content_hash),
            )

        try:
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


class GCSStore:
    """Google Cloud Storage content-addressed store adapter."""

    def __init__(self, bucket: str, prefix: str = "blobs") -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    @property
    def backend_type(self) -> StorageBackendType:
        return StorageBackendType.GCS

    def _object_key(self, content_hash: ContentHash) -> str:
        h = str(content_hash)
        return f"{self.prefix}/{h[:2]}/{h[2:4]}/{h}"

    def get_uri(self, content_hash: ContentHash) -> str:
        content_hash = validate_content_hash(str(content_hash))
        return f"gs://{self.bucket}/{self._object_key(content_hash)}"

    def exists(self, content_hash: ContentHash) -> bool:
        content_hash = validate_content_hash(str(content_hash))
        try:
            import google.cloud.storage as gcs_storage  # type: ignore[import-untyped,import-not-found]
        except ImportError:
            raise ImportError(
                "google-cloud-storage package is required to interact with GCSStore at runtime"
            ) from None
        client = gcs_storage.Client()
        bucket = client.bucket(self.bucket)
        blob = bucket.blob(self._object_key(content_hash))
        return bool(blob.exists())

    def put_payload(self, local_path: Path | str) -> ContentHash:
        """Streamingly compute SHA-256, upload blob to GCS by content hash, and return hash."""
        path = Path(local_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Local payload not found: {path}")

        computed_hex = _compute_stream_sha256(path)
        content_hash = validate_content_hash(computed_hex)
        key = self._object_key(content_hash)

        try:
            import google.cloud.storage as gcs_storage  # type: ignore[import-untyped,import-not-found]
        except ImportError:
            raise ImportError(
                "google-cloud-storage package is required to interact with GCSStore at runtime"
            ) from None

        client = gcs_storage.Client()
        bucket = client.bucket(self.bucket)
        blob = bucket.blob(key)

        if blob.exists():
            try:
                hasher = hashlib.sha256()
                with blob.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
                        hasher.update(chunk)
                if hasher.hexdigest().lower() == content_hash:
                    return content_hash
            except Exception:  # noqa: BLE001, S110 - object verification failed or blob inaccessible
                pass

        try:
            blob.upload_from_filename(str(path))
        except Exception as err:
            raise DatasetError(
                f"Failed uploading payload {content_hash} to GCS bucket '{self.bucket}': {err}"
            ) from err

        # Post-upload remote-side integrity verification (fail-closed)
        try:
            hasher = hashlib.sha256()
            with blob.open("rb") as stream:
                for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
                    hasher.update(chunk)
            remote_hex = hasher.hexdigest().lower()
            if remote_hex != content_hash:
                try:
                    blob.delete()
                except Exception:  # noqa: BLE001, S110 - best-effort deletion of corrupted remote blob
                    pass
                raise PayloadCorruptionError(
                    expected_hash=str(content_hash),
                    actual_hash=str(remote_hex),
                    uri=self.get_uri(content_hash),
                )
        except PayloadCorruptionError:
            raise
        except Exception as err:
            raise DatasetError(
                f"Post-upload remote verification failed for GCS payload {content_hash}: {err}"
            ) from err

        return content_hash

    def get_payload(
        self, content_hash: ContentHash, destination_path: Path | str
    ) -> Path:
        """Retrieve payload from GCS with atomic staging, streaming SHA-256 verification, and 0o444 permissions."""
        content_hash = validate_content_hash(str(content_hash))
        dest = Path(destination_path).resolve()

        if dest.is_file():
            existing_hash = _compute_stream_sha256(dest)
            if existing_hash == content_hash:
                return dest

        try:
            import google.cloud.storage as gcs_storage  # type: ignore[import-untyped,import-not-found]
        except ImportError:
            raise ImportError(
                "google-cloud-storage package is required to interact with GCSStore at runtime"
            ) from None

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.parent / f".tmp_{dest.name}_{os.getpid()}_{uuid.uuid4().hex}"

        client = gcs_storage.Client()
        bucket = client.bucket(self.bucket)
        blob = bucket.blob(self._object_key(content_hash))

        if not blob.exists():
            raise PayloadNotFoundError(content_hash, self.get_uri(content_hash))

        try:
            blob.download_to_filename(str(tmp_dest))
        except Exception as err:
            tmp_dest.unlink(missing_ok=True)
            raise DatasetError(
                f"Failed downloading payload {content_hash} from GCS: {err}"
            ) from err

        actual_hash = _compute_stream_sha256(tmp_dest)
        if actual_hash != content_hash:
            tmp_dest.unlink(missing_ok=True)
            raise PayloadCorruptionError(
                expected_hash=content_hash,
                actual_hash=actual_hash,
                uri=self.get_uri(content_hash),
            )

        try:
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


class MinioStore(S3Store):
    """MinIO content-addressed store adapter (S3-compatible)."""

    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        prefix: str = "blobs",
    ) -> None:
        super().__init__(bucket=bucket, prefix=prefix, endpoint_url=endpoint_url)

    @property
    def backend_type(self) -> StorageBackendType:
        return StorageBackendType.MINIO

    def get_uri(self, content_hash: ContentHash) -> str:
        content_hash = validate_content_hash(str(content_hash))
        endpoint = (
            self.endpoint_url.rstrip("/") if self.endpoint_url else "minio://localhost"
        )
        return f"{endpoint}/{self.bucket}/{self._object_key(content_hash)}"
