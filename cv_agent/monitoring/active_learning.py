"""cv_agent.monitoring.active_learning — Algorithmic candidate curation and bounded buffer for ADR-0013."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from cv_agent.dataset import ContentHash, DatasetId
from cv_agent.monitoring.contracts import (
    ActiveLearningCandidateBatch,
    CandidateSample,
)
from cv_agent.monitoring.errors import (
    ActiveLearningBufferError,
    ExpiredCandidatePayloadError,
)
from cv_agent.monitoring.types import (
    ActiveLearningStrategy,
    CandidateBatchId,
    CandidateSampleId,
)
from cv_agent.optimization.types import DeploymentId


@runtime_checkable
class ActiveLearningSelector(Protocol):
    """Contract for curating high-information frames from continuous streams."""

    def evaluate_candidate(
        self,
        frame_payload: bytes,
        predictions: Sequence[Mapping[str, Any]],
        optical_metrics: Mapping[str, float],
        stream_id: str,
        frame_index: int,
        timestamp: datetime,
        temporary_payload_uri: str,
        expires_at: datetime | None = None,
        embedding_vector: tuple[float, ...] | None = None,
    ) -> CandidateSample | None:
        """Evaluate whether a stream frame qualifies as an active learning candidate."""
        ...

    def assemble_batch(
        self,
        min_candidates: int,
        target_dataset_id: DatasetId,
        deployment_id: DeploymentId,
    ) -> ActiveLearningCandidateBatch | None:
        """Assemble buffered candidates into a bounded candidate batch for ADR-0009."""
        ...

    def evict_expired_candidates(self, current_time: datetime | None = None) -> int:
        """Evict candidates from the buffer whose storage TTL has expired."""
        ...


class DefaultActiveLearningSelector:
    """Production active learning candidate selector with bounded buffer and deterministic eviction.

    Enforces Invariant 5 (Cryptographic Frame Lineage), Invariant 9 (Bounded Storage Buffer
    and deterministic least-informative eviction), and Temporal Eviction Invariants.
    """

    def __init__(
        self,
        max_buffer_capacity: int = 500,
        uncertainty_threshold: float = 0.40,
        margin_threshold: float = 0.20,
        blur_shift_threshold: float = 40.0,
        exposure_shift_threshold: float = 0.25,
    ) -> None:
        if max_buffer_capacity <= 0:
            raise ValueError(
                f"max_buffer_capacity must be positive, got {max_buffer_capacity}"
            )
        self.max_buffer_capacity = max_buffer_capacity
        self.uncertainty_threshold = uncertainty_threshold
        self.margin_threshold = margin_threshold
        self.blur_shift_threshold = blur_shift_threshold
        self.exposure_shift_threshold = exposure_shift_threshold

        # Internal buffer mapping sample_id -> CandidateSample
        self._buffer: dict[CandidateSampleId, CandidateSample] = {}

    @property
    def buffered_count(self) -> int:
        """Current number of candidates residing in memory buffer."""
        return len(self._buffer)

    def evaluate_candidate(
        self,
        frame_payload: bytes,
        predictions: Sequence[Mapping[str, Any]],
        optical_metrics: Mapping[str, float],
        stream_id: str,
        frame_index: int,
        timestamp: datetime,
        temporary_payload_uri: str,
        expires_at: datetime | None = None,
        embedding_vector: tuple[float, ...] | None = None,
    ) -> CandidateSample | None:
        """Evaluate incoming frame against active learning heuristics."""
        if not frame_payload:
            raise ActiveLearningBufferError("frame_payload cannot be empty")
        if not stream_id or not stream_id.strip():
            raise ActiveLearningBufferError("stream_id cannot be empty")
        if not temporary_payload_uri or not temporary_payload_uri.strip():
            raise ActiveLearningBufferError("temporary_payload_uri cannot be empty")

        now = datetime.now(timezone.utc)
        # Check temporal expiry immediately: reject already-expired payload
        if expires_at is not None and expires_at <= now:
            return None

        # Invariant 5: Cryptographic frame lineage via SHA-256
        raw_hash = hashlib.sha256(frame_payload).hexdigest()
        frame_hash = ContentHash(raw_hash)

        # Assess strategies and compute priority score
        strategy, priority_score = self._score_frame(
            predictions=predictions,
            optical_metrics=optical_metrics,
            embedding_vector=embedding_vector,
        )

        if strategy is None or priority_score <= 0.0:
            return None

        sample_id = CandidateSampleId(f"samp-{stream_id}-{frame_index}-{raw_hash[:12]}")

        candidate = CandidateSample(
            sample_id=sample_id,
            stream_id=stream_id,
            frame_index=frame_index,
            timestamp=timestamp,
            frame_content_hash=frame_hash,
            temporary_payload_uri=temporary_payload_uri,
            expires_at=expires_at,
            strategy_applied=strategy,
            priority_score=round(priority_score, 4),
            model_predictions=tuple(predictions),
            embedding_vector=embedding_vector,
            optical_metadata=optical_metrics,
        )

        self._admit_to_buffer(candidate)
        return candidate

    def _score_frame(
        self,
        predictions: Sequence[Mapping[str, Any]],
        optical_metrics: Mapping[str, float],
        embedding_vector: tuple[float, ...] | None,
    ) -> tuple[ActiveLearningStrategy | None, float]:
        """Score candidate and identify primary selection strategy."""
        best_strategy: ActiveLearningStrategy | None = None
        best_score = 0.0

        # Heuristic 1: Uncertainty / Least Confident & Margin Sampling
        confidences = [
            float(p.get("confidence", 1.0))
            for p in predictions
            if "confidence" in p and isinstance(p.get("confidence"), (int, float))
        ]

        if confidences:
            sorted_confs = sorted(confidences, reverse=True)
            max_conf = sorted_confs[0]

            # Least confident: higher when max confidence is low
            least_conf_score = max(0.0, 1.0 - max_conf)
            if (
                least_conf_score >= self.uncertainty_threshold
                and least_conf_score > best_score
            ):
                best_score = least_conf_score
                best_strategy = ActiveLearningStrategy.UNCERTAINTY_LEAST_CONFIDENT

            # Margin sampling: higher when gap between top-1 and top-2 is narrow
            if len(sorted_confs) >= 2:
                margin = sorted_confs[0] - sorted_confs[1]
                margin_score = max(0.0, 1.0 - margin)
                if margin <= self.margin_threshold and margin_score > best_score:
                    best_score = margin_score
                    best_strategy = ActiveLearningStrategy.UNCERTAINTY_MARGIN

            # Uncertainty entropy
            if len(sorted_confs) >= 2:
                sum_c = sum(sorted_confs)
                probs = [c / sum_c for c in sorted_confs]
                entropy = -sum(p * math.log(p + 1e-12) for p in probs)
                max_entropy = math.log(len(sorted_confs))
                norm_entropy = (entropy / max_entropy) if max_entropy > 0 else 0.0
                if norm_entropy > 0.65 and norm_entropy > best_score:
                    best_score = norm_entropy
                    best_strategy = ActiveLearningStrategy.UNCERTAINTY_ENTROPY

        # Heuristic 2: Environmental shift (optical blur, over/underexposure, occlusion)
        blur_val = optical_metrics.get("blur_laplacian_variance")
        if blur_val is not None and blur_val < self.blur_shift_threshold:
            score = max(0.0, 1.0 - (blur_val / self.blur_shift_threshold))
            if score > best_score:
                best_score = score
                best_strategy = ActiveLearningStrategy.ENVIRONMENTAL_SHIFT

        overexp = optical_metrics.get("overexposure_ratio", 0.0)
        underexp = optical_metrics.get("underexposure_ratio", 0.0)
        exp_score = max(overexp, underexp)
        if exp_score >= self.exposure_shift_threshold and exp_score > best_score:
            best_score = exp_score
            best_strategy = ActiveLearningStrategy.ENVIRONMENTAL_SHIFT

        # Heuristic 3: Empirical operator feedback flag
        if (
            optical_metrics.get("operator_false_positive", 0.0) > 0.0
            or optical_metrics.get("operator_false_negative", 0.0) > 0.0
        ):
            best_score = 1.0
            best_strategy = ActiveLearningStrategy.EMPIRICAL_FAILURE_FEEDBACK

        # Heuristic 4: Out of distribution / anomaly
        ood_score = optical_metrics.get("ood_reconstruction_error", 0.0)
        if ood_score >= 0.70 and ood_score > best_score:
            best_score = ood_score
            best_strategy = ActiveLearningStrategy.OUT_OF_DISTRIBUTION_ANOMALY

        # Heuristic 5: Feature-space diversity distance
        if embedding_vector is not None and best_strategy is None and self._buffer:
            dist = self._min_embedding_distance(embedding_vector)
            if dist > 0.5:
                best_score = min(1.0, dist)
                best_strategy = ActiveLearningStrategy.FEATURE_DIVERSITY_CLUSTER

        return best_strategy, best_score

    def _min_embedding_distance(self, embedding: tuple[float, ...]) -> float:
        """Compute minimum Euclidean distance to existing buffer embeddings."""
        min_d = float("inf")
        for sample in self._buffer.values():
            if sample.embedding_vector and len(sample.embedding_vector) == len(
                embedding
            ):
                sq_sum = sum(
                    (a - b) ** 2 for a, b in zip(embedding, sample.embedding_vector)
                )
                d = math.sqrt(sq_sum)
                min_d = min(min_d, d)
        return min_d if min_d != float("inf") else 1.0

    def _admit_to_buffer(self, candidate: CandidateSample) -> None:
        """Admit candidate to buffer enforcing bounded storage capacity and deterministic eviction."""
        # Evict expired candidates first
        self.evict_expired_candidates(candidate.timestamp)

        if len(self._buffer) < self.max_buffer_capacity:
            self._buffer[candidate.sample_id] = candidate
            return

        # Buffer is at capacity: deterministic eviction of lowest-priority sample
        # Sort buffer by priority ascending, then by frame_index ascending (oldest first)
        lowest_id, lowest_sample = min(
            self._buffer.items(),
            key=lambda item: (
                item[1].priority_score,
                item[1].timestamp,
                item[1].frame_index,
            ),
        )

        if candidate.priority_score > lowest_sample.priority_score:
            del self._buffer[lowest_id]
            self._buffer[candidate.sample_id] = candidate
        # Else: candidate priority is lower than everything in the buffer, dropped deterministically

    def evict_expired_candidates(self, current_time: datetime | None = None) -> int:
        """Evict candidates past their storage expires_at timestamp."""
        now = current_time or datetime.now(timezone.utc)
        expired_ids = [
            sid
            for sid, sample in self._buffer.items()
            if sample.expires_at is not None and sample.expires_at <= now
        ]
        for sid in expired_ids:
            del self._buffer[sid]
        return len(expired_ids)

    def assemble_batch(
        self,
        min_candidates: int,
        target_dataset_id: DatasetId,
        deployment_id: DeploymentId,
    ) -> ActiveLearningCandidateBatch | None:
        """Assemble buffered candidates into a bounded candidate batch for ADR-0009."""
        if min_candidates <= 0:
            raise ValueError(
                f"min_candidates must be strictly positive, got {min_candidates}"
            )

        # Evict expired candidates before assembling batch
        self.evict_expired_candidates()

        if len(self._buffer) < min_candidates:
            return None

        # Check for any unevicted expired items (fail closed)
        now = datetime.now(timezone.utc)
        for s in self._buffer.values():
            if s.expires_at is not None and s.expires_at <= now:
                raise ExpiredCandidatePayloadError(
                    f"Candidate {s.sample_id} expired at {s.expires_at} (current time {now}); cannot assemble into batch"
                )

        # Sort candidates by priority descending
        sorted_samples = sorted(
            self._buffer.values(),
            key=lambda s: s.priority_score,
            reverse=True,
        )

        sample_manifest = tuple(sorted_samples)
        strategies = tuple(sorted({s.strategy_applied for s in sample_manifest}))

        batch_id = CandidateBatchId(
            f"batch-{deployment_id.replace('/', '-')}-{int(now.timestamp())}"
        )

        rationale = (
            f"Curated {len(sample_manifest)} high-information active learning candidates "
            f"across strategies: {', '.join(s.value for s in strategies)}."
        )

        batch = ActiveLearningCandidateBatch(
            batch_id=batch_id,
            deployment_id=deployment_id,
            created_at=now,
            strategies_used=strategies,
            total_candidates=len(sample_manifest),
            sample_manifest=sample_manifest,
            selection_rationale=rationale,
            target_dataset_id=target_dataset_id,
        )

        # Clear admitted samples from buffer
        for s in sample_manifest:
            self._buffer.pop(s.sample_id, None)

        return batch
