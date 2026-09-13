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
            raw_content = p.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_content)
        except yaml.YAMLError as err:
            raise ProjectMemoryFormatError(f"YAML syntax error in {p}: {err}") from err

        if not isinstance(data, dict):
            raise ProjectMemoryFormatError(
                f"Expected YAML mapping in {p}, got {type(data).__name__}"
            )

        allowed_keys = {
            "target_device",
            "max_vram_mb",
            "max_ram_mb",
            "power_budget_watts",
            "latency_ceiling_ms",
            "supported_precisions",
        }
        extra_keys = set(data.keys()) - allowed_keys
        if extra_keys:
            raise ProjectMemoryFormatError(
                f"Unexpected keys in hardware constraints {p}: {sorted(extra_keys)}"
            )

        required_keys = allowed_keys
        missing_keys = required_keys - set(data.keys())
        if missing_keys:
            raise ProjectMemoryFormatError(
                f"Missing required keys in hardware constraints {p}: {sorted(missing_keys)}"
            )

        target_device = data["target_device"]
        if (
            not isinstance(target_device, str)
            or isinstance(target_device, bool)
            or not target_device.strip()
        ):
            raise ProjectMemoryFormatError(
                f"target_device must be a non-empty string, got {target_device!r}"
            )

        max_vram_mb = data["max_vram_mb"]
        if type(max_vram_mb) is not int or max_vram_mb <= 0:
            raise ProjectMemoryFormatError(
                f"max_vram_mb must be a positive integer, got {max_vram_mb!r}"
            )

        max_ram_mb = data["max_ram_mb"]
        if type(max_ram_mb) is not int or max_ram_mb <= 0:
            raise ProjectMemoryFormatError(
                f"max_ram_mb must be a positive integer, got {max_ram_mb!r}"
            )

        power_budget_watts = data["power_budget_watts"]
        if power_budget_watts is not None:
            if (
                type(power_budget_watts) not in (int, float)
                or isinstance(power_budget_watts, bool)
                or power_budget_watts <= 0
            ):
                raise ProjectMemoryFormatError(
                    f"power_budget_watts must be a positive number or None, got {power_budget_watts!r}"
                )
            power_budget_watts = float(power_budget_watts)

        latency_ceiling_ms = data["latency_ceiling_ms"]
        if latency_ceiling_ms is not None:
            if (
                type(latency_ceiling_ms) not in (int, float)
                or isinstance(latency_ceiling_ms, bool)
                or latency_ceiling_ms <= 0
            ):
                raise ProjectMemoryFormatError(
                    f"latency_ceiling_ms must be a positive number or None, got {latency_ceiling_ms!r}"
                )
            latency_ceiling_ms = float(latency_ceiling_ms)

        precisions_raw = data["supported_precisions"]
        if not isinstance(precisions_raw, (list, tuple)) or len(precisions_raw) == 0:
            raise ProjectMemoryFormatError(
                f"supported_precisions must be a non-empty sequence, got {precisions_raw!r}"
            )
        for prec in precisions_raw:
            if not isinstance(prec, str) or isinstance(prec, bool) or not prec.strip():
                raise ProjectMemoryFormatError(
                    f"supported_precisions elements must be non-empty strings, got {prec!r}"
                )

        return HardwareConstraints(
            target_device=target_device.strip(),
            max_vram_mb=max_vram_mb,
            max_ram_mb=max_ram_mb,
            power_budget_watts=power_budget_watts,
            latency_ceiling_ms=latency_ceiling_ms,
            supported_precisions=tuple(p.strip() for p in precisions_raw),
        )

    def update_hardware_constraints(self, constraints: HardwareConstraints) -> None:
        if (
            not isinstance(constraints.target_device, str)
            or not constraints.target_device.strip()
        ):
            raise ProjectMemoryFormatError("target_device cannot be empty.")
        if constraints.max_vram_mb <= 0:
            raise ProjectMemoryFormatError("max_vram_mb must be positive.")
        if constraints.max_ram_mb <= 0:
            raise ProjectMemoryFormatError("max_ram_mb must be positive.")
        if (
            constraints.power_budget_watts is not None
            and constraints.power_budget_watts <= 0
        ):
            raise ProjectMemoryFormatError("power_budget_watts must be positive.")
        if (
            constraints.latency_ceiling_ms is not None
            and constraints.latency_ceiling_ms <= 0
        ):
            raise ProjectMemoryFormatError("latency_ceiling_ms must be positive.")
        if (
            not constraints.supported_precisions
            or len(constraints.supported_precisions) == 0
        ):
            raise ProjectMemoryFormatError("supported_precisions cannot be empty.")

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
        seen_ids: set[str] = set()
        blocks = raw.split("\n## ")
        for block in blocks[1:]:  # Skip intro before first ##
            lines = block.splitlines()
            if not lines:
                continue
            header = lines[0].strip()
            if " — " in header:
                title = header.split(" — ", 1)[1].strip()
            else:
                title = header

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

            decision_id = props.get("decision_id")
            timestamp = props.get("timestamp")
            rationale = "\n".join(body_lines).strip()

            if not decision_id:
                raise ProjectMemoryFormatError(
                    f"Malformed architectural decision in {p}: missing required 'decision_id'."
                )
            if not timestamp:
                raise ProjectMemoryFormatError(
                    f"Malformed architectural decision in {p}: missing required 'timestamp'."
                )
            if not title:
                raise ProjectMemoryFormatError(
                    f"Malformed architectural decision in {p}: missing required 'title'."
                )
            if not rationale:
                raise ProjectMemoryFormatError(
                    f"Malformed architectural decision in {p}: missing required 'rationale'."
                )

            if decision_id in seen_ids:
                raise ProjectMemoryFormatError(
                    f"Duplicate architectural decision ID detected: {decision_id!r}"
                )
            seen_ids.add(decision_id)

            linked_str = props.get("linked_experiment_ids", "")
            linked_ids = (
                tuple(x.strip() for x in linked_str.split(",") if x.strip())
                if linked_str
                else ()
            )
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
        if (
            not isinstance(decision.decision_id, str)
            or not decision.decision_id.strip()
        ):
            raise ProjectMemoryFormatError("decision_id cannot be empty.")
        if not isinstance(decision.timestamp, str) or not decision.timestamp.strip():
            raise ProjectMemoryFormatError("timestamp cannot be empty.")
        if not isinstance(decision.title, str) or not decision.title.strip():
            raise ProjectMemoryFormatError("title cannot be empty.")
        if not isinstance(decision.rationale, str) or not decision.rationale.strip():
            raise ProjectMemoryFormatError("rationale cannot be empty.")

        existing_decisions = self.get_architectural_decisions()
        if any(d.decision_id == decision.decision_id for d in existing_decisions):
            raise ProjectMemoryFormatError(
                f"Architectural decision {decision.decision_id!r} already exists."
            )

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
        seen_ids: set[str] = set()
        blocks = raw.split("\n## ")
        for block in blocks[1:]:
            lines = block.splitlines()
            if not lines:
                continue
            props: dict[str, str] = {}
            for line in lines[1:]:
                if line.startswith("- **") and ":** " in line:
                    parts = line[4:].split(":** ", 1)
                    props[parts[0].strip().lower()] = parts[1].strip()

            heuristic_id = props.get("heuristic_id")
            context = props.get("context")
            observation = props.get("observation")
            recommendation = props.get("recommendation")

            if not heuristic_id:
                raise ProjectMemoryFormatError(
                    f"Malformed learned heuristic in {p}: missing required 'heuristic_id'."
                )
            if not context:
                raise ProjectMemoryFormatError(
                    f"Malformed learned heuristic in {p}: missing required 'context'."
                )
            if not observation:
                raise ProjectMemoryFormatError(
                    f"Malformed learned heuristic in {p}: missing required 'observation'."
                )
            if not recommendation:
                raise ProjectMemoryFormatError(
                    f"Malformed learned heuristic in {p}: missing required 'recommendation'."
                )

            if heuristic_id in seen_ids:
                raise ProjectMemoryFormatError(
                    f"Duplicate learned heuristic ID detected: {heuristic_id!r}"
                )
            seen_ids.add(heuristic_id)

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
        if (
            not isinstance(heuristic.heuristic_id, str)
            or not heuristic.heuristic_id.strip()
        ):
            raise ProjectMemoryFormatError("heuristic_id cannot be empty.")
        if not isinstance(heuristic.context, str) or not heuristic.context.strip():
            raise ProjectMemoryFormatError("context cannot be empty.")
        if (
            not isinstance(heuristic.observation, str)
            or not heuristic.observation.strip()
        ):
            raise ProjectMemoryFormatError("observation cannot be empty.")
        if (
            not isinstance(heuristic.recommendation, str)
            or not heuristic.recommendation.strip()
        ):
            raise ProjectMemoryFormatError("recommendation cannot be empty.")

        existing_heuristics = self.get_learned_heuristics()
        if any(h.heuristic_id == heuristic.heuristic_id for h in existing_heuristics):
            raise ProjectMemoryFormatError(
                f"Learned heuristic {heuristic.heuristic_id!r} already exists."
            )

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
