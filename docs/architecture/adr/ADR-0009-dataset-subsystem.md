# ADR-0009: Dataset Subsystem Architecture

- **Status:** Accepted — Frozen
- **Date:** 2026-09-08
- **Layer:** data / dataset engineering
- **Canon:** `[P§7]`, `[P§25]`, `[P§26]`, `[P§27]`, `[P§29.5]`, `[P§34]`
- **Supersedes / Superseded by:** —
- **Issue:** #2
- **Preceded by Decisions:** D-028 (Q10 resolution), D-021 (ADR-0004 experiment foreign key), D-020 (ADR-0003 approval boundary)

## 1. Context

In empirical computer vision engineering, "data is an engineering artifact, not an input file" `[P§26]`. Flawed data engineering undermines the entire lifecycle: ambiguous class definitions cause the majority of post-training failures `[P§27]`; split leakage (temporal, camera, near-duplicate) invalidates evaluation claims `[P§26]`, `[P§29.3]`; and unversioned or in-place modified datasets void experiment reproducibility `[P§25]`, `[P§29.5]`.

Prior to this decision, the storage and versioning architecture was held open as question Q10 in `docs/state/OPEN_QUESTIONS.md`. With decision `D-028`, the project owner and Official PM resolved Q10 by approving the **Manifest-First Content-Addressed Dataset Storage** architecture.

This ADR formalizes that architecture into rigorous component boundaries, schemas, and type-safe interface contracts. It addresses five critical requirements:
1. **Authoritative, Git-Tracked Manifests (`[P§26]`, `docs/DATA.md`):** Dataset metadata, class definitions, edge cases, split policies, and checksums must remain human-readable, Git-diffable, and immutable per version.
2. **Decoupled, Content-Addressed Payloads (`[P§26]`, `D-028`):** Raw media files (images, video streams, point clouds) must not pollute Git history, nor depend on invasive third-party runtime frameworks (such as DVC or Git LFS) for basic agent operations. Instead, payloads are addressed by cryptographic hash across pluggable backends (`local_fs`, `s3`, `gcs`, `minio`).
3. **Independent Annotation Versioning (`[P§26]`):** Relabeling, guideline revisions, and model-assisted labeling iterations produce distinct annotation versions without altering underlying media identities.
4. **Leakage-Audited Splits (`[P§26]`):** Split membership is explicitly recorded, deterministically reproducible, and subjected to automated leakage verification (temporal, camera/site, duplicate, identity, annotation-round).
5. **Remote Execution Compatibility (`[P§10]`, `[P§13]`, `D-014`):** Remote GPU training nodes and cloud cluster workers must be able to fetch exact dataset slices via content identity without cloning the entire Git repository.

## 2. Responsibility (`[P§34]`)

- **This owns:**
  - Authoritative dataset identity (`DatasetId`, `DatasetVersion`, `ContentHash`, `LineageRef`).
  - The typed dataset manifest schema (`DatasetManifest`) implementing all 16 mandatory fields defined in `docs/DATA.md`.
  - The independent annotation manifest schema (`AnnotationManifest`) and guideline references.
  - The explicit split model (`DatasetSplit`, `SplitMembership`) guaranteeing deterministic partition retrieval.
  - The content-addressed storage abstraction (`DatasetStore`) and pluggable backend interfaces (`LocalFSStore`, `S3Store`, `GCSStore`, `MinioStore`).
  - Integrity verification protocols: content hashing, manifest schema validation, and payload integrity checksum verification.
  - Leakage verification contracts (`LeakageAuditor`, `LeakageCheckResult`) covering temporal, camera/site, near-duplicate, identity, and annotation-round checks.
  - Lineage tracking (`DatasetLineage`) recording parent dataset version, transformation parameters, and resulting content hashes.
  - Dataset mutation approval boundaries integrating with `docs/APPROVALS.md` `[P§24]`.
