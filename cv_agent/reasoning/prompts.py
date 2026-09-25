"""cv_agent.reasoning.prompts — Structured Reasoning Prompts and JSON Schemas.

Authoritative schemas and prompt templates for problem framing, baseline selection,
experiment diagnosis, strategy formulation, and bounded schema repairs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cv_agent.reasoning.contracts import NodeType

# --- JSON Schemas for Structured Output (ADR-0008 §3.2) ---

_CITATION_ITEM_SCHEMA = {
    "type": "object",
    "required": ["citation_id", "uri", "sha256", "statement"],
    "properties": {
        "citation_id": {"type": "string"},
        "uri": {"type": "string"},
        "sha256": {"type": "string"},
        "statement": {"type": "string"},
    },
}

_EMPIRICAL_CLAIM_ITEM_SCHEMA = {
    "type": "object",
    "required": ["metric_name", "claimed_value", "comparator", "citation_uri"],
    "properties": {
        "metric_name": {"type": "string"},
        "claimed_value": {"type": "number"},
        "comparator": {"type": "string", "enum": ["EQ", "LT", "LTE", "GT", "GTE"]},
        "citation_uri": {"type": "string"},
    },
}

PROBLEM_FRAMING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["task_type", "target_hardware", "constraints"],
    "properties": {
        "task_type": {"type": "string"},
        "target_hardware": {"type": "string"},
        "operational_definition": {"type": "string"},
        "physical_environment": {"type": "string"},
        "camera_infrastructure": {"type": "string"},
        "error_tolerance": {"type": "string"},
        "data_availability": {"type": "string"},
        "intervention_modality": {"type": "string"},
        "latency_budget_ms": {"type": ["number", "null"]},
        "throughput_fps": {"type": ["number", "null"]},
        "compute_budget_hours": {"type": ["number", "null"]},
        "constraints": {"type": "object"},
        "citations": {"type": "array", "items": _CITATION_ITEM_SCHEMA},
        "empirical_claims": {"type": "array", "items": _EMPIRICAL_CLAIM_ITEM_SCHEMA},
    },
}

OPERATIONAL_INQUIRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "inquiry_id",
        "unresolved_dimensions",
        "targeted_questions",
        "context_summary",
    ],
    "properties": {
        "inquiry_id": {"type": "string"},
        "unresolved_dimensions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "targeted_questions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "context_summary": {"type": "string"},
        "assumptions_and_risks": {
            "type": "array",
            "items": {"type": "string"},
        },
        "citations": {"type": "array", "items": _CITATION_ITEM_SCHEMA},
        "empirical_claims": {"type": "array", "items": _EMPIRICAL_CLAIM_ITEM_SCHEMA},
    },
}

_TASK_NODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["task_id", "task_type", "description"],
    "properties": {
        "task_id": {"type": "string"},
        "task_type": {"type": "string"},
        "description": {"type": "string"},
        "inputs": {"type": "array", "items": {"type": "string"}},
        "outputs": {"type": "array", "items": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
    },
}

TASK_DECOMPOSITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "tasks": {"type": "array", "items": _TASK_NODE_SCHEMA},
        "root_task_ids": {"type": "array", "items": {"type": "string"}},
        "terminal_task_ids": {"type": "array", "items": {"type": "string"}},
        "capability_intents": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["capability_id", "intent_description", "parameter_intent"],
                "properties": {
                    "capability_id": {"type": "string"},
                    "intent_description": {"type": "string"},
                    "parameter_intent": {"type": "object"},
                    "target_preference": {"type": ["string", "null"]},
                },
            },
        },
        "citations": {"type": "array", "items": _CITATION_ITEM_SCHEMA},
        "empirical_claims": {"type": "array", "items": _EMPIRICAL_CLAIM_ITEM_SCHEMA},
    },
}

BASELINE_SELECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "architecture",
        "weights_source",
        "input_resolution",
        "batch_size",
        "evaluation_protocol",
        "hyperparameters",
    ],
    "properties": {
        "architecture": {"type": "string"},
        "weights_source": {"type": "string"},
        "input_resolution": {
            "type": "array",
            "items": {"type": "integer"},
        },
        "batch_size": {"type": "integer"},
        "evaluation_protocol": {"type": "string"},
        "hyperparameters": {"type": "object"},
        "citations": {"type": "array", "items": _CITATION_ITEM_SCHEMA},
        "empirical_claims": {"type": "array", "items": _EMPIRICAL_CLAIM_ITEM_SCHEMA},
    },
}

DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "hypotheses", "bottlenecks", "recommendations"],
    "properties": {
        "summary": {"type": "string"},
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["hypothesis_id", "statement", "confidence"],
                "properties": {
                    "hypothesis_id": {"type": "string"},
                    "statement": {"type": "string"},
                    "confidence": {"type": "number"},
                    "citation_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "bottlenecks": {
            "type": "array",
            "items": {"type": "string"},
        },
        "recommendations": {
            "type": "array",
            "items": {"type": "string"},
        },
        "citations": {"type": "array", "items": _CITATION_ITEM_SCHEMA},
        "empirical_claims": {"type": "array", "items": _EMPIRICAL_CLAIM_ITEM_SCHEMA},
    },
}

STRATEGY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "strategy_id",
        "title",
        "description",
        "expected_impact",
        "risk_level",
        "capability_intents",
    ],
    "properties": {
        "strategy_id": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "expected_impact": {"type": "string"},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "capability_intents": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["capability_id", "intent_description", "parameter_intent"],
                "properties": {
                    "capability_id": {"type": "string"},
                    "intent_description": {"type": "string"},
                    "parameter_intent": {"type": "object"},
                    "target_preference": {"type": ["string", "null"]},
                },
            },
        },
        "approval_recommendation": {
            "type": ["object", "null"],
            "properties": {
                "recommends_approval": {"type": "boolean"},
                "risk_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                },
                "reversibility": {
                    "type": "string",
                    "enum": ["reversible", "irreversible"],
                },
                "rationale": {"type": "string"},
                "estimated_cost_usd": {"type": ["string", "number", "null"]},
            },
        },
        "citations": {"type": "array", "items": _CITATION_ITEM_SCHEMA},
        "empirical_claims": {"type": "array", "items": _EMPIRICAL_CLAIM_ITEM_SCHEMA},
    },
}

SCHEMAS_BY_NODE_TYPE: dict[NodeType, dict[str, Any]] = {
    NodeType.PROBLEM_FRAMING: PROBLEM_FRAMING_SCHEMA,
    NodeType.BASELINE_SELECTION: BASELINE_SELECTION_SCHEMA,
    NodeType.EXPERIMENT_DIAGNOSIS: DIAGNOSIS_SCHEMA,
    NodeType.STRATEGY_FORMULATION: STRATEGY_SCHEMA,
    NodeType.ELICITATION: OPERATIONAL_INQUIRY_SCHEMA,
    NodeType.TASK_DECOMPOSITION: TASK_DECOMPOSITION_SCHEMA,
}


# --- System & User Prompts ---

SYSTEM_PROMPTS_BY_NODE_TYPE: dict[NodeType, str] = {
    NodeType.PROBLEM_FRAMING: (
        "You are an expert Computer Vision Systems Engineer operating as the Problem Framing reasoning node. "
        "Your role is to characterize engineering tasks against latency, throughput, compute budget, and edge hardware constraints [P§6, P§29.1]. "
        "Return only valid JSON conforming strictly to the requested schema. Do not include conversational markdown outside the JSON."
    ),
    NodeType.BASELINE_SELECTION: (
        "You are an expert Computer Vision Systems Engineer operating as the Baseline Selection reasoning node. "
        "Your role is to establish measurable reference baselines before speculative exploration or fine-tuning [P§4, P§29.2]. "
        "Return only valid JSON conforming strictly to the requested schema. Do not include conversational markdown outside the JSON."
    ),
    NodeType.EXPERIMENT_DIAGNOSIS: (
        "You are an expert Computer Vision Systems Engineer operating as the Experiment Diagnosis reasoning node. "
        "Your role is to diagnose completed experiment metrics against baselines and targets to form grounded hypotheses [P§4, P§29.3]. "
        "All empirical claims must strictly reference cited evidence bounds. "
        "Return only valid JSON conforming strictly to the requested schema. Do not include conversational markdown outside the JSON."
    ),
    NodeType.STRATEGY_FORMULATION: (
        "You are an expert Computer Vision Systems Engineer operating as the Strategy Formulation reasoning node. "
        "Your role is to synthesize actionable next-step strategies and express capability intents without executing tools directly [P§4, P§19]. "
        "Flag approval recommendations for expensive, destructive, or production actions per docs/APPROVALS.md [P§24]. "
        "Return only valid JSON conforming strictly to the requested schema. Do not include conversational markdown outside the JSON."
    ),
    NodeType.ELICITATION: (
        "You are an expert Computer Vision Systems Engineer and Technical Lead operating as the Elicitation reasoning node [P§5, P§29.1]. "
        "Your role is operational problem characterization BEFORE solution design. "
        "You must NEVER propose or mention concrete model architectures (such as YOLO, RT-DETR, or Faster R-CNN) during elicitation [P§31]. "
        "Analyze unresolved constraints across the 8 canonical dimensions: operational definition, physical environment, "
        "camera infrastructure, target hardware, performance ceilings, error tolerance, data availability, and intervention modality. "
        "Formulate targeted operational questions to characterize the problem. "
        "Return only valid JSON conforming strictly to the requested schema."
    ),
    NodeType.TASK_DECOMPOSITION: (
        "You are an expert Computer Vision Systems Engineer operating as the Task Decomposition reasoning node [P§4, P§6]. "
        "Your role is to break down an operationally characterized problem into an acyclic directed graph (DAG) of domain CV tasks "
        "(e.g., object detection, tracking, spatial geometry, action recognition, temporal reasoning) and express required capability intents. "
        "Return only valid JSON conforming strictly to the requested schema."
    ),
}


def _serialize_frozen_json(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return dict(obj)
    if isinstance(obj, (list, tuple)):
        return list(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def create_user_prompt(node_type: NodeType, parameters: Mapping[str, Any]) -> str:
    """Format structured user prompt for the given reasoning node."""
    params_json = json.dumps(
        dict(parameters),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        default=_serialize_frozen_json,
    )
    return (
        f"Execute {node_type.value} reasoning with the following input parameters and constraints:\n\n"
        f"```json\n{params_json}\n```\n\n"
        "Provide your structured decision conforming exactly to the configured output schema."
    )


def create_repair_prompt(
    schema: Mapping[str, Any], invalid_output: str, error_message: str
) -> str:
    """Construct a schema-repair prompt instructing the LLM to resolve schema validation failures."""
    schema_json = json.dumps(dict(schema), indent=2, sort_keys=True, ensure_ascii=False)
    return (
        "Your previous output failed JSON Schema validation.\n\n"
        f"Validation error:\n{error_message}\n\n"
        f"Invalid output was:\n```json\n{invalid_output}\n```\n\n"
        f"Target JSON Schema:\n```json\n{schema_json}\n```\n\n"
        "Correct the output to strictly conform to the schema. Output only the corrected JSON object without markdown fences or extraneous text."
    )
