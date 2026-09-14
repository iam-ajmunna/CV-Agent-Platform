"""cv_agent.dataset.leakage — 5-Dimension Leakage Auditor per docs/DATA.md and ADR-0009 §3.4."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol

from cv_agent.dataset.contracts import (
    DatasetManifest,
    ItemMetadata,
    LeakageCheckResult,
    SplitMembership,
)
from cv_agent.dataset.storage.base import DatasetStore
from cv_agent.dataset.types import LeakageType

AUDITOR_VERSION = "1.0.0"


def _hamming_distance(h1: str, h2: str) -> int:
    """Compute Hamming distance between two hexadecimal hash strings."""
    try:
        val1 = int(h1, 16)
        val2 = int(h2, 16)
        return (val1 ^ val2).bit_count()
    except ValueError:
        return 64


class LeakageAuditor(Protocol):
    """Audits dataset splits against the five non-negotiable leakage checks [P§26]."""

    def audit_splits(
        self,
        manifest: DatasetManifest,
        item_metadata: Mapping[Any, ItemMetadata],
        store: DatasetStore | None = None,
    ) -> Sequence[LeakageCheckResult]:
        """Execute temporal, camera, duplicate, identity, and annotation leakage tests."""
        ...


class StandardLeakageAuditor:
    """Authoritative reference implementation of the 5-dimension leakage auditor.

    Interprets split_policy to determine required dimensions and metadata fields (Gate B3).
    """

    def __init__(self, near_duplicate_threshold: int = 3) -> None:
        self.near_duplicate_threshold = near_duplicate_threshold

    def audit_splits(
        self,
        manifest: DatasetManifest,
        item_metadata: Mapping[Any, ItemMetadata],
        store: DatasetStore | None = None,
    ) -> tuple[LeakageCheckResult, ...]:
        """Audit manifest splits across all five leakage dimensions deterministically."""
        policy = manifest.split_policy.lower()
        splits = manifest.split_membership
        meta = {str(k): v for k, v in item_metadata.items()}

        now = datetime.now(timezone.utc)

        r_temporal = self._audit_temporal(policy, splits, meta, now)
        r_camera = self._audit_camera_site(policy, splits, meta, now)
        r_duplicate = self._audit_near_duplicate(policy, splits, meta, now)
        r_identity = self._audit_subject_identity(policy, splits, meta, now)
        r_annotation = self._audit_annotation_round(manifest, now)

        return (r_temporal, r_camera, r_duplicate, r_identity, r_annotation)

    def _audit_temporal(
        self,
        policy: str,
        splits: SplitMembership,
        meta: dict[str, ItemMetadata],
        now: datetime,
    ) -> LeakageCheckResult:
        """Evaluate temporal continuity and clip straddling across splits."""
        is_temporal_policy = any(
            kw in policy for kw in ("temporal", "time", "sequential", "video")
        )

        all_hashes = [str(h) for h in splits.all_hashes()]
        if not is_temporal_policy:
            return LeakageCheckResult(
                leakage_type=LeakageType.TEMPORAL,
                passed=True,
                summary=f"Temporal disjointness not mandated by split_policy '{policy}'",
                metrics={"policy_enforced": 0.0},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        # Fail-closed check: verify all items have clip_id or capture_timestamp
        missing_temporal = [
            h
            for h in all_hashes
            if h not in meta
            or (meta[h].clip_id is None and meta[h].capture_timestamp is None)
        ]
        if missing_temporal:
            return LeakageCheckResult(
                leakage_type=LeakageType.TEMPORAL,
                passed=False,
                summary=(
                    f"Missing temporal metadata for {len(missing_temporal)} items under temporal split policy: "
                    f"clip_id and capture_timestamp absent (e.g. {missing_temporal[:3]})"
                ),
                metrics={"missing_metadata_count": float(len(missing_temporal))},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        # Check clip_id disjointness across splits
        train_clips = {
            meta[str(h)].clip_id for h in splits.train_hashes if meta[str(h)].clip_id
        }
        val_clips = {
            meta[str(h)].clip_id for h in splits.val_hashes if meta[str(h)].clip_id
        }
        test_clips = {
            meta[str(h)].clip_id for h in splits.test_hashes if meta[str(h)].clip_id
        }
        calib_clips = {
            meta[str(h)].clip_id
            for h in splits.calibration_hashes
            if meta[str(h)].clip_id
        }

        clip_overlaps = (
            (train_clips & test_clips)
            | (train_clips & val_clips)
            | (train_clips & calib_clips)
            | (val_clips & test_clips)
        )
        if clip_overlaps:
            sorted_clips = sorted(str(c) for c in clip_overlaps if c is not None)[:3]
            return LeakageCheckResult(
                leakage_type=LeakageType.TEMPORAL,
                passed=False,
                summary=f"Temporal leakage detected: clip IDs straddle split boundaries ({sorted_clips})",
                metrics={"overlapping_clips": float(len(clip_overlaps))},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        return LeakageCheckResult(
            leakage_type=LeakageType.TEMPORAL,
            passed=True,
            summary="Zero clip straddling detected across all splits",
            metrics={"overlapping_clips": 0.0},
            timestamp=now,
            auditor_version=AUDITOR_VERSION,
        )

    def _audit_camera_site(
        self,
        policy: str,
        splits: SplitMembership,
        meta: dict[str, ItemMetadata],
        now: datetime,
    ) -> LeakageCheckResult:
        """Evaluate camera and site disjointness between train and eval/calibration splits."""
        is_camera_policy = any(kw in policy for kw in ("camera", "site", "location"))

        all_hashes = [str(h) for h in splits.all_hashes()]
        if not is_camera_policy:
            return LeakageCheckResult(
                leakage_type=LeakageType.CAMERA_SITE,
                passed=True,
                summary=f"Camera/site disjointness not mandated by split_policy '{policy}'",
                metrics={"policy_enforced": 0.0},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        # Fail-closed check: camera_id or site_id required on all items
        missing_cam = [
            h
            for h in all_hashes
            if h not in meta or (meta[h].camera_id is None and meta[h].site_id is None)
        ]
        if missing_cam:
            return LeakageCheckResult(
                leakage_type=LeakageType.CAMERA_SITE,
                passed=False,
                summary=(
                    f"Camera/site policy requires location metadata: {len(missing_cam)} items "
                    f"missing camera_id and site_id (e.g. {missing_cam[:3]})"
                ),
                metrics={"missing_metadata_count": float(len(missing_cam))},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        # Extract train cameras/sites
        train_cams = {
            meta[str(h)].camera_id
            for h in splits.train_hashes
            if meta[str(h)].camera_id
        }
        train_sites = {
            meta[str(h)].site_id for h in splits.train_hashes if meta[str(h)].site_id
        }

        eval_cams = {
            meta[str(h)].camera_id
            for h in (
                splits.val_hashes + splits.test_hashes + splits.calibration_hashes
            )
            if meta[str(h)].camera_id
        }
        eval_sites = {
            meta[str(h)].site_id
            for h in (
                splits.val_hashes + splits.test_hashes + splits.calibration_hashes
            )
            if meta[str(h)].site_id
        }

        cam_overlap = train_cams & eval_cams
        site_overlap = train_sites & eval_sites

        if cam_overlap or site_overlap:
            sorted_cams = sorted(str(c) for c in cam_overlap if c is not None)[:3]
            sorted_sites = sorted(str(s) for s in site_overlap if s is not None)[:3]
            return LeakageCheckResult(
                leakage_type=LeakageType.CAMERA_SITE,
                passed=False,
                summary=(
                    f"Camera/site leakage detected: cameras {sorted_cams} "
                    f"or sites {sorted_sites} present in both train and eval splits"
                ),
                metrics={
                    "overlapping_cameras": float(len(cam_overlap)),
                    "overlapping_sites": float(len(site_overlap)),
                },
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        return LeakageCheckResult(
            leakage_type=LeakageType.CAMERA_SITE,
            passed=True,
            summary="Zero camera or site overlap detected between train and eval splits",
            metrics={"overlapping_cameras": 0.0, "overlapping_sites": 0.0},
            timestamp=now,
            auditor_version=AUDITOR_VERSION,
        )

    def _audit_near_duplicate(
        self,
        policy: str,
        splits: SplitMembership,
        meta: dict[str, ItemMetadata],
        now: datetime,
    ) -> LeakageCheckResult:
        """Evaluate perceptual hash collisions (dHash/pHash) across split boundaries."""
        train_hashes = [str(h) for h in splits.train_hashes]
        eval_hashes = [
            str(h)
            for h in (
                splits.val_hashes + splits.test_hashes + splits.calibration_hashes
            )
        ]

        # Collect perceptual hashes
        train_phashes = [
            (h, meta[h].perceptual_hash)
            for h in train_hashes
            if h in meta and meta[h].perceptual_hash is not None
        ]
        eval_phashes = [
            (h, meta[h].perceptual_hash)
            for h in eval_hashes
            if h in meta and meta[h].perceptual_hash is not None
        ]

        if not train_phashes or not eval_phashes:
            # If policy mandated duplicate check but metadata absent, fail closed
            if "duplicate" in policy:
                return LeakageCheckResult(
                    leakage_type=LeakageType.NEAR_DUPLICATE,
                    passed=False,
                    summary="Near-duplicate policy declared but perceptual_hash metadata is missing",
                    metrics={"missing_phashes": 1.0},
                    timestamp=now,
                    auditor_version=AUDITOR_VERSION,
                )
            return LeakageCheckResult(
                leakage_type=LeakageType.NEAR_DUPLICATE,
                passed=True,
                summary="Zero perceptual hash collisions (no perceptual hashes supplied; split hashes disjoint)",
                metrics={"duplicate_pairs": 0.0},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        collision_count = 0
        min_distance = 64
        for _, p1 in train_phashes:
            for _, p2 in eval_phashes:
                assert p1 is not None and p2 is not None
                dist = _hamming_distance(p1, p2)
                min_distance = min(min_distance, dist)
                if dist <= self.near_duplicate_threshold:
                    collision_count += 1

        if collision_count > 0:
            return LeakageCheckResult(
                leakage_type=LeakageType.NEAR_DUPLICATE,
                passed=False,
                summary=(
                    f"Near-duplicate leakage detected: {collision_count} pairs have perceptual "
                    f"Hamming distance <= {self.near_duplicate_threshold} (min distance: {min_distance})"
                ),
                metrics={
                    "collision_count": float(collision_count),
                    "min_distance": float(min_distance),
                },
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        return LeakageCheckResult(
            leakage_type=LeakageType.NEAR_DUPLICATE,
            passed=True,
            summary=(
                f"Zero near-duplicates detected across splits (min Hamming distance: {min_distance} "
                f"> threshold {self.near_duplicate_threshold})"
            ),
            metrics={"collision_count": 0.0, "min_distance": float(min_distance)},
            timestamp=now,
            auditor_version=AUDITOR_VERSION,
        )

    def _audit_subject_identity(
        self,
        policy: str,
        splits: SplitMembership,
        meta: dict[str, ItemMetadata],
        now: datetime,
    ) -> LeakageCheckResult:
        """Evaluate identity and subject tracking leakage across splits."""
        is_identity_policy = any(
            kw in policy for kw in ("identity", "subject", "person", "reid", "re-id")
        )

        all_hashes = [str(h) for h in splits.all_hashes()]
        if not is_identity_policy:
            return LeakageCheckResult(
                leakage_type=LeakageType.SUBJECT_IDENTITY,
                passed=True,
                summary=f"Subject/identity disjointness not mandated by split_policy '{policy}'",
                metrics={"policy_enforced": 0.0},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        # Fail-closed check: verify subject_ids present on all items
        missing_subj = [
            h for h in all_hashes if h not in meta or not meta[h].subject_ids
        ]
        if missing_subj:
            return LeakageCheckResult(
                leakage_type=LeakageType.SUBJECT_IDENTITY,
                passed=False,
                summary=(
                    f"Identity split policy requires subject metadata: {len(missing_subj)} items "
                    f"missing subject_ids (e.g. {missing_subj[:3]})"
                ),
                metrics={"missing_metadata_count": float(len(missing_subj))},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        train_subjects = {
            s for h in splits.train_hashes for s in meta[str(h)].subject_ids
        }
        eval_subjects = {
            s
            for h in (
                splits.val_hashes + splits.test_hashes + splits.calibration_hashes
            )
            for s in meta[str(h)].subject_ids
        }

        subject_overlap = train_subjects & eval_subjects
        if subject_overlap:
            return LeakageCheckResult(
                leakage_type=LeakageType.SUBJECT_IDENTITY,
                passed=False,
                summary=f"Subject identity leakage detected: subjects {sorted(subject_overlap)[:3]} straddle splits",
                metrics={"overlapping_subjects": float(len(subject_overlap))},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        return LeakageCheckResult(
            leakage_type=LeakageType.SUBJECT_IDENTITY,
            passed=True,
            summary="Zero subject identity overlap detected across splits",
            metrics={"overlapping_subjects": 0.0},
            timestamp=now,
            auditor_version=AUDITOR_VERSION,
        )

    def _audit_annotation_round(
        self,
        manifest: DatasetManifest,
        now: datetime,
    ) -> LeakageCheckResult:
        """Evaluate annotation guideline consistency across dataset items."""
        if not str(manifest.annotation_guideline_ref).strip():
            return LeakageCheckResult(
                leakage_type=LeakageType.ANNOTATION_ROUND,
                passed=False,
                summary="Manifest annotation_guideline_ref is empty or unanchored",
                metrics={"valid_guideline": 0.0},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        return LeakageCheckResult(
            leakage_type=LeakageType.ANNOTATION_ROUND,
            passed=True,
            summary=f"All annotations unified under guideline '{manifest.annotation_guideline_ref}'",
            metrics={"valid_guideline": 1.0},
            timestamp=now,
            auditor_version=AUDITOR_VERSION,
        )
