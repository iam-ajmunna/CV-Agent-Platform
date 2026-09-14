"""tests/test_dataset_boundary.py — Comprehensive adversarial boundary test suite for ADR-0009.

Verifies all 12 non-negotiable architectural invariants, gates B1–B4, secondary findings I1–I6,
storage integrity, geometry gates, disjointness, and atomic publication.
"""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from cv_agent.dataset import (
    AnnotationManifest,
    AnnotationVersion,
    ClassDefinition,
    CollectionConditions,
    ContentHash,
    DatasetApprovalRequiredError,
    DatasetError,
    DatasetId,
    DatasetLineage,
    DatasetManifest,
    DatasetNotFoundError,
    DatasetVersion,
    DatasetVersionConflictError,
    FileSystemDatasetRegistry,
    GCSStore,
    GuidelineRef,
    InvalidGeometryError,
    ItemMetadata,
    LeakageCheckResult,
    LeakageType,
    LocalFSStore,
    PathTraversalError,
    PayloadCorruptionError,
    PayloadIntegrityError,
    PayloadNotFoundError,
    S3Store,
    SplitContaminationError,
    SplitLeakageError,
    SplitMembership,
    SplitName,
    StandardLeakageAuditor,
    StorageBackendType,
    compute_payload_root_checksum,
    validate_annotation_geometry,
    validate_annotation_version,
    validate_content_hash,
    validate_dataset_id,
    validate_dataset_version,
    validate_guideline_ref,
    verify_payload_root_checksum,
)

# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------


def _dummy_hash(seed: str) -> ContentHash:
    """Generate a deterministic 64-character lowercase hex SHA-256 hash."""
    return ContentHash(hashlib.sha256(seed.encode("utf-8")).hexdigest())


@pytest.fixture
def temp_store(tmp_path: Path) -> LocalFSStore:
    """Fixture providing an isolated LocalFSStore in a temporary directory."""
    return LocalFSStore(tmp_path / "store")


@pytest.fixture
def temp_registry(tmp_path: Path) -> FileSystemDatasetRegistry:
    """Fixture providing an isolated FileSystemDatasetRegistry."""
    return FileSystemDatasetRegistry(tmp_path / "registry")


def _make_class_def(name: str = "person") -> ClassDefinition:
    return ClassDefinition(
        name=name,
        description="Physical observable human body",
        near_miss_negatives=("mannequin", "statue"),
        ambiguity_resolution="If occlusion > 90%, do not annotate",
    )


def _make_collection_conditions() -> CollectionConditions:
    return CollectionConditions(
        camera_models=("Axis-P3245",),
        sites=("Gate-1", "Perimeter-North"),
        times_of_day=("day", "night"),
        lighting_conditions=("direct_sunlight", "floodlight"),
        weather_conditions=("clear", "rain"),
    )


def _make_passed_audits() -> tuple[LeakageCheckResult, ...]:
    now = datetime.now(timezone.utc)
    return tuple(
        LeakageCheckResult(
            leakage_type=lt,
            passed=True,
            summary=f"{lt.value} audit passed",
            timestamp=now,
            auditor_version="1.0.0",
        )
        for lt in LeakageType
    )


# ---------------------------------------------------------------------------
# Gate B1: Canonical DatasetVersion Grammar
# ---------------------------------------------------------------------------


def test_dataset_version_canonical_semver_valid() -> None:
    """B1: Accepts strict SemVer 2.0.0 versions with mandatory 'v' prefix."""
    valid_versions = [
        "v0.1.0",
        "v1.0.0",
        "v2.1.3",
        "v1.0.0-alpha",
        "v1.0.0-alpha.1",
        "v1.0.0-0.3.7",
        "v1.0.0-x.7.z.92",
        "v1.0.0-alpha+001",
        "v1.0.0+20130313144700",
        "v1.0.0-beta+exp.sha.5114f85",
        "v10.20.30",
    ]
    for ver in valid_versions:
        assert validate_dataset_version(ver) == ver


def test_dataset_version_canonical_semver_invalid() -> None:
    """B1: Fails closed on non-conforming, monotonic, or missing-'v' version strings."""
    invalid_versions = [
        "1.0.0",  # missing leading 'v'
        "v1.0",  # missing patch
        "v1",  # missing minor and patch
        "v01.0.0",  # leading zero in major
        "v1.02.0",  # leading zero in minor
        "v1.0.03",  # leading zero in patch
        "latest",  # arbitrary string
        "monotonic-1",  # non-semver monotonic string
        "v1.0.0_beta",  # invalid underscore
        "",  # empty string
        "v1.0.0 ",  # trailing whitespace
    ]
    for ver in invalid_versions:
        with pytest.raises(ValueError, match="does not match canonical SemVer format"):
            validate_dataset_version(ver)


def test_identifier_validation_rules() -> None:
    """ADR-0001 §3.1 / ADR-0009 §4: Validates syntax across all dataset identifiers."""
    # DatasetId
    assert validate_dataset_id("cctv.perimeter.intrusion") == "cctv.perimeter.intrusion"
    assert validate_dataset_id("camera/site-a/feed-1") == "camera/site-a/feed-1"
    with pytest.raises(ValueError):
        validate_dataset_id("CCTV.UPPERCASE")
    with pytest.raises(ValueError):
        validate_dataset_id("spaces in id")

    # ContentHash
    h = hashlib.sha256(b"test").hexdigest()
    assert validate_content_hash(h) == h
    with pytest.raises(ValueError):
        validate_content_hash(h.upper())  # Reject uppercase
    with pytest.raises(ValueError):
        validate_content_hash(h[:63])  # Too short

    # AnnotationVersion
    assert validate_annotation_version("ann-v1.0.0") == "ann-v1.0.0"
    with pytest.raises(ValueError):
        validate_annotation_version("ann with spaces")

    # GuidelineRef
    assert validate_guideline_ref("https://github.com/org/repo/blob/main/guideline.md")
    assert validate_guideline_ref("commit:385f0c1a")


# ---------------------------------------------------------------------------
# Gate B2: Canonical payload_root_checksum Construction
# ---------------------------------------------------------------------------


def test_payload_root_checksum_permutation_invariance() -> None:
    """B2: Permutation invariance — shuffled hash lists produce bit-for-bit identical checksums."""
    hashes = [_dummy_hash(f"item_{i}") for i in range(25)]
    shuffled_1 = list(hashes)
    shuffled_2 = list(reversed(hashes))
    shuffled_3 = [hashes[i] for i in [5, 2, 8, 1, 0, 19, 12, 4, 3, 24, 7]]
    root_3 = compute_payload_root_checksum(shuffled_3)
    assert root_3 == compute_payload_root_checksum(list(reversed(shuffled_3)))

    root_1 = compute_payload_root_checksum(shuffled_1)
    root_2 = compute_payload_root_checksum(shuffled_2)
    assert root_1 == root_2

    # Deduplication invariance
    root_with_dupes = compute_payload_root_checksum(shuffled_1 + shuffled_1)
    assert root_with_dupes == root_1

    # Tampering one hash changes the root checksum
    tampered = list(shuffled_1)
    tampered[0] = _dummy_hash("tampered_item")
    assert compute_payload_root_checksum(tampered) != root_1


