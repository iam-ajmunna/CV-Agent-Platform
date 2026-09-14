"""cv_agent.knowledge.bm25 — Deterministic pure-Python BM25 lexical index.

Implements Okapi BM25 scoring and normalization per ADR-0006 §3.6.
Preserves technical model tokens, function identifiers, and CUDA codes.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

# Tokenizer pattern preserving words with hyphens, underscores, dots (e.g. yolov8n-seg, ptq.yaml)
_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9]+(?:[-_.][a-zA-Z0-9]+)*")


def tokenize(text: str) -> list[str]:
    """Tokenize technical text into lowercase terms preserving special identifiers."""
    return [match.group(0).lower() for match in _TOKEN_PATTERN.finditer(text)]


class BM25Index:
    """Okapi BM25 inverted index with deterministic pure-Python scoring."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.chunk_lengths: dict[str, int] = {}  # chunk_id -> doc length
        self.inverted_index: dict[str, dict[str, int]] = {}  # term -> {chunk_id: tf}
        self.total_docs: int = 0
        self.avg_doc_length: float = 0.0

    def add_document(self, chunk_id: str, text: str) -> None:
        """Add a chunk to the index, purging any previous postings if chunk_id exists."""
        if chunk_id in self.chunk_lengths:
            self.remove_document(chunk_id)

        tokens = tokenize(text)
        length = len(tokens)
        self.chunk_lengths[chunk_id] = length

        counts = Counter(tokens)
        for term, count in counts.items():
            if term not in self.inverted_index:
                self.inverted_index[term] = {}
            self.inverted_index[term][chunk_id] = count

        self.total_docs = len(self.chunk_lengths)
        self.avg_doc_length = (
            sum(self.chunk_lengths.values()) / self.total_docs
            if self.total_docs > 0
            else 0.0
        )

    def remove_document(self, chunk_id: str) -> None:
        """Remove a chunk from the index."""
        if chunk_id not in self.chunk_lengths:
            return
        del self.chunk_lengths[chunk_id]
        for term, postings in list(self.inverted_index.items()):
            if chunk_id in postings:
                del postings[chunk_id]
            if not postings:
                del self.inverted_index[term]
        self.total_docs = len(self.chunk_lengths)
        self.avg_doc_length = (
            sum(self.chunk_lengths.values()) / self.total_docs
            if self.total_docs > 0
            else 0.0
        )

    def score(
        self, query: str, candidate_ids: Sequence[str] | None = None
    ) -> dict[str, float]:
        """Compute raw Okapi BM25 scores for query across indexed documents."""
        query_terms = tokenize(query)
        if not query_terms or self.total_docs == 0:
            candidates = (
                candidate_ids
                if candidate_ids is not None
                else self.chunk_lengths.keys()
            )
            return {cid: 0.0 for cid in candidates}

        candidates_set = (
            set(candidate_ids)
            if candidate_ids is not None
            else set(self.chunk_lengths.keys())
        )
        scores: dict[str, float] = {cid: 0.0 for cid in candidates_set}

        for term in query_terms:
            if term not in self.inverted_index:
                continue
            postings = self.inverted_index[term]
            df = len(postings)
            # Standard Lucene/Okapi smoothed IDF
            idf = math.log(1.0 + (self.total_docs - df + 0.5) / (df + 0.5))
            if idf <= 0.0:
                continue

            for chunk_id, tf in postings.items():
                if chunk_id not in candidates_set:
                    continue
                doc_len = self.chunk_lengths[chunk_id]
                numerator = tf * (self.k1 + 1.0)
                denominator = tf + self.k1 * (
                    1.0 - self.b + self.b * (doc_len / (self.avg_doc_length or 1.0))
                )
                scores[chunk_id] += idf * (numerator / denominator)

        return scores

    @staticmethod
    def normalize_scores(raw_scores: Mapping[str, float]) -> dict[str, float]:
        """Normalize raw BM25 scores to [0.0, 1.0] using min-max scaling per ADR-0006 §3.6."""
        if not raw_scores:
            return {}

        values = list(raw_scores.values())
        r_min = min(values)
        r_max = max(values)

        if r_max > r_min:
            denom = r_max - r_min
            return {cid: (score - r_min) / denom for cid, score in raw_scores.items()}
        elif r_max == r_min and r_max > 0.0:
            return {cid: 1.0 for cid in raw_scores}
        else:
            return {cid: 0.0 for cid in raw_scores}

    def to_dict(self) -> dict[str, Any]:
        """Serialize index state to dictionary."""
        return {
            "k1": self.k1,
            "b": self.b,
            "total_docs": self.total_docs,
            "avg_doc_length": self.avg_doc_length,
            "chunk_lengths": self.chunk_lengths,
            "inverted_index": self.inverted_index,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BM25Index:
        """Deserialize index from dictionary."""
        index = cls(k1=float(data.get("k1", 1.5)), b=float(data.get("b", 0.75)))
        index.total_docs = int(data.get("total_docs", 0))
        index.avg_doc_length = float(data.get("avg_doc_length", 0.0))
        index.chunk_lengths = {
            str(k): int(v) for k, v in data.get("chunk_lengths", {}).items()
        }
        index.inverted_index = {
            str(term): {str(cid): int(tf) for cid, tf in postings.items()}
            for term, postings in data.get("inverted_index", {}).items()
        }
        return index
