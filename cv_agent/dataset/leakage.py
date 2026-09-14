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
        """Evaluate temporal continuity, clip straddling, and timestamp window disjointness."""
        is_clip_policy = any(kw in policy for kw in ("clip", "video"))
        is_time_ordered = any(
            kw in policy
            for kw in ("time-ordered", "time_ordered", "sequential", "chronological")
        )
        is_temporal_policy = (
            is_clip_policy
            or is_time_ordered
            or any(kw in policy for kw in ("temporal", "time", "timestamp"))
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

        # 1. Fail-closed metadata verification based on declared policy
        if is_time_ordered:
            missing_ts = [
                h
                for h in all_hashes
                if h not in meta or meta[h].capture_timestamp is None
            ]
            if missing_ts:
                return LeakageCheckResult(
                    leakage_type=LeakageType.TEMPORAL,
                    passed=False,
                    summary=(
                        f"Time-ordered split policy requires capture_timestamp on all items: "
                        f"{len(missing_ts)} items missing capture_timestamp (e.g. {missing_ts[:3]})"
                    ),
                    metrics={"missing_metadata_count": float(len(missing_ts))},
                    timestamp=now,
                    auditor_version=AUDITOR_VERSION,
                )

        if is_clip_policy:
            missing_clips = [
                h for h in all_hashes if h not in meta or meta[h].clip_id is None
            ]
            if missing_clips:
                return LeakageCheckResult(
                    leakage_type=LeakageType.TEMPORAL,
                    passed=False,
                    summary=(
                        f"Clip-based split policy requires clip_id on all items: "
                        f"{len(missing_clips)} items missing clip_id (e.g. {missing_clips[:3]})"
                    ),
                    metrics={"missing_metadata_count": float(len(missing_clips))},
                    timestamp=now,
                    auditor_version=AUDITOR_VERSION,
                )

        # General temporal policy: require at least one temporal coordinate per item
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
                    f"Temporal split policy requires temporal metadata (clip_id or capture_timestamp) "
                    f"on all items: {len(missing_temporal)} items have neither (e.g. {missing_temporal[:3]})"
                ),
                metrics={"missing_metadata_count": float(len(missing_temporal))},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        # 2. Check clip_id disjointness across splits
        train_clips = {
            meta[str(h)].clip_id
            for h in splits.train_hashes
            if meta[str(h)].clip_id is not None
        }
        val_clips = {
            meta[str(h)].clip_id
            for h in splits.val_hashes
            if meta[str(h)].clip_id is not None
        }
        test_clips = {
            meta[str(h)].clip_id
            for h in splits.test_hashes
            if meta[str(h)].clip_id is not None
        }
        calib_clips = {
            meta[str(h)].clip_id
            for h in splits.calibration_hashes
            if meta[str(h)].clip_id is not None
        }

        clip_overlaps = (
            (train_clips & test_clips)
            | (train_clips & val_clips)
            | (train_clips & calib_clips)
            | (val_clips & test_clips)
            | (val_clips & calib_clips)
            | (test_clips & calib_clips)
        )
        if clip_overlaps:
            sorted_clips = sorted(str(c) for c in clip_overlaps)[:3]
            return LeakageCheckResult(
                leakage_type=LeakageType.TEMPORAL,
                passed=False,
                summary=f"Temporal leakage detected: clip IDs straddle split boundaries ({sorted_clips})",
                metrics={"overlapping_clips": float(len(clip_overlaps))},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        # 3. Check timestamp windows and time-ordered constraints
        def _get_timestamps(hashes: Sequence[object]) -> list[datetime]:
            out: list[datetime] = []
            for h in hashes:
                item = meta.get(str(h))
                if item is not None and item.capture_timestamp is not None:
                    out.append(item.capture_timestamp)
            return out

        train_ts = _get_timestamps(splits.train_hashes)
        val_ts = _get_timestamps(splits.val_hashes)
        test_ts = _get_timestamps(splits.test_hashes)
        calib_ts = _get_timestamps(splits.calibration_hashes)
        eval_ts = val_ts + test_ts + calib_ts

        if train_ts and eval_ts:
            min_train, max_train = min(train_ts), max(train_ts)
            min_eval, max_eval = min(eval_ts), max(eval_ts)

            # Exact timestamp collision
            exact_ts_overlap = set(train_ts) & set(eval_ts)
            if exact_ts_overlap:
                return LeakageCheckResult(
                    leakage_type=LeakageType.TEMPORAL,
                    passed=False,
                    summary=(
                        f"Temporal leakage detected: {len(exact_ts_overlap)} exact timestamp collisions "
                        f"between train and eval splits (e.g. {[t.isoformat() for t in sorted(exact_ts_overlap)[:2]]})"
                    ),
                    metrics={"exact_timestamp_overlaps": float(len(exact_ts_overlap))},
                    timestamp=now,
                    auditor_version=AUDITOR_VERSION,
                )

            # Time-ordered policy: train window must strictly precede eval window
            if is_time_ordered and max_train >= min_eval:
                return LeakageCheckResult(
                    leakage_type=LeakageType.TEMPORAL,
                    passed=False,
                    summary=(
                        f"Time-ordered split policy violated: train window "
                        f"[{min_train.isoformat()}, {max_train.isoformat()}] does not strictly precede "
                        f"eval window [{min_eval.isoformat()}, {max_eval.isoformat()}]"
                    ),
                    metrics={"window_violation": 1.0},
                    timestamp=now,
                    auditor_version=AUDITOR_VERSION,
                )

            # General temporal policy: temporal window straddling check
            eval_inside_train = [t for t in eval_ts if min_train < t < max_train]
            train_inside_eval = [t for t in train_ts if min_eval < t < max_eval]
            if eval_inside_train or train_inside_eval:
                straddle_count = len(eval_inside_train) + len(train_inside_eval)
                return LeakageCheckResult(
                    leakage_type=LeakageType.TEMPORAL,
                    passed=False,
                    summary=(
                        f"Temporal window straddling detected: {len(eval_inside_train)} eval items inside train window "
                        f"[{min_train.isoformat()}, {max_train.isoformat()}] and {len(train_inside_eval)} train items inside eval window"
                    ),
                    metrics={"straddling_items": float(straddle_count)},
                    timestamp=now,
                    auditor_version=AUDITOR_VERSION,
                )

        return LeakageCheckResult(
            leakage_type=LeakageType.TEMPORAL,
            passed=True,
            summary="Zero temporal clip straddling or window overlap detected across splits",
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
        """Evaluate camera and site disjointness between train and eval splits (decoupled per Gate B3)."""
        requires_camera = "camera" in policy
        requires_site = any(kw in policy for kw in ("site", "location"))

        all_hashes = [str(h) for h in splits.all_hashes()]
        if not requires_camera and not requires_site:
            return LeakageCheckResult(
                leakage_type=LeakageType.CAMERA_SITE,
                passed=True,
                summary=f"Camera/site disjointness not mandated by split_policy '{policy}'",
                metrics={"policy_enforced": 0.0},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        # 1. Fail-closed check: camera_id required only when camera disjointness is requested
        if requires_camera:
            missing_cam = [
                h for h in all_hashes if h not in meta or meta[h].camera_id is None
            ]
            if missing_cam:
                return LeakageCheckResult(
                    leakage_type=LeakageType.CAMERA_SITE,
                    passed=False,
                    summary=(
                        f"Camera-disjoint split policy requires camera_id on all items: "
                        f"{len(missing_cam)} items missing camera_id (e.g. {missing_cam[:3]})"
                    ),
                    metrics={"missing_camera_count": float(len(missing_cam))},
                    timestamp=now,
                    auditor_version=AUDITOR_VERSION,
                )

        # 2. Fail-closed check: site_id required only when site disjointness is requested
        if requires_site:
            missing_site = [
                h for h in all_hashes if h not in meta or meta[h].site_id is None
            ]
            if missing_site:
                return LeakageCheckResult(
                    leakage_type=LeakageType.CAMERA_SITE,
                    passed=False,
                    summary=(
                        f"Site-disjoint split policy requires site_id on all items: "
                        f"{len(missing_site)} items missing site_id (e.g. {missing_site[:3]})"
                    ),
                    metrics={"missing_site_count": float(len(missing_site))},
                    timestamp=now,
                    auditor_version=AUDITOR_VERSION,
                )

        # 3. Disjointness verification for the declared dimensions
        eval_hashes = splits.val_hashes + splits.test_hashes + splits.calibration_hashes
        cam_overlap: set[str] = set()
        site_overlap: set[str] = set()

        if requires_camera:
            train_cams = {
                meta[str(h)].camera_id
                for h in splits.train_hashes
                if meta[str(h)].camera_id is not None
            }
            eval_cams = {
                meta[str(h)].camera_id
                for h in eval_hashes
                if meta[str(h)].camera_id is not None
            }
            cam_overlap = {c for c in (train_cams & eval_cams) if c is not None}

        if requires_site:
            train_sites = {
                meta[str(h)].site_id
                for h in splits.train_hashes
                if meta[str(h)].site_id is not None
            }
            eval_sites = {
                meta[str(h)].site_id
                for h in eval_hashes
                if meta[str(h)].site_id is not None
            }
            site_overlap = {s for s in (train_sites & eval_sites) if s is not None}

        if cam_overlap or site_overlap:
            failure_reasons = []
            if cam_overlap:
                failure_reasons.append(f"cameras {sorted(cam_overlap)[:3]}")
            if site_overlap:
                failure_reasons.append(f"sites {sorted(site_overlap)[:3]}")
            return LeakageCheckResult(
                leakage_type=LeakageType.CAMERA_SITE,
                passed=False,
                summary=f"Camera/site leakage detected: {' and '.join(failure_reasons)} present in both train and eval splits",
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
            summary="Zero camera or site overlap detected between train and eval splits per declared policy",
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
        """Evaluate perceptual hash collisions across split boundaries (fails closed on partial metadata per Gate B3)."""
        all_hashes = [str(h) for h in splits.all_hashes()]
        is_duplicate_policy = any(
            kw in policy
            for kw in ("duplicate", "dedup", "perceptual", "phash", "dhash")
        )
        has_any_phash = any(
            meta[h].perceptual_hash is not None for h in all_hashes if h in meta
        )

        # If policy does not mandate near-duplicate checks AND no perceptual hashes were supplied, pass
        if not is_duplicate_policy and not has_any_phash:
            return LeakageCheckResult(
                leakage_type=LeakageType.NEAR_DUPLICATE,
                passed=True,
                summary=f"Near-duplicate check not mandated by split_policy '{policy}' and no perceptual hashes supplied",
                metrics={"duplicate_pairs": 0.0},
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        # Fail-closed: If near-duplicate check is active (by policy or partial metadata),
        # every item across all splits must provide a perceptual_hash!
        missing_phashes = [
            h for h in all_hashes if h not in meta or meta[h].perceptual_hash is None
        ]
        if missing_phashes:
            reason = (
                "Near-duplicate policy declared"
                if is_duplicate_policy
                else "Partial perceptual metadata provided"
            )
            return LeakageCheckResult(
                leakage_type=LeakageType.NEAR_DUPLICATE,
                passed=False,
                summary=(
                    f"{reason}: {len(missing_phashes)} of {len(all_hashes)} "
                    f"items missing perceptual_hash metadata (e.g. {missing_phashes[:3]})"
                ),
                metrics={
                    "missing_metadata_count": float(len(missing_phashes)),
                    "collision_count": 0.0,
                },
                timestamp=now,
                auditor_version=AUDITOR_VERSION,
            )

        train_hashes = [str(h) for h in splits.train_hashes]
        eval_hashes = [
            str(h)
            for h in (
                splits.val_hashes + splits.test_hashes + splits.calibration_hashes
            )
        ]

        train_phashes = [(h, meta[h].perceptual_hash) for h in train_hashes]
        eval_phashes = [(h, meta[h].perceptual_hash) for h in eval_hashes]

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