def test_payload_root_checksum_empty_dataset() -> None:
    """B2: Empty dataset collection produces canonical SHA-256 of empty bytes."""
    empty_root = compute_payload_root_checksum([])
    expected = hashlib.sha256(b"").hexdigest()
    assert empty_root == expected
    assert (
        empty_root == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


# ---------------------------------------------------------------------------
# Split Disjointness & Calibration First-Class Invariant (G2, I5)
# ---------------------------------------------------------------------------


def test_split_membership_strict_disjointness_enforced() -> None:
    """G2/I5: Enforces zero split contamination across train, val, test, and calibration."""
    h1, h2, h3, h4, h5, h6 = [_dummy_hash(f"hash_{i}") for i in range(6)]

    # Valid disjoint splits
    splits = SplitMembership(
        train_hashes=(h1, h2),
        val_hashes=(h3,),
        test_hashes=(h4,),
        calibration_hashes=(h5, h6),
    )
    assert len(splits.all_hashes()) == 6
    assert SplitName.CALIBRATION == "calibration"
    assert SplitName.TRAIN == "train"

    # Train / Test contamination
    with pytest.raises(
        SplitContaminationError,
        match="Split contamination detected between 'train' and 'test'",
    ):
        SplitMembership(
            train_hashes=(h1, h2),
            val_hashes=(h3,),
            test_hashes=(h2, h4),  # h2 in train and test!
            calibration_hashes=(h5,),
        )

    # Train / Calibration contamination
    with pytest.raises(
        SplitContaminationError,
        match="Split contamination detected between 'train' and 'calibration'",
    ):
        SplitMembership(
            train_hashes=(h1, h2),
            val_hashes=(h3,),
            test_hashes=(h4,),
            calibration_hashes=(h1,),  # h1 in train and calibration!
        )

    # Val / Calibration contamination
    with pytest.raises(
        SplitContaminationError,
        match="Split contamination detected between 'val' and 'calibration'",
    ):
        SplitMembership(
            train_hashes=(h1,),
            val_hashes=(h3,),
            test_hashes=(h4,),
            calibration_hashes=(h3,),  # h3 in val and calibration!
        )


# ---------------------------------------------------------------------------
# Storage Boundary (Gate B4 & I6: LocalFSStore & Remote Stubs)
# ---------------------------------------------------------------------------


def test_local_store_put_and_get_integrity(
    temp_store: LocalFSStore, tmp_path: Path
) -> None:
    """B4: Verifies atomic write, read-only permissions, and byte-integrity retrieval."""
    sample_file = tmp_path / "sample.jpg"
    sample_data = b"\xff\xd8\xff\xe0" + b"fake_jpeg_data_payload_12345"
    sample_file.write_bytes(sample_data)

    expected_hash = hashlib.sha256(sample_data).hexdigest()

    # Put payload
    content_hash = temp_store.put_payload(sample_file)
    assert content_hash == expected_hash
    assert temp_store.exists(content_hash)

    # Check read-only permissions (0o444)
    blob_path = temp_store._blob_path(content_hash)
    assert blob_path.is_file()
    # Mask mode to test read-only (user read, group read, other read)
    mode = blob_path.stat().st_mode & 0o777
    assert mode == 0o444

    # Deduplication test: re-putting same file succeeds and preserves state
    re_hash = temp_store.put_payload(sample_file)
    assert re_hash == expected_hash

    # Get payload to new destination
    dest_file = tmp_path / "dest" / "retrieved.jpg"
    retrieved = temp_store.get_payload(content_hash, dest_file)
    assert retrieved == dest_file
    assert retrieved.read_bytes() == sample_data

    # Verified no-op on re-get to existing matching destination
    re_retrieved = temp_store.get_payload(content_hash, dest_file)
    assert re_retrieved == dest_file


def test_local_store_corruption_detection(
    temp_store: LocalFSStore, tmp_path: Path
) -> None:
    """B4: Detects corrupted blob bytes and raises PayloadCorruptionError."""
    sample_file = tmp_path / "sample.bin"
    sample_file.write_bytes(b"initial_valid_bytes")
    content_hash = temp_store.put_payload(sample_file)

    blob_path = temp_store._blob_path(content_hash)
    # Temporarily remove read-only permission to simulate disk bit-rot / tampering
    blob_path.chmod(0o644)
    blob_path.write_bytes(b"corrupted_tampered_bytes")

    dest_file = tmp_path / "dest.bin"
    with pytest.raises(PayloadCorruptionError) as exc_info:
        temp_store.get_payload(content_hash, dest_file)

    assert exc_info.value.expected_hash == content_hash
    assert (
        not dest_file.exists()
    )  # Atomic publication guarantees corrupt file is not published


def test_local_store_missing_payload(temp_store: LocalFSStore, tmp_path: Path) -> None:
    """B4: Requesting non-existent content hash raises PayloadNotFoundError."""
    missing_hash = _dummy_hash("non_existent")
    dest_file = tmp_path / "out.bin"
    with pytest.raises(PayloadNotFoundError) as exc_info:
        temp_store.get_payload(missing_hash, dest_file)
    assert exc_info.value.content_hash == missing_hash


def test_local_store_path_traversal_prevention(temp_store: LocalFSStore) -> None:
    """I6: Traversal attempts escaping store root fail closed with PathTraversalError."""
    traversal_hash = ContentHash("../../etc/passwd" + "a" * 46)
    with pytest.raises((PathTraversalError, ValueError)):
        temp_store._blob_path(traversal_hash)


def test_remote_store_uri_generation() -> None:
    """ADR-0009 §4: Remote S3, GCS, and MinIO adapters generate canonical URIs."""
    h = _dummy_hash("image_123")
    s3 = S3Store(bucket="my-cv-bucket", prefix="datasets/blobs")
    assert s3.backend_type == StorageBackendType.S3
    assert s3.get_uri(h) == f"s3://my-cv-bucket/datasets/blobs/{h[:2]}/{h[2:4]}/{h}"

    # Does not crash when boto3 is absent at import time
    with pytest.raises(ImportError, match="boto3 package is required"):
        s3.exists(h)


# ---------------------------------------------------------------------------
# Gate B3: Policy-Interpreted Leakage Auditing
# ---------------------------------------------------------------------------


def test_leakage_audit_temporal_policy() -> None:
    """B3: Temporal policy enforces clip disjointness; fails closed on missing metadata or overlap."""
    auditor = StandardLeakageAuditor()
    h1, h2, h3 = [_dummy_hash(f"temp_{i}") for i in range(3)]

    splits = SplitMembership(train_hashes=(h1,), val_hashes=(h2,), test_hashes=(h3,))

    # 1. Temporal policy with missing metadata fails closed
    manifest_temporal = DatasetManifest(
        dataset_id=DatasetId("cctv.perimeter"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=3,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 3},
        split_policy="temporal-disjoint",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guidelines"),
        lineage=DatasetLineage(),
        license_and_constraints="Proprietary",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=compute_payload_root_checksum(splits.all_hashes()),
        leakage_audit_records=(),
    )

    meta_missing = {h1: ItemMetadata(content_hash=h1)}  # Missing clip_id and timestamp
    results = auditor.audit_splits(manifest_temporal, meta_missing)
    r_temporal = next(r for r in results if r.leakage_type == LeakageType.TEMPORAL)
    assert not r_temporal.passed
    assert "temporal metadata" in r_temporal.summary.lower()

    # 2. Temporal policy with clip straddling fails closed
    meta_overlap = {
        h1: ItemMetadata(content_hash=h1, clip_id="clip_A"),
        h2: ItemMetadata(content_hash=h2, clip_id="clip_B"),
        h3: ItemMetadata(
            content_hash=h3, clip_id="clip_A"
        ),  # clip_A in train and test!
    }
    results_overlap = auditor.audit_splits(manifest_temporal, meta_overlap)
    r_temporal_overlap = next(
        r for r in results_overlap if r.leakage_type == LeakageType.TEMPORAL
    )
    assert not r_temporal_overlap.passed
    assert "Temporal leakage detected" in r_temporal_overlap.summary

    # 3. Temporal policy with disjoint clips passes
    meta_clean = {
        h1: ItemMetadata(content_hash=h1, clip_id="clip_A"),
        h2: ItemMetadata(content_hash=h2, clip_id="clip_B"),
        h3: ItemMetadata(content_hash=h3, clip_id="clip_C"),
    }
    results_clean = auditor.audit_splits(manifest_temporal, meta_clean)
    r_temporal_clean = next(
        r for r in results_clean if r.leakage_type == LeakageType.TEMPORAL
    )
    assert r_temporal_clean.passed

    # 4. Non-temporal policy passes without temporal metadata
    manifest_static = manifest_temporal.model_copy(
        update={"split_policy": "random-stratified"}
    )
    results_static = auditor.audit_splits(manifest_static, {})
    r_temporal_static = next(
        r for r in results_static if r.leakage_type == LeakageType.TEMPORAL
    )
    assert r_temporal_static.passed


def test_leakage_audit_camera_policy() -> None:
    """B3: Camera/site policy enforces camera disjointness; fails closed on missing metadata or overlap."""
    auditor = StandardLeakageAuditor()
    h1, h2, h3 = [_dummy_hash(f"cam_{i}") for i in range(3)]
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(h2,), test_hashes=(h3,))

    manifest = DatasetManifest(
        dataset_id=DatasetId("cctv.perimeter"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=3,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 3},
        split_policy="camera-disjoint",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guidelines"),
        lineage=DatasetLineage(),
        license_and_constraints="Proprietary",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=compute_payload_root_checksum(splits.all_hashes()),
        leakage_audit_records=(),
    )

    # Missing camera metadata fails closed
    results = auditor.audit_splits(manifest, {h1: ItemMetadata(content_hash=h1)})
    r_cam = next(r for r in results if r.leakage_type == LeakageType.CAMERA_SITE)
    assert not r_cam.passed
    assert "Camera-disjoint split policy requires camera_id" in r_cam.summary

    # Camera overlap fails closed
    meta_overlap = {
        h1: ItemMetadata(content_hash=h1, camera_id="Cam-1"),
        h2: ItemMetadata(content_hash=h2, camera_id="Cam-2"),
        h3: ItemMetadata(
            content_hash=h3, camera_id="Cam-1"
        ),  # Cam-1 in train and test!
    }
    results_overlap = auditor.audit_splits(manifest, meta_overlap)
    r_cam_overlap = next(
        r for r in results_overlap if r.leakage_type == LeakageType.CAMERA_SITE
    )
    assert not r_cam_overlap.passed
    assert "Camera/site leakage detected" in r_cam_overlap.summary

    # Clean camera disjointness passes
    meta_clean = {
        h1: ItemMetadata(content_hash=h1, camera_id="Cam-1"),
        h2: ItemMetadata(content_hash=h2, camera_id="Cam-2"),
        h3: ItemMetadata(content_hash=h3, camera_id="Cam-3"),
    }
    results_clean = auditor.audit_splits(manifest, meta_clean)
    r_cam_clean = next(
        r for r in results_clean if r.leakage_type == LeakageType.CAMERA_SITE
    )
    assert r_cam_clean.passed


