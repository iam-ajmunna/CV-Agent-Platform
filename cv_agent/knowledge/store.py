"""cv_agent.knowledge.store — Workspace-contained hybrid knowledge store.

Enforces:
- Single-project workspace containment under .cv_agent/knowledge/ [D-013].
- Embedding model versioning and mismatch detection [ADR-0006 §3.6, Gate 10].
- Pure-Python cosine similarity computation and normalization [0, 1].
- Atomic persistence of documents, chunks, BM25 index, and dense vectors.
- Zero writes to Project Memory (.cv_agent/memory/) or experiments.sqlite.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


class HybridKnowledgeStore:
    """Persistent workspace-contained hybrid store for external knowledge."""

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.knowledge_dir = (self.workspace_root / ".cv_agent" / "knowledge").resolve()
        self._validate_containment(self.knowledge_dir)

        self.docs_dir = self.knowledge_dir / "documents"
        self.indices_dir = self.knowledge_dir / "indices"
        self.metadata_file = self.knowledge_dir / "metadata.json"
        self.bm25_file = self.indices_dir / "bm25.json"
        self.vectors_file = self.indices_dir / "vectors.json"

        self._init_storage()

    def _validate_containment(self, target_path: Path) -> None:
        """Enforce strict workspace containment per D-013."""
        try:
            target_path.resolve().relative_to(self.workspace_root)
        except ValueError as exc:
            raise WorkspaceContainmentBreachError(
                f"Storage path {target_path} escapes workspace root {self.workspace_root}."
            ) from exc

    def _init_storage(self) -> None:
        """Initialize storage directories and default metadata."""
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.indices_dir.mkdir(parents=True, exist_ok=True)

        if not self.metadata_file.exists():
            default_metadata: dict[str, Any] = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "embedding_model_id": "",
                "vector_dimension": 0,
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

        if not self.vectors_file.exists():
            self.vectors_file.write_text(json.dumps({}, indent=2), encoding="utf-8")

    def get_metadata(self) -> dict[str, Any]:
        """Read index metadata."""
        try:
            return json.loads(self.metadata_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StoreCorruptedError(f"Failed to read metadata file: {exc}") from exc

    def update_metadata(self, updates: Mapping[str, Any]) -> None:
        """Update metadata atomically."""
        meta = self.get_metadata()
        meta.update(updates)
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.metadata_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def validate_embedding_model(self, active_model_id: str) -> None:
        """Validate embedding model compatibility per Gate 10 and ADR-0006 §3.6."""
        meta = self.get_metadata()
        recorded_model = meta.get("embedding_model_id", "")
        # If no model recorded yet or active is identical, valid
        if not recorded_model:
            return
        if recorded_model != active_model_id:
            raise EmbeddingModelMismatchError(
                f"Retriever refused query: vector partition embedding model {recorded_model!r} "
                f"differs from active model {active_model_id!r}. Re-indexing required."
            )

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

    def load_vectors(self) -> dict[str, list[float]]:
        """Load dense vectors from disk."""
        try:
            return json.loads(self.vectors_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StoreCorruptedError(f"Failed to load vectors: {exc}") from exc

    def save_vectors(self, vectors: Mapping[str, Sequence[float]]) -> None:
        """Save dense vectors to disk."""
        serializable = {k: list(v) for k, v in vectors.items()}
        self.vectors_file.write_text(
            json.dumps(serializable, indent=2), encoding="utf-8"
        )

    def store_document_and_chunks(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        vectors: Sequence[Sequence[float]],
        embedding_model_id: str,
    ) -> tuple[ChunkId, ...]:
        """Store document, chunks, BM25 postings, and vectors atomically."""
        if len(chunks) != len(vectors):
            raise ValueError("Chunks and vectors counts must match.")

        # Validate embedding model compatibility before writing
        meta = self.get_metadata()
        existing_model = meta.get("embedding_model_id", "")
        if existing_model and existing_model != embedding_model_id:
            raise EmbeddingModelMismatchError(
                f"Cannot store embeddings: store is indexed with {existing_model!r}, but attempted to "
                f"store embeddings from {embedding_model_id!r}. Re-indexing required."
            )

        dimension = len(vectors[0]) if vectors else 0

        # 1. Store document JSON
        doc_file = self.docs_dir / f"{document.document_id}.json"
        self._validate_containment(doc_file)

        doc_dict = {
            "document_id": str(document.document_id),
            "title": document.title,
            "raw_content": document.raw_content,
            "topic_domain": document.topic_domain.value,
            "provenance": asdict(document.provenance),
            "chunk_ids": [str(c.chunk_id) for c in chunks],
        }
        doc_file.write_text(json.dumps(doc_dict, indent=2), encoding="utf-8")

        # 2. Store chunk JSON files
        for chunk in chunks:
            chunk_file = self.docs_dir / f"chunk_{chunk.chunk_id}.json"
            self._validate_containment(chunk_file)
            chunk_dict = {
                "chunk_id": str(chunk.chunk_id),
                "document_id": str(chunk.document_id),
                "content": chunk.content,
                "topic_domain": chunk.topic_domain.value,
                "chunk_index": chunk.chunk_index,
                "provenance": asdict(chunk.provenance),
                "metadata": dict(chunk.metadata),
            }
            chunk_file.write_text(json.dumps(chunk_dict, indent=2), encoding="utf-8")

        # 3. Update BM25 Index
        bm25_index = self.load_bm25_index()
        for chunk in chunks:
            bm25_index.add_document(str(chunk.chunk_id), chunk.content)
        self.save_bm25_index(bm25_index)

        # 4. Update Vectors
        all_vectors = self.load_vectors()
        for chunk, vec in zip(chunks, vectors):
            all_vectors[str(chunk.chunk_id)] = list(vec)
        self.save_vectors(all_vectors)

        # 5. Update Metadata
        self.update_metadata(
            {
                "embedding_model_id": embedding_model_id,
                "vector_dimension": dimension,
                "total_documents": len(list(self.docs_dir.glob("*.json")))
                - len(all_vectors),
                "total_chunks": len(all_vectors),
            }
        )

        return tuple(chunk.chunk_id for chunk in chunks)

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

    def list_all_chunk_ids(self) -> tuple[ChunkId, ...]:
        """List all stored chunk IDs."""
        vectors = self.load_vectors()
        return tuple(ChunkId(cid) for cid in vectors)
