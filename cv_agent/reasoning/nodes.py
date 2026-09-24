"""cv_agent.reasoning.nodes — Stateless Cognitive Reasoning Node Implementations.

Implements ProblemFramingNode, BaselineSelectionNode, DiagnosisNode, and StrategyNode
operating strictly through the ADR-0002 LLM Gateway with bounded schema repairs and
deterministic grounding validation.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from cv_agent.domain.identifiers import CapabilityId
from cv_agent.llm.contracts import (
    LLMRequest,
    LLMRequirements,
    Message,
    StructuredOutputSpec,
    TextBlock,
)
from cv_agent.llm.errors import InvalidRequestError, LLMError
from cv_agent.llm.gateway import LLMGateway
from cv_agent.llm.schema import SchemaValidationError, validate_json_schema
from cv_agent.reasoning.contracts import (
    ApprovalRecommendation,
    BaselineSpec,
    CapabilityIntent,
    CitationRef,
    CVTaskDecompositionDAG,
    DecisionId,
    DiagnosticReport,
    EmpiricalClaim,
    EmpiricalComparator,
    Hypothesis,
    NodeType,
    OperationalInquiry,
    ProblemCharacterization,
    ReasoningContext,
    ReasoningOutput,
    ReasoningOutputPayload,
    Reversibility,
    RiskLevel,
    StrategyRecommendation,
    TaskNode,
    generate_decision_id,
)
from cv_agent.reasoning.errors import (
    CognitiveBudgetExceededError,
    ReasoningTransportError,
    SchemaRepairExhaustedError,
)
from cv_agent.reasoning.grounding import GroundingValidator
from cv_agent.reasoning.prompts import (
    SCHEMAS_BY_NODE_TYPE,
    SYSTEM_PROMPTS_BY_NODE_TYPE,
    create_repair_prompt,
    create_user_prompt,
)


class ReasoningNode:
    """Base class for stateless cognitive reasoning components (ADR-0008 §3.1)."""

    def __init__(
        self,
        gateway: LLMGateway,
        *,
        validator: GroundingValidator | None = None,
    ) -> None:
        self.gateway = gateway
        self.validator = validator or GroundingValidator()

    @property
    def node_type(self) -> NodeType:
        raise NotImplementedError

    def execute(
        self,
        context: ReasoningContext,
        *,
        decision_id: DecisionId | None = None,
    ) -> ReasoningOutput:
        """Execute reasoning node against context with bounded repairs and grounding validation.

        Args:
            context: Immutable reasoning input context provided by Orchestration.
            decision_id: Optional deterministic or pre-allocated DecisionId.

        Raises:
            CognitiveBudgetExceededError: If context.current_hop >= context.max_hops.
            ReasoningTransportError: If gateway encounters an unhandled transport error.
            SchemaRepairExhaustedError: If output fails schema validation after 2 repairs (3 attempts total).
            ReasoningError subclasses: If grounding or boundary validation fails.
        """
        # 1. Authoritative cognitive hop budget enforcement (Gate I4)
        if context.current_hop >= context.max_hops:
            raise CognitiveBudgetExceededError(
                f"Cognitive hop budget exhausted for {self.node_type.value}: "
                f"current_hop={context.current_hop} >= max_hops={context.max_hops}"
            )

        dec_id = decision_id or generate_decision_id()
        system_prompt = SYSTEM_PROMPTS_BY_NODE_TYPE[self.node_type]
        user_prompt = create_user_prompt(self.node_type, context.parameters)
        target_schema = SCHEMAS_BY_NODE_TYPE[self.node_type]

        current_messages = (
            Message(
                role="system", content=(TextBlock(type="text", text=system_prompt),)
            ),
            Message(role="user", content=(TextBlock(type="text", text=user_prompt),)),
        )

        raw_dict: dict[str, Any] | None = None
        attempt = 0
        max_attempts = 3  # 1 initial + <= 2 schema repairs (Gate I2)

        while attempt < max_attempts:
            attempt += 1
            req = LLMRequest(
                request_id=f"req_{dec_id}_{attempt}",
                idempotency_key=f"idem_{dec_id}_{attempt}",
                trace_id=context.trace_id,
                messages=current_messages,
                requirements=LLMRequirements(
                    input_modalities=frozenset({"text"}),
                    output_modalities=frozenset({"text"}),
                    reasoning=True,
                    structured_output=True,
                ),
                response_schema=StructuredOutputSpec(
                    schema_id=f"schema_{self.node_type.value}",
                    schema_version="1.0.0",
                    schema=target_schema,
                    strict=True,
                ),
                metadata=MappingProxyType(
                    {
                        "node_type": self.node_type.value,
                        "attempt": str(attempt),
                    }
                ),
            )

            # Gateway execution with strict transport isolation
            is_schema_failure = False
            schema_error_msg = ""
            response = None

            try:
                response = self.gateway.complete(req)
            except InvalidRequestError as exc:
                if (
                    "could not be decoded as JSON for schema" in exc.message
                    or "Structured output schema validation failed" in exc.message
                ):
                    is_schema_failure = True
                    schema_error_msg = exc.message
                else:
                    raise ReasoningTransportError(
                        f"LLM Gateway transport failure during {self.node_type.value} reasoning: {exc}"
                    ) from exc
            except LLMError as exc:
                raise ReasoningTransportError(
                    f"LLM Gateway transport failure during {self.node_type.value} reasoning: {exc}"
                ) from exc

            if is_schema_failure:
                if attempt >= max_attempts:
                    raise SchemaRepairExhaustedError(
                        f"Exhausted schema repair attempts ({max_attempts}) for {self.node_type.value}: "
                        f"{schema_error_msg}"
                    )
                repair_prompt = create_repair_prompt(
                    target_schema,
                    "Invalid output failed schema validation",
                    schema_error_msg,
                )
                current_messages = (
                    Message(
                        role="system",
                        content=(TextBlock(type="text", text=system_prompt),),
                    ),
                    Message(
                        role="user",
                        content=(TextBlock(type="text", text=repair_prompt),),
                    ),
                )
                continue

            assert response is not None
            # Parse response content or structured output
            content_to_validate: Any = None
            if response.structured_output is not None and isinstance(
                response.structured_output, dict
            ):
                content_to_validate = response.structured_output
            else:
                try:
                    content_to_validate = json.loads(response.content)
                except Exception as parse_exc:
                    if attempt >= max_attempts:
                        raise SchemaRepairExhaustedError(
                            f"Exhausted schema repair attempts ({max_attempts}) for {self.node_type.value}: "
                            f"JSON parse error: {parse_exc}"
                        ) from parse_exc
                    repair_prompt = create_repair_prompt(
                        target_schema, response.content, str(parse_exc)
                    )
                    current_messages = (
                        Message(
                            role="system",
                            content=(TextBlock(type="text", text=system_prompt),),
                        ),
                        Message(
                            role="user",
                            content=(TextBlock(type="text", text=repair_prompt),),
                        ),
                    )
                    continue

            # Schema validation
            try:
                validate_json_schema(content_to_validate, target_schema)
                raw_dict = content_to_validate
                break
            except SchemaValidationError as schema_exc:
                if attempt >= max_attempts:
                    raise SchemaRepairExhaustedError(
                        f"Exhausted schema repair attempts ({max_attempts}) for {self.node_type.value}: "
                        f"{schema_exc}"
                    ) from schema_exc
                repair_prompt = create_repair_prompt(
                    target_schema,
                    json.dumps(content_to_validate, ensure_ascii=False),
                    str(schema_exc),
                )
                current_messages = (
                    Message(
                        role="system",
                        content=(TextBlock(type="text", text=system_prompt),),
                    ),
                    Message(
                        role="user",
                        content=(TextBlock(type="text", text=repair_prompt),),
                    ),
                )

        if raw_dict is None:
            raise SchemaRepairExhaustedError(
                f"Failed to produce valid structured output for {self.node_type.value}"
            )

        # 2. Extract shared citations and empirical claims
        citations = tuple(
            CitationRef(
                citation_id=c["citation_id"],
                uri=c["uri"],
                sha256=c["sha256"],
                statement=c["statement"],
            )
            for c in raw_dict.get("citations", ())
        )

        empirical_claims = tuple(
            EmpiricalClaim(
                metric_name=ec["metric_name"],
                claimed_value=float(ec["claimed_value"]),
                comparator=EmpiricalComparator(ec["comparator"]),
                citation_uri=ec["citation_uri"],
            )
            for ec in raw_dict.get("empirical_claims", ())
        )

        # 3. Parse node-specific domain payload
        payload, capability_intents, approval_recommendation = self._parse_payload(
            raw_dict
        )

        # 4. Construct immutable output
        output = ReasoningOutput(
            decision_id=dec_id,
            node_type=self.node_type,
            context_ref=context.context_ref,
            payload=payload,
            capability_intents=capability_intents,
            citations=citations,
            empirical_claims=empirical_claims,
            approval_recommendation=approval_recommendation,
            hops_consumed=1,
        )

        # 5. Algorithmic, non-generative grounding validation
        self.validator.validate(context, output)

        return output

    def _parse_payload(
        self, raw_dict: dict[str, Any]
    ) -> tuple[
        ReasoningOutputPayload,
        tuple[CapabilityIntent, ...],
        ApprovalRecommendation | None,
    ]:
        raise NotImplementedError


class ProblemFramingNode(ReasoningNode):
    """Stateless reasoning node for task framing and constraint characterization."""

    @property
    def node_type(self) -> NodeType:
        return NodeType.PROBLEM_FRAMING

    def _parse_payload(
        self, raw_dict: dict[str, Any]
    ) -> tuple[
        ProblemCharacterization,
        tuple[CapabilityIntent, ...],
        ApprovalRecommendation | None,
    ]:
        payload = ProblemCharacterization(
            task_type=raw_dict["task_type"],
            target_hardware=raw_dict["target_hardware"],
            operational_definition=raw_dict.get("operational_definition", ""),
            physical_environment=raw_dict.get("physical_environment", ""),
            camera_infrastructure=raw_dict.get("camera_infrastructure", ""),
            error_tolerance=raw_dict.get("error_tolerance", ""),
            data_availability=raw_dict.get("data_availability", ""),
            intervention_modality=raw_dict.get("intervention_modality", ""),
            latency_budget_ms=(
                float(raw_dict["latency_budget_ms"])
                if raw_dict.get("latency_budget_ms") is not None
                else None
            ),
            throughput_fps=(
                float(raw_dict["throughput_fps"])
                if raw_dict.get("throughput_fps") is not None
                else None
            ),
            compute_budget_hours=(
                float(raw_dict["compute_budget_hours"])
                if raw_dict.get("compute_budget_hours") is not None
                else None
            ),
            constraints=raw_dict.get("constraints", {}),
        )
        return payload, (), None


class BaselineSelectionNode(ReasoningNode):
    """Stateless reasoning node for reference baseline formulation."""

    @property
    def node_type(self) -> NodeType:
        return NodeType.BASELINE_SELECTION

    def _parse_payload(
        self, raw_dict: dict[str, Any]
    ) -> tuple[
        BaselineSpec, tuple[CapabilityIntent, ...], ApprovalRecommendation | None
    ]:
        res = raw_dict["input_resolution"]
        payload = BaselineSpec(
            architecture=raw_dict["architecture"],
            weights_source=raw_dict["weights_source"],
            input_resolution=(int(res[0]), int(res[1])),
            batch_size=int(raw_dict["batch_size"]),
            evaluation_protocol=raw_dict["evaluation_protocol"],
            hyperparameters=raw_dict.get("hyperparameters", {}),
        )
        return payload, (), None


class DiagnosisNode(ReasoningNode):
    """Stateless reasoning node for post-experiment metrics diagnosis."""

    @property
    def node_type(self) -> NodeType:
        return NodeType.EXPERIMENT_DIAGNOSIS

    def _parse_payload(
        self, raw_dict: dict[str, Any]
    ) -> tuple[
        DiagnosticReport, tuple[CapabilityIntent, ...], ApprovalRecommendation | None
    ]:
        hypotheses = tuple(
            Hypothesis(
                hypothesis_id=h["hypothesis_id"],
                statement=h["statement"],
                confidence=float(h["confidence"]),
                citation_refs=tuple(h.get("citation_refs", ())),
            )
            for h in raw_dict["hypotheses"]
        )
        payload = DiagnosticReport(
            summary=raw_dict["summary"],
            hypotheses=hypotheses,
            bottlenecks=tuple(raw_dict["bottlenecks"]),
            recommendations=tuple(raw_dict["recommendations"]),
        )
        return payload, (), None


class StrategyNode(ReasoningNode):
    """Stateless reasoning node for synthesizing next engineering actions and capability intents."""

    @property
    def node_type(self) -> NodeType:
        return NodeType.STRATEGY_FORMULATION

    def _parse_payload(
        self, raw_dict: dict[str, Any]
    ) -> tuple[
        StrategyRecommendation,
        tuple[CapabilityIntent, ...],
        ApprovalRecommendation | None,
    ]:
        capability_intents = tuple(
            CapabilityIntent(
                capability_id=CapabilityId(ci["capability_id"]),
                intent_description=ci["intent_description"],
                parameter_intent=ci.get("parameter_intent", {}),
                target_preference=ci.get("target_preference"),
            )
            for ci in raw_dict.get("capability_intents", ())
        )

        approval_rec: ApprovalRecommendation | None = None
        if raw_dict.get("approval_recommendation") is not None:
            ar = raw_dict["approval_recommendation"]
            cost_val = ar.get("estimated_cost_usd")
            approval_rec = ApprovalRecommendation(
                recommends_approval=bool(ar.get("recommends_approval", False)),
                risk_level=RiskLevel(ar.get("risk_level", "low")),
                reversibility=Reversibility(ar.get("reversibility", "reversible")),
                rationale=ar.get("rationale", ""),
                estimated_cost_usd=Decimal(str(cost_val))
                if cost_val is not None
                else None,
            )

        payload = StrategyRecommendation(
            strategy_id=raw_dict["strategy_id"],
            title=raw_dict["title"],
            description=raw_dict["description"],
            expected_impact=raw_dict["expected_impact"],
            risk_level=RiskLevel(raw_dict["risk_level"]),
            capability_intents=capability_intents,
        )
        return payload, capability_intents, approval_rec


class ElicitationNode(ReasoningNode):
    """Stateless reasoning node for operational inquiry and problem characterization elicitation [P§5], [P§29.1]."""

    @property
    def node_type(self) -> NodeType:
        return NodeType.ELICITATION

    def _parse_payload(
        self, raw_dict: dict[str, Any]
    ) -> tuple[
        OperationalInquiry,
        tuple[CapabilityIntent, ...],
        ApprovalRecommendation | None,
    ]:
        inquiry = OperationalInquiry(
            inquiry_id=raw_dict["inquiry_id"],
            unresolved_dimensions=tuple(raw_dict.get("unresolved_dimensions", ())),
            targeted_questions=tuple(raw_dict.get("targeted_questions", ())),
            context_summary=raw_dict.get("context_summary", ""),
            assumptions_and_risks=tuple(raw_dict.get("assumptions_and_risks", ())),
        )
        return inquiry, (), None


class TaskDecompositionNode(ReasoningNode):
    """Stateless reasoning node for CV task DAG decomposition and capability intent expression [P§4], [P§6]."""

    @property
    def node_type(self) -> NodeType:
        return NodeType.TASK_DECOMPOSITION

    def _parse_payload(
        self, raw_dict: dict[str, Any]
    ) -> tuple[
        CVTaskDecompositionDAG,
        tuple[CapabilityIntent, ...],
        ApprovalRecommendation | None,
    ]:
        raw_tasks = raw_dict.get("tasks", [])
        tasks = tuple(
            TaskNode(
                task_id=t["task_id"],
                task_type=t["task_type"],
                description=t["description"],
                inputs=tuple(t.get("inputs", ())),
                outputs=tuple(t.get("outputs", ())),
                dependencies=tuple(t.get("dependencies", ())),
            )
            for t in raw_tasks
        )
        dag = CVTaskDecompositionDAG(
            tasks=tasks,
            root_task_ids=tuple(raw_dict.get("root_task_ids", ())),
            terminal_task_ids=tuple(raw_dict.get("terminal_task_ids", ())),
        )

        capability_intents = tuple(
            CapabilityIntent(
                capability_id=CapabilityId(ci["capability_id"]),
                intent_description=ci["intent_description"],
                parameter_intent=ci.get("parameter_intent", {}),
                target_preference=ci.get("target_preference"),
            )
            for ci in raw_dict.get("capability_intents", ())
        )

        return dag, capability_intents, None
