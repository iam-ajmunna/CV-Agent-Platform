"""cv_agent.knowledge.ingester — Document ingestion, provenance validation, and chunking.

Enforces:
- Gate 1: Mandatory Provenance verification (raises MissingProvenanceError) [P§18].
- Gate 2: Local file anchoring with Git SHA or content SHA-256; mtime rejected [ADR-0006 §3.4].
- Secondary sanitization: strips null bytes and terminal control escapes without altering evidential text.
- Gateway separation: delegates dense embedding to EmbeddingGateway protocol [P§20].
"""

from __future__ import annotations

import re

from cv_agent.knowledge.exceptions import (
    EmbeddingGenerationFailedError,
    MissingProvenanceError,
)
from cv_agent.knowledge.interfaces import (
    ChunkId,
    EmbeddingGateway,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngester,
    Provenance,
    validate_identifier,
)
from cv_agent.knowledge.store import HybridKnowledgeStore

_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]+$")
_CONTROL_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_local_uri(uri: str) -> bool:
    """Check if a URI points to a local file or relative path per ADR-0006 §3.4."""
    return uri.startswith("file://") or ("://" not in uri)


def _validate_provenance(prov: Provenance) -> None:
    """Enforce Gate 1 and Gate 2 provenance invariants."""
    if not prov or not isinstance(prov, Provenance):
        raise MissingProvenanceError("Provenance object is missing or invalid.")

    if not prov.source_uri or not prov.source_uri.strip():
        raise MissingProvenanceError("Provenance source_uri is missing or empty.")

    # Important Finding #8: Local documents must use file:// scheme or relative path; raw absolute POSIX paths are rejected
    clean_uri = prov.source_uri.strip()
    if clean_uri.startswith("/") and not clean_uri.startswith("file://"):
        raise MissingProvenanceError(
            f"Local workspace document source_uri {prov.source_uri!r} uses a raw absolute POSIX path. "
            "Local workspace documents must use 'file://...' scheme or a relative path."
        )

    if not prov.author_or_organization or not prov.author_or_organization.strip():
        raise MissingProvenanceError(
            "Provenance author_or_organization is missing or empty."
        )

    if not prov.published_date or not prov.published_date.strip():
        raise MissingProvenanceError("Provenance published_date is missing or empty.")

    if not _DATE_PATTERN.match(prov.published_date.strip()):
        raise MissingProvenanceError(
            f"Provenance published_date {prov.published_date!r} must be ISO-8601 YYYY-MM-DD."
        )

    if not prov.origin_anchor or not prov.origin_anchor.strip():
        raise MissingProvenanceError("Provenance origin_anchor is missing or empty.")

    anchor = prov.origin_anchor.strip()

    # Gate 2: Local file anchoring check
    if _is_local_uri(prov.source_uri):
        # Must NOT be an mtime timestamp or explicit mtime marker
        lower_anchor = anchor.lower()
        if "mtime" in lower_anchor or (
            anchor.isdigit() and len(anchor) in (10, 13, 16, 19)
        ):
            raise MissingProvenanceError(
                f"Local workspace document {prov.source_uri!r} specifies mtime anchor {anchor!r}. "
                "Local documents require a Git commit SHA or file content SHA-256 hash. "
                "File mtime is treated strictly as supplementary metadata."
            )

        # Must be valid hex (either 7-40 char Git SHA or 64-char SHA-256)
        is_git_sha = len(anchor) in range(7, 41) and bool(_HEX_PATTERN.match(anchor))
        is_sha256 = len(anchor) == 64 and bool(_HEX_PATTERN.match(anchor))
        if not (is_git_sha or is_sha256):
            raise MissingProvenanceError(
                f"Local workspace document {prov.source_uri!r} has invalid origin_anchor {anchor!r}. "
                "Must be a valid Git commit SHA (7-40 hex chars) or SHA-256 hash (64 hex chars)."
            )


def sanitize_content(text: str) -> str:
    """Secondary sanitization: strips null bytes and terminal control escapes.

    Does NOT heuristically censor technical phrases per ADR-0006 §3.9.
    """
    if "\x00" in text:
        text = text.replace("\x00", "")
    text = _CONTROL_ESCAPE_PATTERN.sub("", text)
    return text


class DefaultKnowledgeIngester(KnowledgeIngester):
    """Default implementation of KnowledgeIngester protocol."""

    def __init__(
        self,
        store: HybridKnowledgeStore,
        embedding_gateway: EmbeddingGateway,
    ) -> None:
        self.store = store
        self.embedding_gateway = embedding_gateway

    def ingest(self, document: KnowledgeDocument) -> tuple[ChunkId, ...]:
        """Validate provenance, chunk document, generate embeddings, and persist."""
        validate_identifier(str(document.document_id), "document_id")
        _validate_provenance(document.provenance)

        clean_raw_content = sanitize_content(document.raw_content)

        # Use existing chunks if provided, else segment raw_content
        chunks_to_store: list[KnowledgeChunk] = []
        if document.chunks:
            for idx, chunk in enumerate(document.chunks):
                validate_identifier(str(chunk.chunk_id), "chunk_id")
                _validate_provenance(chunk.provenance)
                clean_chunk_content = sanitize_content(chunk.content)
                chunks_to_store.append(
                    KnowledgeChunk(
                        chunk_id=chunk.chunk_id,
                        document_id=document.document_id,
                        content=clean_chunk_content,
                        provenance=chunk.provenance,
                        topic_domain=chunk.topic_domain,
                        chunk_index=idx,
                        metadata=chunk.metadata,
                    )
                )
        else:
            # Simple paragraph-based chunking
            paragraphs = [
                p.strip() for p in clean_raw_content.split("\n\n") if p.strip()
            ]
            if not paragraphs:
                paragraphs = [clean_raw_content.strip()]

            for idx, para in enumerate(paragraphs):
                chunk_id = ChunkId(f"{document.document_id}_chunk_{idx}")
                validate_identifier(str(chunk_id), "chunk_id")
                chunks_to_store.append(
                    KnowledgeChunk(
                        chunk_id=chunk_id,
                        document_id=document.document_id,
                        content=para,
                        provenance=document.provenance,
                        topic_domain=document.topic_domain,
                        chunk_index=idx,
                    )
                )

        if not chunks_to_store:
            return ()

        # Generate dense embeddings via LLM Gateway protocol (Gate 8)
        try:
            texts = [c.content for c in chunks_to_store]
            vectors = self.embedding_gateway.embed(texts)
        except Exception as exc:
            raise EmbeddingGenerationFailedError(
                f"Failed to generate embeddings via LLM Gateway: {exc}"
            ) from exc

        model_id = self.embedding_gateway.embedding_model_id
        return self.store.store_document_and_chunks(
            document=document,
            chunks=chunks_to_store,
            vectors=vectors,
            embedding_model_id=model_id,
        )
