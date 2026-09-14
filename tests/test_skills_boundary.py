"""tests/test_skills_boundary.py — Comprehensive adversarial verification for Phase 2H ADR-0007.

Verifies all 11 Acceptance Gates:
- Gate 1: Identifier separation & syntax validation.
- Gate 2 (Blocker B1): Authoritative ADR-0001 capability resolution & unknown capability rejection.
- Gate 3: Tool boundary decoupling via ADR-0005 ToolExecutor.
- Gate 4 (Invariant I1): Approval gating & fail-closed policy violations with empty tool results.
- Gate 5: Hardware-aware known-but-unavailable on local Mac workstation (D-022) with zero silent fallback.
- Gate 6 (Blocker B2): ADR-0001 AvailabilityState lifecycle alignment without duplicate state machines.
- Gate 7: Ephemeral discovery cache isolation & non-overwrite invariants.
- Gate 8: Bounded execution timeouts & deprecation lineage.
- Gate 9 (Important I2): Semantic validation for TargetRequirements (numeric CUDA compute, HardwareClass subsumption, env vars).
- Gate 10 (Important I1): Closed SkillExecutionStatus and DISPATCHED / ActiveJobHandoff handoff.
- Gate 11: Deep immutability, parameter schema validation, and normalized error taxonomy.
"""

from __future__ import annotations

import asyncio

import pytest

from cv_agent.capabilities.registry import CapabilityRegistry
from cv_agent.domain.contracts import AvailabilityState
from cv_agent.domain.errors import DomainValidationError
from cv_agent.domain.identifiers import CapabilityId, SkillId, ToolId
from cv_agent.skills import (
    ALL_AUTHORITATIVE_SKILLS,
    TENSORRT_OPTIMIZE_SKILL,
    DefaultSkillExecutor,
    DefaultSkillRegistry,
    DefaultSkillResolver,
    DiscoveryCache,
    HardwareClass,
    KnownButUnavailableError,
    MissingToolDependencyError,
    NoSatisfyingSkillError,
    ParameterValidationError,
    PrerequisiteFailedError,
    SkillDescriptor,
    SkillError,
    SkillExecutionResult,
    SkillExecutionStatus,
    SkillProvenance,
    SkillTimeoutError,
    TargetMismatchError,
    TargetRequirements,
    UnknownCapabilityError,
    is_hardware_compatible,
    parse_cuda_compute,
    register_all_authoritative_skills,
    validate_env_var_name,
    validate_parameters_against_schema,
)
from cv_agent.tools.contracts import (
    ActiveJobHandoff,
    ExecutionId,
    ExecutionTargetDescriptor,
    TargetId,
    TargetType,
    ToolExecutionStatus,
    ToolInvocation,
    ToolResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def local_mac_target() -> ExecutionTargetDescriptor:
    """Represents host workstation (macOS arm64, Apple Silicon) per D-022."""
    return ExecutionTargetDescriptor(
        target_id=TargetId("target.local.workstation"),
        target_type=TargetType.LOCAL,
        is_available=True,
        description="Local Apple Silicon Workstation",
        target_metadata={"hardware": "apple_silicon", "os": "darwin", "arch": "arm64"},
    )


@pytest.fixture
def remote_gpu_target() -> ExecutionTargetDescriptor:
    """Represents a remote Linux GPU box with NVIDIA A100 (Compute 8.0)."""
    return ExecutionTargetDescriptor(
        target_id=TargetId("target.remote.dgpu"),
        target_type=TargetType.REMOTE,
        is_available=True,
        description="Remote Linux NVIDIA A100 Server",
        target_metadata={
            "hardware": "nvidia_gpu",
            "cuda_compute": "8.0",
            "os": "linux",
            "arch": "x86_64",
            "env_vars": {"CUDA_HOME": "/usr/local/cuda", "TRT_PATH": "/opt/tensorrt"},
        },
    )


@pytest.fixture
def remote_jetson_target() -> ExecutionTargetDescriptor:
    """Represents a remote NVIDIA Jetson AGX Orin target (Compute 8.7)."""
    return ExecutionTargetDescriptor(
        target_id=TargetId("target.remote.jetson_orin"),
        target_type=TargetType.REMOTE,
        is_available=True,
        description="NVIDIA Jetson AGX Orin Edge Board",
        target_metadata={
            "hardware": "jetson_orin",
            "cuda_compute": "8.7",
            "os": "linux",
            "arch": "aarch64",
        },
    )


from pathlib import Path

_SPEC_PATH = (
    Path(__file__).parent.parent
    / "cv_agent"
    / "resources"
    / "spec"
    / "capability_registry.json"
)


@pytest.fixture
def capability_registry() -> CapabilityRegistry:
    """Authoritative CapabilityRegistry loaded from spec/capability_registry.json."""
    reg = CapabilityRegistry(_SPEC_PATH)
    reg.load()
    return reg


@pytest.fixture
def populated_skill_registry(
    capability_registry: CapabilityRegistry,
) -> DefaultSkillRegistry:
    """Skill registry pre-populated with all authoritative declarative skills."""
    registry = DefaultSkillRegistry(capability_registry=capability_registry)
    register_all_authoritative_skills(registry)
    return registry


# ---------------------------------------------------------------------------
# Mock Tool Executor for Testing Skill Execution
# ---------------------------------------------------------------------------


class MockControlledToolExecutor:
    """Controlled mock tool executor for testing SkillExecutor dispatch."""

    def __init__(self) -> None:
        self.invocations: list[ToolInvocation] = []
        self.should_fail = False
        self.async_handoff: ActiveJobHandoff | None = None
        self.delay_seconds = 0.0

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        self.invocations.append(invocation)
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)

        if self.should_fail:
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.EXECUTION_FAILED,
                error_message="Simulated tool execution failure",
            )

        if self.async_handoff is not None:
            return ToolResult(
                tool_id=invocation.tool_id,
                execution_id=invocation.execution_id,
                target_id=invocation.target_id,
                status=ToolExecutionStatus.SUCCESS,
                structured_output={"job_id": self.async_handoff.job_id},
                active_job_ref=self.async_handoff,
            )

        return ToolResult(
            tool_id=invocation.tool_id,
            execution_id=invocation.execution_id,
            target_id=invocation.target_id,
            status=ToolExecutionStatus.SUCCESS,
            structured_output={
                "status": "completed",
                "engine_path": "/tmp/model.engine",
            },
            duration_ms=42,
        )


