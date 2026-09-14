"""cv_agent.dataset.contracts — Frozen Pydantic v2 schemas for Dataset Subsystem (ADR-0009)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from cv_agent.dataset.errors import SplitContaminationError
from cv_agent.dataset.types import (
    AnnotationVersion,
    ContentHash,
    DatasetId,
    DatasetVersion,
    GuidelineRef,
    LeakageType,
    validate_annotation_version,
    validate_content_hash,
    validate_dataset_id,
    validate_dataset_version,
    validate_guideline_ref,
)


def _utcnow() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(timezone.utc)


def _to_frozen_tuple(v: Any) -> tuple[Any, ...]:
    """Convert sequence input to an immutable tuple."""
    if isinstance(v, (list, tuple)):
        return tuple(v)
    return v


# ---------------------------------------------------------------------------
# Manifest Component Models (ADR-0009 §4)
# ---------------------------------------------------------------------------


class ClassDefinition(BaseModel):
    """Rigorous class definition with explicit edge-case boundaries [P§5],[P§7]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Name of the semantic class.")
    description: str = Field(
        description="Observable physical criteria defining class membership."
    )
    temporal_boundaries: str | None = Field(
        default=None,
        description="Temporal start and end observable criteria for event classes.",
    )
    near_miss_negatives: tuple[str, ...] = Field(
        description="Explicit observable behaviors/items that are strictly negative examples.",
    )
    ambiguity_resolution: str = Field(
        description="Deterministic instruction for annotators when classification is ambiguous.",
    )

    @field_validator("near_miss_negatives", mode="before")
    @classmethod
    def _coerce_tuple(cls, v: Any) -> tuple[str, ...]:
        return _to_frozen_tuple(v)


class CollectionConditions(BaseModel):
    """Environmental and hardware parameters during data capture [P§9]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    camera_models: tuple[str, ...]
    sites: tuple[str, ...]
    times_of_day: tuple[str, ...]
    lighting_conditions: tuple[str, ...]
    weather_conditions: tuple[str, ...]
    camera_health_notes: str | None = None

    @field_validator(
        "camera_models",
        "sites",
        "times_of_day",
        "lighting_conditions",
        "weather_conditions",
        mode="before",
    )
    @classmethod
    def _coerce_tuples(cls, v: Any) -> tuple[str, ...]:
        return _to_frozen_tuple(v)


class LeakageCheckResult(BaseModel):
    """Audit result for a single leakage check dimension [P§26]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leakage_type: LeakageType
    passed: bool
    summary: str
    metrics: Mapping[str, float] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utcnow)
    auditor_version: str

    @field_validator("metrics", mode="after")
    @classmethod
    def _freeze_metrics(cls, v: Mapping[str, float]) -> Mapping[str, float]:
        if isinstance(v, dict):
            return MappingProxyType(dict(v))
        return v

    @field_serializer("metrics", when_used="always")
    def _serialize_metrics(self, v: Mapping[str, float]) -> dict[str, float]:
        return dict(v)


