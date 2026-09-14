"""cv_agent.dataset.storage.remote — Remote storage adapters for S3, GCS, and MinIO (ADR-0009).

Lazy-import adapters designed for remote GPU worker retrieval without core SDK dependencies.
"""

from __future__ import annotations

from pathlib import Path

from cv_agent.dataset.types import (
    ContentHash,
    StorageBackendType,
    validate_content_hash,
)


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

    def put_payload(self, local_path: Path) -> ContentHash:
        raise NotImplementedError(
            "Remote S3Store put_payload requires runtime S3 credentials"
        )

    def get_payload(self, content_hash: ContentHash, destination_path: Path) -> Path:
        raise NotImplementedError(
            "Remote S3Store get_payload requires runtime S3 credentials"
        )


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

    def put_payload(self, local_path: Path) -> ContentHash:
        raise NotImplementedError(
            "Remote GCSStore put_payload requires runtime GCS credentials"
        )

    def get_payload(self, content_hash: ContentHash, destination_path: Path) -> Path:
        raise NotImplementedError(
            "Remote GCSStore get_payload requires runtime GCS credentials"
        )


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
