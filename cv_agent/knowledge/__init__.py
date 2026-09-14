"""cv_agent.knowledge — Knowledge & RAG Subsystem (ADR-0006).

Provides hybrid indexing, evidence-weighted retrieval, topic freshness horizons,
and prompt-injection quarantine for external computer vision engineering knowledge.
"""

from __future__ import annotations

from cv_agent.knowledge.bm25 import BM25Index, tokenize
from cv_agent.knowledge.exceptions import (
    DuplicateDocumentError,
    EmbeddingGenerationFailedError,
    EmbeddingModelMismatchError,
    InsufficientEvidenceWeightError,
    IrrelevantContentError,
    KnowledgeError,
    MalformedControlSequencesError,
    MissingProvenanceError,
    StoreCorruptedError,
    UnverifiedSourceError,
    WorkspaceContainmentBreachError,
    ZeroMatchesError,
)
from cv_agent.knowledge.ingester import DefaultKnowledgeIngester, sanitize_content
from cv_agent.knowledge.interfaces import (
    ChunkId,
    DocumentId,
    EmbeddingGateway,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngester,
    KnowledgeRetriever,
    Provenance,
    RetrievalRequest,
    RetrievalResponse,
    ScoredCitation,
    SourceClass,
    TopicDomain,
    validate_identifier,
)
from cv_agent.knowledge.quarantine import (
    quarantine_chunk,
    quarantine_citation,
    quarantine_citations,
)
from cv_agent.knowledge.retriever import (
    DEFAULT_TOPIC_HORIZONS,
    EVIDENCE_WEIGHTS,
    DefaultKnowledgeRetriever,
    compute_freshness,
)
from cv_agent.knowledge.store import HybridKnowledgeStore

__all__ = [
    "DEFAULT_TOPIC_HORIZONS",
    "EVIDENCE_WEIGHTS",
    "BM25Index",
    "ChunkId",
    "DefaultKnowledgeIngester",
    "DefaultKnowledgeRetriever",
    "DocumentId",
    "DuplicateDocumentError",
    "EmbeddingGateway",
    "EmbeddingGenerationFailedError",
    "EmbeddingModelMismatchError",
    "HybridKnowledgeStore",
    "InsufficientEvidenceWeightError",
    "IrrelevantContentError",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeError",
    "KnowledgeIngester",
    "KnowledgeRetriever",
    "MalformedControlSequencesError",
    "MissingProvenanceError",
    "Provenance",
    "RetrievalRequest",
    "RetrievalResponse",
    "ScoredCitation",
    "SourceClass",
    "StoreCorruptedError",
    "TopicDomain",
    "UnverifiedSourceError",
    "WorkspaceContainmentBreachError",
    "ZeroMatchesError",
    "compute_freshness",
    "quarantine_chunk",
    "quarantine_citation",
    "quarantine_citations",
    "sanitize_content",
    "tokenize",
    "validate_identifier",
]
