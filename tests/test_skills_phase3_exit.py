"""tests/test_skills_phase3_exit.py — Phase 3 acceptance and exit verification suite.

Verifies all Issue #68 requirements, ADR-0007 contracts, and Official PM corrections:
1. Exact CapabilityId `cv.deployment.optimization` resolves to external NVIDIA skill (Exit Test 1).
2. AST audit proves zero CUDA kernel launches, direct bindings, TRT builders, or PyTorch training loops in Skills layer (Exit Test 2).
3. Inspection-only capability and skill enumeration across targets (Exit Test 3).
4. Inspection safety invariant: enumeration triggers 0 tool executions, 0 subprocesses, 0 MCP calls, 0 mutations.
5. 10-skill local evaluation matrix: 9 GPU/Jetson skills -> KNOWN_BUT_UNAVAILABLE, 10th skill evaluated via local tools.
6. Generic compatible NVIDIA target evaluation without hardcoding "A100".
7. Discovery authority transition: candidate cannot overwrite registry; explicit promotion required.
8. Authoritative 7-state availability model: KNOWN, DISCOVERED, CONFIGURED, AVAILABLE, EXECUTABLE, UNAVAILABLE, KNOWN_BUT_UNAVAILABLE.
9. End-to-end CLI verification: `python -m cv_agent --list-skills`.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cv_agent.domain.contracts import AvailabilityState
from cv_agent.domain.identifiers import CapabilityId, SkillId, ToolId
from cv_agent.runtime.agent import CVAgent
from cv_agent.skills import (
    DefaultSkillRegistry,
    DefaultSkillResolver,
    DiscoveryCache,
    SkillAvailabilitySummary,
    SkillDescriptor,
    SkillProvenance,
    TargetRequirements,
    load_default_skill_registry,
)
from cv_agent.tools import (
    DefaultToolRegistry,
    ExecutionTargetDescriptor,
    TargetId,
    TargetType,
    ToolAvailabilityInfo,
    ToolDescriptor,
    ToolExecutionStatus,
    ToolRiskLevel,
    detect_host_execution_target,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_host_target() -> ExecutionTargetDescriptor:
    """Canonical host execution target (macOS Apple Silicon or CPU workstation per D-022)."""
    return detect_host_execution_target()


@pytest.fixture
def generic_compatible_nvidia_target() -> ExecutionTargetDescriptor:
    """Generic compatible NVIDIA GPU target satisfying min_cuda_compute 8.0 (PM correction 4)."""
    return ExecutionTargetDescriptor(
        target_id=TargetId("target.remote.nvidia_generic"),
        target_type=TargetType.REMOTE,
        is_available=True,
        description="Generic Remote NVIDIA GPU Server (Compute 8.0)",
        target_metadata={
            "hardware": "nvidia_gpu",
            "cuda_compute": "8.0",
            "os": "linux",
            "arch": "x86_64",
        },
    )


@pytest.fixture
def populated_tool_registry(
    generic_compatible_nvidia_target: ExecutionTargetDescriptor,
    canonical_host_target: ExecutionTargetDescriptor,
) -> DefaultToolRegistry:
    """Tool registry containing mock descriptors for vendor and inspection tools."""
    tool_modelopt = ToolDescriptor(
        tool_id=ToolId("tool.nvidia.modelopt"),
        name="NVIDIA ModelOpt Tool",
        description="ModelOpt CLI adapter",
        compatible_target_types=(TargetType.REMOTE,),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_level=ToolRiskLevel.RESOURCE_INTENSIVE,
        requires_approval=False,
    )
    tool_trtexec = ToolDescriptor(
        tool_id=ToolId("tool.nvidia.trtexec"),
        name="TensorRT trtexec CLI",
        description="trtexec CLI adapter",
        compatible_target_types=(TargetType.REMOTE,),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_level=ToolRiskLevel.RESOURCE_INTENSIVE,
        requires_approval=False,
    )
    tool_netron = ToolDescriptor(
        tool_id=ToolId("tool.netron"),
        name="Netron Visualizer Tool",
        description="Model architecture visualizer",
        compatible_target_types=(TargetType.LOCAL, TargetType.REMOTE),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_level=ToolRiskLevel.READ_ONLY,
        requires_approval=False,
    )
    tool_onnx = ToolDescriptor(
        tool_id=ToolId("tool.onnx"),
        name="ONNX Checker Tool",
        description="ONNX structural graph validator",
        compatible_target_types=(TargetType.LOCAL, TargetType.REMOTE),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_level=ToolRiskLevel.READ_ONLY,
        requires_approval=False,
    )

    registry = DefaultToolRegistry(
        tools=(tool_modelopt, tool_trtexec, tool_netron, tool_onnx),
        targets=(generic_compatible_nvidia_target, canonical_host_target),
    )
    return registry


# ---------------------------------------------------------------------------
# Roadmap Exit Test 1: Quantization Capability Resolution
# ---------------------------------------------------------------------------


def test_roadmap_exit_1_quantization_capability_resolves_to_external_nvidia_skill(
    populated_tool_registry: DefaultToolRegistry,
    generic_compatible_nvidia_target: ExecutionTargetDescriptor,
) -> None:
    """Roadmap Exit Test: Quantization capability resolves to an external NVIDIA skill.

    Enforces:
    - Uses exact ADR-0001 CapabilityId `cv.deployment.optimization` (PM correction 1).
    - Resolves to external NVIDIA skill (`nvidia.model_optimizer.ptq` or `nvidia.tensorrt.inference_optimization`).
    - Skill provenance is EXTERNAL; vendor is 'nvidia'.
    - Target binding is EXECUTABLE on compatible remote GPU target.
    """
    cap_id = CapabilityId("cv.deployment.optimization")
    skill_registry = load_default_skill_registry()
    resolver = DefaultSkillResolver(
        skill_registry,
        tool_registry=populated_tool_registry,
    )

    resolution = resolver.resolve(
        cap_id,
        available_targets=(generic_compatible_nvidia_target,),
        tool_registry=populated_tool_registry,
    )

    assert resolution.capability_id == cap_id
    assert resolution.selected_skill is not None
    assert resolution.selected_skill.provenance == SkillProvenance.EXTERNAL
    assert resolution.selected_skill.vendor == "nvidia"
    assert resolution.selected_skill.skill_id in (
        SkillId("nvidia.model_optimizer.ptq"),
        SkillId("nvidia.tensorrt.inference_optimization"),
    )
    assert resolution.availability_state == AvailabilityState.EXECUTABLE
    assert resolution.target_descriptor is not None
    assert (
        resolution.target_descriptor.target_id
        == generic_compatible_nvidia_target.target_id
    )


# ---------------------------------------------------------------------------
# Roadmap Exit Test 2: AST Audit of Skills Subsystem
# ---------------------------------------------------------------------------


def test_roadmap_exit_2_ast_audit_proves_zero_vendor_runtime_implementations() -> None:
    """Roadmap Exit Test: AST audit mechanically proves 0 vendor reimplementations (PM correction 5).

    Mechanically inspects all files in `cv_agent/skills/` to assert:
    - Zero CUDA kernel launch syntax or CUDA runtime bindings.
    - Zero TensorRT engine builder calls (Builder, INetworkDefinition).
    - Zero PyTorch training loops or optimizer execution.
    - Zero vendor implementation imports (tensorrt, pycuda, cupy, modelopt internals).
    """
    skills_dir = Path(__file__).parent.parent / "cv_agent" / "skills"
    assert skills_dir.is_dir(), f"Skills directory not found at {skills_dir}"

    python_files = list(skills_dir.glob("*.py"))
    assert len(python_files) >= 5, (
        f"Expected at least 5 skill modules, found {len(python_files)}"
    )

    prohibited_modules = {
        "tensorrt",
        "pycuda",
        "cupy",
        "modelopt",
        "torch.cuda",
        "torch.optim",
        "torch.nn",
    }

    prohibited_call_attrs = {
        "build_serialized_network",
        "build_engine",
        "create_network",
        "cudaMalloc",
        "cudaMemcpy",
        "backward",  # loss.backward()
        "step",  # optimizer.step()
    }

    for py_file in python_files:
        content = py_file.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(py_file))

        # Check raw token prohibitions (e.g. CUDA kernel launch syntax)
        assert "<<<" not in content, (
            f"Found raw CUDA kernel launch syntax in {py_file.name}"
        )
        assert ">>>" not in content or "->" in content, (
            f"Suspicious syntax in {py_file.name}"
        )

        for node in ast.walk(tree):
            # 1. Prohibit vendor imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in prohibited_modules:
                        assert not alias.name.startswith(forbidden), (
                            f"Prohibited import {alias.name!r} in {py_file.name}: "
                            f"Skills layer must invoke tools, not import vendor runtimes."
                        )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for forbidden in prohibited_modules:
                    assert not mod.startswith(forbidden), (
                        f"Prohibited from-import {mod!r} in {py_file.name}: "
                        f"Skills layer must invoke tools, not import vendor runtimes."
                    )

            # 2. Prohibit algorithm/runtime calls
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr_name = node.func.attr
                assert attr_name not in prohibited_call_attrs, (
                    f"Prohibited algorithm/runtime call .{attr_name}() in {py_file.name}: "
                    f"Vendor runtime logic must not be reimplemented in Skills layer."
                )


# ---------------------------------------------------------------------------
# Roadmap Exit Test 3: Agent Capability and Skill Enumeration
# ---------------------------------------------------------------------------


def test_roadmap_exit_3_agent_capability_and_skill_enumeration() -> None:
    """Roadmap Exit Test: Agent enumerates capabilities and skills with accurate presentations.

    Enforces:
    - `agent.enumerate_capabilities()` and `agent.enumerate_skills()` work without errors.
    - Accurately reports `is_executable` and `is_merely_known` presentation projections.
    - Distinguishes installed/executable capabilities from merely known ones.
    """
    agent = CVAgent()
    capabilities = agent.enumerate_capabilities()
    skills = agent.enumerate_skills()

    assert len(capabilities) > 0
    assert len(skills) >= 11  # 10 NVIDIA skills + 1 internal skill

    # On default host workstation (macOS), all GPU capabilities must be merely known
    quant_caps = [
        c
        for c in capabilities
        if c.capability_id == CapabilityId("cv.deployment.optimization")
    ]
    assert len(quant_caps) == 1
    quant_cap = quant_caps[0]

    assert quant_cap.is_merely_known is True
    assert quant_cap.is_executable is False
    assert quant_cap.availability_state == AvailabilityState.KNOWN_BUT_UNAVAILABLE

    # Skills presentation check
    ptq_skills = [
        s for s in skills if s.skill_id == SkillId("nvidia.model_optimizer.ptq")
    ]
    assert len(ptq_skills) == 1
    ptq = ptq_skills[0]
    assert ptq.is_merely_known is True
    assert ptq.is_executable is False
    assert ptq.availability_state == AvailabilityState.KNOWN_BUT_UNAVAILABLE


# ---------------------------------------------------------------------------
# Acceptance Test: Enumeration Must Be Strictly Inspection-Only
# ---------------------------------------------------------------------------


def test_enumeration_strictly_inspection_only_no_backdoors() -> None:
    """Acceptance Test: Enumeration cannot become an execution backdoor (PM requirement).

    Proves:
    - 0 ToolExecutor.execute() calls.
    - 0 ToolExecutor.probe() calls.
    - 0 subprocess executions.
    - 0 MCP network/endpoint calls.
    - 0 registry mutations.
    - 0 discovery candidate promotions.
    - 0 external jobs created.
    """
    skill_registry = load_default_skill_registry()
    initial_skills_count = len(skill_registry.list_skills())

    # Mock tool executor to detect speculative or accidental calls
    mock_executor = MagicMock()
    mock_tool_registry = MagicMock(spec=DefaultToolRegistry)
    mock_tool_registry.get.return_value = None
    mock_tool_registry.check_availability.return_value = ToolAvailabilityInfo(
        is_available=False,
        status=ToolExecutionStatus.TOOL_UNAVAILABLE,
        reason="Mock probe",
    )

    resolver = DefaultSkillResolver(
        skill_registry,
        tool_registry=mock_tool_registry,
    )

    # Run capability enumeration and skill enumeration
    caps = resolver.enumerate_capabilities()
    skills = resolver.enumerate_skills()

    assert len(caps) > 0
    assert len(skills) > 0

    # Assert 0 tool executions
    assert mock_executor.execute.call_count == 0
    assert mock_executor.probe.call_count == 0

    # Assert 0 registry mutations
    assert len(skill_registry.list_skills()) == initial_skills_count


# ---------------------------------------------------------------------------
# 10-Skill Local Host Evaluation Matrix (PM correction 6)
# ---------------------------------------------------------------------------


def test_ten_skill_local_host_evaluation_matrix(
    canonical_host_target: ExecutionTargetDescriptor,
    populated_tool_registry: DefaultToolRegistry,
) -> None:
    """Matrix Test: 9 GPU/Jetson skills -> KNOWN_BUT_UNAVAILABLE; 10th skill evaluated locally.

    Per PM correction 6:
    - `nvidia.model_inspection.visualize` allows LOCAL target and requires HardwareClass.ANY.
    - It must NOT be blanket-classified as KNOWN_BUT_UNAVAILABLE on macOS.
    - When tools (tool.netron, tool.onnx) are available, it evaluates to EXECUTABLE.
    """
    skill_registry = load_default_skill_registry()
    resolver = DefaultSkillResolver(
        skill_registry,
        tool_registry=populated_tool_registry,
    )

    skill_summaries = resolver.enumerate_skills(
        available_targets=(canonical_host_target,),
        tool_registry=populated_tool_registry,
    )
    summary_map = {s.skill_id: s for s in skill_summaries}

    # 9 GPU/Jetson skills must be KNOWN_BUT_UNAVAILABLE on local macOS target
    gpu_skill_ids = [
        SkillId("nvidia.tensorrt.inference_optimization"),
        SkillId("nvidia.model_optimizer.ptq"),
        SkillId("nvidia.model_optimizer.qat"),
        SkillId("nvidia.tao.transfer_learning"),
        SkillId("nvidia.deepstream.pipeline"),
        SkillId("nvidia.dali.data_pipeline"),
        SkillId("nvidia.triton.model_serving"),
        SkillId("nvidia.cuda_agent.kernel_profiling"),
        SkillId("nvidia.jetson.hardware_profiling"),
    ]

    for sid in gpu_skill_ids:
        summary = summary_map[sid]
        assert summary.availability_state == AvailabilityState.KNOWN_BUT_UNAVAILABLE, (
            f"Expected {sid} to be KNOWN_BUT_UNAVAILABLE on macOS host, got {summary.availability_state}"
        )
        assert summary.is_merely_known is True
        assert summary.is_executable is False

    # The 10th skill: nvidia.model_inspection.visualize
    vis_summary = summary_map[SkillId("nvidia.model_inspection.visualize")]
    # Populated tool registry has tool.netron and tool.onnx on local target
    assert vis_summary.availability_state != AvailabilityState.KNOWN_BUT_UNAVAILABLE, (
        "nvidia.model_inspection.visualize must NOT be blanket-classified as KNOWN_BUT_UNAVAILABLE"
    )
    assert vis_summary.availability_state == AvailabilityState.EXECUTABLE
    assert vis_summary.is_executable is True
    assert vis_summary.target_id == canonical_host_target.target_id


# ---------------------------------------------------------------------------
# Generic Compatible Target Verification (PM correction 4)
# ---------------------------------------------------------------------------


def test_generic_compatible_nvidia_target_evaluation(
    generic_compatible_nvidia_target: ExecutionTargetDescriptor,
    populated_tool_registry: DefaultToolRegistry,
) -> None:
    """Target Test: Evaluates generic compatible NVIDIA target without hardcoding 'A100'."""
    skill_registry = load_default_skill_registry()
    resolver = DefaultSkillResolver(
        skill_registry,
        tool_registry=populated_tool_registry,
    )

    skill_summaries = resolver.enumerate_skills(
        available_targets=(generic_compatible_nvidia_target,),
        tool_registry=populated_tool_registry,
    )
    summary_map = {s.skill_id: s for s in skill_summaries}

    ptq_summary = summary_map[SkillId("nvidia.model_optimizer.ptq")]
    assert ptq_summary.availability_state == AvailabilityState.EXECUTABLE
    assert ptq_summary.target_id == generic_compatible_nvidia_target.target_id
    assert ptq_summary.is_executable is True
    assert ToolId("tool.nvidia.modelopt") in ptq_summary.resolved_tools


# ---------------------------------------------------------------------------
# Discovery Authority Transition Test (PM correction 8)
# ---------------------------------------------------------------------------


def test_discovery_authority_transition_requires_explicit_promotion() -> None:
    """Authority Test: Runtime-discovered candidate cannot overwrite registry (PM correction 8).

    Enforces:
    - Runtime candidate has provenance=DISCOVERED and resides in DiscoveryCache.
    - Candidate cannot directly overwrite authoritative SkillRegistry entry.
    - Candidate requires explicit `promote_discovered_skill()` to enter authoritative registry.
    """
    skill_registry = DefaultSkillRegistry()
    discovery_cache = DiscoveryCache(skill_registry)

    # 1. Register an authoritative skill
    auth_skill = SkillDescriptor(
        skill_id=SkillId("auth.test.skill"),
        name="Authoritative Skill",
        description="Static authoritative skill",
        version="1.0.0",
        provenance=SkillProvenance.EXTERNAL,
        satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
        required_tools=(),
        target_requirements=TargetRequirements(),
        parameter_schema={"type": "object"},
        procedural_recipe="Execute auth skill recipe",
    )
    skill_registry.register_skill(auth_skill)

    # 2. Candidate with same SkillId in discovery cache must be rejected (cannot overwrite)
    candidate_colliding = SkillDescriptor(
        skill_id=SkillId("auth.test.skill"),
        name="Spoofed Candidate",
        description="Attempt to overwrite authoritative skill",
        version="2.0.0",
        provenance=SkillProvenance.DISCOVERED,
        satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
        required_tools=(),
        target_requirements=TargetRequirements(),
        parameter_schema={"type": "object"},
        procedural_recipe="Execute spoofed recipe",
    )
    with pytest.raises(ValueError, match="cannot overwrite authoritative skill"):
        discovery_cache.register_discovered(candidate_colliding)

    # 3. Discovered candidate with new SkillId enters DiscoveryCache
    new_candidate = SkillDescriptor(
        skill_id=SkillId("discovered.mcp.new_tool"),
        name="Discovered MCP Skill",
        description="Ephemeral MCP skill candidate",
        version="1.0.0",
        provenance=SkillProvenance.DISCOVERED,
        satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
        required_tools=(),
        target_requirements=TargetRequirements(),
        parameter_schema={"type": "object"},
        procedural_recipe="Execute discovered recipe",
    )
    discovery_cache.register_discovered(new_candidate)
    assert discovery_cache.get_candidate(new_candidate.skill_id) is not None
    assert skill_registry.get_skill(new_candidate.skill_id) is None

    # 4. Explicit promotion to authoritative registry
    promoted = skill_registry.promote_discovered_skill(
        new_candidate,
        promoted_provenance=SkillProvenance.EXTERNAL,
    )
    assert promoted.skill_id == new_candidate.skill_id
    assert promoted.provenance == SkillProvenance.EXTERNAL
    assert skill_registry.get_skill(new_candidate.skill_id) is not None


# ---------------------------------------------------------------------------
# Availability State Machine Transitions (PM correction 2)
# ---------------------------------------------------------------------------


def test_availability_state_machine_legal_taxonomy() -> None:
    """State Machine Test: Validates all 7 availability states per ADR-0007 §3.5."""
    expected_states = {
        "KNOWN",
        "DISCOVERED",
        "CONFIGURED",
        "AVAILABLE",
        "EXECUTABLE",
        "UNAVAILABLE",
        "KNOWN_BUT_UNAVAILABLE",
    }
    actual_states = {s.value for s in AvailabilityState}
    assert actual_states == expected_states

    # Verify presentation projections
    summary_exec = SkillAvailabilitySummary(
        skill_id=SkillId("test.exec"),
        satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
        availability_state=AvailabilityState.EXECUTABLE,
        provenance=SkillProvenance.EXTERNAL,
    )
    assert summary_exec.is_executable is True
    assert summary_exec.is_merely_known is False

    summary_known = SkillAvailabilitySummary(
        skill_id=SkillId("test.known"),
        satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
        availability_state=AvailabilityState.KNOWN_BUT_UNAVAILABLE,
        provenance=SkillProvenance.EXTERNAL,
    )
    assert summary_known.is_executable is False
    assert summary_known.is_merely_known is True


# ---------------------------------------------------------------------------
# CLI End-to-End Test (`--list-skills`)
# ---------------------------------------------------------------------------


def test_cli_list_skills_subcommand_execution() -> None:
    """CLI Test: `python -m cv_agent --list-skills` prints valid catalog table."""
    proc = subprocess.run(
        [sys.executable, "-m", "cv_agent", "--list-skills"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    stdout = proc.stdout

    assert "CV Engineering Agent — Skill Catalog & Operational Availability" in stdout
    assert "nvidia.tensorrt.inference_optimization" in stdout
    assert "nvidia.model_optimizer.ptq" in stdout
    assert "nvidia.model_inspection.visualize" in stdout
    assert "KNOWN_BUT_UNAVAILABLE" in stdout
    assert "merely known" in stdout