def test_leakage_audit_near_duplicate_collision() -> None:
    """B3: Detects perceptual hash collisions between train and eval splits (Hamming <= 3)."""
    auditor = StandardLeakageAuditor(near_duplicate_threshold=3)
    h1, h2 = [_dummy_hash(f"dup_{i}") for i in range(2)]
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(), test_hashes=(h2,))

    manifest = DatasetManifest(
        dataset_id=DatasetId("cctv.perimeter"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=2,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 2},
        split_policy="near-duplicate-audited",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guidelines"),
        lineage=DatasetLineage(),
        license_and_constraints="Proprietary",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=compute_payload_root_checksum(splits.all_hashes()),
        leakage_audit_records=(),
    )

    # Identical perceptual hash (Hamming distance 0 <= 3) -> fails
    meta_collision = {
        h1: ItemMetadata(content_hash=h1, perceptual_hash="0000ffff0000ffff"),
        h2: ItemMetadata(content_hash=h2, perceptual_hash="0000ffff0000ffff"),
    }
    results = auditor.audit_splits(manifest, meta_collision)
    r_dup = next(r for r in results if r.leakage_type == LeakageType.NEAR_DUPLICATE)
    assert not r_dup.passed
    assert "Near-duplicate leakage detected" in r_dup.summary

    # Distinct perceptual hashes (Hamming distance >> 3) -> passes
    meta_distinct = {
        h1: ItemMetadata(content_hash=h1, perceptual_hash="0000000000000000"),
        h2: ItemMetadata(content_hash=h2, perceptual_hash="ffffffffffffffff"),
    }
    results_distinct = auditor.audit_splits(manifest, meta_distinct)
    r_dup_distinct = next(
        r for r in results_distinct if r.leakage_type == LeakageType.NEAR_DUPLICATE
    )
    assert r_dup_distinct.passed


# ---------------------------------------------------------------------------
# Gate AC #8: Annotation Geometry Validation Gate (D-047)
# ---------------------------------------------------------------------------


def test_annotation_geometry_valid() -> None:
    """AC #8: Passes valid bounding boxes and polygons within declared extent."""
    h1 = _dummy_hash("img_1")
    classes = [_make_class_def("person"), _make_class_def("car")]

    ann = AnnotationManifest(
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        dataset_id=DatasetId("cctv.perimeter"),
        guideline_ref=GuidelineRef("git:guide"),
        annotation_format="coco_instances",
        payload_annotations={
            h1: (
                {"class_name": "person", "bbox": [10.0, 20.0, 100.0, 200.0]},
                {
                    "class_name": "car",
                    "bbox": {"x_min": 50, "y_min": 60, "x_max": 300, "y_max": 250},
                },
                {
                    "class_name": "person",
                    "polygon": [(10, 10), (50, 10), (50, 50), (10, 50)],
                    "closed": True,
                },
            )
        },
    )

    # Valid within 1920x1080 resolution
    validate_annotation_geometry(ann, classes, image_extents={h1: (1920, 1080)})


def test_annotation_geometry_negative_coords() -> None:
    """AC #8: Rejects negative bounding box coordinates with InvalidGeometryError."""
    h1 = _dummy_hash("img_neg")
    classes = [_make_class_def("person")]

    ann = AnnotationManifest(
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        dataset_id=DatasetId("cctv.perimeter"),
        guideline_ref=GuidelineRef("git:guide"),
        annotation_format="yolo_v8",
        payload_annotations={
            h1: ({"class_name": "person", "bbox": [-5.0, 10.0, 50.0, 60.0]},)
        },
    )
    with pytest.raises(InvalidGeometryError, match="bbox has negative coordinates"):
        validate_annotation_geometry(ann, classes)


def test_annotation_geometry_inverted_coords() -> None:
    """AC #8: Rejects inverted box coordinates (x_min >= x_max or y_min >= y_max)."""
    h1 = _dummy_hash("img_inv")
    classes = [_make_class_def("person")]

    ann = AnnotationManifest(
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        dataset_id=DatasetId("cctv.perimeter"),
        guideline_ref=GuidelineRef("git:guide"),
        annotation_format="yolo_v8",
        payload_annotations={
            h1: ({"class_name": "person", "bbox": [100.0, 50.0, 50.0, 150.0]},)
        },
    )
    with pytest.raises(InvalidGeometryError, match="x_min .* >= x_max"):
        validate_annotation_geometry(ann, classes)


