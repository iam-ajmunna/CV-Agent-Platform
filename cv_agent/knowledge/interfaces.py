"""cv_agent.knowledge.interfaces — Contracts for Knowledge & RAG Subsystem.

Defines:
- Canonical identifier types (DocumentId, ChunkId) conforming to ADR-0001 §3.1.
- SourceClass hierarchy and TopicDomain categorizations per [P§17], [P§18], ADR-0006 §4.
- Frozen dataclasses: Provenance, KnowledgeChunk, KnowledgeDocument, ScoredCitation,
  RetrievalRequest, RetrievalResponse.
- Protocols: EmbeddingGateway, KnowledgeIngester, KnowledgeRetriever.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, NewType, Protocol, runtime_checkable

DocumentId = NewType("DocumentId", str)
ChunkId = NewType("ChunkId", str)

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$")


def validate_identifier(value: str, field_name: str = "identifier") -> str:
    """Validate that value is a non-empty ASCII identifier conforming to ADR-0001 §3.1."""
    if not value or not isinstance(value, str) or not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"Invalid {field_name} {value!r}. Must match regex '^[a-z0-9_.-]+(/[a-z0-9_.-]+)*$'."
        )
    return value


class SourceClass(str, Enum):
    """Evidential hierarchy of knowledge sources per [P§17] and docs/RESEARCH_POLICY.md."""

    PEER_REVIEWED_RESEARCH = "peer_reviewed_research"
    OFFICIAL_DOCUMENTATION = "official_documentation"
    OFFICIAL_REPOSITORY = "official_repository"
    REPUTABLE_BENCHMARK = "reputable_benchmark"
    ENGINEERING_BLOG = "engineering_blog"
    MODEL_ZOO_LEADERBOARD = "model_zoo_leaderboard"
    COMMUNITY_DISCUSSION = "community_discussion"
    PROFESSIONAL_POST = "professional_post"  # Signal only; never evidence ([P§17])


class TopicDomain(str, Enum):
    """Domain categorization determining freshness horizons per [P§18], ADR-0006 §3.5."""

    FRAMEWORK_API = "framework_api"  # Horizon: ~30 days
    ECOSYSTEM_RELEASE = "ecosystem_release"  # Horizon: ~60 days
    HARDWARE_SPEC = "hardware_spec"  # Horizon: ~180 days
    BENCHMARK_RESULT = "benchmark_result"  # Horizon: ~180 days
    CV_ALGORITHM = "cv_algorithm"  # Horizon: ~365 days
    CORE_PRINCIPLES = "core_principles"  # Horizon: ~1825 days


@dataclass(frozen=True)
class Provenance:
    """Mandatory provenance metadata required for every stored knowledge item [P§18]."""

    source_uri: str  # Canonical web URL or workspace-relative file path
    source_class: SourceClass
    author_or_organization: str
    published_date: str  # ISO-8601 YYYY-MM-DD
    retrieved_date: str  # ISO-8601 YYYY-MM-DD
    origin_anchor: str = ""  # Git SHA, content SHA-256, or HTTP ETag
    hardware_context: str | None = None
    citation_title: str = ""


@dataclass(frozen=True)
class KnowledgeChunk:
    """Discrete, searchable text segment with attached provenance."""

    chunk_id: ChunkId
    document_id: DocumentId
    content: str
    provenance: Provenance
    topic_domain: TopicDomain
    chunk_index: int
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_identifier(str(self.chunk_id), "chunk_id")
        validate_identifier(str(self.document_id), "document_id")
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class KnowledgeDocument:
    """Complete ingested document before or during chunking."""

    document_id: DocumentId
    title: str
    raw_content: str
    provenance: Provenance
    topic_domain: TopicDomain
    chunks: tuple[KnowledgeChunk, ...] = ()

    def __post_init__(self) -> None:
        validate_identifier(str(self.document_id), "document_id")


@dataclass(frozen=True)
class ScoredCitation:
    """Retrieved chunk bundled with relevance, credibility, and freshness metrics."""

    chunk: KnowledgeChunk
    relevance_score: float  # Normalized hybrid similarity S_hybrid [0.0, 1.0]
    evidence_weight: float  # Multiplier from SourceClass [0.2, 1.0]
    freshness_factor: float  # Freshness decay [0.05, 1.0]
    composite_score: float  # Final ranked score
    embedding_model_id: str = ""
    is_stale: bool = False
    is_expired: bool = False
    signal_only: bool = False


@dataclass(frozen=True)
class RetrievalRequest:
    """Structured retrieval query dispatched by reasoning nodes."""

    query: str
    top_k: int = 5
    topic_filter: TopicDomain | None = None
    min_evidence_weight: float = 0.0
    min_composite_score: float = 0.25
    allow_expired: bool = False
    require_hardware_context: bool = False


@dataclass(frozen=True)
class RetrievalResponse:
    """Ranked context citations returned to the calling reasoning node."""

    query: str
    citations: tuple[ScoredCitation, ...]
    total_candidates: int
    warning_messages: tuple[str, ...] = ()


@runtime_checkable
class EmbeddingGateway(Protocol):
    """Protocol for generating dense vector embeddings via ADR-0002 LLM Gateway."""

    def embed(
        self,
        texts: Sequence[str],
        *,
        model_id: str | None = None,
    ) -> tuple[tuple[float, ...], ...]:
        """Generate normalized dense embeddings for given texts."""
        ...

    @property
    def embedding_model_id(self) -> str:
        """Identifier of the active embedding model."""
        ...


@runtime_checkable
class KnowledgeIngester(Protocol):
    """Protocol for ingesting, validating, chunking, and indexing external documents."""

    def ingest(self, document: KnowledgeDocument) -> tuple[ChunkId, ...]:
        """Validate provenance and ingest document into hybrid index.

        Raises MissingProvenanceError if provenance is incomplete, un-anchored, or un-dated.
        """
        ...


@runtime_checkable
class KnowledgeRetriever(Protocol):
    """Protocol for querying the knowledge base with composite score ranking."""

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        """Execute hybrid search, compute composite ranking, and return citations."""
        ...