- **This does NOT own:**
  - Workflow orchestration, state transitions, or human-in-the-loop pause/resume loops → Orchestration Subsystem (ADR-0003) `[P§21]`;
  - SQLite experiment result ledgers or metric persistence → Project Memory & Ledger (ADR-0004) `[P§25]`;
  - Low-level subprocess execution, container virtualization, or MCP transport → Tools & MCP Boundary (ADR-0005) `[P§22]`;
  - Paper, code, or documentation vector embeddings and hybrid RAG indexing → Knowledge Subsystem (ADR-0006) `[P§16]`–`[P§18]`;
  - Procedural conversion recipes, vendor-specific preprocessing scripts, or augmentations → Skills Subsystem (ADR-0007) `[P§15]`;
  - Cognitive dataset selection, diagnosis of dataset quality, or hypothesis generation → Reasoning Subsystem (ADR-0008) `[P§4]`;
  - LLM completion requests, prompt templates, or token routing → LLM Gateway (ADR-0002) `[P§20]`;
  - Capability taxonomy definitions or registry authority → Capability Model (ADR-0001) `[P§23]`.
- **Why this responsibility does not belong elsewhere:**
  Treating dataset storage as an implementation detail of tools (ADR-0005) leads to ad-hoc paths, unversioned directories, and silent data leakage. Conflating dataset manifests with experiment ledgers (ADR-0004) creates tight relational coupling between data definitions and runtime execution metrics. Isolating the Dataset Subsystem establishes a clean contract where datasets are first-class, versioned, immutable engineering artifacts.

## 3. Decision

We establish a **Manifest-First Content-Addressed Dataset Subsystem Architecture** adhering to the ten non-negotiable invariants codified in `docs/DATA.md`.

```text
                           Git-Tracked Manifest
                       (.cv_agent/datasets/<id>/<v>.json)
                      ┌─────────────────────────────────┐
                      │  DatasetId & Version            │
                      │  Class Definitions & Edge Cases │
                      │  Lineage (Parent + Transform)   │
                      │  Split Memberships (Hash Lists) │
                      │  Annotation Version Reference   │
                      │  Leakage Audit Records          │
                      │  Payload Checksum Index         │
                      └────────────────┬────────────────┘
                                       │
                      ContentHash Resolution & Fetch
                                       ▼
                   ┌───────────────────────────────────────┐
                   │        DatasetStore Interface         │
                   └───────────────────┬───────────────────┘
                                       │
         ┌─────────────────┬───────────┴───────────┬─────────────────┐
         ▼                 ▼                       ▼                 ▼
   ┌───────────┐     ┌───────────┐           ┌───────────┐     ┌───────────┐
   │ Local FS  │     │ AWS S3    │           │ GCS       │     │ MinIO     │
   │ (file://) │     │ (s3://)   │           │ (gs://)   │     │ (minio://)│
   └───────────┘     └───────────┘           └───────────┘     └───────────┘
```

### 3.1. Manifest-First Content-Addressed Architecture

1. **Manifest as Ground Truth:** The manifest is a single, structured JSON document stored under `.cv_agent/datasets/<dataset_id>/<version>.json`. It is committed directly to Git, ensuring every version change is Git-diffable and tracked in version control history.
2. **Content-Addressed Payload Store:** Raw image/video items are stored in content-addressed blobs named by their cryptographic SHA-256 hash (e.g. `blobs/ab/cd/abcd1234...`). Media files are never renamed or overwritten in place. If an image is modified (e.g. cropped, color-corrected), it receives a new `ContentHash`.
3. **Pluggable Backends:** Payloads reside in external storage addressed via standard URIs (`file:///var/data/blobs`, `s3://bucket/blobs`, `gs://bucket/blobs`, `minio://endpoint/blobs`). The agent core interacts exclusively through the `DatasetStore` abstraction.

### 3.2. Authoritative Dataset Manifest Schema