def test_annotation_geometry_exceeds_resolution() -> None:
    """AC #8: Rejects coordinates exceeding declared image extent."""
    h1 = _dummy_hash("img_extent")
    classes = [_make_class_def("person")]

    ann = AnnotationManifest(
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        dataset_id=DatasetId("cctv.perimeter"),
        guideline_ref=GuidelineRef("git:guide"),
        annotation_format="yolo_v8",
        payload_annotations={
            h1: ({"class_name": "person", "bbox": [0.0, 0.0, 2000.0, 500.0]},)
        },
    )
    with pytest.raises(InvalidGeometryError, match="exceeds image width"):
        validate_annotation_geometry(ann, classes, image_extents={h1: (1920, 1080)})


def test_annotation_geometry_undeclared_class() -> None:
    """AC #8: Rejects annotation referencing class not present in class_definitions."""
    h1 = _dummy_hash("img_class")
    classes = [_make_class_def("person")]

    ann = AnnotationManifest(
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        dataset_id=DatasetId("cctv.perimeter"),
        guideline_ref=GuidelineRef("git:guide"),
        annotation_format="yolo_v8",
        payload_annotations={
            h1: ({"class_name": "ghost_intruder", "bbox": [10.0, 10.0, 50.0, 50.0]},)
        },
    )
    with pytest.raises(
        InvalidGeometryError, match="references undeclared class 'ghost_intruder'"
    ):
        validate_annotation_geometry(ann, classes)


# ---------------------------------------------------------------------------
# Gate I2: Atomic Manifest Publication & FileSystemDatasetRegistry
# ---------------------------------------------------------------------------


def test_registry_register_and_get_roundtrip(
    temp_registry: FileSystemDatasetRegistry,
    temp_store: LocalFSStore,
    tmp_path: Path,
) -> None:
    """I2: Verifies atomic registration pipeline, immutability, and 100% roundtrip fidelity."""
    # Create sample payloads and store them
    p1 = tmp_path / "img1.jpg"
    p2 = tmp_path / "img2.jpg"
    p1.write_bytes(b"image_bytes_one")
    p2.write_bytes(b"image_bytes_two")

    h1 = temp_store.put_payload(p1)
    h2 = temp_store.put_payload(p2)

    splits = SplitMembership(train_hashes=(h1,), val_hashes=(), test_hashes=(h2,))
    root_checksum = compute_payload_root_checksum(splits.all_hashes())

    manifest = DatasetManifest(
        dataset_id=DatasetId("cctv.perimeter.intrusion"),
        version=DatasetVersion("v1.0.0"),
        sources=("Gate-1", "North-Camera"),
        n_items=2,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 2},
        split_policy="temporal-site-disjoint",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("https://internal.wiki/cv/guideline-v1"),
        known_issues=("Night glare on camera 2",),
        lineage=DatasetLineage(transformation_name="raw_ingest"),
        license_and_constraints="Internal security use only",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=root_checksum,
        leakage_audit_records=_make_passed_audits(),
    )

    # 1. Register manifest with payload verification
    manifest_path = temp_registry.register(manifest, store=temp_store)
    assert manifest_path.is_file()
    assert manifest_path.name == "v1.0.0.json"
    # Check read-only permission (0o444)
    assert (manifest_path.stat().st_mode & 0o777) == 0o444

    # 2. Re-registering existing immutable version fails closed
    with pytest.raises(DatasetVersionConflictError):
        temp_registry.register(manifest, store=temp_store)

    # 3. Retrieve and verify roundtrip fidelity
    retrieved = temp_registry.get(manifest.dataset_id, manifest.version)
    assert retrieved.dataset_id == manifest.dataset_id
    assert retrieved.version == manifest.version
    assert retrieved.n_items == manifest.n_items
    assert retrieved.payload_root_checksum == manifest.payload_root_checksum
    assert retrieved.split_membership.train_hashes == (h1,)
    assert retrieved.split_membership.test_hashes == (h2,)

    # 4. List versions
    versions = temp_registry.list_versions(manifest.dataset_id)
    assert versions == (DatasetVersion("v1.0.0"),)
    assert temp_registry.exists(manifest.dataset_id, DatasetVersion("v1.0.0"))
    assert not temp_registry.exists(manifest.dataset_id, DatasetVersion("v2.0.0"))


def test_registry_fails_on_missing_store_payload(
    temp_registry: FileSystemDatasetRegistry,
    temp_store: LocalFSStore,
) -> None:
    """I2: Registration fails closed if declared payloads are missing from the store."""
    h1 = _dummy_hash("missing_payload_1")
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(), test_hashes=())

    manifest = DatasetManifest(
        dataset_id=DatasetId("cctv.perimeter"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=1,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 1},
        split_policy="single-site",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guide"),
        lineage=DatasetLineage(),
        license_and_constraints="Proprietary",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=compute_payload_root_checksum(splits.all_hashes()),
        leakage_audit_records=_make_passed_audits(),
    )

    with pytest.raises(
        PayloadNotFoundError, match=f"Payload with content hash '{h1}' not found"
    ):
        temp_registry.register(manifest, store=temp_store)


def test_registry_fails_on_incomplete_leakage_audit(
    temp_registry: FileSystemDatasetRegistry,
) -> None:
    """I2: Registration fails closed if any leakage dimension is missing or failed."""
    h1 = _dummy_hash("item_1")
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(), test_hashes=())

    # Only 4 audits provided (missing ANNOTATION_ROUND)
    partial_audits = tuple(_make_passed_audits()[:4])

    manifest = DatasetManifest(
        dataset_id=DatasetId("cctv.perimeter"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=1,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 1},
        split_policy="single-site",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guide"),
        lineage=DatasetLineage(),
        license_and_constraints="Proprietary",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=compute_payload_root_checksum(splits.all_hashes()),
        leakage_audit_records=partial_audits,
    )

    with pytest.raises(SplitLeakageError, match="missing_annotation_round"):
        temp_registry.register(manifest)


# ---------------------------------------------------------------------------
# Gate I4: Approval Gating with ADR-0003 4-Tuple Operation Identity
# ---------------------------------------------------------------------------


