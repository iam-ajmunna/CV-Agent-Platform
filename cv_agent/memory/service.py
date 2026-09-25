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

import json
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from cv_agent.domain.identifiers import CapabilityId
from cv_agent.memory.interfaces import (
    ArchitecturalDecision,
    HardwareConstraints,
    LearnedHeuristic,
    ProjectMemoryError,
    ProjectMemoryReader,
    ProjectMemoryWriter,
    ProjectSpecification,
)
from cv_agent.reasoning.contracts import (
    CapabilityIntent,
    CVTaskDecompositionDAG,
    ProblemCharacterization,
    TaskNode,
    assert_zero_prohibited_models,
)


class WorkspaceContainmentError(ProjectMemoryError):
    """Raised when a path violates workspace containment."""


class ProjectMemoryFormatError(ProjectMemoryError):
    """Raised when structured memory files violate canonical schema."""


def _to_json_serializable(val: Any) -> Any:
    """Recursively convert mappingproxy, mappings, and sequences to JSON-serializable types."""
    if hasattr(val, "items"):
        return {str(k): _to_json_serializable(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set, frozenset)):
        return [_to_json_serializable(v) for v in val]
    return val


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

    @property
    def project_spec_path(self) -> Path:
        return self._validate_containment(self.memory_dir / "project_spec.md")

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

        max_vram_mb = data.get("max_vram_mb")
        if max_vram_mb is not None and (
            type(max_vram_mb) is not int or max_vram_mb <= 0
        ):
            raise ProjectMemoryFormatError(
                f"max_vram_mb must be a positive integer or None, got {max_vram_mb!r}"
            )

        max_ram_mb = data.get("max_ram_mb")
        if max_ram_mb is not None and (type(max_ram_mb) is not int or max_ram_mb <= 0):
            raise ProjectMemoryFormatError(
                f"max_ram_mb must be a positive integer or None, got {max_ram_mb!r}"
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

        precisions_raw = data.get("supported_precisions")
        supported_precisions: tuple[str, ...] = ()
        if precisions_raw is not None:
            if (
                not isinstance(precisions_raw, (list, tuple))
                or len(precisions_raw) == 0
            ):
                raise ProjectMemoryFormatError(
                    f"supported_precisions must be a non-empty sequence, got {precisions_raw!r}"
                )
            for prec in precisions_raw:
                if (
                    not isinstance(prec, str)
                    or isinstance(prec, bool)
                    or not prec.strip()
                ):
                    raise ProjectMemoryFormatError(
                        f"supported_precisions elements must be non-empty strings, got {prec!r}"
                    )
            supported_precisions = tuple(p.strip() for p in precisions_raw)

        return HardwareConstraints(
            target_device=target_device.strip(),
            max_vram_mb=max_vram_mb,
            max_ram_mb=max_ram_mb,
            power_budget_watts=power_budget_watts,
            latency_ceiling_ms=latency_ceiling_ms,
            supported_precisions=supported_precisions,
        )

    def update_hardware_constraints(self, constraints: HardwareConstraints) -> None:
        if (
            not isinstance(constraints.target_device, str)
            or not constraints.target_device.strip()
        ):
            raise ProjectMemoryFormatError("target_device cannot be empty.")
        if constraints.max_vram_mb is not None and constraints.max_vram_mb <= 0:
            raise ProjectMemoryFormatError("max_vram_mb must be positive.")
        if constraints.max_ram_mb is not None and constraints.max_ram_mb <= 0:
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
        for prec in constraints.supported_precisions:
            if not isinstance(prec, str) or not prec.strip():
                raise ProjectMemoryFormatError(
                    f"supported_precisions elements must be non-empty strings, got {prec!r}"
                )

        p = self.hardware_constraints_path
        payload: dict[str, Any] = {
            "target_device": constraints.target_device,
            "max_vram_mb": constraints.max_vram_mb,
            "max_ram_mb": constraints.max_ram_mb,
            "power_budget_watts": constraints.power_budget_watts,
            "latency_ceiling_ms": constraints.latency_ceiling_ms,
            "supported_precisions": (
                list(constraints.supported_precisions)
                if constraints.supported_precisions
                else None
            ),
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

    def _format_project_spec_markdown(self, spec: ProjectSpecification) -> str:
        """Format ProjectSpecification into canonical project_spec.md content."""
        char = spec.characterization
        dag = spec.decomposition
        lines = [
            "# Project Specification",
            "",
            "## Problem Characterization",
            f"- **task_type:** {char.task_type}",
            f"- **operational_definition:** {char.operational_definition}",
            f"- **physical_environment:** {char.physical_environment}",
            f"- **camera_infrastructure:** {char.camera_infrastructure}",
            f"- **target_hardware:** {char.target_hardware}",
            f"- **latency_budget_ms:** {char.latency_budget_ms}",
            f"- **throughput_fps:** {char.throughput_fps}",
            f"- **error_tolerance:** {char.error_tolerance}",
            f"- **data_availability:** {char.data_availability}",
            f"- **intervention_modality:** {char.intervention_modality}",
            "",
            "## CV Task Decomposition",
        ]
        for task in dag.tasks:
            deps_str = ", ".join(task.dependencies) if task.dependencies else "none"
            inputs_str = ", ".join(task.inputs) if task.inputs else "none"
            outputs_str = ", ".join(task.outputs) if task.outputs else "none"
            lines.extend(
                [
                    f"### {task.task_id} — {task.task_type}",
                    f"- **description:** {task.description}",
                    f"- **dependencies:** {deps_str}",
                    f"- **inputs:** {inputs_str}",
                    f"- **outputs:** {outputs_str}",
                    "",
                ]
            )
        if hasattr(spec, "capability_intents") and spec.capability_intents:
            lines.extend(["## Required Capabilities", ""])
            for ci in spec.capability_intents:
                target_pref_str = (
                    ci.target_preference if ci.target_preference else "none"
                )
                param_str = json.dumps(
                    _to_json_serializable(ci.parameter_intent), sort_keys=True
                )
                lines.extend(
                    [
                        f"### {ci.capability_id}",
                        f"- **intent_description:** {ci.intent_description}",
                        f"- **target_preference:** {target_pref_str}",
                        f"- **parameter_intent:** {param_str}",
                        "",
                    ]
                )
        if spec.dataset_lineage_notes:
            lines.extend(
                [
                    "## Dataset Lineage Notes",
                    spec.dataset_lineage_notes,
                    "",
                ]
            )
        return "\n".join(lines).strip() + "\n"

    def commit_project_specification(self, spec: ProjectSpecification) -> None:
        """Atomically commit project_spec.md, hardware_constraints.yaml, and architectural_choices.md.

        Enforces all 5 Phase 4 validation invariants:
        1. Characterization completeness across all 8 canonical dimensions.
        2. Valid, non-empty, acyclic CV task DAG.
        3. Valid HardwareConstraints budgets.
        4. Prohibited models check across all characterization, tasks, and choices.
        5. Atomic multi-file write with transactional rollback on failure.
        """
        from cv_agent.reasoning.errors import MemoryTransactionValidationError

        # Validation 1: Characterization completeness
        if not spec.characterization or not spec.characterization.is_well_formed:
            raise MemoryTransactionValidationError(
                "Cannot commit project specification: problem characterization is incomplete or not well-formed across the 8 canonical dimensions."
            )

        # Validation 2: DAG validation
        if not spec.decomposition or not spec.decomposition.tasks:
            raise MemoryTransactionValidationError(
                "Cannot commit project specification: decomposition DAG is missing or empty."
            )

        # Validation 3: Hardware constraints validation
        hw = spec.hardware_constraints
        if not hw or not hw.target_device or not hw.target_device.strip():
            raise MemoryTransactionValidationError(
                "Cannot commit project specification: target_device cannot be empty."
            )
        if hw.max_vram_mb is not None and hw.max_vram_mb <= 0:
            raise MemoryTransactionValidationError(
                "Cannot commit project specification: memory budgets must be positive integers."
            )
        if hw.max_ram_mb is not None and hw.max_ram_mb <= 0:
            raise MemoryTransactionValidationError(
                "Cannot commit project specification: memory budgets must be positive integers."
            )
        if hw.power_budget_watts is not None and hw.power_budget_watts <= 0:
            raise MemoryTransactionValidationError(
                "Cannot commit project specification: power_budget_watts must be positive."
            )
        if hw.latency_ceiling_ms is not None and hw.latency_ceiling_ms <= 0:
            raise MemoryTransactionValidationError(
                "Cannot commit project specification: latency_ceiling_ms must be positive."
            )
        for prec in hw.supported_precisions:
            if not isinstance(prec, str) or not prec.strip():
                raise MemoryTransactionValidationError(
                    f"supported_precisions elements must be non-empty strings, got {prec!r}"
                )

        # Validation for capability intents (B27)
        if hasattr(spec, "capability_intents") and spec.capability_intents:
            from cv_agent.capabilities.registry import load_default_capability_registry

            cap_reg = load_default_capability_registry()
            for ci in spec.capability_intents:
                cap_def = cap_reg.get_capability(ci.capability_id)
                if cap_def is None:
                    raise MemoryTransactionValidationError(
                        f"Cannot commit project specification: capability {ci.capability_id!r} is not registered in authoritative ADR-0001 registry."
                    )

        # Validation 4: Prohibited models check

        for dim_val in (
            spec.characterization.task_type,
            spec.characterization.operational_definition,
            spec.characterization.physical_environment,
            spec.characterization.camera_infrastructure,
            spec.characterization.target_hardware,
            spec.characterization.error_tolerance,
            spec.characterization.data_availability,
            spec.characterization.intervention_modality,
        ):
            try:
                assert_zero_prohibited_models(
                    dim_val, "ProblemCharacterization dimension"
                )
            except Exception as exc:
                raise MemoryTransactionValidationError(str(exc)) from exc

        for task in spec.decomposition.tasks:
            try:
                assert_zero_prohibited_models(
                    task.description, f"task node '{task.task_id}' description"
                )
            except Exception as exc:
                raise MemoryTransactionValidationError(str(exc)) from exc

        for arch in spec.architectural_choices:
            try:
                assert_zero_prohibited_models(
                    arch.title, f"architectural decision '{arch.decision_id}' title"
                )
                assert_zero_prohibited_models(
                    arch.rationale,
                    f"architectural decision '{arch.decision_id}' rationale",
                )
            except Exception as exc:
                raise MemoryTransactionValidationError(str(exc)) from exc

        # Format and validate formatted strings
        spec_content = self._format_project_spec_markdown(spec)
        assert_zero_prohibited_models(spec_content, "formatted project_spec.md")

        hw_payload: dict[str, Any] = {
            "target_device": hw.target_device,
            "max_vram_mb": hw.max_vram_mb,
            "max_ram_mb": hw.max_ram_mb,
            "power_budget_watts": hw.power_budget_watts,
            "latency_ceiling_ms": hw.latency_ceiling_ms,
            "supported_precisions": (
                list(hw.supported_precisions) if hw.supported_precisions else None
            ),
        }
        hw_content = yaml.dump(hw_payload, sort_keys=False)
        assert_zero_prohibited_models(hw_content, "formatted hardware_constraints.yaml")

        arch_lines = ["# Architectural Choices", ""]
        for arch in spec.architectural_choices:
            linked_str = ", ".join(arch.linked_experiment_ids)
            arch_lines.extend(
                [
                    f"## {arch.decision_id} — {arch.title}",
                    f"- **decision_id:** {arch.decision_id}",
                    f"- **timestamp:** {arch.timestamp}",
                    f"- **linked_experiment_ids:** {linked_str}",
                    "",
                    arch.rationale.strip(),
                    "",
                ]
            )
        arch_content = "\n".join(arch_lines).strip() + "\n"
        assert_zero_prohibited_models(
            arch_content, "formatted architectural_choices.md"
        )

        # Validation 5: Atomic multi-file write with rollback
        spec_path = self.project_spec_path
        hw_path = self.hardware_constraints_path
        arch_path = self.architectural_choices_path

        backups: dict[Path, str | None] = {
            spec_path: (
                spec_path.read_text(encoding="utf-8") if spec_path.exists() else None
            ),
            hw_path: (
                hw_path.read_text(encoding="utf-8") if hw_path.exists() else None
            ),
            arch_path: (
                arch_path.read_text(encoding="utf-8") if arch_path.exists() else None
            ),
        }

        spec_tmp = spec_path.with_suffix(".md.tmp")
        hw_tmp = hw_path.with_suffix(".yaml.tmp")
        arch_tmp = arch_path.with_suffix(".md.tmp")

        try:
            spec_tmp.write_text(spec_content, encoding="utf-8")
            hw_tmp.write_text(hw_content, encoding="utf-8")
            arch_tmp.write_text(arch_content, encoding="utf-8")

            spec_tmp.replace(spec_path)
            hw_tmp.replace(hw_path)
            arch_tmp.replace(arch_path)
        except Exception as exc:
            # Atomic rollback on any failure
            for p, original_content in backups.items():
                if original_content is not None:
                    p.write_text(original_content, encoding="utf-8")
                elif p.exists():
                    p.unlink()
            for tmp_p in (spec_tmp, hw_tmp, arch_tmp):
                if tmp_p.exists():
                    tmp_p.unlink()
            raise MemoryTransactionValidationError(
                f"Atomic memory transaction failed: {exc}"
            ) from exc

    def get_project_specification(self) -> ProjectSpecification | None:
        """Retrieve the committed project specification, or None if uncommitted."""
        p = self.project_spec_path
        if not p.exists():
            return None
        raw = p.read_text(encoding="utf-8")
        if not raw.strip() or raw.strip() == "# Project Spec":
            return None

        # Split into sections by top-level ## headers
        sections: dict[str, str] = {}
        current_section = ""
        current_lines: list[str] = []
        for line in raw.splitlines():
            if line.startswith("## "):
                if current_section:
                    sections[current_section] = "\n".join(current_lines)
                current_section = line[3:].strip()
                current_lines = []
            elif current_section:
                current_lines.append(line)
        if current_section:
            sections[current_section] = "\n".join(current_lines)

        # Parse Problem Characterization section
        char_raw = sections.get("Problem Characterization", "")
        props: dict[str, str] = {}
        for line in char_raw.splitlines():
            if line.startswith("- **") and ":** " in line:
                k, v = line[4:].split(":** ", 1)
                props[k.strip().lower()] = v.strip()

        lat_str = props.get("latency_budget_ms")
        latency_val = (
            float(lat_str) if lat_str and lat_str not in ("None", "") else None
        )
        fps_str = props.get("throughput_fps")
        fps_val = float(fps_str) if fps_str and fps_str not in ("None", "") else None

        char = ProblemCharacterization(
            task_type=props.get("task_type", ""),
            target_hardware=props.get("target_hardware", ""),
            operational_definition=props.get("operational_definition", ""),
            physical_environment=props.get("physical_environment", ""),
            camera_infrastructure=props.get("camera_infrastructure", ""),
            error_tolerance=props.get("error_tolerance", ""),
            data_availability=props.get("data_availability", ""),
            intervention_modality=props.get("intervention_modality", ""),
            latency_budget_ms=latency_val,
            throughput_fps=fps_val,
        )

        # Parse CV Task DAG Decomposition section
        dag_raw = (
            sections.get("CV Task Decomposition")
            or sections.get("CV Task DAG Decomposition")
            or ""
        )
        tasks: list[TaskNode] = []
        if "### " in dag_raw:
            for block in dag_raw.split("### "):
                block = block.strip()
                if not block:
                    continue
                lines = block.splitlines()
                header = lines[0].strip()
                if " — " in header:
                    tid, ttype = header.split(" — ", 1)
                else:
                    tid, ttype = header, "object_detection"
                tid = tid.strip()
                ttype = ttype.strip()

                t_props: dict[str, str] = {}
                for line in lines[1:]:
                    if line.startswith("- **") and ":** " in line:
                        k, v = line[4:].split(":** ", 1)
                        t_props[k.strip().lower()] = v.strip()

                tdesc = t_props.get("description", "")
                deps_raw = t_props.get("dependencies", "")
                deps = (
                    ()
                    if deps_raw in ("", "none")
                    else tuple(x.strip() for x in deps_raw.split(","))
                )
                inputs_raw = t_props.get("inputs", "")
                inputs = (
                    ()
                    if inputs_raw in ("", "none")
                    else tuple(x.strip() for x in inputs_raw.split(","))
                )
                outputs_raw = t_props.get("outputs", "")
                outputs = (
                    ()
                    if outputs_raw in ("", "none")
                    else tuple(x.strip() for x in outputs_raw.split(","))
                )

                tasks.append(
                    TaskNode(
                        task_id=tid,
                        task_type=ttype,
                        description=tdesc,
                        dependencies=deps,
                        inputs=inputs,
                        outputs=outputs,
                    )
                )

        if not tasks:
            return None
        dag = CVTaskDecompositionDAG(tasks=tuple(tasks))
        try:
            hw = self.get_hardware_constraints()
        except (ProjectMemoryError, KeyError):
            return None
        arch = self.get_architectural_decisions()

        # Parse Required Capabilities section (lossless round-trip per B27)
        cap_raw = sections.get("Required Capabilities", "")
        intents: list[CapabilityIntent] = []
        if cap_raw:
            if "### " in cap_raw:
                for block in cap_raw.split("### "):
                    block = block.strip()
                    if not block:
                        continue
                    block_lines = block.splitlines()
                    cid_str = block_lines[0].strip()
                    if not cid_str:
                        continue
                    props_c: dict[str, str] = {}
                    for line in block_lines[1:]:
                        if line.startswith("- **") and ":** " in line:
                            k, v = line[4:].split(":** ", 1)
                            props_c[k.strip().lower()] = v.strip()
                    idesc = props_c.get("intent_description", "")
                    target_pref_raw = props_c.get("target_preference", "none")
                    target_pref = (
                        None
                        if target_pref_raw in ("none", "", "null")
                        else target_pref_raw
                    )
                    param_raw = props_c.get("parameter_intent", "{}")
                    try:
                        param_dict = json.loads(param_raw) if param_raw else {}
                    except json.JSONDecodeError:
                        param_dict = {}
                    intents.append(
                        CapabilityIntent(
                            capability_id=CapabilityId(cid_str),
                            intent_description=idesc,
                            parameter_intent=param_dict,
                            target_preference=target_pref,
                        )
                    )
            else:
                for line in cap_raw.splitlines():
                    if line.startswith("- **") and ":** " in line:
                        cid_str, idesc = line[4:].split(":** ", 1)
                        intents.append(
                            CapabilityIntent(
                                capability_id=CapabilityId(cid_str.strip()),
                                intent_description=idesc.strip(),
                                parameter_intent={},
                                target_preference=None,
                            )
                        )

        dataset_notes = sections.get("Dataset Lineage Notes", "").strip()

        return ProjectSpecification(
            characterization=char,
            decomposition=dag,
            hardware_constraints=hw,
            capability_intents=tuple(intents),
            architectural_choices=arch,
            dataset_lineage_notes=dataset_notes,
        )