Per `docs/DATA.md`, the dataset manifest strictly mandates 16 fields, enforced by a Pydantic v2 frozen schema (`DatasetManifest`):
- `dataset_id`: Unique identifier (e.g. `cctv.perimeter.intrusion`).
- `version`: SemVer or monotonic incremental string (e.g. `v1.2.0`).
- `created`: ISO 8601 UTC timestamp.
- `sources`: Provenance URIs, camera feeds, physical sites, or source archive hashes.
- `n_items`: Total number of individual media elements (images or video clips).
- `class_definitions`: Mapping of class names to physical observable definitions, start/end temporal boundaries, near-miss negative criteria, and ambiguity rules `[P§5]`, `[P§7]`.
- `class_distribution`: Item count and percentage breakdown per class across all items.
- `split_policy`: Strategy used to partition data (e.g. `temporal-camera-disjoint`).
- `split_membership`: Explicit mapping of split names (`train`, `val`, `test`) to lists of item content hashes. Random seed generation at runtime is strictly prohibited.
- `annotation_version`: Identifier of the annotation set applied to this dataset version.
- `annotation_guideline_ref`: URI or Git commit reference to the authoritative annotation guideline document.
- `known_issues`: Documented anomalies, sensor artifacts, lighting limitations, or known annotation noise.
- `lineage`: Parent dataset version, applied transformation operations, and source hashes.
- `license_and_constraints`: Legal usage restrictions, GDPR/privacy constraints, and retention policies.
- `collection_conditions`: Environmental parameters including camera models, mounting heights, lighting, weather, and resolution.
- `payload_checksums`: Cryptographic root hash (Merkle root or composite manifest hash) sealing the entire payload collection.

### 3.3. Independent Annotation Versioning

Annotations (bounding boxes, polygons, keypoints, event timestamps) evolve at a different cadence than media capture:
1. **Decoupled Identity:** An `AnnotationManifest` exists independently with its own `annotation_version` (e.g. `ann-v2.1.0`), referencing media items solely by their `ContentHash`.
2. **Zero In-Place Relabeling:** Correcting labels or refining boundaries generates a new `AnnotationManifest`. Existing annotations remain immutable.
3. **Guideline Reference:** Every annotation set must cite the specific guideline document and version under which labels were created.

### 3.4. Explicit Split Model & Leakage Auditing

"A split regenerated by a random seed at runtime is not reproducible" `[P§26]`, `docs/DATA.md`.

1. **Explicit Membership:** Split partitions (`train`, `val`, `test`) are explicitly frozen in the manifest as sets of `ContentHash` references.
2. **Mandatory Leakage Verification:** Before any dataset split is accepted or consumed by training, an automated audit must evaluate and record five distinct leakage checks:
   - **Temporal Leakage:** Ensures no frames from the same video clip or temporally contiguous window straddle splits.
   - **Camera / Site Leakage:** Verifies that physical camera locations or capture sites appearing in train do not appear in test (when cross-camera generalization is required).
   - **Near-Duplicate Leakage:** Executes perceptual hashing (pHash / dHash) across splits to detect identical or near-identical frames.
   - **Subject / Identity Leakage:** Ensures unique individuals, vehicles, or tracked objects do not straddle train and test splits for re-identification or tracking tasks.
   - **Annotation-Round Leakage:** Confirms that data labeled under conflicting annotation guideline revisions is not blended silently.
3. **Audit Recording:** All five check results (pass/fail status, metric values, audit timestamp, and auditor version) are embedded directly into the manifest.

### 3.5. Lineage & Immutable Lifecycle

Dataset mutations follow a strict directed acyclic graph (DAG):
$$\text{Parent Version } (V_0) + \text{Transformation } (T) \longrightarrow \text{New Version } (V_1)$$

- **Immutability:** Once a manifest is committed to `.cv_agent/datasets/`, its metadata and payload hashes are permanently locked. Any change—whether filtering, augmentations, relabeling, or re-splitting—produces a new version.
- **Transformation Record:** The `DatasetLineage` record captures the exact transformation recipe, including parameter arguments, filtering thresholds, and tooling references (ADR-0005/ADR-0007).

### 3.6. Remote Execution Support (`[P§10]`, `D-014`)

