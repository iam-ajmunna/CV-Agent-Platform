"""tests/test_discover_define_phase4_exit.py — Acceptance and Adversarial Suite for Phase 4.

Verifies the five Phase 4 Exit Authority Gates per Issue #70:
- Gate G1: DISCOVER — Operational elicitation dialogue before model proclamation (Zero-YOLO invariant).
- Gate G2: Multi-turn session checkpointing and resumption via SQLiteLangGraphCheckpointer.
- Gate G3: DEFINE — Problem characterization completeness (8 dimensions) and CV task DAG decomposition.
- Gate G4: Atomic GitProjectMemory persistence (project_spec.md, hardware_constraints.yaml, architectural_choices.md).
- Gate G5: Runtime execution barrier (0 tools/skills/subprocesses) and AST architecture audit.
- B26 Adversarial: Partial human answers keep DISCOVER active (8 -> answer 2 -> 6 -> answer remainder -> DEFINE).
- B27 Adversarial: Unregistered/synthetic CapabilityId fails closed before memory publication.
- B28 Adversarial: Unspecified hardware properties remain None/() without inferring from "jetson".
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from cv_agent.domain.errors import DomainValidationError
from cv_agent.domain.identifiers import CapabilityId
from cv_agent.llm.contracts import (
    FallbackPolicy,
    LLMRequest,
    LLMResponse,
    ModelProfile,
    ModelSelector,
    RetryPolicy,
    RoutingPolicy,
)
from cv_agent.llm.gateway import LLMGateway
from cv_agent.llm.mock import MockProviderAdapter
from cv_agent.memory.interfaces import (
    ArchitecturalDecision,
    HardwareConstraints,
    ProjectSpecification,
)
from cv_agent.memory.service import GitProjectMemory
from cv_agent.orchestration.contracts import LifecycleStatus
from cv_agent.orchestration.discover_define import (
    DiscoverDefineGraphState,
    create_discover_define_graph,
)
from cv_agent.orchestration.graph import SQLiteLangGraphCheckpointer
from cv_agent.orchestration.store import SQLiteOrchestrationStore
from cv_agent.reasoning.contracts import (
    CapabilityIntent,
    CVTaskDecompositionDAG,
    ProblemCharacterization,
    TaskNode,
    assert_zero_prohibited_models,
)
from cv_agent.reasoning.errors import (
    MemoryTransactionValidationError,
    PrematureModelSelectionError,
    WorkflowBoundaryViolationError,
)
from cv_agent.runtime.agent import CVAgent
from cv_agent.tools.barrier import Phase4ExecutionBarrier
from cv_agent.tools.contracts import (
    ExecutionId,
    TargetId,
    ToolId,
    ToolInvocation,
)


class SequentialMockAdapter(MockProviderAdapter):
    """Mock adapter returning sequential pre-recorded responses for multi-node tests."""

    def __init__(
        self, responses: list[dict[str, Any]], provider_id: str = "mock-phase4"
    ) -> None:
        super().__init__(provider_id=provider_id)
        self._response_queue = [json.dumps(r) for r in responses]

    def complete(self, request: LLMRequest, model: ModelProfile) -> LLMResponse:
        if self._response_queue:
            self._fixed_response = self._response_queue.pop(0)
        return super().complete(request, model)


def _setup_test_gateway(
    responses: list[dict[str, Any]],
) -> tuple[LLMGateway, SequentialMockAdapter]:
    """Helper creating an authoritative LLMGateway with sequential mock JSON responses."""
    adapter = SequentialMockAdapter(responses=responses, provider_id="mock-phase4")
    profiles = {
        "mock-phase4:model-phase4": ModelProfile(
            provider_id="mock-phase4",
            model_id="model-phase4",
            profile_version="1.0",
            context_window_tokens=8192,
            input_modalities=frozenset({"text"}),
            output_modalities=frozenset({"text"}),
            supports_structured_output=True,
            supports_tool_calls=True,
            supports_streaming=True,
            supports_reasoning=True,
            pricing_version="2026-v1",
        )
    }
    selector = ModelSelector("mock-phase4", "model-phase4")
    gw = LLMGateway(
        providers={"mock-phase4": adapter},
        model_profiles=profiles,
        default_routing_policy=RoutingPolicy(
            policy_id="test-phase4-routing",
            policy_version="1.0",
            candidates=(selector,),
            max_attempts=3,
        ),
        default_retry_policy=RetryPolicy(
            max_retries=1,
            initial_backoff_ms=5,
            max_backoff_ms=10,
            jitter=False,
        ),
        default_fallback_policy=FallbackPolicy(
            enabled=False,
            allowed_failure_categories=frozenset(),
            max_provider_switches=0,
        ),
    )
    return gw, adapter


@pytest.fixture
def mock_prison_elicitation_response() -> dict[str, Any]:
    """Canonical prison workload elicitation response: targeted operational questions, zero models."""
    return {
        "inquiry_id": "inq-prison-001",
        "unresolved_dimensions": [
            "camera_infrastructure",
            "physical_environment",
            "target_hardware",
            "latency_budget_ms",
            "throughput_fps",
            "error_tolerance",
            "data_availability",
            "intervention_modality",
        ],
        "targeted_questions": [
            "What camera infrastructure is installed (fixed/PTZ, resolution, RTSP framerate)?",
            "What is the physical environment along the perimeter (outdoor lighting shifts, weather)?",
            "What target edge hardware is specified (e.g. Jetson Orin, server GPU)?",
            "What latency budget and throughput framerate ceiling must be maintained?",
            "What error tolerance / false alarm rate ceiling is acceptable to operations?",
            "What annotated historical or simulated data is available?",
            "What is the intervention modality (automated alarm, guard dispatch)?",
        ],
        "context_summary": "Prison perimeter escape-attempt detection scenario with undefined camera, hardware, and operational error constraints.",
        "assumptions_and_risks": [
            "Assumption: System must operate in real-time under edge compute constraints.",
            "Risk: False alarms caused by weather/animals can cause alarm fatigue.",
        ],
    }


@pytest.fixture
def mock_prison_elicitation_resolved_response() -> dict[str, Any]:
    """Canonical prison workload elicitation response when all operational dimensions are characterized."""
    return {
        "inquiry_id": "inq-prison-resolved",
        "unresolved_dimensions": [],
        "targeted_questions": [],
        "context_summary": "All 8 operational dimensions characterized.",
        "assumptions_and_risks": [],
    }


@pytest.fixture
def mock_prison_framing_response() -> dict[str, Any]:
    """Canonical prison workload framing response: complete across all 8 dimensions, zero models."""
    return {
        "task_type": "escape_attempt_detection",
        "target_hardware": "NVIDIA Jetson AGX Orin 64GB",
        "operational_definition": "Detect and track individuals breaching or climbing physical perimeter fence lines within 300ms",
        "physical_environment": "Outdoor perimeter fence line with day/night illumination shifts, rain, and fog",
        "camera_infrastructure": "CCTV 1080p 30fps RTSP fixed streams mounted on perimeter poles",
        "error_tolerance": "Zero missed perimeter breaches; false positive rate < 1 per camera per 24 hours",
        "data_availability": "50 hours annotated perimeter CCTV video including simulated breach scenarios",
        "intervention_modality": "Real-time alert dispatch to central monitoring station and physical siren trigger",
        "latency_budget_ms": 100.0,
        "throughput_fps": 30.0,
        "compute_budget_hours": 10.0,
        "constraints": {
            "max_vram_mb": 16384,
            "max_ram_mb": 32768,
            "power_budget_watts": 60.0,
            "supported_precisions": ["fp32", "fp16", "int8"],
        },
    }


@pytest.fixture
def mock_prison_dag_response() -> dict[str, Any]:
    """Canonical prison workload CV task DAG decomposition response: domain primitives, zero models."""
    return {
        "tasks": [
            {
                "task_id": "task-01-person-det",
                "task_type": "object_detection",
                "description": "Localize human subjects along perimeter fence zones from raw RTSP frames",
                "inputs": ["raw_rtsp_frames"],
                "outputs": ["person_bounding_boxes"],
                "dependencies": [],
            },
            {
                "task_id": "task-02-person-track",
                "task_type": "tracking",
                "description": "Associate temporal detections into trajectories across camera views",
                "inputs": ["person_bounding_boxes"],
                "outputs": ["tracked_trajectories"],
                "dependencies": ["task-01-person-det"],
            },
            {
                "task_id": "task-03-fence-geom",
                "task_type": "spatial_geometry",
                "description": "Evaluate spatial proximity of tracked trajectories to calibrated fence 3D boundary polygons",
                "inputs": ["tracked_trajectories", "fence_calibration_polygons"],
                "outputs": ["perimeter_proximity_events"],
                "dependencies": ["task-02-person-track"],
            },
            {
                "task_id": "task-04-action-rec",
                "task_type": "action_recognition",
                "description": "Classify climbing or cutting motions in proximity events to confirm breach attempt",
                "inputs": ["perimeter_proximity_events", "raw_rtsp_frames"],
                "outputs": ["verified_breach_alerts"],
                "dependencies": ["task-03-fence-geom"],
            },
        ],
        "root_task_ids": ["task-01-person-det"],
        "terminal_task_ids": ["task-04-action-rec"],
        "capability_intents": [
            {
                "capability_id": "cv.requirements.analysis",
                "intent_description": "Analyze operational requirements against edge hardware ceilings",
                "parameter_intent": {},
            },
            {
                "capability_id": "cv.problem.formulation",
                "intent_description": "Formulate multi-stage vision pipeline for perimeter monitoring",
                "parameter_intent": {},
            },
        ],
    }


# ==============================================================================
# Gate G1 & G2: DISCOVER Elicitation, Zero-YOLO Invariant, & Resumption
# ==============================================================================


def test_gate_g1_discover_zero_yolo_and_operational_questions(
    tmp_path: Path,
    mock_prison_elicitation_response: dict[str, Any],
) -> None:
    """Gate G1: An underspecified request yields operational questions, zero models, and PAUSES."""
    db_path = tmp_path / "orchestration.db"
    store = SQLiteOrchestrationStore(db_path)
    session_id = "session-prison-g1"
    lease = store.acquire_lease(session_id, session_id, "test-runner")
    memory = GitProjectMemory(tmp_path / "workspace")
    barrier = Phase4ExecutionBarrier()

    gateway, _ = _setup_test_gateway([mock_prison_elicitation_response])
    checkpointer = SQLiteLangGraphCheckpointer(str(db_path))

    graph = create_discover_define_graph(
        store=store,
        lease=lease,
        memory=memory,
        llm_gateway=gateway,
        checkpointer=checkpointer,
        tool_barrier=barrier,
    )

    config = {"configurable": {"thread_id": session_id}}
    state: DiscoverDefineGraphState = graph.invoke(
        {
            "session_id": session_id,
            "thread_id": session_id,
            "lifecycle_status": LifecycleStatus.INITIALIZING.value,
            "user_prompt": "I have a prison project — escape-attempt detection",
        },
        config=config,
    )

    # 1. Verification of G1 Invariant: Lifecycle PAUSED at operational questions
    assert state["lifecycle_status"] == LifecycleStatus.PAUSED.value
    assert store.get_lifecycle_status(session_id) == LifecycleStatus.PAUSED

    # 2. Verification of G1 Output: Operational inquiry produced
    assert state["needs_elicitation"] is True
    inquiry = state["operational_inquiry"]
    assert inquiry is not None
    assert len(inquiry["targeted_questions"]) >= 5
    assert len(inquiry["unresolved_dimensions"]) >= 5

    # 3. Verification of Zero-YOLO invariant in inquiry
    assert_zero_prohibited_models(inquiry["context_summary"])
    for q in inquiry["targeted_questions"]:
        assert_zero_prohibited_models(q)

    # 4. Zero tool executions during DISCOVER
    assert barrier.invocation_count == 0


def test_gate_g2_resumption_from_checkpoint(
    tmp_path: Path,
    mock_prison_elicitation_response: dict[str, Any],
    mock_prison_elicitation_resolved_response: dict[str, Any],
    mock_prison_framing_response: dict[str, Any],
    mock_prison_dag_response: dict[str, Any],
) -> None:
    """Gate G2: Resuming with user answers continues on same thread_id through DEFINE to completion."""
    db_path = tmp_path / "orchestration.db"
    store = SQLiteOrchestrationStore(db_path)
    session_id = "session-prison-g2"
    lease = store.acquire_lease(session_id, session_id, "test-runner")
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)
    barrier = Phase4ExecutionBarrier()

    gateway, _ = _setup_test_gateway(
        [
            mock_prison_elicitation_response,
            mock_prison_elicitation_resolved_response,
            mock_prison_framing_response,
            mock_prison_dag_response,
        ]
    )
    checkpointer = SQLiteLangGraphCheckpointer(str(db_path))

    graph = create_discover_define_graph(
        store=store,
        lease=lease,
        memory=memory,
        llm_gateway=gateway,
        checkpointer=checkpointer,
        tool_barrier=barrier,
    )

    config = {"configurable": {"thread_id": session_id}}

    # Turn 1: Initial underspecified prompt
    turn1_state = graph.invoke(
        {
            "session_id": session_id,
            "thread_id": session_id,
            "lifecycle_status": LifecycleStatus.INITIALIZING.value,
            "user_prompt": "I have a prison project — escape-attempt detection",
        },
        config=config,
    )
    assert turn1_state["lifecycle_status"] == LifecycleStatus.PAUSED.value
    assert store.get_lifecycle_status(session_id) == LifecycleStatus.PAUSED

    # Turn 2: Resume session with user operational answers on same thread_id
    answers = {
        "camera_infrastructure": "CCTV 1080p 30fps RTSP fixed streams",
        "physical_environment": "Outdoor perimeter fence line, day/night lighting shifts, rain/fog",
        "target_hardware": "NVIDIA Jetson AGX Orin 64GB",
        "latency_budget_ms": "100.0",
        "throughput_fps": "30.0",
        "error_tolerance": "Zero missed perimeter breaches; false positives < 1 per camera per 24h",
        "data_availability": "50 hours annotated perimeter CCTV video including simulated breaches",
        "intervention_modality": "Real-time alert dispatch to central monitoring station and siren trigger",
    }

    graph.update_state(config, {"human_answers": answers})
    turn2_state = graph.invoke(None, config=config)

    # Verification of Gate G2: Successfully resumed and completed
    assert turn2_state["lifecycle_status"] == LifecycleStatus.COMPLETED.value
    assert store.get_lifecycle_status(session_id) == LifecycleStatus.COMPLETED
    assert turn2_state["project_spec_committed"] is True
    assert barrier.invocation_count == 0


# ==============================================================================
# Gate G3: DEFINE Characterization Completeness & CV Task DAG Decomposition
# ==============================================================================


def test_gate_g3_define_characterization_and_task_dag(
    tmp_path: Path,
    mock_prison_elicitation_response: dict[str, Any],
    mock_prison_elicitation_resolved_response: dict[str, Any],
    mock_prison_framing_response: dict[str, Any],
    mock_prison_dag_response: dict[str, Any],
) -> None:
    """Gate G3: ProblemCharacterization 8 dimensions complete; DAG decomposed into domain primitives."""
    db_path = tmp_path / "orchestration.db"
    store = SQLiteOrchestrationStore(db_path)
    session_id = "session-prison-g3"
    lease = store.acquire_lease(session_id, session_id, "test-runner")
    memory = GitProjectMemory(tmp_path / "workspace")

    gateway, _ = _setup_test_gateway(
        [
            mock_prison_elicitation_response,
            mock_prison_elicitation_resolved_response,
            mock_prison_framing_response,
            mock_prison_dag_response,
        ]
    )

    graph = create_discover_define_graph(
        store=store,
        lease=lease,
        memory=memory,
        llm_gateway=gateway,
    )
    config = {"configurable": {"thread_id": session_id}}

    graph.invoke(
        {
            "session_id": session_id,
            "thread_id": session_id,
            "lifecycle_status": LifecycleStatus.INITIALIZING.value,
            "user_prompt": "I have a prison project — escape-attempt detection",
        },
        config=config,
    )

    answers = {
        "camera_infrastructure": "CCTV 1080p 30fps RTSP fixed streams",
        "physical_environment": "Outdoor perimeter fence line, day/night lighting shifts, rain/fog",
        "target_hardware": "NVIDIA Jetson AGX Orin 64GB",
        "latency_budget_ms": "100.0",
        "throughput_fps": "30.0",
        "error_tolerance": "Zero missed perimeter breaches",
        "data_availability": "50 hours annotated perimeter CCTV video",
        "intervention_modality": "Real-time alert dispatch",
    }
    graph.update_state(config, {"human_answers": answers})
    result = graph.invoke(None, config=config)

    char = result["characterization"]
    assert char["is_well_formed"] is True
    # Verify all 8 canonical dimensions are present and non-empty
    assert char["task_type"]
    assert char["target_hardware"]
    assert char["operational_definition"]
    assert char["physical_environment"]
    assert char["camera_infrastructure"]
    assert char["error_tolerance"]
    assert char["data_availability"]
    assert char["intervention_modality"]

    # Verify Task DAG domain primitives
    decomp = result["decomposition"]
    tasks = decomp["tasks"]
    assert len(tasks) == 4
    task_types = [t["task_type"] for t in tasks]
    assert "object_detection" in task_types
    assert "tracking" in task_types
    assert "spatial_geometry" in task_types
    assert "action_recognition" in task_types

    # B27: Verify capability_intents preserved in result
    assert "capability_intents" in result
    c_intents = result["capability_intents"]
    assert len(c_intents) == 2
    assert c_intents[0]["capability_id"] == "cv.requirements.analysis"
    assert c_intents[1]["capability_id"] == "cv.problem.formulation"

    # Verify zero prohibited model mentions anywhere in output
    for t in tasks:
        assert_zero_prohibited_models(t["description"])


# ==============================================================================
# Gate G4: Atomic GitProjectMemory Persistence
# ==============================================================================


def test_gate_g4_atomic_memory_persistence(tmp_path: Path) -> None:
    """Gate G4: Commits project_spec.md, hardware_constraints.yaml, and architectural_choices.md atomically."""
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)

    char = ProblemCharacterization(
        task_type="escape_attempt_detection",
        target_hardware="NVIDIA Jetson AGX Orin 64GB",
        operational_definition="Detect fence breaches within 300ms",
        physical_environment="Outdoor perimeter fence line",
        camera_infrastructure="CCTV 1080p 30fps",
        error_tolerance="Zero missed breaches",
        data_availability="50 hours annotated video",
        intervention_modality="Real-time alarm",
        latency_budget_ms=100.0,
        throughput_fps=30.0,
    )
    assert char.is_well_formed is True

    dag = CVTaskDecompositionDAG(
        tasks=(
            TaskNode(
                task_id="t1",
                task_type="object_detection",
                description="Detect humans near perimeter",
            ),
            TaskNode(
                task_id="t2",
                task_type="tracking",
                description="Track human trajectories",
                dependencies=("t1",),
            ),
        )
    )

    intents = (
        CapabilityIntent(
            capability_id=CapabilityId("cv.requirements.analysis"),
            intent_description="Analyze operational requirements against edge ceilings",
            parameter_intent={},
        ),
        CapabilityIntent(
            capability_id=CapabilityId("cv.problem.formulation"),
            intent_description="Formulate multi-stage vision pipeline",
            parameter_intent={},
        ),
    )

    hw = HardwareConstraints(
        target_device="NVIDIA Jetson AGX Orin 64GB",
        max_vram_mb=16384,
        max_ram_mb=32768,
        power_budget_watts=60.0,
        latency_ceiling_ms=100.0,
        supported_precisions=("fp32", "fp16", "int8"),
    )

    arch = (
        ArchitecturalDecision(
            decision_id="AD-001",
            timestamp="2026-09-24T00:00:00Z",
            title="Two-stage detection and tracking pipeline",
            rationale="Decouples detector evaluation from spatial proximity calculations",
            linked_experiment_ids=(),
        ),
    )

    spec = ProjectSpecification(
        characterization=char,
        decomposition=dag,
        hardware_constraints=hw,
        capability_intents=intents,
        architectural_choices=arch,
    )

    # 1. Commit specification
    memory.commit_project_specification(spec)

    # 2. Verify all 3 files exist on disk
    assert memory.project_spec_path.exists()
    assert memory.hardware_constraints_path.exists()
    assert memory.architectural_choices_path.exists()

    # 3. Verify round-trip loading
    loaded_spec = memory.get_project_specification()
    assert loaded_spec is not None
    assert loaded_spec.characterization.task_type == "escape_attempt_detection"
    assert (
        loaded_spec.hardware_constraints.target_device == "NVIDIA Jetson AGX Orin 64GB"
    )
    assert len(loaded_spec.decomposition.tasks) == 2
    assert len(loaded_spec.capability_intents) == 2
    assert loaded_spec.capability_intents[0].capability_id == CapabilityId(
        "cv.requirements.analysis"
    )


def test_gate_g4_atomic_memory_rollback_on_prohibited_model(tmp_path: Path) -> None:
    """Gate G4: Prohibited model name in specification causes complete rollback (0 files altered)."""
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)

    # Characterization with forbidden model "YOLO"
    char = ProblemCharacterization(
        task_type="escape_attempt_detection",
        target_hardware="NVIDIA Jetson AGX Orin 64GB",
        operational_definition="Deploy YOLOv8 for fence breach detection",  # Prohibited!
        physical_environment="Outdoor perimeter",
        camera_infrastructure="CCTV 1080p",
        error_tolerance="Zero missed breaches",
        data_availability="50 hours video",
        intervention_modality="Real-time alarm",
        latency_budget_ms=100.0,
        throughput_fps=30.0,
    )
    dag = CVTaskDecompositionDAG(
        tasks=(
            TaskNode(
                task_id="t1",
                task_type="object_detection",
                description="Detect humans",
            ),
        )
    )
    hw = HardwareConstraints(
        target_device="Jetson",
        max_vram_mb=8192,
        max_ram_mb=16384,
        power_budget_watts=30.0,
        latency_ceiling_ms=100.0,
        supported_precisions=("fp32",),
    )
    spec = ProjectSpecification(
        characterization=char, decomposition=dag, hardware_constraints=hw
    )

    with pytest.raises(MemoryTransactionValidationError):
        memory.commit_project_specification(spec)

    # Invariant: Transactional rollback leaves zero uncommitted files on disk
    assert not memory.hardware_constraints_path.exists()
    assert memory.get_project_specification() is None


def test_gate_g4_atomic_memory_rejects_incomplete_characterization(
    tmp_path: Path,
) -> None:
    """Gate G4: Incomplete characterization (< 8 dimensions) is rejected before any write."""
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)

    char = ProblemCharacterization(
        task_type="escape_attempt_detection",
        target_hardware="NVIDIA Jetson AGX Orin 64GB",
        operational_definition="",  # Missing required dimension!
        physical_environment="Outdoor",
        camera_infrastructure="CCTV",
        error_tolerance="Low",
        data_availability="Video",
        intervention_modality="Alarm",
    )
    assert char.is_well_formed is False

    dag = CVTaskDecompositionDAG(
        tasks=(
            TaskNode(
                task_id="t1",
                task_type="object_detection",
                description="Detect humans",
            ),
        )
    )
    hw = HardwareConstraints(
        target_device="Jetson",
        max_vram_mb=8192,
        max_ram_mb=16384,
        power_budget_watts=30.0,
        latency_ceiling_ms=100.0,
        supported_precisions=("fp32",),
    )
    spec = ProjectSpecification(
        characterization=char, decomposition=dag, hardware_constraints=hw
    )

    with pytest.raises(
        MemoryTransactionValidationError,
        match="incomplete or not well-formed across the 8 canonical dimensions",
    ):
        memory.commit_project_specification(spec)

    assert not memory.hardware_constraints_path.exists()
    assert memory.get_project_specification() is None


# ==============================================================================
# Gate G5: Execution Barrier & Mechanically Enforceable AST Boundary Audits
# ==============================================================================


@pytest.mark.asyncio
async def test_gate_g5_execution_barrier_blocks_all_tools() -> None:
    """Gate G5: Phase4ExecutionBarrier strictly prohibits tool execution during DISCOVER/DEFINE."""
    barrier = Phase4ExecutionBarrier()

    invocation = ToolInvocation(
        execution_id=ExecutionId("exec-001"),
        tool_id=ToolId("tool.local.echo"),
        target_id=TargetId("local-mac"),
        parameters={"message": "hello"},
        timeout_seconds=30,
    )

    with pytest.raises(WorkflowBoundaryViolationError) as exc_info:
        await barrier.execute(invocation)

    assert "Workflow boundary violation" in str(exc_info.value)
    assert barrier.invocation_count == 1
    assert len(barrier.attempted_invocations) == 1
    assert barrier.attempted_invocations[0].tool_id == ToolId("tool.local.echo")


def test_gate_g5_ast_audit_zero_provider_sdks_and_zero_cuda_launches() -> None:
    """Gate G5: AST audit verifies 0 provider SDK imports and 0 CUDA launches in Phase 4 code."""
    workspace_root = Path(__file__).parent.parent
    phase4_modules = [
        workspace_root / "cv_agent" / "reasoning" / "nodes.py",
        workspace_root / "cv_agent" / "reasoning" / "contracts.py",
        workspace_root / "cv_agent" / "reasoning" / "prompts.py",
        workspace_root / "cv_agent" / "orchestration" / "discover_define.py",
    ]

    prohibited_imports = {
        "openai",
        "anthropic",
        "ollama",
        "google.generativeai",
        "torch.cuda",
        "tensorrt",
        "pycuda",
        "cupy",
    }
    prohibited_calls = {
        "cudaMalloc",
        "cudaMemcpy",
        "cudaLaunchKernel",
        "create_network",
    }

    for module_path in phase4_modules:
        assert module_path.exists(), f"Module {module_path} must exist"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in prohibited_imports, (
                        f"Prohibited import {alias.name!r} in {module_path.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod not in prohibited_imports, (
                    f"Prohibited import-from {mod!r} in {module_path.name}"
                )

            elif isinstance(node, ast.Call):
                func = node.func
                func_name = ""
                if isinstance(func, ast.Name):
                    func_name = func.id
                elif isinstance(func, ast.Attribute):
                    func_name = func.attr
                assert func_name not in prohibited_calls, (
                    f"Prohibited call {func_name!r} in {module_path.name}"
                )


# ==============================================================================
# Facade Integration & Adversarial Scenarios
# ==============================================================================


def test_cvagent_facade_run_discover_define_end_to_end(
    tmp_path: Path,
    mock_prison_elicitation_response: dict[str, Any],
    mock_prison_elicitation_resolved_response: dict[str, Any],
    mock_prison_framing_response: dict[str, Any],
    mock_prison_dag_response: dict[str, Any],
) -> None:
    """Verify CVAgent facade coordinates the multi-turn DISCOVER/DEFINE workflow."""
    agent = CVAgent()
    db_path = tmp_path / "orchestration.db"
    store = SQLiteOrchestrationStore(db_path)
    session_id = "session-facade-test"
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)
    barrier = Phase4ExecutionBarrier()

    gateway, _ = _setup_test_gateway(
        [
            mock_prison_elicitation_response,
            mock_prison_elicitation_resolved_response,
            mock_prison_framing_response,
            mock_prison_dag_response,
        ]
    )

    # Turn 1: Initial elicitation
    result1 = agent.run_discover_define(
        "I have a prison project — escape-attempt detection",
        session_id=session_id,
        store=store,
        memory=memory,
        llm_gateway=gateway,
        tool_barrier=barrier,
    )
    assert result1["lifecycle_status"] == LifecycleStatus.PAUSED.value
    assert result1["needs_elicitation"] is True

    # Turn 2: Resumption with operational answers
    answers = {
        "camera_infrastructure": "CCTV 1080p 30fps RTSP fixed streams",
        "physical_environment": "Outdoor perimeter fence line, day/night lighting shifts, rain/fog",
        "target_hardware": "NVIDIA Jetson AGX Orin 64GB",
        "latency_budget_ms": "100.0",
        "throughput_fps": "30.0",
        "error_tolerance": "Zero missed perimeter breaches",
        "data_availability": "50 hours annotated perimeter CCTV video",
        "intervention_modality": "Real-time alert dispatch",
    }
    result2 = agent.run_discover_define(
        "I have a prison project — escape-attempt detection",
        session_id=session_id,
        human_answers=answers,
        store=store,
        memory=memory,
        llm_gateway=gateway,
        tool_barrier=barrier,
    )

    assert result2["lifecycle_status"] == LifecycleStatus.COMPLETED.value
    assert result2["project_spec_committed"] is True
    assert memory.project_spec_path.exists()
    assert barrier.invocation_count == 0


def test_adversarial_cyclic_dag_rejection() -> None:
    """Adversarial: Cyclic task dependencies in CVTaskDecompositionDAG raise ValueError immediately."""
    with pytest.raises(ValueError, match="Cycle detected"):
        CVTaskDecompositionDAG(
            tasks=(
                TaskNode(
                    task_id="t1",
                    task_type="object_detection",
                    description="d1",
                    dependencies=("t2",),
                ),
                TaskNode(
                    task_id="t2",
                    task_type="tracking",
                    description="d2",
                    dependencies=("t1",),
                ),
            )
        )


def test_adversarial_premature_model_terms_detection() -> None:
    """Adversarial: Prohibited model terms are caught by assert_zero_prohibited_models."""
    with pytest.raises(
        PrematureModelSelectionError, match="Premature model architecture 'yolo'"
    ):
        assert_zero_prohibited_models("Let us use YOLO for detection", "test")

    with pytest.raises(
        PrematureModelSelectionError, match="Premature model architecture 'rt-detr'"
    ):
        assert_zero_prohibited_models("RT-DETR is great for real-time", "test")

    with pytest.raises(
        PrematureModelSelectionError, match="Premature model architecture 'faster-rcnn'"
    ):
        assert_zero_prohibited_models("Deploy Faster-RCNN on Jetson", "test")


# ==============================================================================
# Adversarial Remediation Suite: B26, B27, B28
# ==============================================================================


def test_adversarial_b26_partial_human_answers_keeps_discover_active(
    tmp_path: Path,
    mock_prison_elicitation_response: dict[str, Any],
    mock_prison_framing_response: dict[str, Any],
    mock_prison_dag_response: dict[str, Any],
) -> None:
    """Adversarial B26: Answering only 2 of 8 dimensions produces another OperationalInquiry (6 remaining) and PAUSES."""
    db_path = tmp_path / "orchestration.db"
    store = SQLiteOrchestrationStore(db_path)
    session_id = "session-prison-b26"
    lease = store.acquire_lease(session_id, session_id, "test-runner")
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)
    checkpointer = SQLiteLangGraphCheckpointer(str(db_path))

    # Turn 2: LLM receives partial answers (2 answered), returns remaining 6 unresolved dimensions
    mock_elicitation_turn2 = {
        "inquiry_id": "inq-prison-turn2",
        "unresolved_dimensions": [
            "camera_infrastructure",
            "physical_environment",
            "throughput_fps",
            "error_tolerance",
            "data_availability",
            "intervention_modality",
        ],
        "targeted_questions": [
            "What camera infrastructure is installed?",
            "What is the physical environment?",
            "What throughput fps is needed?",
            "What is the error tolerance ceiling?",
            "What data is available?",
            "What is the intervention modality?",
        ],
        "context_summary": "Hardware and latency characterized; 6 operational dimensions remaining.",
        "assumptions_and_risks": [],
    }

    # Turn 3: LLM receives remaining 6 answers, returns 0 unresolved dimensions
    mock_elicitation_turn3 = {
        "inquiry_id": "inq-prison-resolved",
        "unresolved_dimensions": [],
        "targeted_questions": [],
        "context_summary": "All 8 operational dimensions characterized.",
        "assumptions_and_risks": [],
    }

    gateway, _ = _setup_test_gateway(
        [
            mock_prison_elicitation_response,  # Turn 1: initial discovery (8 unresolved)
            mock_elicitation_turn2,  # Turn 2: after answering 2 (6 unresolved)
            mock_elicitation_turn3,  # Turn 3: after answering remaining 6 (0 unresolved)
            mock_prison_framing_response,  # DEFINE framing
            mock_prison_dag_response,  # DEFINE DAG
        ]
    )

    graph = create_discover_define_graph(
        store=store,
        lease=lease,
        memory=memory,
        llm_gateway=gateway,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": session_id}}

    # Turn 1: Initial prompt -> 8 unresolved dimensions -> PAUSED
    turn1 = graph.invoke(
        {
            "session_id": session_id,
            "thread_id": session_id,
            "lifecycle_status": LifecycleStatus.INITIALIZING.value,
            "user_prompt": "I have a prison project — escape-attempt detection",
        },
        config=config,
    )
    assert turn1["lifecycle_status"] == LifecycleStatus.PAUSED.value
    assert len(turn1["operational_inquiry"]["unresolved_dimensions"]) == 8

    # Turn 2: User provides only 2 answers (target_hardware, latency_budget_ms)
    partial_answers = {
        "target_hardware": "NVIDIA Jetson AGX Orin 64GB",
        "latency_budget_ms": "100.0",
    }
    graph.update_state(config, {"human_answers": partial_answers})
    turn2 = graph.invoke(None, config=config)

    # Invariant verified: Partial answers DO NOT bypass DISCOVER; graph PAUSES again
    assert turn2["lifecycle_status"] == LifecycleStatus.PAUSED.value
    assert store.get_lifecycle_status(session_id) == LifecycleStatus.PAUSED
    assert turn2["needs_elicitation"] is True
    assert len(turn2["operational_inquiry"]["unresolved_dimensions"]) == 6
    assert turn2.get("project_spec_committed") is not True
    assert memory.get_project_specification() is None

    # Turn 3: User provides remaining 6 answers
    remaining_answers = {
        "camera_infrastructure": "CCTV 1080p 30fps RTSP fixed streams",
        "physical_environment": "Outdoor perimeter fence line, day/night shifts, rain/fog",
        "throughput_fps": "30.0",
        "error_tolerance": "Zero missed perimeter breaches",
        "data_availability": "50 hours annotated perimeter CCTV video",
        "intervention_modality": "Real-time alert dispatch",
    }
    graph.update_state(config, {"human_answers": remaining_answers})
    turn3 = graph.invoke(None, config=config)

    # Invariant verified: Full characterization enters DEFINE and commits
    assert turn3["lifecycle_status"] == LifecycleStatus.COMPLETED.value
    assert store.get_lifecycle_status(session_id) == LifecycleStatus.COMPLETED
    assert turn3["project_spec_committed"] is True
    assert memory.get_project_specification() is not None


def test_adversarial_b27_unregistered_capability_fails_closed(
    tmp_path: Path,
    mock_prison_elicitation_response: dict[str, Any],
    mock_prison_elicitation_resolved_response: dict[str, Any],
    mock_prison_framing_response: dict[str, Any],
) -> None:
    """Adversarial B27: Syntactically valid but unregistered CapabilityId causes DEFINE to fail closed before memory write."""
    db_path = tmp_path / "orchestration.db"
    store = SQLiteOrchestrationStore(db_path)
    session_id = "session-prison-b27"
    lease = store.acquire_lease(session_id, session_id, "test-runner")
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)
    checkpointer = SQLiteLangGraphCheckpointer(str(db_path))

    # Synthetic unregistered capability_id
    invalid_dag_response = {
        "tasks": [
            {
                "task_id": "task-01",
                "task_type": "object_detection",
                "description": "Detect objects",
                "inputs": [],
                "outputs": [],
                "dependencies": [],
            }
        ],
        "root_task_ids": ["task-01"],
        "terminal_task_ids": ["task-01"],
        "capability_intents": [
            {
                "capability_id": "cv.synthetic.unregistered_vision_capability",
                "intent_description": "Fabricated unregistered capability",
                "parameter_intent": {},
            }
        ],
    }

    gateway, _ = _setup_test_gateway(
        [
            mock_prison_elicitation_response,
            mock_prison_elicitation_resolved_response,
            mock_prison_framing_response,
            invalid_dag_response,
        ]
    )

    graph = create_discover_define_graph(
        store=store,
        lease=lease,
        memory=memory,
        llm_gateway=gateway,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": session_id}}

    graph.invoke(
        {
            "session_id": session_id,
            "thread_id": session_id,
            "lifecycle_status": LifecycleStatus.INITIALIZING.value,
            "user_prompt": "I have a prison project — escape-attempt detection",
        },
        config=config,
    )

    answers = {
        "camera_infrastructure": "CCTV 1080p 30fps",
        "physical_environment": "Outdoor perimeter",
        "target_hardware": "NVIDIA Jetson AGX Orin 64GB",
        "latency_budget_ms": "100.0",
        "throughput_fps": "30.0",
        "error_tolerance": "Zero missed breaches",
        "data_availability": "50 hours video",
        "intervention_modality": "Real-time alert",
    }
    graph.update_state(config, {"human_answers": answers})

    with pytest.raises(
        DomainValidationError, match="unregistered/synthetic capability"
    ):
        graph.invoke(None, config=config)

    # Invariant verified: Fail-closed before memory publication
    assert memory.get_project_specification() is None
    assert not memory.hardware_constraints_path.exists()


def test_adversarial_b28_define_does_not_fabricate_hardware_defaults(
    tmp_path: Path,
    mock_prison_elicitation_response: dict[str, Any],
    mock_prison_elicitation_resolved_response: dict[str, Any],
    mock_prison_dag_response: dict[str, Any],
) -> None:
    """Adversarial B28: Unspecified hardware properties remain None/() without inferring defaults from 'jetson'."""
    db_path = tmp_path / "orchestration.db"
    store = SQLiteOrchestrationStore(db_path)
    session_id = "session-prison-b28"
    lease = store.acquire_lease(session_id, session_id, "test-runner")
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)
    checkpointer = SQLiteLangGraphCheckpointer(str(db_path))

    # Framing response specifies Jetson hardware, but empty constraints (no VRAM/RAM/power/precisions)
    framing_without_hardware_specs = {
        "task_type": "escape_attempt_detection",
        "target_hardware": "NVIDIA Jetson AGX Orin 64GB",
        "operational_definition": "Detect and track individuals breaching perimeter",
        "physical_environment": "Outdoor perimeter fence line",
        "camera_infrastructure": "CCTV 1080p 30fps RTSP fixed streams",
        "error_tolerance": "Zero missed perimeter breaches",
        "data_availability": "50 hours annotated perimeter CCTV video",
        "intervention_modality": "Real-time alert dispatch",
        "latency_budget_ms": 100.0,
        "throughput_fps": 30.0,
        "compute_budget_hours": 10.0,
        "constraints": {},  # No VRAM, RAM, power, or supported_precisions!
    }

    gateway, _ = _setup_test_gateway(
        [
            mock_prison_elicitation_response,
            mock_prison_elicitation_resolved_response,
            framing_without_hardware_specs,
            mock_prison_dag_response,
        ]
    )

    graph = create_discover_define_graph(
        store=store,
        lease=lease,
        memory=memory,
        llm_gateway=gateway,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": session_id}}

    graph.invoke(
        {
            "session_id": session_id,
            "thread_id": session_id,
            "lifecycle_status": LifecycleStatus.INITIALIZING.value,
            "user_prompt": "I have a prison project — escape-attempt detection",
        },
        config=config,
    )

    answers = {
        "camera_infrastructure": "CCTV 1080p 30fps",
        "physical_environment": "Outdoor perimeter",
        "target_hardware": "NVIDIA Jetson AGX Orin 64GB",
        "latency_budget_ms": "100.0",
        "throughput_fps": "30.0",
        "error_tolerance": "Zero missed breaches",
        "data_availability": "50 hours video",
        "intervention_modality": "Real-time alert",
    }
    graph.update_state(config, {"human_answers": answers})
    result = graph.invoke(None, config=config)

    assert result["lifecycle_status"] == LifecycleStatus.COMPLETED.value
    spec = memory.get_project_specification()
    assert spec is not None
    hw = spec.hardware_constraints

    # Invariant verified: Phase 4 DID NOT manufacture defaults based on "jetson"
    assert hw.target_device == "NVIDIA Jetson AGX Orin 64GB"
    assert hw.max_vram_mb is None  # NOT 16384!
    assert hw.max_ram_mb is None  # NOT 32768!
    assert hw.power_budget_watts is None  # NOT 60.0!
    assert hw.supported_precisions == ()  # NOT ("fp32", "fp16", "int8")!
    assert hw.latency_ceiling_ms == 100.0


def test_adversarial_b26_mock_llm_false_complete_fails_closed(
    tmp_path: Path,
    mock_prison_elicitation_response: dict[str, Any],
) -> None:
    """Adversarial B26: Mock LLM falsely reports zero unresolved dimensions with only 2 answers provided.

    The workflow must deterministically fail closed from accumulated authoritative
    evidence and must NOT enter DEFINE.
    """
    db_path = tmp_path / "orchestration.db"
    store = SQLiteOrchestrationStore(db_path)
    session_id = "session-prison-b26-adversarial"
    lease = store.acquire_lease(session_id, session_id, "test-runner")
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)
    checkpointer = SQLiteLangGraphCheckpointer(str(db_path))

    # LLM hallucinates/falsely asserts that elicitation is complete
    mock_llm_falsely_complete_response = {
        "inquiry_id": "inq-fake-complete",
        "unresolved_dimensions": [],  # False negative!
        "targeted_questions": [],  # False negative!
        "context_summary": "LLM falsely claims operational characterization is fully resolved.",
        "assumptions_and_risks": [],
    }

    gateway, _ = _setup_test_gateway(
        [
            mock_prison_elicitation_response,
            mock_llm_falsely_complete_response,
        ]
    )

    graph = create_discover_define_graph(
        store=store,
        lease=lease,
        memory=memory,
        llm_gateway=gateway,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": session_id}}

    # Turn 1: Initial prompt -> 8 unresolved dimensions -> PAUSED
    turn1 = graph.invoke(
        {
            "session_id": session_id,
            "thread_id": session_id,
            "lifecycle_status": LifecycleStatus.INITIALIZING.value,
            "user_prompt": "I have a prison project — escape-attempt detection",
        },
        config=config,
    )
    assert turn1["lifecycle_status"] == LifecycleStatus.PAUSED.value
    assert len(turn1["operational_inquiry"]["unresolved_dimensions"]) == 8

    # Turn 2: User provides only 2 answers (target_hardware, latency_budget_ms)
    # The mock LLM returns mock_llm_falsely_complete_response (unresolved_dimensions=[], targeted_questions=[])
    partial_answers = {
        "target_hardware": "NVIDIA Jetson AGX Orin 64GB",
        "latency_budget_ms": "100.0",
    }
    graph.update_state(config, {"human_answers": partial_answers})
    turn2 = graph.invoke(None, config=config)

    # Invariant verified: Workflow deterministically checks accumulated evidence, rejects LLM claim,
    # does NOT enter DEFINE, and remains PAUSED awaiting human operational input.
    assert turn2["lifecycle_status"] == LifecycleStatus.PAUSED.value
    assert store.get_lifecycle_status(session_id) == LifecycleStatus.PAUSED
    assert turn2["needs_elicitation"] is True
    # At least 5 canonical dimensions are missing (camera_infrastructure, physical_environment, error_tolerance, data_availability, intervention_modality)
    assert len(turn2["operational_inquiry"]["unresolved_dimensions"]) >= 5
    assert len(turn2["operational_inquiry"]["targeted_questions"]) >= 5
    assert turn2.get("project_spec_committed") is not True
    assert memory.get_project_specification() is None


def test_b27_capability_intent_lossless_round_trip(tmp_path: Path) -> None:
    """B27: Complete typed CapabilityIntent serialization/deserialization is lossless and deterministic.

    Verifies round-trip equality across capability_id, intent_description,
    parameter_intent (including nested structures and booleans/integers),
    and target_preference (both specified and None).
    """
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)

    char = ProblemCharacterization(
        task_type="escape_attempt_detection",
        target_hardware="NVIDIA Jetson AGX Orin 64GB",
        operational_definition="Detect fence breaches within 300ms",
        physical_environment="Outdoor perimeter fence line",
        camera_infrastructure="CCTV 1080p 30fps",
        error_tolerance="Zero missed breaches",
        data_availability="50 hours annotated video",
        intervention_modality="Real-time alarm",
        latency_budget_ms=100.0,
        throughput_fps=30.0,
    )

    dag = CVTaskDecompositionDAG(
        tasks=(
            TaskNode(
                task_id="t1",
                task_type="object_detection",
                description="Detect humans near perimeter",
            ),
        )
    )

    hw = HardwareConstraints(
        target_device="NVIDIA Jetson AGX Orin 64GB",
        latency_ceiling_ms=100.0,
    )

    # CapabilityIntents covering all 4 fields:
    # 1. Populated parameter_intent with nested dict/list/bool/int and explicit target_preference
    # 2. Empty parameter_intent and None target_preference
    cap1 = CapabilityIntent(
        capability_id=CapabilityId("cv.requirements.analysis"),
        intent_description="Analyze operational requirements against edge hardware ceilings",
        parameter_intent={
            "model_family": "detection",
            "batch_size": 16,
            "fp16": True,
            "nested_config": {"threshold": 0.75, "classes": ["person", "vehicle"]},
        },
        target_preference="remote_gpu",
    )
    cap2 = CapabilityIntent(
        capability_id=CapabilityId("cv.problem.formulation"),
        intent_description="Formulate multi-stage vision pipeline for perimeter monitoring",
        parameter_intent={},
        target_preference=None,
    )

    spec = ProjectSpecification(
        characterization=char,
        decomposition=dag,
        hardware_constraints=hw,
        capability_intents=(cap1, cap2),
        architectural_choices=(),
        dataset_lineage_notes="Authoritative baseline test dataset.",
    )

    # 1. Commit specification to GitProjectMemory
    memory.commit_project_specification(spec)

    # 2. Reload specification from disk
    reloaded = memory.get_project_specification()
    assert reloaded is not None

    # 3. Assert full tuple equality
    assert reloaded.capability_intents == spec.capability_intents

    # 4. Assert individual typed field equality
    assert len(reloaded.capability_intents) == 2
    for orig, loaded in zip(spec.capability_intents, reloaded.capability_intents):
        assert loaded.capability_id == orig.capability_id
        assert loaded.intent_description == orig.intent_description
        assert loaded.parameter_intent == orig.parameter_intent
        assert loaded.target_preference == orig.target_preference

    # Specific checks on cap1
    loaded_cap1 = reloaded.capability_intents[0]
    assert loaded_cap1.target_preference == "remote_gpu"
    assert loaded_cap1.parameter_intent["batch_size"] == 16
    assert loaded_cap1.parameter_intent["fp16"] is True
    assert loaded_cap1.parameter_intent["nested_config"]["classes"] == (
        "person",
        "vehicle",
    )  # type: ignore[index]

    # Specific checks on cap2
    loaded_cap2 = reloaded.capability_intents[1]
    assert loaded_cap2.target_preference is None
    assert loaded_cap2.parameter_intent == {}
