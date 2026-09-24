"""tests/test_discover_define_phase4_exit.py — Acceptance and Adversarial Suite for Phase 4.

Verifies the five Phase 4 Exit Authority Gates per Issue #70:
- Gate G1: DISCOVER — Operational elicitation dialogue before model proclamation (Zero-YOLO invariant).
- Gate G2: Multi-turn session checkpointing and resumption via SQLiteLangGraphCheckpointer.
- Gate G3: DEFINE — Problem characterization completeness (8 dimensions) and CV task DAG decomposition.
- Gate G4: Atomic GitProjectMemory persistence (project_spec.md, hardware_constraints.yaml, architectural_choices.md).
- Gate G5: Runtime execution barrier (0 tools/skills/subprocesses) and AST architecture audit.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

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
    initial_state: DiscoverDefineGraphState = {
        "session_id": session_id,
        "thread_id": session_id,
        "lifecycle_status": LifecycleStatus.INITIALIZING.value,
        "user_prompt": "I have a prison project — escape-attempt detection",
    }

    result = graph.invoke(initial_state, config=config)

    # 1. Verification of Gate G1: Session PAUSED awaiting operational answers
    assert result.get("lifecycle_status") == LifecycleStatus.PAUSED.value
    assert store.get_lifecycle_status(session_id) == LifecycleStatus.PAUSED

    # 2. Targeted operational questions emitted
    inquiry = result.get("operational_inquiry")
    assert inquiry is not None
    assert len(inquiry["targeted_questions"]) >= 5
    assert len(inquiry["unresolved_dimensions"]) >= 5

    # 3. Anti-YOLO invariant verification: ZERO concrete model mentions in inquiry
    assert_zero_prohibited_models(inquiry["context_summary"])
    for q in inquiry["targeted_questions"]:
        assert_zero_prohibited_models(q)

    # 4. Zero tool executions during DISCOVER
    assert barrier.invocation_count == 0


def test_gate_g2_resumption_from_checkpoint(
    tmp_path: Path,
    mock_prison_elicitation_response: dict[str, Any],
    mock_prison_framing_response: dict[str, Any],
    mock_prison_dag_response: dict[str, Any],
) -> None:
    """Gate G2: Resuming paused session from SQLiteLangGraphCheckpointer under active lease."""
    db_path = tmp_path / "orchestration.db"
    store = SQLiteOrchestrationStore(db_path)
    session_id = "session-prison-g2"
    lease = store.acquire_lease(session_id, session_id, "test-runner")
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)
    barrier = Phase4ExecutionBarrier()

    # Provide elicitation response, framing response, and DAG response in order
    gateway, _ = _setup_test_gateway(
        [
            mock_prison_elicitation_response,
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


def test_gate_g4_atomic_memory_rollback_on_prohibited_model(tmp_path: Path) -> None:
    """Gate G4: Prohibited model name in specification causes complete rollback (0 files altered)."""
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)

    # Invalid characterization mentioning prohibited model "yolov8"
    char = ProblemCharacterization(
        task_type="escape_attempt_detection using yolov8",
        target_hardware="NVIDIA Jetson AGX Orin",
        operational_definition="Detect fence breaches",
        physical_environment="Outdoor perimeter",
        camera_infrastructure="CCTV 1080p",
        error_tolerance="Zero missed breaches",
        data_availability="50 hours video",
        intervention_modality="Alarm",
        latency_budget_ms=100.0,
        throughput_fps=30.0,
    )

    dag = CVTaskDecompositionDAG(
        tasks=(
            TaskNode(
                task_id="t1", task_type="object_detection", description="Detect humans"
            ),
        )
    )
    hw = HardwareConstraints(
        target_device="NVIDIA Jetson AGX Orin",
        max_vram_mb=16384,
        max_ram_mb=32768,
        power_budget_watts=60.0,
        latency_ceiling_ms=100.0,
        supported_precisions=("fp16",),
    )

    spec = ProjectSpecification(
        characterization=char,
        decomposition=dag,
        hardware_constraints=hw,
    )

    # Attempting to commit must raise MemoryTransactionValidationError
    with pytest.raises(MemoryTransactionValidationError):
        memory.commit_project_specification(spec)

    # Verify no persistent files were written
    assert not memory.hardware_constraints_path.exists()
    assert memory.get_project_specification() is None


def test_gate_g4_atomic_memory_rejects_incomplete_characterization(
    tmp_path: Path,
) -> None:
    """Gate G4: Incomplete characterization (< 8 dimensions) is rejected before any write."""
    workspace = tmp_path / "workspace"
    memory = GitProjectMemory(workspace)

    # Missing operational_definition, camera_infrastructure, etc.
    char = ProblemCharacterization(
        task_type="escape_attempt_detection",
        target_hardware="NVIDIA Jetson AGX Orin",
    )
    assert char.is_well_formed is False

    dag = CVTaskDecompositionDAG(
        tasks=(
            TaskNode(
                task_id="t1", task_type="object_detection", description="Detect humans"
            ),
        )
    )
    hw = HardwareConstraints(
        target_device="NVIDIA Jetson AGX Orin",
        max_vram_mb=16384,
        max_ram_mb=32768,
        power_budget_watts=60.0,
        latency_ceiling_ms=100.0,
        supported_precisions=("fp16",),
    )

    spec = ProjectSpecification(
        characterization=char,
        decomposition=dag,
        hardware_constraints=hw,
    )

    with pytest.raises(MemoryTransactionValidationError) as exc_info:
        memory.commit_project_specification(spec)

    assert "characterization is incomplete" in str(exc_info.value).lower()
    assert not memory.hardware_constraints_path.exists()


# ==============================================================================
# Gate G5: Boundary Enforcement & AST Architecture Audit
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
    """Gate G5: Mechanically audit AST of reasoning, orchestration, and memory modules.

    Proves:
    1. Zero direct provider SDK imports (e.g. anthropic, openai, google.generativeai).
    2. Zero CUDA kernel launch syntax or bindings (pycuda, cupy).
    3. Zero PyTorch training loops.
    """
    repo_root = Path(__file__).resolve().parent.parent
    target_dirs = [
        repo_root / "cv_agent" / "reasoning",
        repo_root / "cv_agent" / "orchestration",
        repo_root / "cv_agent" / "memory",
    ]

    forbidden_modules = {
        "anthropic",
        "openai",
        "google.generativeai",
        "cohere",
        "mistralai",
        "pycuda",
        "cupy",
    }

    for target_dir in target_dirs:
        for py_file in target_dir.glob("**/*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name not in forbidden_modules, (
                            f"Forbidden provider SDK import {alias.name!r} found in {py_file}"
                        )
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    assert mod not in forbidden_modules, (
                        f"Forbidden provider SDK import {mod!r} found in {py_file}"
                    )


# ==============================================================================
# Facade Integration & Adversarial Scenarios
# ==============================================================================


def test_cvagent_facade_run_discover_define_end_to_end(
    tmp_path: Path,
    mock_prison_elicitation_response: dict[str, Any],
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
    """Adversarial: Cyclic CV task DAG is rejected during initialization."""
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