Per decision `D-014`, training is submitted to external local or remote GPU compute targets:
1. **Git-Free Retrieval:** Remote compute environments do not require access to the agent's Git repository. Given a `DatasetManifest` or a split hash list, the remote execution launcher invokes `DatasetStore.get_payload()` or synchronizes the required content-addressed blobs directly from the configured remote store (`s3://`, `gs://`, `minio://`).
2. **Targeted Fetching:** Only payloads belonging to the active split (e.g. `train` + `val` for training; `test` for evaluation) need to be downloaded, drastically reducing egress overhead.

### 3.7. Human Approval Integration (`[P§24]`, `docs/APPROVALS.md`)

Per `[P§24]` and `docs/DATA.md`, destructive or costly dataset operations are non-negotiable approval gates:
- `dataset.delete`: Deleting dataset manifests or stored payload blobs.
- `dataset.relabel`: Modifying labels across an existing dataset version.
- `dataset.resplit`: Re-partitioning existing train/val/test splits.
- `dataset.mutate`: Bulk filtering, synthetic augmentation, or destructive preprocessing.

These actions generate an `ApprovalRequest` routed through Orchestration (ADR-0003), requiring explicit human authorization before execution.

## 4. Interface Contracts (Type Stubs)

The following type definitions specify the architecture contracts of the Dataset Subsystem. All data schemas are standardized on Pydantic v2 with `ConfigDict(frozen=True)`.

