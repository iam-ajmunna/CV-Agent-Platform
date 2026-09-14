"""cv_agent.training.retention — Checkpoint lineage evaluation and retention management for ADR-0010."""

from __future__ import annotations

from collections.abc import Sequence

from cv_agent.training.contracts import (
    CheckpointManifest,
    RetentionDecision,
    RetentionPolicy,
)
from cv_agent.training.errors import CheckpointRetentionError


class CheckpointRetentionManager:
    """Evaluates checkpoint collections against retention policies.

    Produces a pure logical RetentionDecision distinguishing logical retention
    from physical storage deletion (which is delegated to configured artifact storage adapters).
    Guarantees that the best checkpoint and final epoch checkpoint are NEVER pruned.
    """

    def evaluate_retention(
        self,
        checkpoints: Sequence[CheckpointManifest],
        policy: RetentionPolicy,
        total_epochs: int,
    ) -> RetentionDecision:
        """Evaluate checkpoints and compute retained vs prunable sets.

        Ranking policy:
            - Best K by primary metric (deterministic tie-breaking: epoch desc, checkpoint_id asc).
            - Last N by step/epoch coordinates.
            - Final epoch checkpoint always preserved if policy.keep_final is True.
        """
        if not checkpoints:
            raise CheckpointRetentionError(
                "No checkpoints provided for retention evaluation"
            )
        if total_epochs < 1:
            raise CheckpointRetentionError(
                f"total_epochs must be >= 1, got {total_epochs}"
            )

        # 1. Ranking keys for deterministic ordering
        class _BestRankingKey:
            __slots__ = ("checkpoint_id", "epoch", "higher_is_better", "val")

            def __init__(
                self,
                c: CheckpointManifest,
                primary_metric: str,
                higher_is_better: bool,
            ) -> None:
                m = c.metrics.get(primary_metric)
                if m is None:
                    self.val = float("-inf") if higher_is_better else float("inf")
                else:
                    self.val = m
                self.epoch = c.epoch
                self.checkpoint_id = str(c.checkpoint_id)
                self.higher_is_better = higher_is_better

            def __lt__(self, other: _BestRankingKey) -> bool:
                # Primary metric: better metric comes first
                if self.val != other.val:
                    return (
                        self.val > other.val
                        if self.higher_is_better
                        else self.val < other.val
                    )
                # Deterministic tie-break 1: higher epoch comes first (epoch desc)
                if self.epoch != other.epoch:
                    return self.epoch > other.epoch
                # Deterministic tie-break 2: smaller checkpoint_id comes first (checkpoint_id asc)
                return self.checkpoint_id < other.checkpoint_id

        class _RecencyRankingKey:
            __slots__ = ("checkpoint_id", "epoch", "step")

            def __init__(self, c: CheckpointManifest) -> None:
                self.epoch = c.epoch
                self.step = c.step
                self.checkpoint_id = str(c.checkpoint_id)

            def __lt__(self, other: _RecencyRankingKey) -> bool:
                if self.epoch != other.epoch:
                    return self.epoch > other.epoch
                if self.step != other.step:
                    return self.step > other.step
                return self.checkpoint_id < other.checkpoint_id

        # 2. Identify final checkpoint
        # Prefer exact match epoch == total_epochs - 1, fallback to highest epoch/step
        exact_final = [c for c in checkpoints if c.epoch == total_epochs - 1]
        if exact_final:
            final_ckpt = min(exact_final, key=_RecencyRankingKey)
        else:
            final_ckpt = min(checkpoints, key=_RecencyRankingKey)

        # 3. Rank checkpoints for keep_best (primary metric best first, epoch desc, checkpoint_id asc)
        ranked_best = sorted(
            checkpoints,
            key=lambda c: _BestRankingKey(
                c, policy.primary_metric, policy.higher_is_better
            ),
        )
        best_ckpt = ranked_best[0]

        # 4. Select keep_best IDs
        best_k = min(policy.keep_best, len(ranked_best))
        best_ids = {c.checkpoint_id for c in ranked_best[:best_k]}

        # 5. Select keep_last IDs
        sorted_by_recency = sorted(checkpoints, key=_RecencyRankingKey)
        last_n = min(policy.keep_last, len(sorted_by_recency))
        last_ids = {c.checkpoint_id for c in sorted_by_recency[:last_n]}

        # 6. Formulate retained set
        retained_set = best_ids | last_ids
        retained_set.add(best_ckpt.checkpoint_id)
        if policy.keep_final:
            retained_set.add(final_ckpt.checkpoint_id)

        all_ids = {c.checkpoint_id for c in checkpoints}
        prunable_set = all_ids - retained_set

        # Verify invariants
        assert best_ckpt.checkpoint_id in retained_set
        assert best_ckpt.checkpoint_id not in prunable_set
        if policy.keep_final:
            assert final_ckpt.checkpoint_id in retained_set
            assert final_ckpt.checkpoint_id not in prunable_set

        return RetentionDecision(
            retained_checkpoint_ids=tuple(sorted(retained_set)),
            prunable_checkpoint_ids=tuple(sorted(prunable_set)),
            best_checkpoint_id=best_ckpt.checkpoint_id,
            final_checkpoint_id=final_ckpt.checkpoint_id,
        )
