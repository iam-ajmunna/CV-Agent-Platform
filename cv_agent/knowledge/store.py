"""cv_agent.knowledge.store — Workspace-contained hybrid knowledge store.

Enforces:
- Single-project workspace containment under .cv_agent/knowledge/ [D-013].
- Model-scoped partition storage and mismatch invariant [ADR-0006 §3.6, Gate 10, Blocker 3].
- Atomic multi-file persistence with automatic rollback on failure [Blocker 2].
- Pure-Python cosine similarity computation and normalization [0, 1].
- Zero writes to Project Memory (.cv_agent/memory/) or experiments.sqlite.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from cv_agent.knowledge.bm25 import BM25Index
from cv_agent.knowledge.exceptions import (
    EmbeddingModelMismatchError,
    StoreCorruptedError,
    WorkspaceContainmentBreachError,
)
from cv_agent.knowledge.interfaces import (
    ChunkId,
    DocumentId,
    KnowledgeChunk,
    KnowledgeDocument,
    Provenance,
    SourceClass,
    TopicDomain,
)


def _compute_cosine_similarity(v1: Sequence[float], v2: Sequence[float]) -> float:
    """Compute cosine similarity between two float vectors in [-1.0, 1.0]."""
    if len(v1) != len(v2) or not v1:
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 <= 0.0 or norm2 <= 0.0:
        return 0.0
    sim = dot / (norm1 * norm2)
    # Clamp to [-1.0, 1.0] to protect against floating point imprecision
    return max(-1.0, min(1.0, sim))


def _sanitize_partition_id(model_id: str) -> str:
    """Derive a safe filesystem partition ID from embedding_model_id."""
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", model_id)


class HybridKnowledgeStore:
    """Persistent workspace-contained hybrid store for external knowledge."""

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.knowledge_dir = (self.workspace_root / ".cv_agent" / "knowledge").resolve()
        self._validate_containment(self.knowledge_dir)

        self.docs_dir = self.knowledge_dir / "documents"
        self.indices_dir = self.knowledge_dir / "indices"
        self.partitions_dir = self.knowledge_dir / "partitions"
        self.metadata_file = self.knowledge_dir / "metadata.json"
        self.bm25_file = self.indices_dir / "bm25.json"

        self._init_storage()

    def _validate_containment(self, target_path: Path) -> None:
        """Enforce strict workspace containment per D-013."""
        try:
            resolved = target_path.resolve()
            resolved.relative_to(self.workspace_root)
        except (ValueError, RuntimeError) as exc:
            raise WorkspaceContainmentBreachError(
                f"Storage path {target_path} escapes workspace root {self.workspace_root}."
            ) from exc

    def _init_storage(self) -> None:
        """Initialize storage directories and default metadata."""
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.indices_dir.mkdir(parents=True, exist_ok=True)
        self.partitions_dir.mkdir(parents=True, exist_ok=True)

        if not self.metadata_file.exists():
            default_metadata: dict[str, Any] = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "active_model_id": "",
                "total_documents": 0,
                "total_chunks": 0,
            }
            self.metadata_file.write_text(
                json.dumps(default_metadata, indent=2), encoding="utf-8"
            )

        if not self.bm25_file.exists():
            self.bm25_file.write_text(
                json.dumps(BM25Index().to_dict(), indent=2), encoding="utf-8"
            )

    def get_metadata(self) -> dict[str, Any]:
        """Read global index metadata."""
        try:
            return json.loads(self.metadata_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StoreCorruptedError(f"Failed to read metadata file: {exc}") from exc

    def update_metadata(self, updates: Mapping[str, Any]) -> None:
        """Update global metadata atomically."""
        meta = self.get_metadata()
        meta.update(updates)
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.metadata_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # ==========================================================================
    # Partition Management (Blocker 3 / Gate 10)
    # ==========================================================================

    def get_partition_dir(self, model_id: str) -> Path:
        """Get partition directory for a given embedding model."""
        partition_id = _sanitize_partition_id(model_id)
        p_dir = self.partitions_dir / partition_id
        self._validate_containment(p_dir)
        return p_dir

    def list_partitions(self) -> tuple[dict[str, Any], ...]:
        """List all embedding model partitions in the store."""
        partitions: list[dict[str, Any]] = []
        for p_dir in self.partitions_dir.iterdir():
            if p_dir.is_dir():
                manifest_file = p_dir / "manifest.json"
                if manifest_file.exists():
                    try:
                        partitions.append(
                            json.loads(manifest_file.read_text(encoding="utf-8"))
                        )
                    except (json.JSONDecodeError, OSError):
                        logger.warning(
                            "Corrupted partition manifest encountered in %s", p_dir
                        )
                        continue
        return tuple(partitions)

    def get_partition_manifest(self, model_id: str) -> dict[str, Any] | None:
        """Retrieve partition manifest for model_id if it exists."""
        p_dir = self.get_partition_dir(model_id)
        manifest_file = p_dir / "manifest.json"
        if not manifest_file.exists():
            return None
        try:
            return json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StoreCorruptedError(
                f"Failed to read partition manifest: {exc}"
            ) from exc

    def mark_partition_invalid(self, model_id: str) -> None:
        """Mark a model partition as invalid / reindex_required per ADR-0006 §3.6."""
        p_dir = self.get_partition_dir(model_id)
        manifest_file = p_dir / "manifest.json"
        if manifest_file.exists():
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            data["status"] = "reindex_required"
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            manifest_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def validate_embedding_model(self, active_model_id: str) -> None:
        """Validate embedding model compatibility per Gate 10 and Blocker 3."""
        partitions = self.list_partitions()
        if not partitions:
            # Store has no indexed vector partitions yet
            return

        matching_manifest = self.get_partition_manifest(active_model_id)
        if matching_manifest is None:
            indexed_models = [p.get("embedding_model_id", "") for p in partitions]
            models_str = ", ".join(repr(m) for m in indexed_models)
            raise EmbeddingModelMismatchError(
                f"Retriever refused query: no valid vector partition exists for active model {active_model_id!r} "
                f"(does not match vector partition embedding model {models_str}). Re-indexing required."
            )

        if matching_manifest.get("status") != "valid":
            raise EmbeddingModelMismatchError(
                f"Retriever refused query: vector partition for active model {active_model_id!r} is marked "
                f"{matching_manifest.get('status')!r}. Re-indexing required."
            )

    def load_partition_vectors(self, model_id: str) -> dict[str, list[float]]:
        """Load dense vectors specifically from the partition for model_id."""
        p_dir = self.get_partition_dir(model_id)
        vectors_file = p_dir / "vectors.json"
        if not vectors_file.exists():
            return {}
        try:
            return json.loads(vectors_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StoreCorruptedError(
                f"Failed to load partition vectors: {exc}"
            ) from exc

    def save_partition_vectors(
        self,
        model_id: str,
        vectors: Mapping[str, Sequence[float]],
        dimension: int,
    ) -> None:
        """Save vectors and update manifest for a partition."""
        p_dir = self.get_partition_dir(model_id)
        p_dir.mkdir(parents=True, exist_ok=True)
        manifest_file = p_dir / "manifest.json"
        vectors_file = p_dir / "vectors.json"

        # Update vectors
        serializable = {k: list(v) for k, v in vectors.items()}
        vectors_file.write_text(json.dumps(serializable, indent=2), encoding="utf-8")

        # Update partition manifest
        manifest_data = {
            "partition_id": _sanitize_partition_id(model_id),
            "embedding_model_id": model_id,
            "vector_dimension": dimension,
            "total_vectors": len(serializable),
            "status": "valid",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if not manifest_file.exists():
            manifest_data["created_at"] = datetime.now(timezone.utc).isoformat()
        else:
            try:
                old_m = json.loads(manifest_file.read_text(encoding="utf-8"))
                manifest_data["created_at"] = old_m.get(
                    "created_at", manifest_data["updated_at"]
                )
            except (json.JSONDecodeError, OSError):
                manifest_data["created_at"] = manifest_data["updated_at"]

        manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # ==========================================================================
    # Lexical BM25 & Documents
    # ==========================================================================

    def load_bm25_index(self) -> BM25Index:
        """Load BM25 lexical index from disk."""
        try:
            data = json.loads(self.bm25_file.read_text(encoding="utf-8"))
            return BM25Index.from_dict(data)
        except Exception as exc:
            raise StoreCorruptedError(f"Failed to load BM25 index: {exc}") from exc

    def save_bm25_index(self, index: BM25Index) -> None:
        """Save BM25 lexical index to disk."""
        self.bm25_file.write_text(
            json.dumps(index.to_dict(), indent=2), encoding="utf-8"
        )

    # ==========================================================================
    # Atomic Multi-File Persistence with Rollback (Blocker 2)
    # ==========================================================================

    def store_document_and_chunks(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        vectors: Sequence[Sequence[float]],
        embedding_model_id: str,
    ) -> tuple[ChunkId, ...]:
        """Store document, chunks, BM25 postings, and partition vectors atomically.

        Enforces full rollback if any failure occurs midway, guaranteeing no partially
        committed index state [Blocker 2].
        """
        if len(chunks) != len(vectors):
            raise ValueError("Chunks and vectors counts must match.")

        dimension = len(vectors[0]) if vectors else 0

        # Verify containment before doing any writes
        doc_file = self.docs_dir / f"{document.document_id}.json"
        self._validate_containment(doc_file)
        chunk_files: list[Path] = []
        for chunk in chunks:
            c_file = self.docs_dir / f"chunk_{chunk.chunk_id}.json"
            self._validate_containment(c_file)
            chunk_files.append(c_file)

        # 1. Capture backups of state that may be modified
        bm25_backup = (
            self.bm25_file.read_text(encoding="utf-8")
            if self.bm25_file.exists()
            else None
        )
        meta_backup = (
            self.metadata_file.read_text(encoding="utf-8")
            if self.metadata_file.exists()
            else None
        )

        p_dir = self.get_partition_dir(embedding_model_id)
        p_manifest_file = p_dir / "manifest.json"
        p_vectors_file = p_dir / "vectors.json"
        p_manifest_backup = (
            p_manifest_file.read_text(encoding="utf-8")
            if p_manifest_file.exists()
            else None
        )
        p_vectors_backup = (
            p_vectors_file.read_text(encoding="utf-8")
            if p_vectors_file.exists()
            else None
        )

        created_files: list[Path] = []

        try:
            # Step A: Write document JSON
            doc_dict = {
                "document_id": str(document.document_id),
                "title": document.title,
                "raw_content": document.raw_content,
                "topic_domain": document.topic_domain.value,
                "provenance": asdict(document.provenance),
                "chunk_ids": [str(c.chunk_id) for c in chunks],
            }
            if not doc_file.exists():
                created_files.append(doc_file)
            doc_file.write_text(json.dumps(doc_dict, indent=2), encoding="utf-8")

            # Step B: Write chunk JSON files
            for chunk, c_file in zip(chunks, chunk_files):
                chunk_dict = {
                    "chunk_id": str(chunk.chunk_id),
                    "document_id": str(chunk.document_id),
                    "content": chunk.content,
                    "topic_domain": chunk.topic_domain.value,
                    "chunk_index": chunk.chunk_index,
                    "provenance": asdict(chunk.provenance),
                    "metadata": dict(chunk.metadata),
                }
                if not c_file.exists():
                    created_files.append(c_file)
                c_file.write_text(json.dumps(chunk_dict, indent=2), encoding="utf-8")

            # Step C: Update BM25 index
            bm25_index = self.load_bm25_index()
            for chunk in chunks:
                bm25_index.add_document(str(chunk.chunk_id), chunk.content)
            self.save_bm25_index(bm25_index)

            # Step D: Update Partition Vectors
            partition_vectors = self.load_partition_vectors(embedding_model_id)
            for chunk, vec in zip(chunks, vectors):
                partition_vectors[str(chunk.chunk_id)] = list(vec)
            self.save_partition_vectors(
                embedding_model_id, partition_vectors, dimension
            )

            # Step E: Update Global Metadata
            self.update_metadata(
                {
                    "active_model_id": embedding_model_id,
                    "total_documents": len(list(self.docs_dir.glob("*.json")))
                    - len(chunk_files),
                    "total_chunks": len(partition_vectors),
                }
            )

            return tuple(chunk.chunk_id for chunk in chunks)

        except Exception:
            # Rollback: delete newly created files
            for f in created_files:
                f.unlink(missing_ok=True)
            # Restore backups of modified index files
            if bm25_backup is not None:
                self.bm25_file.write_text(bm25_backup, encoding="utf-8")
            if meta_backup is not None:
                self.metadata_file.write_text(meta_backup, encoding="utf-8")
            if p_manifest_backup is not None:
                p_manifest_file.write_text(p_manifest_backup, encoding="utf-8")
            else:
                p_manifest_file.unlink(missing_ok=True)
            if p_vectors_backup is not None:
                p_vectors_file.write_text(p_vectors_backup, encoding="utf-8")
            else:
                p_vectors_file.unlink(missing_ok=True)

            raise

    def load_chunk(self, chunk_id: ChunkId) -> KnowledgeChunk:
        """Load a single chunk by chunk_id."""
        chunk_file = self.docs_dir / f"chunk_{chunk_id}.json"
        if not chunk_file.exists():
            raise KeyError(f"Chunk {chunk_id} not found in store.")
        data = json.loads(chunk_file.read_text(encoding="utf-8"))
        prov_data = data["provenance"]
        provenance = Provenance(
            source_uri=prov_data["source_uri"],
            source_class=SourceClass(prov_data["source_class"]),
            author_or_organization=prov_data["author_or_organization"],
            published_date=prov_data["published_date"],
            retrieved_date=prov_data["retrieved_date"],
            origin_anchor=prov_data.get("origin_anchor", ""),
            hardware_context=prov_data.get("hardware_context"),
            citation_title=prov_data.get("citation_title", ""),
        )
        return KnowledgeChunk(
            chunk_id=ChunkId(data["chunk_id"]),
            document_id=DocumentId(data["document_id"]),
            content=data["content"],
            provenance=provenance,
            topic_domain=TopicDomain(data["topic_domain"]),
            chunk_index=int(data["chunk_index"]),
            metadata=data.get("metadata", {}),
        )

    def list_all_chunk_ids(self, model_id: str | None = None) -> tuple[ChunkId, ...]:
        """List all stored chunk IDs for the active or given model partition."""
        target_model = model_id
        if not target_model:
            meta = self.get_metadata()
            target_model = meta.get("active_model_id", "")
        if not target_model:
            partitions = self.list_partitions()
            if partitions:
                target_model = partitions[0].get("embedding_model_id", "")
        if not target_model:
            return ()
        vectors = self.load_partition_vectors(target_model)
        return tuple(ChunkId(cid) for cid in vectors)