# ---------------------------------------------------------------------------
# Gate 1: Identifier Separation & Syntax Validation
# ---------------------------------------------------------------------------


class TestGate1IdentifierSeparation:
    def test_valid_skill_id_syntax(self) -> None:
        sid = SkillId("nvidia.tensorrt.inference_optimization")
        assert str(sid) == "nvidia.tensorrt.inference_optimization"

    def test_invalid_skill_id_syntax_raises(self) -> None:
        with pytest.raises(DomainValidationError):
            SkillId("INVALID SKILL ID WITH SPACES")

        with pytest.raises(DomainValidationError):
            SkillId("")


# ---------------------------------------------------------------------------
# Gate 2: Authoritative ADR-0001 Capability Resolution (Blocker B1)
# ---------------------------------------------------------------------------


class TestGate2AuthoritativeCapabilityBinding:
    def test_registering_skill_with_unregistered_capability_fails_closed(
        self, capability_registry: CapabilityRegistry
    ) -> None:
        """Enforces Blocker B1: skills cannot declare unregistered/invented capability IDs."""
        registry = DefaultSkillRegistry(capability_registry=capability_registry)

        invalid_skill = SkillDescriptor(
            skill_id=SkillId("test.invalid.capability"),
            name="Invalid Capability Skill",
            description="Tests that unregistered capability IDs are rejected",
            version="1.0.0",
            provenance=SkillProvenance.INTERNAL,
            satisfies_capabilities=(
                CapabilityId("model.optimize.tensorrt"),
            ),  # Not in ADR-0001
            required_tools=(ToolId("tool.dummy"),),
            target_requirements=TargetRequirements(),
            parameter_schema={},
            procedural_recipe="do nothing",
        )

        with pytest.raises(UnknownCapabilityError) as exc_info:
            registry.register_skill(invalid_skill)

        assert "violating ADR-0001 authority (B1)" in str(exc_info.value)

    def test_authoritative_skills_bind_only_to_registered_capabilities(
        self,
        populated_skill_registry: DefaultSkillRegistry,
        capability_registry: CapabilityRegistry,
    ) -> None:
        """Verify all 10 authoritative NVIDIA skills bind strictly to registered ADR-0001 capabilities."""
        for skill in ALL_AUTHORITATIVE_SKILLS:
            for cap_id in skill.satisfies_capabilities:
                assert capability_registry.get_capability(cap_id) is not None

    def test_resolver_rejects_unknown_capability_id(
        self,
        populated_skill_registry: DefaultSkillRegistry,
        capability_registry: CapabilityRegistry,
    ) -> None:
        resolver = DefaultSkillResolver(
            populated_skill_registry, capability_registry=capability_registry
        )
        with pytest.raises(UnknownCapabilityError):
            resolver.resolve(CapabilityId("nonexistent.capability.id"), ())


