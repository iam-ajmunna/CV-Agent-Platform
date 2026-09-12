"""cv_agent.capabilities.registry — Authoritative ADR-0001 typed capability registry.

This module implements the ADR-0001 Capability Registry subsystem, establishing a
typed, immutable registry over declarative capability definitions, SkillBindings,
ToolBindings, and AgentBindings backed by the Phase 2A domain substrate.

Legacy dictionary-based queries (list, describe, check, select) are provided solely
as read-only compatibility projections and are strictly separated from authoritative
typed state.
"""

from __future__ import annotations

import builtins
import json
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

if sys.version_info >= (3, 11):
    from importlib.resources.abc import Traversable
else:
    from importlib.abc import Traversable

from cv_agent.domain.contracts import (
    AgentBinding,
    CapabilityDefinition,
    CompatibilityConstraint,
    Provenance,
    ProvenanceKind,
    SkillBinding,
    Stage,
    ToolBinding,
)
from cv_agent.domain.errors import DomainValidationError, ValidationCode
from cv_agent.domain.identifiers import AgentId, CapabilityId, SkillId, ToolId
from cv_agent.domain.resolution import (
    DeterministicCapabilityResolver,
    Resolution,
    ResolutionCandidate,
)
from cv_agent.domain.snapshots import ResolutionSnapshot

# Legacy projection types preserved for backward compatibility
CapabilityStatus = Literal[
    "planned", "available", "partial", "experimental", "unavailable"
]
RiskLevel = Literal["low", "medium", "high"]
ItemType = Literal["skill", "tool", "agent", "knowledge_source"]


@dataclass(frozen=True)
class InputSpec:
    """Specification for a capability input parameter (legacy projection)."""

    name: str
    type: str
    description: str


@dataclass(frozen=True)
class OutputSpec:
    """Specification for a capability output (legacy projection)."""

    name: str
    type: str
    description: str


@dataclass(frozen=True)
class Capability:
    """Read-only compatibility projection of catalog declaration metadata."""

    id: str
    name: str
    category: str
    description: str
    required_inputs: tuple[InputSpec, ...]
    outputs: tuple[OutputSpec, ...]
    relevant_skills: tuple[str, ...]
    relevant_tools: tuple[str, ...]
    relevant_agents: tuple[str, ...]
    knowledge_sources: tuple[str, ...]
    applicable_task_types: tuple[str, ...]
    prerequisites: tuple[str, ...]
    status: CapabilityStatus
    risk_level: RiskLevel

    @property
    def is_available(self) -> bool:
        return self.status == "available"


@dataclass(frozen=True)
class RegistryItem:
    """Read-only compatibility projection of a supporting catalog entity."""

    id: str
    item_type: ItemType
    name: str
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _parse_stage(category_or_stage: str) -> Stage:
    val = category_or_stage.strip().upper()
    for stage in Stage:
        if stage.value == val:
            return stage
    mapping: dict[str, Stage] = {
        "PLANNING": Stage.DEFINE,
        "RESEARCH": Stage.RESEARCH,
        "ANALYSIS": Stage.DEFINE,
        "DATA": Stage.DATA,
        "MODELING": Stage.DESIGN,
        "EXPERIMENTATION": Stage.TRAIN,
        "EVALUATION": Stage.EVALUATE,
        "PLATFORM": Stage.OPTIMIZE,
        "DEPLOYMENT": Stage.DEPLOY,
        "KNOWLEDGE": Stage.DISCOVER,
        "ORCHESTRATION": Stage.DEFINE,
        "MONITORING": Stage.MONITOR,
    }
    if val in mapping:
        return mapping[val]
    raise DomainValidationError(
        ValidationCode.INVALID_VALUE,
        "capability.stage",
        f"unrecognized stage or category {category_or_stage!r}",
        category_or_stage,
    )