def test_mutation_approval_enforces_adr0003_4tuple_identity(
    temp_registry: FileSystemDatasetRegistry,
) -> None:
    """I4: Gated dataset actions enforce ADR-0003 4-tuple operation identity."""
    dataset_id = DatasetId("cctv.perimeter")
    ver = DatasetVersion("v1.0.0")

    # Missing approval evidence completely
    with pytest.raises(DatasetApprovalRequiredError, match="requires human approval"):
        temp_registry.require_mutation_approval("dataset.delete", dataset_id, ver, None)

    # Incomplete 4-tuple identity (missing canonical_payload_hash)
    incomplete_evidence = {
        "operation_id": "op-dataset-delete-01",
        "operation_version": "1.0.0",
        "target_identity": "cctv.perimeter:v1.0.0",
        "approved": True,
    }
    with pytest.raises(
        DatasetApprovalRequiredError, match="incomplete 4-tuple operation identity"
    ):
        temp_registry.require_mutation_approval(
            "dataset.delete", dataset_id, ver, incomplete_evidence
        )

    # Rejected approval evidence
    rejected_evidence = dict(incomplete_evidence)
    rejected_evidence["canonical_payload_hash"] = "abc123canonicalhash"
    rejected_evidence["approved"] = False
    with pytest.raises(
        DatasetApprovalRequiredError, match="approval rejected or not granted"
    ):
        temp_registry.require_mutation_approval(
            "dataset.delete", dataset_id, ver, rejected_evidence
        )

    # Valid complete 4-tuple approval passes when bound to exact operation identity
    valid_evidence = dict(rejected_evidence)
    valid_evidence["approved"] = True
    temp_registry.require_mutation_approval(
        "dataset.delete",
        dataset_id,
        ver,
        valid_evidence,
        expected_operation_id="op-dataset-delete-01",
        expected_canonical_payload_hash="abc123canonicalhash",
    )

    # PM Adversarial Review Blocker 3: Approval for Operation A presented to Operation B fails closed
    # 1. Target dataset identity mismatch
    other_dataset = DatasetId("traffic.cam")
    with pytest.raises(
        DatasetApprovalRequiredError,
        match="ADR-0003 operation identity mismatch.*target_identity mismatch",
    ):
        temp_registry.require_mutation_approval(
            "dataset.delete",
            other_dataset,
            ver,
            valid_evidence,
            expected_operation_id="op-dataset-delete-01",
            expected_canonical_payload_hash="abc123canonicalhash",
        )

    # 2. Target version identity mismatch
    other_ver = DatasetVersion("v2.0.0")
    with pytest.raises(
        DatasetApprovalRequiredError,
        match="ADR-0003 operation identity mismatch.*target_identity mismatch",
    ):
        temp_registry.require_mutation_approval(
            "dataset.delete",
            dataset_id,
            other_ver,
            valid_evidence,
            expected_operation_id="op-dataset-delete-01",
            expected_canonical_payload_hash="abc123canonicalhash",
        )

    # 3. Operation ID mismatch (e.g. approval for delete presented to retrain)
    with pytest.raises(
        DatasetApprovalRequiredError,
        match="ADR-0003 operation identity mismatch.*operation_id mismatch",
    ):
        temp_registry.require_mutation_approval(
            "dataset.retrain",
            dataset_id,
            ver,
            valid_evidence,
            expected_operation_id="op-dataset-retrain-99",
            expected_canonical_payload_hash="abc123canonicalhash",
        )

    # 4. Canonical payload hash mismatch
    with pytest.raises(
        DatasetApprovalRequiredError,
        match="ADR-0003 operation identity mismatch.*canonical_payload_hash mismatch",
    ):
        temp_registry.require_mutation_approval(
            "dataset.delete",
            dataset_id,
            ver,
            valid_evidence,
            expected_operation_id="op-dataset-delete-01",
            expected_canonical_payload_hash="different_payload_hash_value",
        )


# ---------------------------------------------------------------------------
# Immutability & Model Protection
# ---------------------------------------------------------------------------


def test_manifest_and_membership_immutability() -> None:
    """ADR-0009 §4: All models strictly reject in-place attribute mutation (frozen=True)."""
    h1 = _dummy_hash("item_immut")
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(), test_hashes=())

    with pytest.raises(ValidationError):
        splits.train_hashes = ()  # type: ignore[misc]

    cdef = _make_class_def()
    with pytest.raises(ValidationError):
        cdef.name = "vehicle"  # type: ignore[misc]


def test_leakage_audit_subject_identity_policy() -> None:
    """B3: Subject/identity policy enforces subject disjointness; fails closed on missing metadata or overlap."""
    auditor = StandardLeakageAuditor()
    h1, h2, h3 = [_dummy_hash(f"subj_{i}") for i in range(3)]
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(h2,), test_hashes=(h3,))

    manifest = DatasetManifest(
        dataset_id=DatasetId("reid.pedestrian"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=3,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 3},
        split_policy="identity-disjoint",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guidelines"),
        lineage=DatasetLineage(),
        license_and_constraints="Proprietary",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=compute_payload_root_checksum(splits.all_hashes()),
        leakage_audit_records=(),
    )

    # 1. Missing subject metadata fails closed
    results = auditor.audit_splits(manifest, {h1: ItemMetadata(content_hash=h1)})
    r_subj = next(r for r in results if r.leakage_type == LeakageType.SUBJECT_IDENTITY)
    assert not r_subj.passed
    assert "Identity split policy requires subject metadata" in r_subj.summary

    # 2. Subject overlap between train and test fails closed
    meta_overlap = {
        h1: ItemMetadata(content_hash=h1, subject_ids=("person_101",)),
        h2: ItemMetadata(content_hash=h2, subject_ids=("person_102",)),
        h3: ItemMetadata(
            content_hash=h3, subject_ids=("person_101",)
        ),  # person_101 in train and test!
    }
    results_overlap = auditor.audit_splits(manifest, meta_overlap)
    r_subj_overlap = next(
        r for r in results_overlap if r.leakage_type == LeakageType.SUBJECT_IDENTITY
    )
    assert not r_subj_overlap.passed
    assert "Subject identity leakage detected" in r_subj_overlap.summary

    # 3. Clean disjoint subjects pass
    meta_clean = {
        h1: ItemMetadata(content_hash=h1, subject_ids=("person_101",)),
        h2: ItemMetadata(content_hash=h2, subject_ids=("person_102",)),
        h3: ItemMetadata(content_hash=h3, subject_ids=("person_103",)),
    }
    results_clean = auditor.audit_splits(manifest, meta_clean)
    r_subj_clean = next(
        r for r in results_clean if r.leakage_type == LeakageType.SUBJECT_IDENTITY
    )
    assert r_subj_clean.passed


def test_annotation_geometry_polygons_and_edge_cases() -> None:
    """AC #8: Thorough polygon validation and invalid primitive structure checks."""
    h1 = _dummy_hash("poly_item")
    classes = [_make_class_def("person")]

    # Missing class_name
    ann_no_class = AnnotationManifest(
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        dataset_id=DatasetId("cctv.perimeter"),
        guideline_ref=GuidelineRef("git:guide"),
        annotation_format="yolo_v8",
        payload_annotations={h1: ({"bbox": [10, 10, 50, 50]},)},
    )
    with pytest.raises(InvalidGeometryError, match="missing 'class_name'"):
        validate_annotation_geometry(ann_no_class, classes)

    # Polygon with < 3 vertices
    ann_poly_short = AnnotationManifest(
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        dataset_id=DatasetId("cctv.perimeter"),
        guideline_ref=GuidelineRef("git:guide"),
        annotation_format="yolo_v8",
        payload_annotations={
            h1: ({"class_name": "person", "polygon": [(10, 10), (20, 20)]},)
        },
    )
    with pytest.raises(InvalidGeometryError, match="polygon must have >= 3 vertices"):
        validate_annotation_geometry(ann_poly_short, classes)

    # Polygon with negative vertex
    ann_poly_neg = AnnotationManifest(
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        dataset_id=DatasetId("cctv.perimeter"),
        guideline_ref=GuidelineRef("git:guide"),
        annotation_format="yolo_v8",
        payload_annotations={
            h1: ({"class_name": "person", "polygon": [(10, 10), (-5, 20), (30, 30)]},)
        },
    )
    with pytest.raises(
        InvalidGeometryError, match="polygon vertex .* has negative coords"
    ):
        validate_annotation_geometry(ann_poly_neg, classes)

    # Polygon vertex exceeding image extent
    ann_poly_extent = AnnotationManifest(
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        dataset_id=DatasetId("cctv.perimeter"),
        guideline_ref=GuidelineRef("git:guide"),
        annotation_format="yolo_v8",
        payload_annotations={
            h1: ({"class_name": "person", "polygon": [(10, 10), (2000, 20), (30, 30)]},)
        },
    )
    with pytest.raises(InvalidGeometryError, match="polygon vertex .* x .* > width"):
        validate_annotation_geometry(
            ann_poly_extent, classes, image_extents={h1: (1920, 1080)}
        )


def test_payload_root_checksum_mismatch_fails_closed() -> None:
    """Gate B2: verify_payload_root_checksum raises PayloadIntegrityError on declared mismatch."""
    h1 = _dummy_hash("item_1")
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(), test_hashes=())

    manifest = DatasetManifest(
        dataset_id=DatasetId("cctv.perimeter"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=1,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 1},
        split_policy="single-site",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guide"),
        lineage=DatasetLineage(),
        license_and_constraints="Proprietary",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum="0000000000000000000000000000000000000000000000000000000000000000",  # wrong root!
        leakage_audit_records=_make_passed_audits(),
    )
    with pytest.raises(PayloadIntegrityError, match="Payload root checksum mismatch"):
        verify_payload_root_checksum(manifest)