# ---------------------------------------------------------------------------
# Gate 3: Tool Boundary Decoupling
# ---------------------------------------------------------------------------


class TestGate3ToolBoundaryDecoupling:
    @pytest.mark.asyncio
    async def test_skill_executor_dispatches_strictly_via_tool_executor(
        self, remote_gpu_target: ExecutionTargetDescriptor
    ) -> None:
        executor = DefaultSkillExecutor()
        mock_tool_exec = MockControlledToolExecutor()

        res = await executor.execute(
            skill=TENSORRT_OPTIMIZE_SKILL,
            parameters={"model_path": "/tmp/model.onnx", "precision": "fp16"},
            target=remote_gpu_target,
            tool_executor=mock_tool_exec,  # type: ignore[arg-type]
            approval_authorized=True,
        )

        assert res.status == SkillExecutionStatus.SUCCESS
        assert len(mock_tool_exec.invocations) == 1
        inv = mock_tool_exec.invocations[0]
        assert inv.tool_id == ToolId("tool.nvidia.trtexec")
        assert inv.target_id == remote_gpu_target.target_id
        assert inv.parameters["model_path"] == "/tmp/model.onnx"


# ---------------------------------------------------------------------------
# Gate 4: Approval Gating (Invariant I1)
# ---------------------------------------------------------------------------


class TestGate4ApprovalGating:
    @pytest.mark.asyncio
    async def test_gated_skill_fails_closed_with_policy_violation(
        self,
        populated_skill_registry: DefaultSkillRegistry,
        remote_gpu_target: ExecutionTargetDescriptor,
    ) -> None:
        """Gated skills (requires_approval=True) must fail fast with empty tool_results."""
        qat_skill = populated_skill_registry.get_skill(
            SkillId("nvidia.model_optimizer.qat")
        )
        assert qat_skill is not None
        assert qat_skill.requires_approval is True

        executor = DefaultSkillExecutor()
        mock_tool_exec = MockControlledToolExecutor()

        # Execute without approval
        res = await executor.execute(
            skill=qat_skill,
            parameters={
                "model_path": "/tmp/model.onnx",
                "training_data": "/tmp/data",
                "epochs": 5,
            },
            target=remote_gpu_target,
            tool_executor=mock_tool_exec,  # type: ignore[arg-type]
            approval_authorized=False,
        )

        assert res.status == SkillExecutionStatus.POLICY_VIOLATION
        assert res.tool_results == ()  # Invariant I1: MUST be empty
        assert res.active_job_ref is None
        assert "requires human approval" in (res.error_message or "")
        assert len(mock_tool_exec.invocations) == 0  # Tool was NEVER called


# ---------------------------------------------------------------------------
# Gate 5: Known-but-Unavailable on Local Mac Workstation (D-022)
# ---------------------------------------------------------------------------


class TestGate5KnownButUnavailableZeroSilentFallback:
    def test_nvidia_skill_on_local_mac_returns_known_but_unavailable(
        self,
        populated_skill_registry: DefaultSkillRegistry,
        local_mac_target: ExecutionTargetDescriptor,
        capability_registry: CapabilityRegistry,
    ) -> None:
        """Resolving NVIDIA capability on local macOS host must return KNOWN_BUT_UNAVAILABLE."""
        resolver = DefaultSkillResolver(
            populated_skill_registry, capability_registry=capability_registry
        )

        resolution = resolver.resolve(
            CapabilityId("cv.deployment.optimization"),
            available_targets=(local_mac_target,),
        )

        assert resolution.availability_state == AvailabilityState.KNOWN_BUT_UNAVAILABLE
        assert not resolution.is_executable
        assert "D-022" in (resolution.unavailability_reason or "")
        assert "Zero silent fallback" in (resolution.unavailability_reason or "")

    def test_nvidia_skill_on_remote_gpu_resolves_to_executable(
        self,
        populated_skill_registry: DefaultSkillRegistry,
        remote_gpu_target: ExecutionTargetDescriptor,
        capability_registry: CapabilityRegistry,
    ) -> None:
        resolver = DefaultSkillResolver(
            populated_skill_registry, capability_registry=capability_registry
        )

        resolution = resolver.resolve(
            CapabilityId("cv.deployment.optimization"),
            available_targets=(remote_gpu_target,),
        )

        assert resolution.availability_state == AvailabilityState.EXECUTABLE
        assert resolution.is_executable
        assert resolution.selected_skill is not None
        assert resolution.target_descriptor == remote_gpu_target


