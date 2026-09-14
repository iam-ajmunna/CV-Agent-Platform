"""cv_agent.dataset.registry — Authoritative Git-tracked Dataset Registry (ADR-0009 §4)."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from cv_agent.dataset.contracts import DatasetManifest
from cv_agent.dataset.errors import (
    DatasetApprovalRequiredError,
    DatasetError,
    DatasetNotFoundError,
    DatasetVersionConflictError,
    PayloadNotFoundError,
    SplitLeakageError,
)
from cv_agent.dataset.storage.base import DatasetStore
from cv_agent.dataset.types import (
    DatasetId,
    DatasetVersion,
    LeakageType,
    validate_dataset_id,
    validate_dataset_version,
)
from cv_agent.dataset.validation import verify_payload_root_checksum

DEFAULT_DATASET_ROOT = Path(".cv_agent/datasets")


class DatasetRegistry(Protocol):
    """Authoritative registry for storing, resolving, and retrieving dataset manifests."""

    def register(
        self,
        manifest: DatasetManifest,
        store: DatasetStore | None = None,
    ) -> Path:
        """Validate, atomically persist manifest to .cv_agent/datasets/, and return manifest path."""
        ...

    def get(self, dataset_id: DatasetId, version: DatasetVersion) -> DatasetManifest:
        """Retrieve authoritative manifest for dataset id and version."""
        ...

    def list_versions(self, dataset_id: DatasetId) -> Sequence[DatasetVersion]:
        """List all registered immutable versions for a dataset id."""
        ...

    def exists(self, dataset_id: DatasetId, version: DatasetVersion) -> bool:
        """Check whether a specific dataset version manifest is registered."""
        ...


class FileSystemDatasetRegistry:
    """Authoritative filesystem-backed registry storing Git-tracked dataset manifests (ADR-0009).

    Directory layout:
    `<base_dir>/<dataset_id>/<version>.json`
    """

    def __init__(self, base_dir: Path | str = DEFAULT_DATASET_ROOT) -> None:
        self._base_dir = Path(base_dir).resolve()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def _manifest_path(self, dataset_id: DatasetId, version: DatasetVersion) -> Path:
        dataset_id = validate_dataset_id(str(dataset_id))
        version = validate_dataset_version(str(version))
        return self._base_dir / str(dataset_id) / f"{version}.json"

    def register(
        self,
        manifest: DatasetManifest,
        store: DatasetStore | None = None,
    ) -> Path:
        """Execute atomic publication pipeline (Gate I2) and register immutable manifest.

        Pipeline:
        1. Verify payload root checksum against declared split membership (Gate B2).
        2. Verify all payload blobs exist in store if store provided (Gate B4).
        3. Verify all 5 leakage dimensions are audited and passed (Gate B3).
        4. Guard against overwriting existing immutable version (write-once immutability).
        5. Atomically write to temp file, fsync, rename to <version>.json, and chmod 0o444.
        """
        # 1. Verify root checksum
        verify_payload_root_checksum(manifest)

        # 2. Verify payloads exist in store
        if store is not None:
            all_hashes = manifest.split_membership.all_hashes()
            for h in sorted(all_hashes):
                if not store.exists(h):
                    raise PayloadNotFoundError(str(h), store.get_uri(h))

        # 3. Verify leakage audit records cover all 5 dimensions and all passed
        audited_types = {r.leakage_type for r in manifest.leakage_audit_records}
        required_types = set(LeakageType)
        missing_audits = required_types - audited_types
        failed_audits = [
            r.leakage_type.value for r in manifest.leakage_audit_records if not r.passed
        ]

        if missing_audits or failed_audits:
            failures = failed_audits + [f"missing_{t.value}" for t in missing_audits]
            raise SplitLeakageError(failures)

        # 4. Check write-once immutability
        target_path = self._manifest_path(manifest.dataset_id, manifest.version)
        if target_path.exists():
            raise DatasetVersionConflictError(
                str(manifest.dataset_id), str(manifest.version)
            )

        # 5. Atomic publication (Gate I2)
        target_dir = target_path.parent
        tmp_dir = target_dir / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = tmp_dir / f"{manifest.version}_{uuid.uuid4().hex}.tmp"

        manifest_json = manifest.model_dump_json(indent=2)
        try:
            with tmp_file.open("w", encoding="utf-8") as f:
                f.write(manifest_json)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_file, target_path)
            try:
                os.chmod(target_path, 0o444)
            except OSError as err:
                try:
                    os.chmod(target_path, 0o644)
                except OSError:
                    pass
                target_path.unlink(missing_ok=True)
                raise DatasetError(
                    f"Failed to enforce read-only permissions (0o444) on published manifest {target_path}: {err}"
                ) from err
            return target_path
        except Exception:
            tmp_file.unlink(missing_ok=True)
            if target_path.exists():
                try:
                    os.chmod(target_path, 0o644)
                except OSError:
                    pass
                target_path.unlink(missing_ok=True)
            raise

    def get(self, dataset_id: DatasetId, version: DatasetVersion) -> DatasetManifest:
        """Retrieve and deserialize authoritative manifest for dataset id and version."""
        path = self._manifest_path(dataset_id, version)
        if not path.is_file():
            raise DatasetNotFoundError(str(dataset_id), str(version))

        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return DatasetManifest.model_validate(data)

    def list_versions(self, dataset_id: DatasetId) -> tuple[DatasetVersion, ...]:
        """List all registered immutable versions for a dataset id, sorted."""
        dataset_id = validate_dataset_id(str(dataset_id))
        dataset_dir = self._base_dir / str(dataset_id)
        if not dataset_dir.is_dir():
            return ()

        versions: list[DatasetVersion] = []
        for file in dataset_dir.glob("*.json"):
            # Exclude temporary or dot files
            if file.name.startswith("."):
                continue
            ver_str = file.stem
            try:
                versions.append(validate_dataset_version(ver_str))
            except ValueError:
                continue
        return tuple(sorted(versions))

    def exists(self, dataset_id: DatasetId, version: DatasetVersion) -> bool:
        """Check whether a specific dataset version manifest is registered."""
        try:
            return self._manifest_path(dataset_id, version).is_file()
        except ValueError:
            return False

    def require_mutation_approval(
        self,
        action: str,
        dataset_id: DatasetId,
        version: DatasetVersion,
        approval_evidence: Mapping[str, Any] | None = None,
        *,
        expected_operation_id: str | None = None,
        expected_operation_version: str | None = None,
        expected_canonical_payload_hash: str | None = None,
        expected_target_identity: str | None = None,
    ) -> None:
        """Enforce ADR-0003 4-tuple operation identity binding for gated dataset actions (Gate I4).

        Required Operation Identity:
        (operation_id, operation_version, canonical_payload_hash, target_identity)
        """
        if not approval_evidence:
            raise DatasetApprovalRequiredError(action, str(dataset_id), str(version))

        required_keys = {
            "operation_id",
            "operation_version",
            "canonical_payload_hash",
            "target_identity",
        }
        if not required_keys.issubset(approval_evidence.keys()):
            raise DatasetApprovalRequiredError(
                f"{action} (incomplete 4-tuple operation identity: missing {sorted(required_keys - approval_evidence.keys())})",
                str(dataset_id),
                str(version),
            )
        if not approval_evidence.get("approved"):
            raise DatasetApprovalRequiredError(
                f"{action} (approval rejected or not granted)",
                str(dataset_id),
                str(version),
            )

        # Derive expected 4-tuple operation identity
        exp_target = expected_target_identity or f"{dataset_id}:{version}"
        exp_op_id = expected_operation_id or action
        exp_op_ver = expected_operation_version or "1.0.0"

        if expected_canonical_payload_hash is not None:
            exp_payload_hash = expected_canonical_payload_hash
        else:
            exp_payload_hash = hashlib.sha256(
                f"{exp_op_id}:{exp_op_ver}:{exp_target}".encode()
            ).hexdigest()

        expected_identity = (exp_op_id, exp_op_ver, exp_payload_hash, exp_target)
        actual_identity = (
            str(approval_evidence.get("operation_id")),
            str(approval_evidence.get("operation_version")),
            str(approval_evidence.get("canonical_payload_hash")),
            str(approval_evidence.get("target_identity")),
        )

        if actual_identity != expected_identity:
            mismatches: list[str] = []
            if actual_identity[0] != expected_identity[0]:
                mismatches.append(
                    f"operation_id mismatch: expected '{expected_identity[0]}', got '{actual_identity[0]}'"
                )
            if actual_identity[1] != expected_identity[1]:
                mismatches.append(
                    f"operation_version mismatch: expected '{expected_identity[1]}', got '{actual_identity[1]}'"
                )
            if actual_identity[2] != expected_identity[2]:
                mismatches.append(
                    f"canonical_payload_hash mismatch: expected '{expected_identity[2]}', got '{actual_identity[2]}'"
                )
            if actual_identity[3] != expected_identity[3]:
                mismatches.append(
                    f"target_identity mismatch: expected '{expected_identity[3]}', got '{actual_identity[3]}'"
                )

            raise DatasetApprovalRequiredError(
                f"{action} (ADR-0003 operation identity mismatch: {'; '.join(mismatches)})",
                str(dataset_id),
                str(version),
            )