```python
"""Dataset Subsystem Architecture Contracts (ADR-0009).

Architecture interface stubs only — no runtime implementation bodies.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, NewType, Protocol, Sequence
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Typed Identifiers
# ---------------------------------------------------------------------------
# Identifier syntax rules:
# - DatasetId: conforms to `[a-z0-9_.-]+(/[a-z0-9_.-]+)*` per ADR-0001 §3.1 (e.g. 'cctv.perimeter.intrusion').
# - DatasetVersion: conforms to `v[0-9]+\.[0-9]+\.[0-9]+(-[a-z0-9_.-]+)?` or monotonic increment string (e.g. 'v1.0.0').
# - ContentHash: lowercase 64-character hex string conforming to `^[a-f0-9]{64}$` (SHA-256 digest of payload bytes).
# - AnnotationVersion: conforms to `[a-z0-9_.-]+` (e.g. 'ann-2026.09.01' or 'ann-v1.0.0').
# - GuidelineRef: URI or commit SHA conforming to `[a-z0-9_./:-]+`.

DatasetId = NewType("DatasetId", str)
"""Authoritative dataset identifier, e.g. 'cctv.perimeter.intrusion'."""

DatasetVersion = NewType("DatasetVersion", str)
"""Monotonic dataset version string, e.g. 'v1.0.0'."""

ContentHash = NewType("ContentHash", str)
"""Cryptographic SHA-256 hex digest of payload bytes."""

AnnotationVersion = NewType("AnnotationVersion", str)
"""Authoritative annotation version string, e.g. 'ann-2026.09.01'."""

GuidelineRef = NewType("GuidelineRef", str)
"""URI or commit SHA pointing to the governing annotation guideline."""


# ---------------------------------------------------------------------------
# Enums and Value Objects
# ---------------------------------------------------------------------------


class StorageBackendType(str, Enum):
    """Supported storage backend protocol types."""

    LOCAL_FS = "local_fs"
    S3 = "s3"
    GCS = "gcs"
    MINIO = "minio"


class SplitName(str, Enum):
    """Standard split partition identifiers."""

    TRAIN = "train"
    VAL = "val"
    TEST = "test"
    CALIBRATION = "calibration"


class LeakageType(str, Enum):
    """Taxonomy of dataset split leakage checks per docs/DATA.md."""

    TEMPORAL = "temporal"
    CAMERA_SITE = "camera_site"
    NEAR_DUPLICATE = "near_duplicate"
    SUBJECT_IDENTITY = "subject_identity"
    ANNOTATION_ROUND = "annotation_round"


# ---------------------------------------------------------------------------
# Manifest Components (Pydantic v2 Models)
# ---------------------------------------------------------------------------


class ClassDefinition(BaseModel):
    """Rigorous class definition with explicit edge-case boundaries [P§5],[P§7]."""

    model_config = ConfigDict(frozen=True)

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


class CollectionConditions(BaseModel):
    """Environmental and hardware parameters during data capture [P§9]."""

    model_config = ConfigDict(frozen=True)

    camera_models: tuple[str, ...]
    sites: tuple[str, ...]
    times_of_day: tuple[str, ...]
    lighting_conditions: tuple[str, ...]
    weather_conditions: tuple[str, ...]
    camera_health_notes: str | None = None


class LeakageCheckResult(BaseModel):
    """Audit result for a single leakage check dimension [P§26]."""

    model_config = ConfigDict(frozen=True)

    leakage_type: LeakageType
    passed: bool
    summary: str
    metrics: Mapping[str, float] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    auditor_version: str


class DatasetLineage(BaseModel):
    """Provenance and derivation graph for a dataset version."""

    model_config = ConfigDict(frozen=True)

    parent_version: DatasetVersion | None = None
    transformation_name: str | None = None
    transformation_params: Mapping[str, Any] = Field(default_factory=dict)
    source_manifest_hash: str | None = None


class SplitMembership(BaseModel):
    """Explicit item membership for partitions without random runtime generation."""

    model_config = ConfigDict(frozen=True)

    train_hashes: tuple[ContentHash, ...]
    val_hashes: tuple[ContentHash, ...]
    test_hashes: tuple[ContentHash, ...]
    calibration_hashes: tuple[ContentHash, ...] = Field(
        default_factory=tuple,
        description="Optional calibration partition for INT8 PTQ quantization without test split leakage.",
    )


# ---------------------------------------------------------------------------
# Authoritative Dataset & Annotation Manifests
# ---------------------------------------------------------------------------


class AnnotationManifest(BaseModel):
    """Independent annotation version manifest [P§26]."""

    model_config = ConfigDict(frozen=True)

    annotation_version: AnnotationVersion
    dataset_id: DatasetId
    created: datetime = Field(default_factory=datetime.utcnow)
    guideline_ref: GuidelineRef
    annotator_id: str | None = None
    annotation_format: str = Field(
        description="e.g. 'yolo_v8', 'coco_instances', 'csv_events'"
    )
    payload_annotations: Mapping[ContentHash, tuple[Mapping[str, Any], ...]] = Field(
        description="Mapping of content hash to list of annotation primitives. Each primitive must define valid geometric coordinates (e.g. non-negative bounding boxes, polygons, keypoints) and valid class IDs.",
    )


class DatasetManifest(BaseModel):
    """Complete, immutable dataset version manifest satisfying docs/DATA.md."""

    model_config = ConfigDict(frozen=True)

    # 1-5: Identity & Provenance
    dataset_id: DatasetId
    version: DatasetVersion
    created: datetime = Field(default_factory=datetime.utcnow)
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
        description="Root checksum sealing payload collection."
    )
    leakage_audit_records: tuple[LeakageCheckResult, ...] = Field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Storage & Registry Interfaces (Protocols)
# ---------------------------------------------------------------------------


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
        """Retrieve payload matching content hash to local destination path."""
        ...

    def exists(self, content_hash: ContentHash) -> bool:
        """Check whether a content hash blob exists in the store."""
        ...

    def get_uri(self, content_hash: ContentHash) -> str:
        """Return the canonical storage URI (e.g. 's3://bucket/blobs/ab/cd/abcd1234')."""
        ...


class LeakageAuditor(Protocol):
    """Audits dataset splits against the five non-negotiable leakage checks [P§26]."""

    def audit_splits(
        self,
        manifest: DatasetManifest,
        store: DatasetStore,
    ) -> Sequence[LeakageCheckResult]:
        """Execute temporal, camera, duplicate, identity, and annotation leakage tests."""
        ...


class DatasetRegistry(Protocol):
    """Authoritative registry for storing, resolving, and retrieving dataset manifests."""

    def register(self, manifest: DatasetManifest) -> Path:
        """Validate, persist manifest to .cv_agent/datasets/, and return manifest path."""
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
```