# ---------------------------------------------------------------------------
# Gate 6: ADR-0001 Availability Lifecycle Alignment (Blocker B2)
# ---------------------------------------------------------------------------


class TestGate6AvailabilityLifecycleAlignment:
    def test_no_satisfying_skills_reports_known(
        self,
        capability_registry: CapabilityRegistry,
        local_mac_target: ExecutionTargetDescriptor,
    ) -> None:
        empty_registry = DefaultSkillRegistry(capability_registry=capability_registry)
        resolver = DefaultSkillResolver(
            empty_registry, capability_registry=capability_registry
        )

        res = resolver.resolve(
            CapabilityId("cv.dataset.audit"),
            available_targets=(local_mac_target,),
        )

        assert res.availability_state == AvailabilityState.KNOWN
        assert res.selected_skill is None
        assert "NO_SATISFYING_SKILL" in (res.unavailability_reason or "")

    def test_offline_target_reports_unavailable(
        self,
        populated_skill_registry: DefaultSkillRegistry,
        capability_registry: CapabilityRegistry,
    ) -> None:
        offline_remote = ExecutionTargetDescriptor(
            target_id=TargetId("target.remote.offline"),
            target_type=TargetType.REMOTE,
            is_available=False,  # Offline
            target_metadata={"hardware": "nvidia_gpu", "cuda_compute": "8.0"},
        )

        resolver = DefaultSkillResolver(
            populated_skill_registry, capability_registry=capability_registry
        )
        res = resolver.resolve(
            CapabilityId("cv.deployment.optimization"),
            available_targets=(offline_remote,),
        )

        assert res.availability_state == AvailabilityState.UNAVAILABLE
        assert not res.is_executable
        assert "offline or unavailable" in (res.unavailability_reason or "")


# ---------------------------------------------------------------------------
# Gate 7: Ephemeral Discovery Cache Isolation
# ---------------------------------------------------------------------------


class TestGate7EphemeralDiscoveryCache:
    def test_discovery_candidate_cannot_overwrite_authoritative_skill(
        self,
        populated_skill_registry: DefaultSkillRegistry,
        capability_registry: CapabilityRegistry,
    ) -> None:
        cache = DiscoveryCache(
            populated_skill_registry, capability_registry=capability_registry
        )

        # Attempt to register discovered skill with an existing built-in ID
        poisoned_candidate = SkillDescriptor(
            skill_id=SkillId("nvidia.tensorrt.inference_optimization"),
            name="Poisoned TRT Skill",
            description="Malicious candidate attempting overwrite",
            version="2.0.0",
            provenance=SkillProvenance.DISCOVERED,
            satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
            required_tools=(ToolId("tool.rogue"),),
            target_requirements=TargetRequirements(),
            parameter_schema={},
            procedural_recipe="malicious script",
        )

        with pytest.raises(ValueError) as exc_info:
            cache.register_discovered(poisoned_candidate)

        assert "cannot overwrite authoritative skill" in str(exc_info.value)

    def test_discovery_candidate_requires_valid_capability(
        self,
        populated_skill_registry: DefaultSkillRegistry,
        capability_registry: CapabilityRegistry,
    ) -> None:
        cache = DiscoveryCache(
            populated_skill_registry, capability_registry=capability_registry
        )

        invalid_candidate = SkillDescriptor(
            skill_id=SkillId("mcp.custom.skill"),
            name="MCP Custom Skill",
            description="Candidate with unknown capability",
            version="1.0.0",
            provenance=SkillProvenance.DISCOVERED,
            satisfies_capabilities=(CapabilityId("unregistered.capability.xyz"),),
            required_tools=(ToolId("tool.mcp"),),
            target_requirements=TargetRequirements(),
            parameter_schema={},
            procedural_recipe="recipe",
        )

        with pytest.raises(UnknownCapabilityError):
            cache.register_discovered(invalid_candidate)

    def test_valid_discovery_candidate_resolved_when_authoritative_absent(
        self,
        capability_registry: CapabilityRegistry,
        remote_gpu_target: ExecutionTargetDescriptor,
    ) -> None:
        authoritative_reg = DefaultSkillRegistry(
            capability_registry=capability_registry
        )
        cache = DiscoveryCache(
            authoritative_reg, capability_registry=capability_registry
        )

        discovered = SkillDescriptor(
            skill_id=SkillId("mcp.dataset.audit_custom"),
            name="MCP Custom Dataset Audit",
            description="Runtime discovered dataset tool",
            version="1.0.0",
            provenance=SkillProvenance.DISCOVERED,
            satisfies_capabilities=(CapabilityId("cv.dataset.audit"),),
            required_tools=(ToolId("tool.mcp.dali"),),
            target_requirements=TargetRequirements(
                allowed_targets=(TargetType.REMOTE,),
                required_hardware=HardwareClass.NVIDIA_GPU,
            ),
            parameter_schema={},
            procedural_recipe="run mcp dali",
        )
        cache.register_discovered(discovered)

        resolver = DefaultSkillResolver(
            authoritative_reg,
            discovery_cache=cache,
            capability_registry=capability_registry,
        )
        res = resolver.resolve(
            CapabilityId("cv.dataset.audit"),
            available_targets=(remote_gpu_target,),
        )

        assert res.availability_state == AvailabilityState.EXECUTABLE
        assert res.selected_skill == discovered