class DatasetLineage(BaseModel):
    """Provenance and derivation graph for a dataset version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_version: DatasetVersion | None = None
    transformation_name: str | None = None
    transformation_params: Mapping[str, Any] = Field(default_factory=dict)
    source_manifest_hash: str | None = None

    @field_validator("parent_version", mode="before")
    @classmethod
    def _validate_parent_version(cls, v: Any) -> DatasetVersion | None:
        if v is None:
            return None
        return validate_dataset_version(str(v))

    @field_validator("transformation_params", mode="after")
    @classmethod
    def _freeze_params(cls, v: Mapping[str, Any]) -> Mapping[str, Any]:
        if isinstance(v, dict):
            return MappingProxyType(dict(v))
        return v

    @field_serializer("transformation_params", when_used="always")
    def _serialize_params(self, v: Mapping[str, Any]) -> dict[str, Any]:
        return dict(v)


class SplitMembership(BaseModel):
    """Explicit item membership for partitions without random runtime generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    train_hashes: tuple[ContentHash, ...]
    val_hashes: tuple[ContentHash, ...]
    test_hashes: tuple[ContentHash, ...]
    calibration_hashes: tuple[ContentHash, ...] = Field(
        default_factory=tuple,
        description="Calibration partition for INT8 PTQ quantization without test split leakage.",
    )

    @field_validator(
        "train_hashes", "val_hashes", "test_hashes", "calibration_hashes", mode="before"
    )
    @classmethod
    def _coerce_and_validate_hashes(cls, v: Any) -> tuple[ContentHash, ...]:
        coerced = _to_frozen_tuple(v)
        return tuple(validate_content_hash(str(h)) for h in coerced)

    @model_validator(mode="after")
    def _enforce_split_disjointness(self) -> SplitMembership:
        """Enforce that train, val, test, and calibration partitions are strictly disjoint."""
        partitions = [
            ("train", set(self.train_hashes)),
            ("val", set(self.val_hashes)),
            ("test", set(self.test_hashes)),
            ("calibration", set(self.calibration_hashes)),
        ]
        for i in range(len(partitions)):
            name_a, set_a = partitions[i]
            for j in range(i + 1, len(partitions)):
                name_b, set_b = partitions[j]
                intersection = set_a & set_b
                if intersection:
                    raise SplitContaminationError(intersection, (name_a, name_b))
        return self

    def all_hashes(self) -> set[ContentHash]:
        """Return the union of all unique content hashes across all splits."""
        return (
            set(self.train_hashes)
            | set(self.val_hashes)
            | set(self.test_hashes)
            | set(self.calibration_hashes)
        )


# ---------------------------------------------------------------------------
# Leakage Audit Input Model (Gate B3)
# ---------------------------------------------------------------------------


class ItemMetadata(BaseModel):
    """Metadata for a single media item used during leakage auditing (Gate B3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content_hash: ContentHash
    clip_id: str | None = None
    capture_timestamp: datetime | None = None
    camera_id: str | None = None
    site_id: str | None = None
    subject_ids: tuple[str, ...] = Field(default_factory=tuple)
    perceptual_hash: str | None = None

    @field_validator("content_hash", mode="before")
    @classmethod
    def _val_hash(cls, v: Any) -> ContentHash:
        return validate_content_hash(str(v))

    @field_validator("subject_ids", mode="before")
    @classmethod
    def _coerce_subjects(cls, v: Any) -> tuple[str, ...]:
        return _to_frozen_tuple(v)


# ---------------------------------------------------------------------------
# Authoritative Dataset & Annotation Manifests (ADR-0009 §4)
# ---------------------------------------------------------------------------


class AnnotationManifest(BaseModel):
    """Independent annotation version manifest [P§26]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    annotation_version: AnnotationVersion
    dataset_id: DatasetId
    created: datetime = Field(default_factory=_utcnow)
    guideline_ref: GuidelineRef
    annotator_id: str | None = None
    annotation_format: str = Field(
        description="e.g. 'yolo_v8', 'coco_instances', 'csv_events'"
    )
    payload_annotations: Mapping[ContentHash, tuple[Mapping[str, Any], ...]] = Field(
        default_factory=dict,
        description="Mapping of content hash to list of annotation primitives.",
    )

    @field_validator("annotation_version", mode="before")
    @classmethod
    def _val_ann_version(cls, v: Any) -> AnnotationVersion:
        return validate_annotation_version(str(v))

    @field_validator("dataset_id", mode="before")
    @classmethod
    def _val_dataset_id(cls, v: Any) -> DatasetId:
        return validate_dataset_id(str(v))

    @field_validator("guideline_ref", mode="before")
    @classmethod
    def _val_guideline_ref(cls, v: Any) -> GuidelineRef:
        return validate_guideline_ref(str(v))

    @field_validator("payload_annotations", mode="before")
    @classmethod
    def _freeze_payload_annotations(
        cls, v: Any
    ) -> Mapping[ContentHash, tuple[Mapping[str, Any], ...]]:
        if not isinstance(v, (dict, Mapping)):
            return v
        frozen_dict: dict[ContentHash, tuple[Mapping[str, Any], ...]] = {}
        for k, prim_list in v.items():
            ch = validate_content_hash(str(k))
            frozen_prims = tuple(
                MappingProxyType(dict(p)) if isinstance(p, dict) else p
                for p in prim_list
            )
            frozen_dict[ch] = frozen_prims
        return MappingProxyType(frozen_dict)

    @field_serializer("payload_annotations", when_used="always")
    def _serialize_payload_annotations(
        self, v: Mapping[ContentHash, tuple[Mapping[str, Any], ...]]
    ) -> dict[str, list[dict[str, Any]]]:
        res: dict[str, list[dict[str, Any]]] = {}
        for k, prim_tuple in v.items():
            res[str(k)] = [dict(p) for p in prim_tuple]
        return res


