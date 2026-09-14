"""cv_agent.reasoning.grounding — Deterministic, Algorithmic Grounding Validator.

Enforces citation integrity, formal boundary semantics for empirical claims,
and speculation gating against quarantined context evidence without calling LLMs.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from cv_agent.reasoning.contracts import (
    DiagnosticReport,
    EmpiricalClaim,
    EmpiricalComparator,
    EvidenceRef,
    Hypothesis,
    ReasoningContext,
    ReasoningOutput,
)
from cv_agent.reasoning.errors import (
    EvidenceContradictionError,
    UnanchoredCitationError,
    UncitedEmpiricalClaimError,
    UnsupportedSpeculationError,
)

_FLOAT_ABS_TOL = 1e-6


class GroundingValidator:
    """Deterministic, algorithmic validator for reasoning outputs (ADR-0008 §3.3).

    Zero generative logic, zero LLM calls. Evaluates structured claims against
    quarantined evidence bounds and citation graphs.
    """

    def validate(self, context: ReasoningContext, output: ReasoningOutput) -> None:
        """Validate reasoning output citations, empirical claims, and hypotheses.

        Raises:
            UnanchoredCitationError: If citations reference nonexistent evidence URIs.
            UncitedEmpiricalClaimError: If empirical claims are made with zero citations.
            EvidenceContradictionError: If empirical claims contradict evidence bounds.
            UnsupportedSpeculationError: If high-confidence hypotheses lack citations.
        """
        evidence_by_uri: dict[str, EvidenceRef] = {
            ref.uri: ref for ref in context.evidence_refs
        }

        # 1. Validate citation identity against context evidence
        for citation in output.citations:
            if citation.uri not in evidence_by_uri:
                raise UnanchoredCitationError(
                    f"Citation {citation.citation_id!r} references unknown evidence URI: {citation.uri!r}"
                )
            evidence = evidence_by_uri[citation.uri]
            if citation.sha256 != evidence.sha256:
                raise UnanchoredCitationError(
                    f"Citation {citation.citation_id!r} hash mismatch for URI {citation.uri!r}: "
                    f"claimed {citation.sha256!r}, expected {evidence.sha256!r}"
                )

        # 2. Enforce zero-citation fail-closed rule for empirical claims
        if len(output.citations) == 0 and len(output.empirical_claims) > 0:
            first_claim = output.empirical_claims[0]
            raise UncitedEmpiricalClaimError(
                f"Empirical claim for metric {first_claim.metric_name!r} asserted with zero citations"
            )

        # 3. Validate empirical claims against evidence bounds
        for claim in output.empirical_claims:
            if claim.citation_uri not in evidence_by_uri:
                raise UnanchoredCitationError(
                    f"Empirical claim for metric {claim.metric_name!r} references unknown evidence URI: {claim.citation_uri!r}"
                )
            evidence = evidence_by_uri[claim.citation_uri]
            self._validate_empirical_claim_bounds(claim, evidence)

        # 4. Validate hypotheses speculation threshold
        hypotheses: Sequence[Hypothesis] = ()
        if isinstance(output.payload, DiagnosticReport):
            hypotheses = output.payload.hypotheses

        for hyp in hypotheses:
            if hyp.confidence > 0.8:
                if len(hyp.citation_refs) == 0:
                    raise UnsupportedSpeculationError(
                        f"Hypothesis {hyp.hypothesis_id!r} has high confidence ({hyp.confidence}) "
                        "but provides zero supporting citation references"
                    )
                # Verify that referenced citations exist in output citations or evidence URIs
                for cref in hyp.citation_refs:
                    is_in_citations = any(
                        c.citation_id == cref or c.uri == cref for c in output.citations
                    )
                    is_in_evidence = cref in evidence_by_uri
                    if not (is_in_citations or is_in_evidence):
                        raise UnanchoredCitationError(
                            f"Hypothesis {hyp.hypothesis_id!r} references unknown citation ID/URI: {cref!r}"
                        )

    def _validate_empirical_claim_bounds(
        self, claim: EmpiricalClaim, evidence: EvidenceRef
    ) -> None:
        """Evaluate formal boundary semantics for an EmpiricalClaim against EvidenceRef bounds."""
        if claim.metric_name not in evidence.bounds:
            raise EvidenceContradictionError(
                f"Empirical claim for {claim.metric_name!r} cites evidence {evidence.uri!r}, "
                f"which does not define bounds for {claim.metric_name!r}"
            )

        min_val, max_val = evidence.bounds[claim.metric_name]
        val = claim.claimed_value
        comp = claim.comparator

        if comp == EmpiricalComparator.LTE:
            if val > max_val:
                raise EvidenceContradictionError(
                    f"Claim {claim.metric_name} LTE {val} contradicts evidence upper bound {max_val}"
                )
        elif comp == EmpiricalComparator.LT:
            if val >= max_val:
                raise EvidenceContradictionError(
                    f"Claim {claim.metric_name} LT {val} contradicts evidence upper bound {max_val}"
                )
        elif comp == EmpiricalComparator.GTE:
            if val < min_val:
                raise EvidenceContradictionError(
                    f"Claim {claim.metric_name} GTE {val} contradicts evidence lower bound {min_val}"
                )
        elif comp == EmpiricalComparator.GT:
            if val <= min_val:
                raise EvidenceContradictionError(
                    f"Claim {claim.metric_name} GT {val} contradicts evidence lower bound {min_val}"
                )
        elif comp == EmpiricalComparator.EQ and (
            min_val != max_val or not math.isclose(val, min_val, abs_tol=_FLOAT_ABS_TOL)
        ):
            raise EvidenceContradictionError(
                f"Claim {claim.metric_name} EQ {val} contradicts evidence point bound [{min_val}, {max_val}]"
            )