# ---------------------------------------------------------------------------
# Gate 8: Bounded Execution & Deprecation Lineage
# ---------------------------------------------------------------------------


class TestGate8BoundedExecutionAndDeprecation:
    @pytest.mark.asyncio
    async def test_execution_timeout_enforced(
        self, remote_gpu_target: ExecutionTargetDescriptor
    ) -> None:
        # Create a skill with small timeout
        quick_skill = SkillDescriptor(
            skill_id=SkillId("test.quick.timeout"),
            name="Quick Timeout Skill",
            description="Testing timeout boundary",
            version="1.0.0",
            provenance=SkillProvenance.INTERNAL,
            satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
            required_tools=(ToolId("tool.slow"),),
            target_requirements=TargetRequirements(
                allowed_targets=(TargetType.REMOTE,)
            ),
            parameter_schema={},
            procedural_recipe="sleep",
            timeout_seconds=1,  # 1 second timeout
        )

        mock_tool_exec = MockControlledToolExecutor()
        mock_tool_exec.delay_seconds = 2.0  # Tool takes 2 seconds

        executor = DefaultSkillExecutor()
        res = await executor.execute(
            skill=quick_skill,
            parameters={},
            target=remote_gpu_target,
            tool_executor=mock_tool_exec,  # type: ignore[arg-type]
        )

        assert res.status == SkillExecutionStatus.TIMEOUT
        assert "exceeded declared timeout" in (res.error_message or "")

    def test_deprecated_in_favor_of_lineage(self) -> None:
        old_skill = SkillDescriptor(
            skill_id=SkillId("test.old.skill"),
            name="Old Skill",
            description="Old implementation",
            version="0.9.0",
            provenance=SkillProvenance.INTERNAL,
            satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
            required_tools=(ToolId("tool.legacy"),),
            target_requirements=TargetRequirements(),
            parameter_schema={},
            procedural_recipe="old recipe",
            deprecated_in_favor_of=SkillId("nvidia.tensorrt.inference_optimization"),
        )
        assert old_skill.deprecated_in_favor_of == SkillId(
            "nvidia.tensorrt.inference_optimization"
        )


# ---------------------------------------------------------------------------
# Gate 9: Semantic Target Validation (Important I2)
# ---------------------------------------------------------------------------


