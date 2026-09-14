"""cv_agent.knowledge.retriever — Knowledge retrieval, hybrid scoring, and ranking engine.

Enforces:
- Gate 3: Normalized hybrid scoring (S_dense in [0,1], S_sparse in [0,1], 0.65/0.35 fusion) [ADR-0006 §3.6].
- Gate 4: Topic freshness horizons, linear degradation, and volatile expired suppression [P§18].
- Gate 5: Evidence weighting and LinkedIn signal isolation (weight <= 0.2, signal_only=True) [P§17].
- Gate 10: Embedding model compatibility enforcement (raises EmbeddingModelMismatchError).
- Deterministic tie-breaking and structured warning propagation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone

from cv_agent.knowledge.bm25 import BM25Index
from cv_agent.knowledge.interfaces import (
    EmbeddingGateway,
    KnowledgeRetriever,
    RetrievalRequest,
    RetrievalResponse,
    ScoredCitation,
    SourceClass,
    TopicDomain,
)
from cv_agent.knowledge.store import HybridKnowledgeStore, _compute_cosine_similarity

# Default topic staleness horizons in days per ADR-0006 §3.5
DEFAULT_TOPIC_HORIZONS: dict[TopicDomain, int] = {
    TopicDomain.FRAMEWORK_API: 30,
    TopicDomain.ECOSYSTEM_RELEASE: 60,
    TopicDomain.HARDWARE_SPEC: 180,
    TopicDomain.BENCHMARK_RESULT: 180,
    TopicDomain.CV_ALGORITHM: 365,
    TopicDomain.CORE_PRINCIPLES: 1825,
}

# Architectural evidence weights per ADR-0006 §3.3
EVIDENCE_WEIGHTS: dict[SourceClass, float] = {
    SourceClass.PEER_REVIEWED_RESEARCH: 1.0,
    SourceClass.OFFICIAL_DOCUMENTATION: 1.0,
    SourceClass.OFFICIAL_REPOSITORY: 1.0,
    SourceClass.REPUTABLE_BENCHMARK: 0.9,
    SourceClass.ENGINEERING_BLOG: 0.6,
    SourceClass.MODEL_ZOO_LEADERBOARD: 0.6,
    SourceClass.COMMUNITY_DISCUSSION: 0.4,
    SourceClass.PROFESSIONAL_POST: 0.2,
}

VOLATILE_DOMAINS = frozenset({TopicDomain.FRAMEWORK_API, TopicDomain.ECOSYSTEM_RELEASE})


def compute_freshness(
    published_date_str: str,
    domain: TopicDomain,
    reference_date: date | None = None,
    horizons_override: Mapping[TopicDomain, int] | None = None,
) -> tuple[float, bool, bool]:
    """Compute freshness factor F in [0.05, 1.0], is_stale, and is_expired per ADR-0006 §3.5.

    Returns:
        (freshness_factor, is_stale, is_expired)
    """
    ref_date = (
        reference_date
        if reference_date is not None
        else datetime.now(timezone.utc).date()
    )
    try:
        pub_date = date.fromisoformat(published_date_str.strip())
    except ValueError:
        # Fallback if unparseable
        pub_date = ref_date

    age_days = max(0, (ref_date - pub_date).days)
    horizons = (
        horizons_override if horizons_override is not None else DEFAULT_TOPIC_HORIZONS
    )
    horizon_days = horizons.get(domain, 180)

    # 1. Active State (age <= horizon)
    if age_days <= horizon_days:
        return 1.0, False, False

    # 2. Degraded State (horizon < age <= 2 * horizon)
    if age_days <= 2 * horizon_days:
        decay = 1.0 - 0.9 * ((age_days - horizon_days) / float(horizon_days))
        freshness = max(0.1, decay)
        return freshness, True, False

    # 3. Expired State (age > 2 * horizon)
    if domain in VOLATILE_DOMAINS:
        return 0.05, True, True
    else:
        return 0.20, True, True


class DefaultKnowledgeRetriever(KnowledgeRetriever):
    """Default implementation of KnowledgeRetriever protocol."""

    def __init__(
        self,
        store: HybridKnowledgeStore,
        embedding_gateway: EmbeddingGateway,
        alpha: float = 0.65,
        horizons_override: Mapping[TopicDomain, int] | None = None,
    ) -> None:
        self.store = store
        self.embedding_gateway = embedding_gateway
        self.alpha = alpha  # dense weight (default 0.65)
        self.horizons_override = horizons_override

    async def retrieve(
        self,
        request: RetrievalRequest,
        reference_date: date | None = None,
    ) -> RetrievalResponse:
        """Execute hybrid search, compute composite ranking, and return citations."""
        # Gate 10: Validate embedding model compatibility before query execution
        active_model_id = self.embedding_gateway.embedding_model_id
        self.store.validate_embedding_model(active_model_id)

        all_chunk_ids = self.store.list_all_chunk_ids()
        if not all_chunk_ids:
            return RetrievalResponse(
                query=request.query,
                citations=(),
                total_candidates=0,
                warning_messages=(),
            )

        # 1. Sparse Lexical Scoring (BM25)
        bm25_index = self.store.load_bm25_index()
        raw_bm25_scores = bm25_index.score(
            request.query, candidate_ids=[str(cid) for cid in all_chunk_ids]
        )
        sparse_scores = BM25Index.normalize_scores(raw_bm25_scores)

        # 2. Dense Semantic Scoring (via LLM Gateway)
        query_vectors = self.embedding_gateway.embed([request.query])
        query_vector = query_vectors[0] if query_vectors else ()

        stored_vectors = self.store.load_vectors()
        dense_scores: dict[str, float] = {}
        for cid in all_chunk_ids:
            cid_str = str(cid)
            chunk_vec = stored_vectors.get(cid_str, ())
            raw_cos = _compute_cosine_similarity(query_vector, chunk_vec)
            # Gate 3: Dense normalization S_dense = (S_cos + 1.0) / 2.0 in [0.0, 1.0]
            dense_scores[cid_str] = (raw_cos + 1.0) / 2.0

        # 3. Candidate evaluation, filtering, and scoring
        warnings: list[str] = []
        candidate_citations: list[ScoredCitation] = []

        for cid in all_chunk_ids:
            cid_str = str(cid)
            chunk = self.store.load_chunk(cid)

            # Topic domain filter
            if (
                request.topic_filter is not None
                and chunk.topic_domain != request.topic_filter
            ):
                continue

            # Hardware context filter
            if (
                request.require_hardware_context
                and not chunk.provenance.hardware_context
            ):
                continue

            # Evidence weight & Gate 5 LinkedIn signal isolation
            evidence_weight = EVIDENCE_WEIGHTS.get(chunk.provenance.source_class, 0.5)
            is_signal_only = (
                chunk.provenance.source_class == SourceClass.PROFESSIONAL_POST
            )

            if evidence_weight < request.min_evidence_weight:
                continue

            # Freshness evaluation (Gate 4)
            freshness_factor, is_stale, is_expired = compute_freshness(
                chunk.provenance.published_date,
                chunk.topic_domain,
                reference_date=reference_date,
                horizons_override=self.horizons_override,
            )

            # Gate 4: Volatile expired suppression unless explicitly permitted
            if (
                is_expired
                and chunk.topic_domain in VOLATILE_DOMAINS
                and not request.allow_expired
            ):
                continue

            if is_stale and not is_expired:
                stale_msg = (
                    f"WARNING_STALE_CONTEXT: chunk {cid_str} exceeds freshness horizon."
                )
                if stale_msg not in warnings:
                    warnings.append(stale_msg)

            # Gate 3: Normalized hybrid fusion
            s_dense = dense_scores.get(cid_str, 0.0)
            s_sparse = sparse_scores.get(cid_str, 0.0)
            s_hybrid = self.alpha * s_dense + (1.0 - self.alpha) * s_sparse

            # Composite score
            composite_score = s_hybrid * evidence_weight * freshness_factor

            if composite_score < request.min_composite_score:
                continue

            candidate_citations.append(
                ScoredCitation(
                    chunk=chunk,
                    relevance_score=s_hybrid,
                    evidence_weight=evidence_weight,
                    freshness_factor=freshness_factor,
                    composite_score=composite_score,
                    embedding_model_id=active_model_id,
                    is_stale=is_stale,
                    is_expired=is_expired,
                    signal_only=is_signal_only,
                )
            )

        # 4. Deterministic Tie-Breaking (ADR-0006 §3.6)
        # Primary: Higher composite_score
        # Secondary: Higher evidence_weight
        # Tertiary: More recent published_date
        # Quaternary: Higher S_dense
        # Quinary: Lexicographical chunk_id
        def sort_key(c: ScoredCitation) -> tuple[float, float, str, float, str]:
            s_dense = dense_scores.get(str(c.chunk.chunk_id), 0.0)
            return (
                c.composite_score,
                c.evidence_weight,
                c.chunk.provenance.published_date,
                s_dense,
                str(c.chunk.chunk_id),
            )

        candidate_citations.sort(key=sort_key, reverse=True)
        top_citations = tuple(candidate_citations[: request.top_k])

        return RetrievalResponse(
            query=request.query,
            citations=top_citations,
            total_candidates=len(candidate_citations),
            warning_messages=tuple(warnings),
        )