class DatasetManifest(BaseModel):
    """Complete, immutable dataset version manifest satisfying docs/DATA.md."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 1-5: Identity & Provenance
    dataset_id: DatasetId
    version: DatasetVersion
    created: datetime = Field(default_factory=_utcnow)
    sources: tuple[str, ...] = Field(
        description="Camera feeds, source archives, or site origins."
    )
    n_items: int = Field(ge=0, description="Total count of media elements in dataset.")

    # 6-7: Classes & Distribution
    class_definitions: tuple[ClassDefinition, ...]
    class_distribution: Mapping[str, int] = Field(description="Item count per class.")

    # 8-9: Splits
    split_policy: str = Field(
        description="Partition strategy, e.g. 'temporal-site-disjoint'."
    )
    split_membership: SplitMembership

    # 10-11: Annotations
    annotation_version: AnnotationVersion
    annotation_guideline_ref: GuidelineRef

    # 12-14: Lineage, Quality & Constraints
    known_issues: tuple[str, ...] = Field(default_factory=tuple)
    lineage: DatasetLineage
    license_and_constraints: str

    # 15-16: Collection Conditions & Integrity
    collection_conditions: CollectionConditions
    payload_root_checksum: str = Field(
        description="Canonical SHA-256 root checksum sealing payload collection (Gate B2)."
    )
    leakage_audit_records: tuple[LeakageCheckResult, ...] = Field(default_factory=tuple)

    @field_validator("dataset_id", mode="before")
    @classmethod
    def _val_id(cls, v: Any) -> DatasetId:
        return validate_dataset_id(str(v))

    @field_validator("version", mode="before")
    @classmethod
    def _val_ver(cls, v: Any) -> DatasetVersion:
        return validate_dataset_version(str(v))

    @field_validator("annotation_version", mode="before")
    @classmethod
    def _val_ann_ver(cls, v: Any) -> AnnotationVersion:
        return validate_annotation_version(str(v))

    @field_validator("annotation_guideline_ref", mode="before")
    @classmethod
    def _val_guide_ref(cls, v: Any) -> GuidelineRef:
        return validate_guideline_ref(str(v))

    @field_validator(
        "sources",
        "class_definitions",
        "known_issues",
        "leakage_audit_records",
        mode="before",
    )
    @classmethod
    def _coerce_manifest_tuples(cls, v: Any) -> tuple[Any, ...]:
        return _to_frozen_tuple(v)

    @field_validator("class_distribution", mode="after")
    @classmethod
    def _freeze_distribution(cls, v: Mapping[str, int]) -> Mapping[str, int]:
        if isinstance(v, dict):
            return MappingProxyType(dict(v))
        return v

    @field_serializer("class_distribution", when_used="always")
    def _serialize_class_distribution(self, v: Mapping[str, int]) -> dict[str, int]:
        return dict(v)

    @model_validator(mode="after")
    def _validate_item_count_matches_splits(self) -> DatasetManifest:
        """Verify that n_items strictly matches the total unique items in split membership."""
        unique_hashes = self.split_membership.all_hashes()
        if self.n_items != len(unique_hashes):
            raise ValueError(
                f"n_items ({self.n_items}) does not equal count of unique split hashes ({len(unique_hashes)})"
            )
        return self
