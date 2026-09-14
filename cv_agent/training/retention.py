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

        # 1. Identify final checkpoint
        # Prefer exact match epoch == total_epochs - 1, fallback to highest epoch/step
        exact_final = [c for c in checkpoints if c.epoch == total_epochs - 1]
        if exact_final:
            final_ckpt = max(exact_final, key=lambda c: (c.step, c.checkpoint_id))
        else:
            final_ckpt = max(
                checkpoints, key=lambda c: (c.epoch, c.step, c.checkpoint_id)
            )

        # 2. Rank checkpoints for keep_best
        def _metric_key(c: CheckpointManifest) -> tuple[float, int, str]:
            metric_val = c.metrics.get(policy.primary_metric)
            if metric_val is None:
                # If metric missing, penalize to worst possible rank
                val = float("-inf") if policy.higher_is_better else float("inf")
            else:
                val = metric_val if policy.higher_is_better else -metric_val
            # Deterministic tie-breaking: epoch descending (represented as c.epoch), checkpoint_id ascending
            return (val, c.epoch, str(c.checkpoint_id))

        ranked_best = sorted(checkpoints, key=_metric_key, reverse=True)
        best_ckpt = ranked_best[0]

        # 3. Select keep_best IDs
        best_k = min(policy.keep_best, len(ranked_best))
        best_ids = {c.checkpoint_id for c in ranked_best[:best_k]}

        # 4. Select keep_last IDs
        sorted_by_recency = sorted(
            checkpoints, key=lambda c: (c.epoch, c.step, c.checkpoint_id), reverse=True
        )
        last_n = min(policy.keep_last, len(sorted_by_recency))
        last_ids = {c.checkpoint_id for c in sorted_by_recency[:last_n]}

        # 5. Formulate retained set
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