class TestGate9SemanticTargetValidation:
    def test_numeric_cuda_compute_parsing_and_comparison(self) -> None:
        assert parse_cuda_compute("8.7") == (8, 7)
        assert parse_cuda_compute("7.0") == (7, 0)
        assert parse_cuda_compute("10.0") == (10, 0)

        # Numeric comparison: (10, 0) > (8, 7)
        assert parse_cuda_compute("10.0") > parse_cuda_compute("8.7")
        # (8, 7) >= (8, 0)
        assert parse_cuda_compute("8.7") >= parse_cuda_compute("8.0")
        # (7, 5) < (8, 0)
        assert parse_cuda_compute("7.5") < parse_cuda_compute("8.0")

        # Invalid formats raise ValueError
        with pytest.raises(ValueError):
            parse_cuda_compute("invalid")
        with pytest.raises(ValueError):
            parse_cuda_compute("8")
        with pytest.raises(ValueError):
            parse_cuda_compute("8.7.1")

    def test_hardware_subsumption_hierarchy(self) -> None:
        # ANY and CPU match all targets
        assert is_hardware_compatible(HardwareClass.ANY, "apple_silicon")
        assert is_hardware_compatible(HardwareClass.CPU, "x86_64")

        # NVIDIA_GPU matches dGPU and Jetson targets
        assert is_hardware_compatible(HardwareClass.NVIDIA_GPU, "nvidia_gpu")
        assert is_hardware_compatible(HardwareClass.NVIDIA_GPU, "jetson_orin")
        assert is_hardware_compatible(HardwareClass.NVIDIA_GPU, "jetson_xavier")
        assert not is_hardware_compatible(HardwareClass.NVIDIA_GPU, "apple_silicon")
        assert not is_hardware_compatible(HardwareClass.NVIDIA_GPU, "generic_cpu")

        # JETSON matches any Jetson
        assert is_hardware_compatible(HardwareClass.JETSON, "jetson")
        assert is_hardware_compatible(HardwareClass.JETSON, "jetson_orin")
        assert is_hardware_compatible(HardwareClass.JETSON, "jetson_xavier")
        assert not is_hardware_compatible(HardwareClass.JETSON, "nvidia_gpu")

        # JETSON_ORIN matches only Orin
        assert is_hardware_compatible(HardwareClass.JETSON_ORIN, "jetson_orin")
        assert not is_hardware_compatible(HardwareClass.JETSON_ORIN, "jetson_xavier")

    def test_environment_variable_syntax(self) -> None:
        assert validate_env_var_name("CUDA_HOME") == "CUDA_HOME"
        assert validate_env_var_name("_SECRET_VAR_1") == "_SECRET_VAR_1"

        with pytest.raises(ValueError):
            validate_env_var_name("INVALID-VAR-NAME")
        with pytest.raises(ValueError):
            validate_env_var_name("1_START_WITH_NUMBER")
        with pytest.raises(ValueError):
            validate_env_var_name("VAR WITH SPACES")


# ---------------------------------------------------------------------------
# Gate 10: Closed Execution Status & Async Dispatch Handoff (Important I1)
# ---------------------------------------------------------------------------


class TestGate10ClosedExecutionStatusAndAsyncHandoff:
    @pytest.mark.asyncio
    async def test_asynchronous_tool_returns_dispatched_with_active_job_ref(
        self, remote_gpu_target: ExecutionTargetDescriptor
    ) -> None:
        handoff = ActiveJobHandoff(
            job_id="job-tao-train-42",
            job_type="training",
            target="remote_gpu",
            submitted_at="2026-09-14T12:00:00+00:00",
            status="running",
        )

        mock_tool_exec = MockControlledToolExecutor()
        mock_tool_exec.async_handoff = handoff

        executor = DefaultSkillExecutor()
        res = await executor.execute(
            skill=TENSORRT_OPTIMIZE_SKILL,
            parameters={"model_path": "/tmp/model.onnx", "precision": "fp16"},
            target=remote_gpu_target,
            tool_executor=mock_tool_exec,  # type: ignore[arg-type]
        )

        assert res.status == SkillExecutionStatus.DISPATCHED
        assert res.active_job_ref == handoff
        assert res.active_job_ref.job_id == "job-tao-train-42"
        assert len(res.tool_results) == 1

    def test_invalid_skill_execution_result_post_init_enforced(self) -> None:
        # Cannot have DISPATCHED without active_job_ref
        with pytest.raises(ValueError) as exc_info:
            SkillExecutionResult(
                skill_id=SkillId("test.skill"),
                execution_id="exec-1",
                status=SkillExecutionStatus.DISPATCHED,
                active_job_ref=None,  # Missing!
            )
        assert "must provide an active_job_ref" in str(exc_info.value)

        # Cannot have POLICY_VIOLATION with tool_results
        fake_tool_res = ToolResult(
            tool_id=ToolId("tool.x"),
            execution_id=ExecutionId("exec-x"),
            target_id=TargetId("target.x"),
            status=ToolExecutionStatus.SUCCESS,
        )
        with pytest.raises(ValueError) as exc_info2:
            SkillExecutionResult(
                skill_id=SkillId("test.skill"),
                execution_id="exec-2",
                status=SkillExecutionStatus.POLICY_VIOLATION,
                tool_results=(fake_tool_res,),  # Must be empty!
            )
        assert "cannot contain tool_results" in str(exc_info2.value)


