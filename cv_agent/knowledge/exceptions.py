"""cv_agent.knowledge.exceptions — Normalized failure taxonomy for Knowledge & RAG.

Maps directly to ADR-0006 §5:
- Ingestion & Validation Failures
- Index & Storage Failures
- Retrieval & Ranking Failures
"""

from __future__ import annotations


class KnowledgeError(Exception):
    """Base exception for all Knowledge & RAG subsystem failures."""


# Ingestion & Validation Failures (ADR-0006 §5.A)
class MissingProvenanceError(KnowledgeError):
    """Document rejected because mandatory provenance metadata is missing or empty."""


class UnverifiedSourceError(KnowledgeError):
    """Document rejected because source identity or origin domain cannot be confirmed."""


class IrrelevantContentError(KnowledgeError):
    """Content discarded at the ingestion relevance gate before storage."""


class DuplicateDocumentError(KnowledgeError):
    """Content already indexed with identical provenance hash."""


class MalformedControlSequencesError(KnowledgeError):
    """Document contains un-sanitizable binary control sequences."""


# Index & Storage Failures (ADR-0006 §5.B)
class EmbeddingGenerationFailedError(KnowledgeError):
    """LLM Gateway failed to produce dense vector representation."""


class StoreCorruptedError(KnowledgeError):
    """Index storage in .cv_agent/knowledge/ failed integrity check."""


class WorkspaceContainmentBreachError(KnowledgeError):
    """Storage path resolution pointed outside project workspace [D-013]."""


class EmbeddingModelMismatchError(KnowledgeError):
    """Retriever refused query because vector partition model differs from active model."""


# Retrieval & Ranking Failures (ADR-0006 §5.C)
class ZeroMatchesError(KnowledgeError):
    """Query produced zero chunks meeting minimum similarity threshold."""


class InsufficientEvidenceWeightError(KnowledgeError):
    """Matches found but rejected because they fail the query's minimum evidence threshold."""