def test_manifest_item_count_mismatch_fails_closed() -> None:
    """ADR-0009 §4: n_items must strictly equal unique hashes count in split_membership."""
    h1 = _dummy_hash("item_1")
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(), test_hashes=())

    with pytest.raises(
        ValidationError, match="n_items .* does not equal count of unique split hashes"
    ):
        DatasetManifest(
            dataset_id=DatasetId("cctv.perimeter"),
            version=DatasetVersion("v1.0.0"),
            sources=("site-1",),
            n_items=5,  # Mismatch! only 1 hash in splits
            class_definitions=(_make_class_def(),),
            class_distribution={"person": 1},
            split_policy="single-site",
            split_membership=splits,
            annotation_version=AnnotationVersion("ann-v1.0.0"),
            annotation_guideline_ref=GuidelineRef("git:guide"),
            lineage=DatasetLineage(),
            license_and_constraints="Proprietary",
            collection_conditions=_make_collection_conditions(),
            payload_root_checksum=compute_payload_root_checksum(splits.all_hashes()),
            leakage_audit_records=_make_passed_audits(),
        )


def test_local_store_edge_cases(temp_store: LocalFSStore) -> None:
    """B4: LocalFSStore handling of non-existent files and exists() method."""
    with pytest.raises(FileNotFoundError):
        temp_store.put_payload(Path("/non/existent/path/photo.jpg"))

    assert not temp_store.exists(_dummy_hash("definitely_not_there"))
    assert not temp_store.exists(ContentHash("invalid_hash_string"))


def test_registry_get_non_existent(temp_registry: FileSystemDatasetRegistry) -> None:
    """I2: FileSystemDatasetRegistry.get raises DatasetNotFoundError for unregistered versions."""
    with pytest.raises(DatasetNotFoundError):
        temp_registry.get(DatasetId("cctv.perimeter"), DatasetVersion("v9.9.9"))


# ---------------------------------------------------------------------------
# PM Adversarial Review Remediation Tests (Findings 1–6)
# ---------------------------------------------------------------------------


def test_leakage_audit_partial_perceptual_hash_fails_closed() -> None:
    """Finding 1 (B3): Partial perceptual metadata cannot silently pass; must fail closed."""
    auditor = StandardLeakageAuditor()
    h1, h2, h3 = [_dummy_hash(f"phash_partial_{i}") for i in range(3)]
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(h2,), test_hashes=(h3,))

    # 1. Random split policy without duplicate check, but partial perceptual hashes provided
    manifest_unmandated = DatasetManifest(
        dataset_id=DatasetId("cctv.perimeter"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=3,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 3},
        split_policy="random-stratified",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guidelines"),
        lineage=DatasetLineage(),
        license_and_constraints="Proprietary",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=compute_payload_root_checksum(splits.all_hashes()),
        leakage_audit_records=(),
    )

    # h1 and h2 provide perceptual_hash, but h3 does not: MUST fail closed!
    meta_partial = {
        h1: ItemMetadata(content_hash=h1, perceptual_hash="0123456789abcdef"),
        h2: ItemMetadata(content_hash=h2, perceptual_hash="fedcba9876543210"),
        h3: ItemMetadata(content_hash=h3, perceptual_hash=None),
    }
    results = auditor.audit_splits(manifest_unmandated, meta_partial)
    r_dup = next(r for r in results if r.leakage_type == LeakageType.NEAR_DUPLICATE)
    assert not r_dup.passed
    assert "Partial perceptual metadata provided" in r_dup.summary
    assert "missing perceptual_hash metadata" in r_dup.summary

    # 2. Policy explicitly mandating duplicate check with missing hash on an item: fails closed
    manifest_mandated = manifest_unmandated.model_copy(
        update={"split_policy": "near-duplicate-audited"}
    )
    results_mandated = auditor.audit_splits(manifest_mandated, meta_partial)
    r_dup_mandated = next(
        r for r in results_mandated if r.leakage_type == LeakageType.NEAR_DUPLICATE
    )
    assert not r_dup_mandated.passed
    assert "Near-duplicate policy declared" in r_dup_mandated.summary

    # 3. Complete perceptual hashes across all items: passes when Hamming distance > threshold
    meta_complete = {
        h1: ItemMetadata(content_hash=h1, perceptual_hash="0000000000000000"),
        h2: ItemMetadata(content_hash=h2, perceptual_hash="ffffffffffffffff"),
        h3: ItemMetadata(content_hash=h3, perceptual_hash="aaaaaaaaaaaaaaaa"),
    }
    results_complete = auditor.audit_splits(manifest_mandated, meta_complete)
    r_dup_complete = next(
        r for r in results_complete if r.leakage_type == LeakageType.NEAR_DUPLICATE
    )
    assert r_dup_complete.passed


def test_leakage_audit_camera_site_decoupling() -> None:
    """Finding 2 (B3): camera-disjoint requires camera_id; site-disjoint requires site_id; cleanly decoupled."""
    auditor = StandardLeakageAuditor()
    h1, h2 = [_dummy_hash(f"decouple_{i}") for i in range(2)]
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(), test_hashes=(h2,))

    base_manifest = DatasetManifest(
        dataset_id=DatasetId("cctv.perimeter"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=2,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 2},
        split_policy="camera-disjoint",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guidelines"),
        lineage=DatasetLineage(),
        license_and_constraints="Proprietary",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=compute_payload_root_checksum(splits.all_hashes()),
        leakage_audit_records=(),
    )

    # 1. camera-disjoint: items have camera_id but site_id=None -> PASSES!
    meta_cam_only = {
        h1: ItemMetadata(content_hash=h1, camera_id="Cam-1", site_id=None),
        h2: ItemMetadata(content_hash=h2, camera_id="Cam-2", site_id=None),
    }
    res_cam = auditor.audit_splits(base_manifest, meta_cam_only)
    r_cam = next(r for r in res_cam if r.leakage_type == LeakageType.CAMERA_SITE)
    assert r_cam.passed

    # 2. camera-disjoint: items have site_id but camera_id=None -> FAILS closed on camera_id!
    meta_site_only = {
        h1: ItemMetadata(content_hash=h1, camera_id=None, site_id="Site-1"),
        h2: ItemMetadata(content_hash=h2, camera_id=None, site_id="Site-2"),
    }
    res_cam_fail = auditor.audit_splits(base_manifest, meta_site_only)
    r_cam_fail = next(
        r for r in res_cam_fail if r.leakage_type == LeakageType.CAMERA_SITE
    )
    assert not r_cam_fail.passed
    assert "missing camera_id" in r_cam_fail.summary

    # 3. site-disjoint: items have site_id but camera_id=None -> PASSES!
    manifest_site = base_manifest.model_copy(update={"split_policy": "site-disjoint"})
    res_site = auditor.audit_splits(manifest_site, meta_site_only)
    r_site = next(r for r in res_site if r.leakage_type == LeakageType.CAMERA_SITE)
    assert r_site.passed

    # 4. site-disjoint: items have camera_id but site_id=None -> FAILS closed on site_id!
    res_site_fail = auditor.audit_splits(manifest_site, meta_cam_only)
    r_site_fail = next(
        r for r in res_site_fail if r.leakage_type == LeakageType.CAMERA_SITE
    )
    assert not r_site_fail.passed
    assert "missing site_id" in r_site_fail.summary