## 5. Consequences & Boundaries

### 5.1. Positive Consequences
- **Zero Framework Lock-in:** Eliminates heavy runtime dependencies on DVC, Git LFS, or proprietary data platforms for core agent operations.
- **Reproducible Science (`[P§25]`, `[P§29.5]`):** Every experiment records the exact `DatasetVersion` and `AnnotationVersion`; historical retrieval of exact byte payloads is guaranteed.
- **Git Hygiene:** Repositories remain lightweight because large media payloads never enter Git trees. Manifests remain clean and Git-diffable.
- **Leakage Prevention:** Automating the five leakage checks in `docs/DATA.md` ensures optimistic evaluation errors are detected prior to model training.
- **Remote Cluster Readiness (`[P§10]`, `D-014`):** Remote worker nodes pull only the required payload slices directly from object stores via content hash, enabling headless cloud execution.

### 5.2. Trade-offs & Limitations
- **External Object Store Dependency:** Running with remote compute targets requires an accessible object store endpoint (`s3`, `gcs`, `minio`). For local-only development, `LocalFSStore` serves as the fallback.
- **Storage Redundancy:** Content addressing stores unique files by hash. Large datasets cannot be partially mutated in place; any alteration requires generating a new payload blob.
- **Audit Computation Overhead:** Running full perceptual hash sweeps for near-duplicate detection on millions of frames is computationally intensive. The `LeakageAuditor` must support indexed perceptual hashing and sampling strategies for massive archives.

### 5.3. Revisit Triggers
1. **DVC Ecosystem Integration:** If external enterprise collaborators mandate `.dvc` files, a bidirectional export/import adapter may be introduced without modifying the core `DatasetManifest` authority.
2. **Video Streaming Datasets:** If multi-camera video streams (`[P§9]`, Q11) require chunked temporal indexing beyond discrete frame files, the `ContentHash` abstraction will be extended to support content-addressed video segment chunks.
3. **Automated Active Learning Mutators:** If Phase 5 active learning agents require automated dataset relabeling loops, approval thresholds for autonomous mutation will be calibrated per `docs/APPROVALS.md` (Q6).

## 6. Implementation Acceptance Criteria

1. **Manifest Round-Trip & Immutability:** A `DatasetManifest` instantiated with all 16 required fields serializes to JSON under `.cv_agent/datasets/<dataset_id>/<version>.json` and deserializes without loss of fidelity. All models reject attribute mutation (`frozen=True`).
2. **Deterministic Split Retrieval:** Partitions defined in `SplitMembership` (`train_hashes`, `val_hashes`, `test_hashes`, and optional `calibration_hashes`) retrieve exact byte-identical payloads via `DatasetStore` using `ContentHash` resolution without runtime seed generation.
3. **Zero Split Contamination:** Disjointness invariants enforce that the intersection of `train_hashes`, `val_hashes`, `test_hashes`, and `calibration_hashes` is empty.
4. **Mandatory Leakage Gate:** A dataset version cannot be registered or passed to training without recorded `leakage_audit_records` passing all five leakage dimensions (temporal, camera/site, near-duplicate, subject/identity, annotation-round) unless an explicit approved exemption is documented.
5. **Independent Annotation Decoupling:** Revisions to annotations produce a distinct `AnnotationVersion` manifest referencing existing `ContentHash` items without modifying raw media payload hashes.
6. **Storage Protocol Independence:** The `DatasetStore` abstraction supports `local_fs`, `s3`, `gcs`, and `minio` backends without leaking transport or SDK details into reasoning, orchestration, or training subsystems.
7. **Approval Gate Integration:** Invocations of destructive or mutating dataset operations (`dataset.delete`, `dataset.relabel`, `dataset.resplit`, `dataset.mutate`) generate persistent `ApprovalRequest` entities per `docs/APPROVALS.md` requiring human approval before execution.
