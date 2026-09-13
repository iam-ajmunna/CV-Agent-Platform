"""cv_agent.memory.service — Git-tracked structured project memory service.

Manages persistent domain state under <workspace_root>/.cv_agent/memory/:
- hardware_constraints.yaml
- architectural_choices.md
- learned_heuristics.md
- project_spec.md
- dataset_lineage.md

Enforces symlink-aware workspace containment and canonical serialization.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from cv_agent.memory.interfaces import (
    ArchitecturalDecision,
    HardwareConstraints,
    LearnedHeuristic,
    ProjectMemoryReader,
    ProjectMemoryWriter,
)


class ProjectMemoryError(Exception):
    """Base exception for project memory operations."""


class WorkspaceContainmentError(ProjectMemoryError):
    """Raised when a path violates workspace containment."""


class ProjectMemoryFormatError(ProjectMemoryError):
    """Raised when structured memory files violate canonical schema."""


class GitProjectMemory(ProjectMemoryReader, ProjectMemoryWriter):
    """Service managing Git-tracked structured project memory documents."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.memory_dir = (self.workspace_root / ".cv_agent" / "memory").resolve()
        self._validate_containment(self.memory_dir)
        self._init_memory_tree()

    def _validate_containment(self, target_path: Path) -> Path:
        """Enforces symlink-aware containment within workspace_root."""
        resolved = target_path.resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError as err:
            raise WorkspaceContainmentError(
                f"Path {target_path} resolves outside workspace root {self.workspace_root}"
            ) from err
        return resolved

    def _init_memory_tree(self) -> None:
        """Ensures the .cv_agent/memory directory and scaffolded files exist."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        # Touch scaffolded files if absent
        for filename in ("project_spec.md", "dataset_lineage.md"):
            p = self.memory_dir / filename
            if not p.exists():
                p.write_text(
                    f"# {filename.replace('_', ' ').replace('.md', '').title()}\n\n",
                    encoding="utf-8",
                )

    @property
    def hardware_constraints_path(self) -> Path:
        return self._validate_containment(self.memory_dir / "hardware_constraints.yaml")

    @property
    def architectural_choices_path(self) -> Path:
        return self._validate_containment(self.memory_dir / "architectural_choices.md")

    @property
    def learned_heuristics_path(self) -> Path:
        return self._validate_containment(self.memory_dir / "learned_heuristics.md")

    def get_hardware_constraints(self) -> HardwareConstraints:
        p = self.hardware_constraints_path
        if not p.exists():
            raise ProjectMemoryFormatError(f"Hardware constraints file missing: {p}")
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ProjectMemoryFormatError(
                    f"Expected mapping in {p}, got {type(data)}"
                )
            precisions = tuple(str(x) for x in data.get("supported_precisions", ()))
            return HardwareConstraints(
                target_device=str(data["target_device"]),
                max_vram_mb=int(data["max_vram_mb"]),
                max_ram_mb=int(data["max_ram_mb"]),
                power_budget_watts=(
                    float(data["power_budget_watts"])
                    if data.get("power_budget_watts") is not None
                    else None
                ),
                latency_ceiling_ms=(
                    float(data["latency_ceiling_ms"])
                    if data.get("latency_ceiling_ms") is not None
                    else None
                ),
                supported_precisions=precisions,
            )
        except (yaml.YAMLError, KeyError, TypeError, ValueError) as err:
            raise ProjectMemoryFormatError(
                f"Invalid hardware constraints schema: {err}"
            ) from err

    def update_hardware_constraints(self, constraints: HardwareConstraints) -> None:
        p = self.hardware_constraints_path
        payload: dict[str, Any] = {
            "target_device": constraints.target_device,
            "max_vram_mb": constraints.max_vram_mb,
            "max_ram_mb": constraints.max_ram_mb,
            "power_budget_watts": constraints.power_budget_watts,
            "latency_ceiling_ms": constraints.latency_ceiling_ms,
            "supported_precisions": list(constraints.supported_precisions),
        }
        content = yaml.dump(payload, sort_keys=False)
        p.write_text(content, encoding="utf-8")

    def get_architectural_decisions(self) -> tuple[ArchitecturalDecision, ...]:
        p = self.architectural_choices_path
        if not p.exists():
            return ()
        raw = p.read_text(encoding="utf-8")
        decisions: list[ArchitecturalDecision] = []
        blocks = raw.split("\n## ")
        for block in blocks[1:]:  # Skip intro before first ##
            lines = block.splitlines()
            if not lines:
                continue
            title = lines[0].strip()
            props: dict[str, str] = {}
            body_lines: list[str] = []
            is_body = False
            for line in lines[1:]:
                if line.startswith("- **") and ":** " in line and not is_body:
                    parts = line[4:].split(":** ", 1)
                    props[parts[0].strip().lower()] = parts[1].strip()
                elif line.strip() == "":
                    if props:
                        is_body = True
                else:
                    is_body = True
                    body_lines.append(line)

            decision_id = props.get("decision_id", title.split()[0])
            timestamp = props.get("timestamp", "")
            linked_str = props.get("linked_experiment_ids", "")
            linked_ids = (
                tuple(x.strip() for x in linked_str.split(",") if x.strip())
                if linked_str
                else ()
            )
            rationale = "\n".join(body_lines).strip()
            decisions.append(
                ArchitecturalDecision(
                    decision_id=decision_id,
                    timestamp=timestamp,
                    title=title,
                    rationale=rationale,
                    linked_experiment_ids=linked_ids,
                )
            )
        return tuple(decisions)

    def record_architectural_decision(self, decision: ArchitecturalDecision) -> None:
        p = self.architectural_choices_path
        existing = (
            p.read_text(encoding="utf-8")
            if p.exists()
            else "# Architectural Choices\n\n"
        )
        linked_str = ", ".join(decision.linked_experiment_ids)
        entry = (
            f"\n## {decision.decision_id} — {decision.title}\n"
            f"- **decision_id:** {decision.decision_id}\n"
            f"- **timestamp:** {decision.timestamp}\n"
            f"- **linked_experiment_ids:** {linked_str}\n\n"
            f"{decision.rationale.strip()}\n"
        )
        p.write_text(existing + entry, encoding="utf-8")

    def get_learned_heuristics(self) -> tuple[LearnedHeuristic, ...]:
        p = self.learned_heuristics_path
        if not p.exists():
            return ()
        raw = p.read_text(encoding="utf-8")
        heuristics: list[LearnedHeuristic] = []
        blocks = raw.split("\n## ")
        for block in blocks[1:]:
            lines = block.splitlines()
            if not lines:
                continue
            header = lines[0].strip()
            props: dict[str, str] = {}
            for line in lines[1:]:
                if line.startswith("- **") and ":** " in line:
                    parts = line[4:].split(":** ", 1)
                    props[parts[0].strip().lower()] = parts[1].strip()

            heuristic_id = props.get("heuristic_id", header.split()[0])
            context = props.get("context", "")
            observation = props.get("observation", "")
            recommendation = props.get("recommendation", "")
            evidence_str = props.get("evidence_experiment_ids", "")
            evidence_ids = (
                tuple(x.strip() for x in evidence_str.split(",") if x.strip())
                if evidence_str
                else ()
            )
            heuristics.append(
                LearnedHeuristic(
                    heuristic_id=heuristic_id,
                    context=context,
                    observation=observation,
                    recommendation=recommendation,
                    evidence_experiment_ids=evidence_ids,
                )
            )
        return tuple(heuristics)

    def append_learned_heuristic(self, heuristic: LearnedHeuristic) -> None:
        p = self.learned_heuristics_path
        existing = (
            p.read_text(encoding="utf-8") if p.exists() else "# Learned Heuristics\n\n"
        )
        evidence_str = ", ".join(heuristic.evidence_experiment_ids)
        entry = (
            f"\n## {heuristic.heuristic_id}\n"
            f"- **heuristic_id:** {heuristic.heuristic_id}\n"
            f"- **context:** {heuristic.context}\n"
            f"- **observation:** {heuristic.observation}\n"
            f"- **recommendation:** {heuristic.recommendation}\n"
            f"- **evidence_experiment_ids:** {evidence_str}\n"
        )
        p.write_text(existing + entry, encoding="utf-8")