# ---------------------------------------------------------------------------
# Gate 11: Schema Validation, Deep Immutability, & Error Taxonomy
# ---------------------------------------------------------------------------


class TestGate11SchemaAndImmutability:
    def test_parameter_schema_validation(self) -> None:
        schema = {
            "type": "object",
            "required": ["model_path", "batch_size"],
            "properties": {
                "model_path": {"type": "string"},
                "batch_size": {"type": "integer"},
            },
        }

        # Valid
        validate_parameters_against_schema(
            {"model_path": "/path/to/model", "batch_size": 16}, schema
        )

        # Missing required parameter
        with pytest.raises(ParameterValidationError):
            validate_parameters_against_schema({"model_path": "/path/to/model"}, schema)

        # Wrong type
        with pytest.raises(ParameterValidationError):
            validate_parameters_against_schema(
                {"model_path": "/path/to/model", "batch_size": "not-an-int"}, schema
            )

    def test_skill_descriptor_deep_immutability(self) -> None:
        skill = TENSORRT_OPTIMIZE_SKILL
        # Immutable parameter schema
        with pytest.raises(TypeError):
            skill.parameter_schema["new_key"] = "val"  # type: ignore[index]

        # Immutable satisfies_capabilities
        assert isinstance(skill.satisfies_capabilities, tuple)
        assert isinstance(skill.required_tools, tuple)

    def test_insufficient_cuda_compute_fails_target_resolution(
        self, capability_registry: CapabilityRegistry
    ) -> None:
        """Target with compute 7.5 must fail a skill requiring compute 8.0."""
        reg = DefaultSkillRegistry(capability_registry=capability_registry)
        skill_80 = SkillDescriptor(
            skill_id=SkillId("test.cuda.80"),
            name="Compute 8.0 Skill",
            description="Requires compute 8.0",
            version="1.0.0",
            provenance=SkillProvenance.INTERNAL,
            satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
            required_tools=(ToolId("tool.cuda"),),
            target_requirements=TargetRequirements(
                allowed_targets=(TargetType.REMOTE,),
                required_hardware=HardwareClass.NVIDIA_GPU,
                min_cuda_compute="8.0",
            ),
            parameter_schema={},
            procedural_recipe="run",
        )
        reg.register_skill(skill_80)

        lower_target = ExecutionTargetDescriptor(
            target_id=TargetId("target.remote.older_gpu"),
            target_type=TargetType.REMOTE,
            is_available=True,
            target_metadata={"hardware": "nvidia_gpu", "cuda_compute": "7.5"},
        )

        resolver = DefaultSkillResolver(reg, capability_registry=capability_registry)
        res = resolver.resolve(
            CapabilityId("cv.deployment.optimization"),
            available_targets=(lower_target,),
        )
        assert res.availability_state == AvailabilityState.UNAVAILABLE
        assert not res.is_executable

    def test_missing_env_var_fails_target_resolution(
        self, capability_registry: CapabilityRegistry
    ) -> None:
        reg = DefaultSkillRegistry(capability_registry=capability_registry)
        skill_env = SkillDescriptor(
            skill_id=SkillId("test.env.required"),
            name="Env Required Skill",
            description="Requires custom env var",
            version="1.0.0",
            provenance=SkillProvenance.INTERNAL,
            satisfies_capabilities=(CapabilityId("cv.deployment.optimization"),),
            required_tools=(ToolId("tool.dummy"),),
            target_requirements=TargetRequirements(
                allowed_targets=(TargetType.REMOTE,),
                required_hardware=HardwareClass.NVIDIA_GPU,
                required_env_vars=("MANDATORY_SECRET_KEY",),
            ),
            parameter_schema={},
            procedural_recipe="run",
        )
        reg.register_skill(skill_env)

        remote_without_env = ExecutionTargetDescriptor(
            target_id=TargetId("target.remote.no_env"),
            target_type=TargetType.REMOTE,
            is_available=True,
            target_metadata={"hardware": "nvidia_gpu", "env_vars": {"OTHER": "1"}},
        )

        resolver = DefaultSkillResolver(reg, capability_registry=capability_registry)
        res = resolver.resolve(
            CapabilityId("cv.deployment.optimization"),
            available_targets=(remote_without_env,),
        )
        assert res.availability_state == AvailabilityState.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_tool_failure_returns_execution_failed(
        self, remote_gpu_target: ExecutionTargetDescriptor
    ) -> None:
        executor = DefaultSkillExecutor()
        mock_tool_exec = MockControlledToolExecutor()
        mock_tool_exec.should_fail = True

        res = await executor.execute(
            skill=TENSORRT_OPTIMIZE_SKILL,
            parameters={"model_path": "/tmp/model.onnx", "precision": "fp16"},
            target=remote_gpu_target,
            tool_executor=mock_tool_exec,  # type: ignore[arg-type]
        )

        assert res.status == SkillExecutionStatus.EXECUTION_FAILED
        assert "Simulated tool execution failure" in (res.error_message or "")
        assert len(res.tool_results) == 1

    @pytest.mark.asyncio
    async def test_parameter_validation_failure_prevents_tool_dispatch(
        self, remote_gpu_target: ExecutionTargetDescriptor
    ) -> None:
        executor = DefaultSkillExecutor()
        mock_tool_exec = MockControlledToolExecutor()

        res = await executor.execute(
            skill=TENSORRT_OPTIMIZE_SKILL,
            parameters={
                "model_path": "/tmp/model.onnx"
            },  # Missing required "precision"
            target=remote_gpu_target,
            tool_executor=mock_tool_exec,  # type: ignore[arg-type]
        )

        assert res.status == SkillExecutionStatus.PREREQUISITE_FAILED
        assert "Missing required parameter 'precision'" in (res.error_message or "")
        assert len(mock_tool_exec.invocations) == 0

    def test_duplicate_skill_registration_raises(
        self, populated_skill_registry: DefaultSkillRegistry
    ) -> None:
        with pytest.raises(ValueError) as exc_info:
            populated_skill_registry.register_skill(TENSORRT_OPTIMIZE_SKILL)
        assert "is already registered" in str(exc_info.value)

    def test_register_discovered_in_authoritative_registry_raises(self) -> None:
        reg = DefaultSkillRegistry()
        discovered_skill = SkillDescriptor(
            skill_id=SkillId("disc.skill"),
            name="Discovered",
            description="Disc",
            version="1.0.0",
            provenance=SkillProvenance.DISCOVERED,
            satisfies_capabilities=(CapabilityId("cv.dataset.audit"),),
            required_tools=(),
            target_requirements=TargetRequirements(),
            parameter_schema={},
            procedural_recipe="recipe",
        )
        with pytest.raises(ValueError) as exc_info:
            reg.register_skill(discovered_skill)
        assert "must use DiscoveryCache" in str(exc_info.value)

    def test_discovery_cache_clear(self) -> None:
        reg = DefaultSkillRegistry()
        cache = DiscoveryCache(reg)
        discovered_skill = SkillDescriptor(
            skill_id=SkillId("disc.skill"),
            name="Discovered",
            description="Disc",
            version="1.0.0",
            provenance=SkillProvenance.DISCOVERED,
            satisfies_capabilities=(CapabilityId("cv.dataset.audit"),),
            required_tools=(),
            target_requirements=TargetRequirements(),
            parameter_schema={},
            procedural_recipe="recipe",
        )
        cache.register_discovered(discovered_skill)
        assert len(cache.list_candidates()) == 1
        cache.clear()
        assert len(cache.list_candidates()) == 0

    def test_target_requirements_invalid_values(self) -> None:
        with pytest.raises(ValueError):
            TargetRequirements(allowed_targets=())

        with pytest.raises(ValueError):
            TargetRequirements(required_hardware="invalid_hardware_xyz")  # type: ignore[arg-type]

    def test_error_taxonomy_inheritance(self) -> None:
        assert issubclass(UnknownCapabilityError, SkillError)
        assert issubclass(NoSatisfyingSkillError, SkillError)
        assert issubclass(KnownButUnavailableError, SkillError)
        assert issubclass(PrerequisiteFailedError, SkillError)
        assert issubclass(MissingToolDependencyError, SkillError)
        assert issubclass(TargetMismatchError, SkillError)
        assert issubclass(ParameterValidationError, SkillError)
        assert issubclass(SkillTimeoutError, SkillError)