def _parse_legacy_capability(data: dict[str, Any]) -> Capability:
    return Capability(
        id=data["id"],
        name=data["name"],
        category=data["category"],
        description=data["description"],
        required_inputs=tuple(InputSpec(**s) for s in data.get("required_inputs", [])),
        outputs=tuple(OutputSpec(**s) for s in data.get("outputs", [])),
        relevant_skills=tuple(data.get("relevant_skills", [])),
        relevant_tools=tuple(data.get("relevant_tools", [])),
        relevant_agents=tuple(data.get("relevant_agents", [])),
        knowledge_sources=tuple(data.get("knowledge_sources", [])),
        applicable_task_types=tuple(data.get("applicable_task_types", [])),
        prerequisites=tuple(data.get("prerequisites", [])),
        status=data.get("status", "unavailable"),
        risk_level=data.get("risk_level", "medium"),
    )


def _parse_legacy_item(data: dict[str, Any], item_type: ItemType) -> RegistryItem:
    core_keys = {"id", "name", "description"}
    return RegistryItem(
        id=data["id"],
        item_type=item_type,
        name=data["name"],
        description=data["description"],
        metadata={k: v for k, v in data.items() if k not in core_keys},
    )


class CapabilityRegistry:
    """Authoritative ADR-0001 typed capability registry and catalog provider.

    Exposes typed, immutable CapabilityDefinition, SkillBinding, ToolBinding, and
    AgentBinding entities validated at load time, with deterministic capability
    resolution backed by DeterministicCapabilityResolver and emitting ResolutionSnapshot.
    """

    def __init__(self, registry_path: Path | Traversable | None = None) -> None:
        self._path: Path | Traversable | None = registry_path
        self._capabilities: Mapping[CapabilityId, CapabilityDefinition] = (
            MappingProxyType({})
        )
        self._skill_bindings: Mapping[CapabilityId, tuple[SkillBinding, ...]] = (
            MappingProxyType({})
        )
        self._tool_bindings: Mapping[SkillId, tuple[ToolBinding, ...]] = (
            MappingProxyType({})
        )
        self._agent_bindings: Mapping[ToolId, tuple[AgentBinding, ...]] = (
            MappingProxyType({})
        )
        self._legacy_capabilities: Mapping[str, Capability] = MappingProxyType({})
        self._legacy_items: Mapping[tuple[ItemType, str], RegistryItem] = (
            MappingProxyType({})
        )
        self._version: str = "unknown"
        self._loaded: bool = False

    @classmethod
    def from_definitions(
        cls,
        *,
        version: str = "1.0.0",
        capabilities: tuple[CapabilityDefinition, ...] = (),
        skill_bindings: tuple[SkillBinding, ...] = (),
        tool_bindings: tuple[ToolBinding, ...] = (),
        agent_bindings: tuple[AgentBinding, ...] = (),
    ) -> CapabilityRegistry:
        """Construct an immutable registry instance directly from typed domain objects."""
        registry = cls()
        caps_dict: dict[CapabilityId, CapabilityDefinition] = {}
        for cap in capabilities:
            caps_dict[cap.capability_id] = cap

        sb_dict: dict[CapabilityId, list[SkillBinding]] = {}
        for sb in skill_bindings:
            sb_dict.setdefault(sb.capability_id, []).append(sb)

        tb_dict: dict[SkillId, list[ToolBinding]] = {}
        for tb in tool_bindings:
            tb_dict.setdefault(tb.skill_id, []).append(tb)

        ab_dict: dict[ToolId, list[AgentBinding]] = {}
        for ab in agent_bindings:
            ab_dict.setdefault(ab.tool_id, []).append(ab)

        registry._capabilities = MappingProxyType(caps_dict)
        registry._skill_bindings = MappingProxyType(
            {k: tuple(v) for k, v in sb_dict.items()}
        )
        registry._tool_bindings = MappingProxyType(
            {k: tuple(v) for k, v in tb_dict.items()}
        )
        registry._agent_bindings = MappingProxyType(
            {k: tuple(v) for k, v in ab_dict.items()}
        )
        registry._version = version
        registry._loaded = True
        return registry

    def load(self) -> None:
        """Load and publish immutable registry state from the configured resource."""
        if self._path is None or not self._path.is_file():
            path_str = str(self._path) if self._path is not None else "<none>"
            raise FileNotFoundError(
                f"Capability registry resource could not be loaded: {path_str}"
            )

        with self._path.open("r", encoding="utf-8") as fh:
            raw: dict[str, Any] = json.load(fh)

        self._load_from_dict(raw)

    def _load_from_dict(self, raw: dict[str, Any]) -> None:
        # Local mutable collections during loading phase
        version: str = raw.get("version", "1.0.0")
        caps_dict: dict[CapabilityId, CapabilityDefinition] = {}
        sb_dict: dict[CapabilityId, list[SkillBinding]] = {}
        tb_dict: dict[SkillId, list[ToolBinding]] = {}
        ab_dict: dict[ToolId, list[AgentBinding]] = {}
        legacy_caps_dict: dict[str, Capability] = {}
        legacy_items_dict: dict[tuple[ItemType, str], RegistryItem] = {}

        # 1. Parse capabilities into authoritative CapabilityDefinition
        for cap_data in raw.get("capabilities", []):
            cap_id = CapabilityId(cap_data["id"])
            stage = _parse_stage(
                cap_data.get("stage", cap_data.get("category", "discover"))
            )
            cap_def = CapabilityDefinition(
                capability_id=cap_id,
                version=cap_data.get("version", version),
                stage=stage,
                description=cap_data.get("description", ""),
                risk_level=cap_data.get("risk_level", "medium"),
                requires_approval=bool(
                    cap_data.get(
                        "requires_approval", cap_data.get("risk_level") == "high"
                    )
                ),
            )
            caps_dict[cap_id] = cap_def
            legacy_caps_dict[cap_data["id"]] = _parse_legacy_capability(cap_data)

            # Extract declarative skill bindings from relevant_skills if not explicitly defined
            for skill_name in cap_data.get("relevant_skills", []):
                skill_id = SkillId(skill_name)
                sb_dict.setdefault(cap_id, []).append(
                    SkillBinding(
                        capability_id=cap_id,
                        skill_id=skill_id,
                        compatibility=CompatibilityConstraint(),
                        priority=100,
                        provenance=Provenance(kind=ProvenanceKind.INTERNAL),
                    )
                )

        # 2. Parse explicit typed bindings if present in raw catalogue
        for b_data in raw.get("skill_bindings", []):
            cap_id = CapabilityId(b_data["capability_id"])
            skill_id = SkillId(b_data["skill_id"])
            compat = CompatibilityConstraint(
                requirements=b_data.get("compatibility", {})
            )
            prov_raw = b_data.get("provenance", {})
            prov = Provenance(
                kind=ProvenanceKind(prov_raw.get("kind", "internal")),
                publisher=prov_raw.get("publisher"),
                source=prov_raw.get("source"),
                specification_version=prov_raw.get("specification_version"),
                verified=prov_raw.get("verified"),
                metadata=prov_raw.get("metadata", {}),
            )
            sb = SkillBinding(
                capability_id=cap_id,
                skill_id=skill_id,
                compatibility=compat,
                priority=int(b_data.get("priority", 100)),
                provenance=prov,
            )
            sb_dict.setdefault(cap_id, []).append(sb)

        for t_data in raw.get("tool_bindings", []):
            skill_id = SkillId(t_data["skill_id"])
            tool_id = ToolId(t_data["tool_id"])
            compat = CompatibilityConstraint(
                requirements=t_data.get("compatibility", {})
            )
            tb_dict.setdefault(skill_id, []).append(
                ToolBinding(skill_id=skill_id, tool_id=tool_id, compatibility=compat)
            )

        for a_data in raw.get("agent_bindings", []):
            tool_id = ToolId(a_data["tool_id"])
            agent_id = AgentId(a_data["agent_id"])
            ab_dict.setdefault(tool_id, []).append(
                AgentBinding(tool_id=tool_id, agent_id=agent_id)
            )

        # 3. Parse legacy items for compatibility projection
        type_map: dict[str, ItemType] = {
            "skills": "skill",
            "tools": "tool",
            "agents": "agent",
            "knowledge_sources": "knowledge_source",
        }
        for json_key, item_type in type_map.items():
            for item_data in raw.get(json_key, []):
                item = _parse_legacy_item(item_data, item_type)
                legacy_items_dict[(item.item_type, item.id)] = item

        # 4. Publish immutable state
        self._capabilities = MappingProxyType(caps_dict)
        self._skill_bindings = MappingProxyType(
            {
                k: tuple(sorted(v, key=lambda b: (-b.priority, str(b.skill_id))))
                for k, v in sb_dict.items()
            }
        )
        self._tool_bindings = MappingProxyType(
            {
                k: tuple(sorted(v, key=lambda b: str(b.tool_id)))
                for k, v in tb_dict.items()
            }
        )
        self._agent_bindings = MappingProxyType(
            {
                k: tuple(sorted(v, key=lambda b: str(b.agent_id)))
                for k, v in ab_dict.items()
            }
        )
        self._legacy_capabilities = MappingProxyType(legacy_caps_dict)
        self._legacy_items = MappingProxyType(legacy_items_dict)
        self._version = version
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # --- Authoritative Typed ADR-0001 API ---

    def get_capability(
        self, capability_id: CapabilityId | str
    ) -> CapabilityDefinition | None:
        """Retrieve authoritative CapabilityDefinition by typed ID."""
        self._ensure_loaded()
        cid = (
            capability_id
            if isinstance(capability_id, CapabilityId)
            else CapabilityId(capability_id)
        )
        return self._capabilities.get(cid)

    def list_capabilities(
        self, *, stage: Stage | None = None
    ) -> tuple[CapabilityDefinition, ...]:
        """Return all registered CapabilityDefinitions, optionally filtered by Stage."""
        self._ensure_loaded()
        caps = tuple(self._capabilities.values())
        if stage is not None:
            caps = tuple(c for c in caps if c.stage is stage)
        return tuple(sorted(caps, key=lambda c: str(c.capability_id)))

    def get_skill_bindings(
        self, capability_id: CapabilityId | str
    ) -> tuple[SkillBinding, ...]:
        """Return registered SkillBindings for a capability, sorted deterministically."""
        self._ensure_loaded()
        cid = (
            capability_id
            if isinstance(capability_id, CapabilityId)
            else CapabilityId(capability_id)
        )
        return self._skill_bindings.get(cid, ())

    def get_tool_bindings(self, skill_id: SkillId | str) -> tuple[ToolBinding, ...]:
        """Return registered ToolBindings for a skill, sorted deterministically."""
        self._ensure_loaded()
        sid = skill_id if isinstance(skill_id, SkillId) else SkillId(skill_id)
        return self._tool_bindings.get(sid, ())

    def get_agent_bindings(self, tool_id: ToolId | str) -> tuple[AgentBinding, ...]:
        """Return registered AgentBindings for a tool, sorted deterministically."""
        self._ensure_loaded()
        tid = tool_id if isinstance(tool_id, ToolId) else ToolId(tool_id)
        return self._agent_bindings.get(tid, ())

    def resolve_candidates(
        self,
        capability: CapabilityDefinition | CapabilityId | str,
        candidates: tuple[ResolutionCandidate, ...],
        *,
        resolver_policy_version: str = "1",
    ) -> Resolution:
        """Execute deterministic capability resolution returning complete Resolution facts."""
        self._ensure_loaded()
        cap_def: CapabilityDefinition
        if isinstance(capability, CapabilityDefinition):
            cap_def = capability
        else:
            cid = (
                capability
                if isinstance(capability, CapabilityId)
                else CapabilityId(capability)
            )
            found = self.get_capability(cid)
            if found is None:
                raise DomainValidationError(
                    ValidationCode.INVALID_VALUE,
                    "resolution.capability",
                    f"capability {cid!r} is not registered",
                    cid,
                )
            cap_def = found

        resolver = DeterministicCapabilityResolver(
            policy_version=resolver_policy_version
        )
        return resolver.resolve(cap_def, candidates)

    def resolve(
        self,
        capability: CapabilityDefinition | CapabilityId | str,
        candidates: tuple[ResolutionCandidate, ...],
        *,
        resolver_policy_version: str = "1",
        registry_version: str | None = None,
        snapshot_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ResolutionSnapshot:
        """Execute deterministic resolution and emit an immutable ResolutionSnapshot."""
        resolution = self.resolve_candidates(
            capability, candidates, resolver_policy_version=resolver_policy_version
        )
        sid = snapshot_id or f"res_{uuid.uuid4().hex[:16]}"
        reg_ver = registry_version or self.version
        ts = created_at or datetime.now(timezone.utc)

        return ResolutionSnapshot(
            snapshot_id=sid,
            registry_version=reg_ver,
            resolver_policy_version=resolver_policy_version,
            capability_id=resolution.capability.capability_id,
            capability_version=resolution.capability.version,
            skill_id=resolution.skill_id,
            skill_version=resolution.skill_version,
            tools=resolution.tools,
            agent_id=resolution.agent_id,
            target_reference=resolution.target_reference,
            compatibility_result=resolution.compatibility_result,
            availability_state=resolution.availability_state,
            created_at=ts,
        )

    # --- Read-Only Legacy Compatibility Projections ---

    def list(
        self, *, category: str | None = None, status: str | None = None
    ) -> builtins.list[Capability]:
        """Legacy compatibility projection: list catalog capabilities."""
        self._ensure_loaded()
        caps = list(self._legacy_capabilities.values())
        if category is not None:
            caps = [c for c in caps if c.category == category]
        if status is not None:
            caps = [c for c in caps if c.status == status]
        return sorted(caps, key=lambda c: c.id)

    def describe(self, capability_id: str) -> Capability:
        """Legacy compatibility projection: describe catalog capability."""
        self._ensure_loaded()
        if capability_id not in self._legacy_capabilities:
            available = ", ".join(sorted(self._legacy_capabilities))
            raise KeyError(
                f"Unknown capability {capability_id!r}. Registered: {available}"
            )
        return self._legacy_capabilities[capability_id]

    def check(self, capability_id: str) -> dict[str, Any]:
        """Legacy compatibility projection: report catalog declaration status.

        NOTE: Catalog declaration does NOT imply execution readiness (ADR-0001 §3.5).
        """
        self._ensure_loaded()
        cap = self.describe(capability_id)
        missing_prereqs = [
            p
            for p in cap.prerequisites
            if p in self._legacy_capabilities
            and not self._legacy_capabilities[p].is_available
        ]
        return {
            "capability_id": capability_id,
            "declared": True,
            "executable": False,
            "status": cap.status,
            "risk_level": cap.risk_level,
            "reason": (
                f"Catalog declaration only (status={cap.status!r}); "
                "operational execution requires external target binding and resolution."
            ),
            "missing_prerequisites": missing_prereqs,
        }

    def select(
        self, task_type: str, *, category: str | None = None
    ) -> builtins.list[Capability]:
        """Legacy compatibility projection: catalog task-type filter."""
        self._ensure_loaded()
        results = [
            c
            for c in self._legacy_capabilities.values()
            if task_type in c.applicable_task_types
            and c.is_available
            and (category is None or c.category == category)
        ]
        return sorted(results, key=lambda c: c.id)

    def list_items(
        self, item_type: ItemType | None = None
    ) -> builtins.list[RegistryItem]:
        """Legacy compatibility projection: list catalog supporting items."""
        self._ensure_loaded()
        items = list(self._legacy_items.values())
        if item_type is not None:
            items = [i for i in items if i.item_type == item_type]
        return sorted(items, key=lambda i: (i.item_type, i.id))

    def describe_item(self, item_type: ItemType, item_id: str) -> RegistryItem:
        """Legacy compatibility projection: describe catalog supporting item."""
        self._ensure_loaded()
        key = (item_type, item_id)
        if key not in self._legacy_items:
            available = ", ".join(
                sorted(
                    i.id
                    for i in self._legacy_items.values()
                    if i.item_type == item_type
                )
            )
            raise KeyError(f"Unknown {item_type} {item_id!r}. Registered: {available}")
        return self._legacy_items[key]

    def check_item(self, item_type: ItemType, item_id: str) -> dict[str, Any]:
        """Legacy compatibility projection: check supporting item."""
        item = self.describe_item(item_type, item_id)
        return {
            "item_id": item.id,
            "item_type": item.item_type,
            "declared": True,
            "executable": False,
            "reason": "no executable binding or discovery mechanism implemented",
        }

    @property
    def capability_count(self) -> int:
        """Total number of authoritative capabilities registered."""
        self._ensure_loaded()
        return len(self._capabilities)

    @property
    def version(self) -> str:
        """Declared registry specification version."""
        self._ensure_loaded()
        return self._version