def test_leakage_audit_temporal_timestamp_and_window_contracts() -> None:
    """Finding 3 (B3): Time-ordered split policy and temporal window straddling enforcement."""
    auditor = StandardLeakageAuditor()
    h1, h2, h3 = [_dummy_hash(f"time_win_{i}") for i in range(3)]
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(h2,), test_hashes=(h3,))

    manifest_time_ordered = DatasetManifest(
        dataset_id=DatasetId("cctv.perimeter"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=3,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 3},
        split_policy="time-ordered",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guidelines"),
        lineage=DatasetLineage(),
        license_and_constraints="Proprietary",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=compute_payload_root_checksum(splits.all_hashes()),
        leakage_audit_records=(),
    )

    t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc)

    # 1. Time-ordered missing capture_timestamp fails closed
    meta_missing_ts = {
        h1: ItemMetadata(content_hash=h1, capture_timestamp=t1),
        h2: ItemMetadata(content_hash=h2, capture_timestamp=None),  # Missing!
        h3: ItemMetadata(content_hash=h3, capture_timestamp=t3),
    }
    res_missing = auditor.audit_splits(manifest_time_ordered, meta_missing_ts)
    r_ts_missing = next(
        r for r in res_missing if r.leakage_type == LeakageType.TEMPORAL
    )
    assert not r_ts_missing.passed
    assert "requires capture_timestamp on all items" in r_ts_missing.summary

    # 2. Time-ordered where eval precedes train fails closed
    meta_inverted = {
        h1: ItemMetadata(content_hash=h1, capture_timestamp=t3),  # Train is latest!
        h2: ItemMetadata(content_hash=h2, capture_timestamp=t2),
        h3: ItemMetadata(content_hash=h3, capture_timestamp=t1),  # Test is earliest!
    }
    res_inv = auditor.audit_splits(manifest_time_ordered, meta_inverted)
    r_inv = next(r for r in res_inv if r.leakage_type == LeakageType.TEMPORAL)
    assert not r_inv.passed
    assert "Time-ordered split policy violated" in r_inv.summary

    # 3. Clean time-ordered (t1 < t2 < t3) passes
    meta_ordered = {
        h1: ItemMetadata(content_hash=h1, capture_timestamp=t1),
        h2: ItemMetadata(content_hash=h2, capture_timestamp=t2),
        h3: ItemMetadata(content_hash=h3, capture_timestamp=t3),
    }
    res_ordered = auditor.audit_splits(manifest_time_ordered, meta_ordered)
    r_ordered = next(r for r in res_ordered if r.leakage_type == LeakageType.TEMPORAL)
    assert r_ordered.passed

    # 4. General temporal policy: temporal window straddling check
    # Train has t1 and t3; Eval has t2 which is strictly inside [t1, t3]!
    splits_straddle = SplitMembership(
        train_hashes=(h1, h3), val_hashes=(), test_hashes=(h2,)
    )
    manifest_straddle = manifest_time_ordered.model_copy(
        update={
            "split_policy": "temporal-disjoint",
            "split_membership": splits_straddle,
        }
    )
    res_straddle = auditor.audit_splits(manifest_straddle, meta_ordered)
    r_straddle = next(r for r in res_straddle if r.leakage_type == LeakageType.TEMPORAL)
    assert not r_straddle.passed
    assert "Temporal window straddling detected" in r_straddle.summary


def test_annotation_geometry_nan_inf_and_polygons() -> None:
    """Finding 4 (AC #8): Rejection of NaN/Inf coordinates, non-closed polygons, and zero-area polygons."""
    h1 = _dummy_hash("geom_adv_item")
    classes = [_make_class_def("person")]

    def _make_manifest(primitives: tuple[dict[str, object], ...]) -> AnnotationManifest:
        return AnnotationManifest(
            annotation_version=AnnotationVersion("ann-v1.0.0"),
            dataset_id=DatasetId("cctv.perimeter"),
            guideline_ref=GuidelineRef("git:guide"),
            annotation_format="yolo_v8",
            payload_annotations={h1: primitives},
        )

    # 1. Bbox with NaN coordinate
    with pytest.raises(InvalidGeometryError, match="must be finite"):
        validate_annotation_geometry(
            _make_manifest(
                ({"class_name": "person", "bbox": [float("nan"), 10, 50, 50]},)
            ),
            classes,
        )

    # 2. Bbox with Inf coordinate
    with pytest.raises(InvalidGeometryError, match="must be finite"):
        validate_annotation_geometry(
            _make_manifest(
                ({"class_name": "person", "bbox": [10, 10, float("inf"), 50]},)
            ),
            classes,
        )

    # 3. Polygon with NaN coordinate
    with pytest.raises(InvalidGeometryError, match="must be finite"):
        validate_annotation_geometry(
            _make_manifest(
                (
                    {
                        "class_name": "person",
                        "polygon": [(float("nan"), 10), (50, 10), (50, 50)],
                    },
                )
            ),
            classes,
        )

    # 4. Polygon with Inf coordinate
    with pytest.raises(InvalidGeometryError, match="must be finite"):
        validate_annotation_geometry(
            _make_manifest(
                (
                    {
                        "class_name": "person",
                        "polygon": [(10, 10), (float("-inf"), 10), (50, 50)],
                    },
                )
            ),
            classes,
        )

    # 5a. Polygon explicitly marked non-closed
    with pytest.raises(InvalidGeometryError, match="explicitly marked as non-closed"):
        validate_annotation_geometry(
            _make_manifest(
                (
                    {
                        "class_name": "person",
                        "polygon": [(10, 10), (50, 10), (50, 50)],
                        "closed": False,
                    },
                )
            ),
            classes,
        )

    # 5b. Unclosed polygon with omitted closure field fails closed (PM Adversarial Blocker 1)
    with pytest.raises(InvalidGeometryError, match="polygon is not closed"):
        validate_annotation_geometry(
            _make_manifest(
                (
                    {
                        "class_name": "person",
                        "polygon": [(10, 10), (50, 10), (50, 50), (10, 50)],
                        # closure field omitted, and points[0] != points[-1]
                    },
                )
            ),
            classes,
        )

    # 6. Collinear polygon with closed=True (zero area)
    with pytest.raises(InvalidGeometryError, match="zero or degenerate area"):
        validate_annotation_geometry(
            _make_manifest(
                (
                    {
                        "class_name": "person",
                        "polygon": [(10, 10), (20, 20), (30, 30)],
                        "closed": True,
                    },
                )
            ),
            classes,
        )

    # 7. Duplicate points polygon with closed=True (zero area)
    with pytest.raises(
        InvalidGeometryError,
        match="fewer than 3 distinct vertices|zero or degenerate area",
    ):
        validate_annotation_geometry(
            _make_manifest(
                (
                    {
                        "class_name": "person",
                        "polygon": [(10, 10), (10, 10), (10, 10)],
                        "closed": True,
                    },
                )
            ),
            classes,
        )

    # 8. Valid closed ring polygon with positive area passes
    valid_ring_manifest = _make_manifest(
        (
            {
                "class_name": "person",
                "polygon": [(10, 10), (50, 10), (50, 50), (10, 50), (10, 10)],
            },
        )
    )
    validate_annotation_geometry(valid_ring_manifest, classes)  # Should not raise!


def test_publication_chmod_failure_fails_closed(
    temp_store: LocalFSStore,
    temp_registry: FileSystemDatasetRegistry,
    tmp_path: Path,
) -> None:
    """Finding 5 (I2): chmod(0o444) failure is not swallowed; publication fails closed with DatasetError."""
    # 1. Test LocalFSStore.put_payload fails closed on chmod failure
    payload_file = tmp_path / "test_payload.bin"
    payload_file.write_bytes(b"content to store")

    with (
        patch("os.chmod", side_effect=OSError("Read-only file system")),
        pytest.raises(DatasetError, match="Failed to enforce read-only permissions"),
    ):
        temp_store.put_payload(payload_file)

    # 2. Test FileSystemDatasetRegistry.register_dataset fails closed on chmod failure
    h1 = temp_store.put_payload(payload_file)
    splits = SplitMembership(train_hashes=(h1,), val_hashes=(), test_hashes=())
    manifest = DatasetManifest(
        dataset_id=DatasetId("cctv.perimeter"),
        version=DatasetVersion("v1.0.0"),
        sources=("site-1",),
        n_items=1,
        class_definitions=(_make_class_def(),),
        class_distribution={"person": 1},
        split_policy="single-site",
        split_membership=splits,
        annotation_version=AnnotationVersion("ann-v1.0.0"),
        annotation_guideline_ref=GuidelineRef("git:guide"),
        lineage=DatasetLineage(),
        license_and_constraints="Proprietary",
        collection_conditions=_make_collection_conditions(),
        payload_root_checksum=compute_payload_root_checksum(splits.all_hashes()),
        leakage_audit_records=_make_passed_audits(),
    )

    with (
        patch("os.chmod", side_effect=OSError("Operation not permitted")),
        pytest.raises(DatasetError, match="Failed to enforce read-only permissions"),
    ):
        temp_registry.register(manifest, store=temp_store)

    # Verify target manifest was cleaned up and does not exist
    target_manifest_path = temp_registry._base_dir / "cctv.perimeter" / "v1.0.0.json"
    assert not target_manifest_path.exists()


def test_remote_storage_s3_and_gcs_full_integrity_and_corruption_semantics(
    tmp_path: Path,
) -> None:
    """Finding 6 (B4): Full remote storage put/get lifecycle, streaming SHA-256 verification, and corruption detection."""
    # 1. S3Store tests with mocked boto3
    s3_store = S3Store(bucket="cv-agent-data", prefix="blobs")

    payload_file = tmp_path / "s3_sample.bin"
    sample_bytes = b"sample s3 data payload"
    payload_file.write_bytes(sample_bytes)
    expected_hash = ContentHash(hashlib.sha256(sample_bytes).hexdigest())

    mock_boto3 = MagicMock()
    mock_s3_client = MagicMock()
    mock_boto3.client.return_value = mock_s3_client
    # Simulate head_object raising NoSuchKey on first check
    mock_s3_client.head_object.side_effect = Exception("NoSuchKey")
    mock_s3_client.get_object.return_value = {"Body": io.BytesIO(sample_bytes)}

    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        # Put payload - clean upload with verified remote-side integrity
        put_hash = s3_store.put_payload(payload_file)
        assert put_hash == expected_hash
        assert mock_s3_client.upload_file.called
        assert mock_s3_client.get_object.called

        # Put payload - corrupt remote-side post-upload verification detection (Blocker 2)
        mock_s3_client.get_object.return_value = {
            "Body": io.BytesIO(b"corrupted bytes on remote S3")
        }
        with pytest.raises(
            PayloadCorruptionError, match="Payload byte corruption detected"
        ):
            s3_store.put_payload(payload_file)
        assert mock_s3_client.delete_object.called

        # Reset get_object for subsequent get_payload tests
        mock_s3_client.get_object.return_value = {"Body": io.BytesIO(sample_bytes)}

        # Get payload - clean download
        dest_path = tmp_path / "downloaded_s3.bin"

        def _mock_download(bucket: str, key: str, filename: str) -> None:
            Path(filename).write_bytes(sample_bytes)

        mock_s3_client.download_file.side_effect = _mock_download
        retrieved_path = s3_store.get_payload(expected_hash, dest_path)
        assert retrieved_path == dest_path
        assert dest_path.read_bytes() == sample_bytes

        # Get payload - corrupt download detection
        dest_corrupt = tmp_path / "downloaded_s3_corrupt.bin"

        def _mock_corrupt_download(bucket: str, key: str, filename: str) -> None:
            Path(filename).write_bytes(b"corrupted bytes from s3 wire")

        mock_s3_client.download_file.side_effect = _mock_corrupt_download
        with pytest.raises(
            PayloadCorruptionError, match="Payload byte corruption detected"
        ):
            s3_store.get_payload(expected_hash, dest_corrupt)
        assert not dest_corrupt.exists()

        # Get payload - 404 not found
        dest_missing = tmp_path / "downloaded_s3_missing.bin"
        mock_s3_client.download_file.side_effect = Exception("404 Not Found")
        with pytest.raises(PayloadNotFoundError, match="not found"):
            s3_store.get_payload(expected_hash, dest_missing)

    # 2. GCSStore tests with mocked google.cloud.storage
    gcs_store = GCSStore(bucket="cv-agent-gcs-data", prefix="blobs")
    mock_google = MagicMock()
    mock_google_cloud = MagicMock()
    mock_gcs = MagicMock()
    mock_google_cloud.storage = mock_gcs
    mock_google.cloud = mock_google_cloud
    mock_gcs_client = MagicMock()
    mock_gcs.Client.return_value = mock_gcs_client
    mock_bucket = MagicMock()
    mock_blob = MagicMock()
    mock_gcs_client.bucket.return_value = mock_bucket
    mock_bucket.blob.return_value = mock_blob
    mock_blob.exists.return_value = False
    mock_blob.open.return_value.__enter__.return_value = io.BytesIO(sample_bytes)

    with patch.dict(
        "sys.modules",
        {
            "google": mock_google,
            "google.cloud": mock_google_cloud,
            "google.cloud.storage": mock_gcs,
        },
    ):
        # Put payload - clean upload with verified remote-side integrity
        gcs_put_hash = gcs_store.put_payload(payload_file)
        assert gcs_put_hash == expected_hash
        assert mock_blob.upload_from_filename.called

        # Put payload - corrupt remote-side post-upload verification detection (Blocker 2)
        mock_blob.open.return_value.__enter__.return_value = io.BytesIO(
            b"corrupted bytes on remote GCS"
        )
        with pytest.raises(
            PayloadCorruptionError, match="Payload byte corruption detected"
        ):
            gcs_store.put_payload(payload_file)
        assert mock_blob.delete.called

        # Reset mock_blob for get_payload tests
        mock_blob.open.return_value.__enter__.return_value = io.BytesIO(sample_bytes)

        # Get payload - clean download
        mock_blob.exists.return_value = True

        def _mock_gcs_download(filename: str) -> None:
            Path(filename).write_bytes(sample_bytes)

        mock_blob.download_to_filename.side_effect = _mock_gcs_download
        gcs_dest = tmp_path / "downloaded_gcs.bin"
        retrieved_gcs = gcs_store.get_payload(expected_hash, gcs_dest)
        assert retrieved_gcs == gcs_dest
        assert gcs_dest.read_bytes() == sample_bytes

        # Get payload - corruption detection
        def _mock_gcs_corrupt_download(filename: str) -> None:
            Path(filename).write_bytes(b"tampered gcs bytes")

        mock_blob.download_to_filename.side_effect = _mock_gcs_corrupt_download
        gcs_corrupt_dest = tmp_path / "downloaded_gcs_corrupt.bin"
        with pytest.raises(
            PayloadCorruptionError, match="Payload byte corruption detected"
        ):
            gcs_store.get_payload(expected_hash, gcs_corrupt_dest)
        assert not gcs_corrupt_dest.exists()

        # Get payload - not found
        mock_blob.exists.return_value = False
        gcs_missing_dest = tmp_path / "downloaded_gcs_missing.bin"
        with pytest.raises(PayloadNotFoundError, match="not found"):
            gcs_store.get_payload(expected_hash, gcs_missing_dest)
