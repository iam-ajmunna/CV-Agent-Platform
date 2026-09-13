"""tests/test_tools_boundary.py — Comprehensive acceptance tests for ADR-0005 Tools and MCP.

Verifies all 13 ADR-0005 contract areas and PM review constraints:
1. ToolId integration & syntax validation (TargetId, ExecutionId, MCPServerId, ToolId)
2. Registry authority preserved (ToolId owned by ADR-0001, duplicate rejection)
3. ExecutionTarget abstraction (LOCAL, REMOTE, MCP_ENDPOINT resolution and compatibility)
4. Representation of unconfigured NVIDIA tools (clean known-but-unavailable, no crashes, D-022)
5. Approval precondition enforcement (requires_approval=True without auth fails with PERMISSION_DENIED)
6. Prohibition of raw shell interpretation (structured argv only, shell=False)
7. Workspace path containment (path traversal and external paths rejected with POLICY_VIOLATION)
8. Secret isolation (rejection of sensitive credential parameter keys)
9. Parameter schema validation (deterministic enforcement rejecting invalid types/bounds)
10. Asynchronous ActiveJobHandoff (status=SUCCESS denotes dispatch accepted, not completion)
11. Handoff expiry invariant (expiry does not equate to execution failure)
12. MCP transport boundary (output schema validation, unmapped tool rejection, transport error)
13. Deep defensive immutability (recursive freezing across descriptors, invocations, results)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from cv_agent.domain.errors import DomainValidationError
from cv_agent.domain.identifiers import ToolId
from cv_agent.tools.adapters import (
    LocalProcessAdapter,
    MockMCPEndpointAdapter,
    MockRemoteTargetAdapter,
)
from cv_agent.tools.contracts import (
    ActiveJobHandoff,
    ExecutionId,
    ExecutionTargetDescriptor,
    MCPServerId,
    TargetId,
    TargetType,
    ToolDescriptor,
    ToolExecutionStatus,
    ToolInvocation,
    ToolResult,
    ToolRiskLevel,
)
from cv_agent.tools.executor import DefaultToolExecutor
from cv_agent.tools.registry import DefaultToolRegistry
from cv_agent.tools.security import (
    ToolSecurityError,
    check_approval_precondition,
    validate_argv_safety,
    validate_secret_isolation,
    validate_workspace_containment,
)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def sample_local_tool() -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("git-status"),
        name="Git Status Inspector",
        description="Inspects git working directory status",
        compatible_target_types=(TargetType.LOCAL,),
        input_schema={
            "type": "object",
            "properties": {
                "short": {"type": "boolean"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "clean": {"type": "boolean"},
            },
        },
        risk_level=ToolRiskLevel.READ_ONLY,
        requires_approval=False,
        default_timeout_seconds=30,
        is_idempotent=True,
    )


@pytest.fixture
def sample_nvidia_tool() -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("trt-build"),
        name="TensorRT Engine Builder",
        description="Compiles ONNX models to optimized TensorRT engines",
        compatible_target_types=(TargetType.REMOTE, TargetType.MCP_ENDPOINT),
        input_schema={
            "type": "object",
            "properties": {
                "onnx_path": {"type": "string"},
                "precision": {"type": "string", "enum": ["fp32", "fp16", "int8"]},
            },
            "required": ["onnx_path"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "engine_path": {"type": "string"},
            },
        },
        risk_level=ToolRiskLevel.RESOURCE_INTENSIVE,
        requires_approval=True,
        default_timeout_seconds=600,
        is_idempotent=False,
    )


class TestTypedIdentityAndSyntaxIntegrity:
    def test_valid_identifiers_conform_to_canonical_pattern(self) -> None:
        """Area 1: Valid ASCII namespaced identifiers matching [a-z0-9_.-]+(/[a-z0-9_.-]+)*."""
        tid = ToolId("cv.detector/yolo-v8")
        tgt = TargetId("cluster-gpu/node-01")
        eid = ExecutionId("exec-2026-09-13.001")
        sid = MCPServerId("deepstream-mcp-server")

        assert str(tid) == "cv.detector/yolo-v8"
        assert str(tgt) == "cluster-gpu/node-01"
        assert str(eid) == "exec-2026-09-13.001"
        assert str(sid) == "deepstream-mcp-server"

    def test_invalid_identifiers_raise_validation_error(self) -> None:
        """Area 1: Invalid identifier syntax is rejected deterministically."""
        with pytest.raises(DomainValidationError):
            ToolId("INVALID_UPPERCASE")

        with pytest.raises(DomainValidationError):
            TargetId("invalid space target")

        with pytest.raises(DomainValidationError):
            ExecutionId("")


class TestRegistryAuthorityAndOperationalView:
    def test_registry_stores_and_retrieves_tools(
        self, sample_local_tool: ToolDescriptor
    ) -> None:
        """Area 2: ToolRegistry stores and retrieves tool descriptors by ToolId."""
        reg = DefaultToolRegistry()
        reg.register(sample_local_tool)

        retrieved = reg.get(sample_local_tool.tool_id)
        assert retrieved is not None
        assert retrieved.name == sample_local_tool.name
        assert retrieved.risk_level == ToolRiskLevel.READ_ONLY

    def test_registry_rejects_duplicate_tool_registration(
        self, sample_local_tool: ToolDescriptor
    ) -> None:
        """Area 2: Duplicate registration of the same ToolId fails deterministically."""
        reg = DefaultToolRegistry()
        reg.register(sample_local_tool)

        with pytest.raises(ValueError) as exc:
            reg.register(sample_local_tool)
        assert "Duplicate tool registration" in str(exc.value)

    def test_registry_rejects_duplicate_target_registration(self) -> None:
        """Area 2: Duplicate registration of the same TargetId fails deterministically."""
        reg = DefaultToolRegistry()
        target = ExecutionTargetDescriptor(
            target_id=TargetId("local-host"),
            target_type=TargetType.LOCAL,
            is_available=True,
        )
        reg.register_target(target)

        with pytest.raises(ValueError) as exc:
            reg.register_target(target)
        assert "Duplicate target registration" in str(exc.value)

    def test_list_tools_filters_by_target_type(
        self, sample_local_tool: ToolDescriptor, sample_nvidia_tool: ToolDescriptor
    ) -> None:
        """Area 2: Listing tools filtered by compatible target type."""
        reg = DefaultToolRegistry([sample_local_tool, sample_nvidia_tool])

        local_tools = reg.list_tools(target_type=TargetType.LOCAL)
        assert len(local_tools) == 1
        assert local_tools[0].tool_id == sample_local_tool.tool_id

        remote_tools = reg.list_tools(target_type=TargetType.REMOTE)
        assert len(remote_tools) == 1
        assert remote_tools[0].tool_id == sample_nvidia_tool.tool_id


class TestExecutionTargetAbstraction:
    def test_local_remote_and_mcp_target_descriptors(self) -> None:
        """Area 3: Target descriptors accurately categorize execution environments."""
        t_local = ExecutionTargetDescriptor(
            target_id=TargetId("mac-workstation"),
            target_type=TargetType.LOCAL,
            is_available=True,
            description="Developer workstation",
        )
        t_remote = ExecutionTargetDescriptor(
            target_id=TargetId("dgx-h100-cluster"),
            target_type=TargetType.REMOTE,
            is_available=True,
            description="Remote GPU cluster",
        )
        t_mcp = ExecutionTargetDescriptor(
            target_id=TargetId("profiler-mcp"),
            target_type=TargetType.MCP_ENDPOINT,
            is_available=True,
            description="Hardware profiling MCP endpoint",
        )

        assert t_local.target_type == TargetType.LOCAL
        assert t_remote.target_type == TargetType.REMOTE
        assert t_mcp.target_type == TargetType.MCP_ENDPOINT

    def test_incompatible_target_type_rejected_in_availability(
        self, sample_local_tool: ToolDescriptor
    ) -> None:
        """Area 3: Probing a tool against an incompatible target type reports TARGET_UNAVAILABLE."""
        reg = DefaultToolRegistry([sample_local_tool])
        reg.register_target(
            ExecutionTargetDescriptor(
                target_id=TargetId("remote-gpu"),
                target_type=TargetType.REMOTE,
                is_available=True,
            )
        )

        avail = reg.check_availability(
            sample_local_tool.tool_id, TargetId("remote-gpu")
        )
        assert avail.is_available is False
        assert avail.status == ToolExecutionStatus.TARGET_UNAVAILABLE
        assert "not compatible with target type 'remote'" in str(avail.reason)


class TestUnconfiguredNVIDIAToolRepresentation:
    def test_nvidia_tool_on_local_macos_workstation_is_known_but_unavailable(
        self, sample_nvidia_tool: ToolDescriptor
    ) -> None:
        """Area 4 & D-022: NVIDIA tools cleanly report known-but-unavailable on local macOS workstation."""
        reg = DefaultToolRegistry([sample_nvidia_tool])
        reg.register_target(
            ExecutionTargetDescriptor(
                target_id=TargetId("local-mac"),
                target_type=TargetType.LOCAL,
                is_available=True,
            )
        )

        # Must not raise an unhandled exception or crash
        avail = reg.check_availability(
            sample_nvidia_tool.tool_id, TargetId("local-mac")
        )
        assert avail.is_available is False
        assert avail.status == ToolExecutionStatus.TOOL_UNAVAILABLE
        assert "cannot execute on host-local macOS workstation (D-022)" in str(
            avail.reason
        )

    def test_nvidia_tool_on_unconfigured_remote_reports_unavailable(
        self, sample_nvidia_tool: ToolDescriptor
    ) -> None:
        """Area 4: Unconfigured remote target reports TARGET_UNAVAILABLE."""
        reg = DefaultToolRegistry([sample_nvidia_tool])
        avail = reg.check_availability(
            sample_nvidia_tool.tool_id, TargetId("unconfigured-gpu")
        )
        assert avail.is_available is False
        assert avail.status == ToolExecutionStatus.TARGET_UNAVAILABLE
        assert "not configured" in str(avail.reason)


class TestApprovalPreconditionEnforcement:
    def test_unauthorized_gated_tool_fails_fast_with_permission_denied(
        self, sample_nvidia_tool: ToolDescriptor
    ) -> None:
        """Area 5: Invoking tool with requires_approval=True without authorization fails fast."""
        invocation = ToolInvocation(
            tool_id=sample_nvidia_tool.tool_id,
            parameters={"onnx_path": "model.onnx"},
            target_id=TargetId("remote-gpu"),
            execution_id=ExecutionId("exec-001"),
            timeout_seconds=300,
            approval_authorized=False,  # Unauthorized!
        )

        with pytest.raises(ToolSecurityError) as exc_info:
            check_approval_precondition(sample_nvidia_tool, invocation)

        assert exc_info.value.code == "PERMISSION_DENIED"
        assert "requires human approval" in exc_info.value.message

    def test_authorized_gated_tool_passes_approval_check(
        self, sample_nvidia_tool: ToolDescriptor
    ) -> None:
        """Area 5: Invoking tool with approval_authorized=True passes pre-execution check."""
        invocation = ToolInvocation(
            tool_id=sample_nvidia_tool.tool_id,
            parameters={"onnx_path": "model.onnx"},
            target_id=TargetId("remote-gpu"),
            execution_id=ExecutionId("exec-002"),
            timeout_seconds=300,
            approval_authorized=True,  # Authorized!
        )
        check_approval_precondition(sample_nvidia_tool, invocation)


class TestShellInterpretationProhibition:
    def test_structured_argv_vector_is_enforced(self) -> None:
        """Area 6: Structured argv validation ensures non-empty strings without shell interpretation."""
        validate_argv_safety(["git", "status", "--porcelain"])

        with pytest.raises(ToolSecurityError) as exc:
            validate_argv_safety([])
        assert "Command argv vector cannot be empty" in str(exc.value)

    @pytest.mark.asyncio
    async def test_local_adapter_executes_argv_strictly_without_shell(
        self, workspace_root: Path, sample_local_tool: ToolDescriptor
    ) -> None:
        """Area 6: Local adapter executes subprocess via argv vector (shell=False)."""
        adapter = LocalProcessAdapter(workspace_root=workspace_root)
        target = ExecutionTargetDescriptor(
            target_id=TargetId("local-host"),
            target_type=TargetType.LOCAL,
            is_available=True,
        )
        invocation = ToolInvocation(
            tool_id=sample_local_tool.tool_id,
            parameters={"argv": ["echo", "argv execution ok"]},
            target_id=target.target_id,
            execution_id=ExecutionId("exec-local-01"),
            timeout_seconds=10,
        )

        res = await adapter.execute(sample_local_tool, target, invocation)
        assert res.status == ToolExecutionStatus.SUCCESS
        assert "argv execution ok" in res.diagnostics.get("stdout", "")

    def test_argv_with_shell_metacharacters_rejected(self) -> None:
        """Area 6 (PM Blocker 1): Structured argv containing shell metacharacters fails closed."""
        metacharacters = [";", "&", "|", "`", "$", "(", ")", "\n", "\r"]
        for char in metacharacters:
            with pytest.raises(ToolSecurityError) as exc:
                validate_argv_safety(["git", f"status{char} rm -rf /"])
            assert exc.value.code == "POLICY_VIOLATION"
            assert f"forbidden shell metacharacter {char!r}" in exc.value.message

    @pytest.mark.asyncio
    async def test_local_adapter_fails_closed_when_no_argv_or_binding_configured(
        self, workspace_root: Path, sample_local_tool: ToolDescriptor
    ) -> None:
        """Area 6 (PM Blocker 4): Logical ToolId is never treated as a fallback command name."""
        adapter = LocalProcessAdapter(workspace_root=workspace_root)
        target = ExecutionTargetDescriptor(
            target_id=TargetId("local-host"),
            target_type=TargetType.LOCAL,
            is_available=True,
        )
        invocation = ToolInvocation(
            tool_id=sample_local_tool.tool_id,
            parameters={},  # No 'argv' parameter provided
            target_id=target.target_id,
            execution_id=ExecutionId("exec-local-nobind"),
            timeout_seconds=10,
        )

        res = await adapter.execute(sample_local_tool, target, invocation)
        assert res.status == ToolExecutionStatus.POLICY_VIOLATION
        assert "No executable command binding or argv_builder configured" in str(
            res.error_message
        )
        assert "prohibited" in str(res.error_message).lower()

    @pytest.mark.asyncio
    async def test_local_adapter_executes_with_explicit_command_bindings(
        self, workspace_root: Path, sample_local_tool: ToolDescriptor
    ) -> None:
        """Area 6 (PM Blocker 4): Explicit command bindings configured for local transport execute successfully."""
        bindings = {
            sample_local_tool.tool_id: [
                "echo",
                "bound command execution ok",
            ]
        }
        adapter = LocalProcessAdapter(
            workspace_root=workspace_root, command_bindings=bindings
        )
        target = ExecutionTargetDescriptor(
            target_id=TargetId("local-host"),
            target_type=TargetType.LOCAL,
            is_available=True,
        )
        invocation = ToolInvocation(
            tool_id=sample_local_tool.tool_id,
            parameters={},
            target_id=target.target_id,
            execution_id=ExecutionId("exec-local-bound"),
            timeout_seconds=10,
        )

        res = await adapter.execute(sample_local_tool, target, invocation)
        assert res.status == ToolExecutionStatus.SUCCESS
        assert "bound command execution ok" in res.diagnostics.get("stdout", "")


class TestWorkspacePathContainment:
    def test_valid_workspace_relative_path_is_contained(
        self, workspace_root: Path
    ) -> None:
        """Area 7: Relative path inside workspace resolves properly."""
        sub = workspace_root / "data"
        sub.mkdir()
        resolved = validate_workspace_containment("data/test.json", workspace_root)
        assert resolved == (workspace_root / "data" / "test.json").resolve()

    def test_path_traversal_escaping_workspace_raises_policy_violation(
        self, workspace_root: Path
    ) -> None:
        """Area 7: Path traversal escaping workspace raises POLICY_VIOLATION."""
        with pytest.raises(ToolSecurityError) as exc:
            validate_workspace_containment("../../outside.txt", workspace_root)
        assert exc.value.code == "POLICY_VIOLATION"

    def test_absolute_path_outside_workspace_raises_policy_violation(
        self, workspace_root: Path
    ) -> None:
        """Area 7: Absolute path outside workspace raises POLICY_VIOLATION."""
        with pytest.raises(ToolSecurityError) as exc:
            validate_workspace_containment("/etc/shadow", workspace_root)
        assert exc.value.code == "POLICY_VIOLATION"


class TestSecretIsolation:
    def test_sensitive_credential_keys_in_parameters_are_rejected(self) -> None:
        """Area 8 & Invariant 14: Sensitive credential patterns in parameters are rejected."""
        with pytest.raises(ToolSecurityError) as exc:
            validate_secret_isolation({"model": "yolo", "api_key": "secret_token_123"})
        assert exc.value.code == "POLICY_VIOLATION"
        assert "violating secret isolation" in exc.value.message

    def test_nested_sensitive_keys_are_rejected(self) -> None:
        """Area 8: Nested credential keys in parameter dictionaries are rejected."""
        with pytest.raises(ToolSecurityError) as exc:
            validate_secret_isolation({"options": {"auth_token": "bearer xyz"}})
        assert exc.value.code == "POLICY_VIOLATION"

    def test_clean_parameters_pass_secret_isolation(self) -> None:
        """Area 8: Clean parameters without credential patterns pass validation."""
        validate_secret_isolation(
            {"batch_size": 32, "learning_rate": 0.001, "epochs": 10}
        )

    def test_nested_sensitive_keys_in_sequences_are_rejected(self) -> None:
        """Area 8 (PM Blocker 6): Sensitive keys nested inside lists, tuples, and sets fail closed."""
        # Inside list of dicts
        with pytest.raises(ToolSecurityError) as exc:
            validate_secret_isolation({"options": [{"api_token": "secret_val"}]})
        assert exc.value.code == "POLICY_VIOLATION"
        assert "api_token" in exc.value.message

        # Inside tuple
        with pytest.raises(ToolSecurityError) as exc:
            validate_secret_isolation({"configs": ({"secret_key": "abc"},)})
        assert exc.value.code == "POLICY_VIOLATION"
        assert "secret_key" in exc.value.message

        # Deeply nested list inside dict inside list
        with pytest.raises(ToolSecurityError) as exc:
            validate_secret_isolation(
                {"layers": [{"config": {"auth": [{"password": "pwd"}]}}]}
            )
        assert exc.value.code == "POLICY_VIOLATION"
        assert "password" in exc.value.message


class TestParameterSchemaValidation:
    @pytest.mark.asyncio
    async def test_invalid_parameters_rejected_before_execution(
        self, sample_nvidia_tool: ToolDescriptor, workspace_root: Path
    ) -> None:
        """Area 9: Invocations violating input_schema fail fast with INVALID_PARAMETERS."""
        reg = DefaultToolRegistry([sample_nvidia_tool])
        reg.register_target(
            ExecutionTargetDescriptor(
                target_id=TargetId("remote-gpu"),
                target_type=TargetType.REMOTE,
                is_available=True,
            )
        )
        executor = DefaultToolExecutor(registry=reg, workspace_root=workspace_root)

        # Missing required parameter 'onnx_path'
        inv_missing = ToolInvocation(
            tool_id=sample_nvidia_tool.tool_id,
            parameters={"precision": "fp16"},
            target_id=TargetId("remote-gpu"),
            execution_id=ExecutionId("exec-val-1"),
            timeout_seconds=300,
            approval_authorized=True,
        )
        res_missing = await executor.execute(inv_missing)
        assert res_missing.status == ToolExecutionStatus.INVALID_PARAMETERS
        assert (
            "missing required property 'onnx_path'"
            in str(res_missing.error_message).lower()
        )

        # Invalid enum value for 'precision'
        inv_enum = ToolInvocation(
            tool_id=sample_nvidia_tool.tool_id,
            parameters={"onnx_path": "model.onnx", "precision": "invalid_precision"},
            target_id=TargetId("remote-gpu"),
            execution_id=ExecutionId("exec-val-2"),
            timeout_seconds=300,
            approval_authorized=True,
        )
        res_enum = await executor.execute(inv_enum)
        assert res_enum.status == ToolExecutionStatus.INVALID_PARAMETERS
        assert "not in allowed enum" in str(res_enum.error_message).lower()


class TestAsynchronousActiveJobHandoff:
    @pytest.mark.asyncio
    async def test_asynchronous_dispatch_returns_active_job_handoff(
        self, sample_nvidia_tool: ToolDescriptor, workspace_root: Path
    ) -> None:
        """Area 10: Asynchronous dispatch returns ActiveJobHandoff where status=SUCCESS denotes dispatch."""
        reg = DefaultToolRegistry([sample_nvidia_tool])
        reg.register_target(
            ExecutionTargetDescriptor(
                target_id=TargetId("remote-gpu"),
                target_type=TargetType.REMOTE,
                is_available=True,
            )
        )
        remote_adapter = MockRemoteTargetAdapter(simulate_async=True)
        executor = DefaultToolExecutor(
            registry=reg, workspace_root=workspace_root, remote_adapter=remote_adapter
        )

        inv = ToolInvocation(
            tool_id=sample_nvidia_tool.tool_id,
            parameters={"onnx_path": "model.onnx", "precision": "fp16"},
            target_id=TargetId("remote-gpu"),
            execution_id=ExecutionId("exec-async-01"),
            timeout_seconds=300,
            approval_authorized=True,
        )

        res = await executor.execute(inv)
        # ADR-0005 §3.8: ToolResult.status == SUCCESS denotes successful DISPATCH, not completion!
        assert res.status == ToolExecutionStatus.SUCCESS
        assert res.active_job_ref is not None
        assert isinstance(res.active_job_ref, ActiveJobHandoff)
        assert res.active_job_ref.job_id == "job-exec-async-01"
        assert res.active_job_ref.status == "submitted"
        assert res.active_job_ref.target == "remote_gpu"

    def test_handoff_expiry_invariant_does_not_equate_to_failure(self) -> None:
        """Area 11 (PM Blocker 5): is_expired() evaluates expiration while preserving status (expired != failed)."""
        expired_handoff = ActiveJobHandoff(
            job_id="job-999",
            job_type="training",
            target="remote_gpu",
            submitted_at="2026-09-13T00:00:00Z",
            status="running",
            heartbeat_timeout_seconds=60,
            expires_at="2026-09-13T00:01:00Z",
        )
        # Evaluated against instant after expiry
        now_after = datetime.fromisoformat("2026-09-13T00:02:00Z")
        assert expired_handoff.is_expired(now_after) is True

        # Evaluated against instant before expiry
        now_before = datetime.fromisoformat("2026-09-13T00:00:30Z")
        assert expired_handoff.is_expired(now_before) is False

        # Crucial ADR-0005 §3.8 invariant: expired != failed
        assert expired_handoff.status == "running"

    def test_handoff_expires_at_validation(self) -> None:
        """Area 11 (PM Blocker 5): expires_at requires valid timezone-aware ISO-8601 string."""
        # Non-ISO string raises ValueError
        with pytest.raises(ValueError) as exc:
            ActiveJobHandoff(
                job_id="job-bad-time",
                job_type="training",
                target="remote_gpu",
                submitted_at="2026-09-13T00:00:00Z",
                status="submitted",
                expires_at="not-a-timestamp",
            )
        assert "valid ISO-8601 string" in str(exc.value)

        # Naive timestamp (no timezone) raises ValueError
        with pytest.raises(ValueError) as exc_tz:
            ActiveJobHandoff(
                job_id="job-naive-time",
                job_type="training",
                target="remote_gpu",
                submitted_at="2026-09-13T00:00:00Z",
                status="submitted",
                expires_at="2026-09-13T12:00:00",
            )
        assert "timezone-aware" in str(exc_tz.value)


class TestMCPTransportBoundary:
    @pytest.mark.asyncio
    async def test_mcp_untrusted_output_validation(
        self, sample_nvidia_tool: ToolDescriptor, workspace_root: Path
    ) -> None:
        """Area 12: Untrusted MCP output is validated against declared output_schema."""
        reg = DefaultToolRegistry([sample_nvidia_tool])
        reg.register_target(
            ExecutionTargetDescriptor(
                target_id=TargetId("mcp-target"),
                target_type=TargetType.MCP_ENDPOINT,
                is_available=True,
            )
        )

        # MCP server returns output violating output_schema (engine_path should be string, got int)
        mcp_adapter = MockMCPEndpointAdapter(simulated_response={"engine_path": 12345})
        executor = DefaultToolExecutor(
            registry=reg, workspace_root=workspace_root, mcp_adapter=mcp_adapter
        )

        inv = ToolInvocation(
            tool_id=sample_nvidia_tool.tool_id,
            parameters={"onnx_path": "model.onnx"},
            target_id=TargetId("mcp-target"),
            execution_id=ExecutionId("exec-mcp-val"),
            timeout_seconds=60,
            approval_authorized=True,
        )

        res = await executor.execute(inv)
        # Area 12 (PM Blocker 3): Output schema violation produces distinct SCHEMA_VIOLATION status
        assert res.status == ToolExecutionStatus.SCHEMA_VIOLATION
        assert "MCP output validation failed" in str(res.error_message)

    @pytest.mark.asyncio
    async def test_mcp_transport_disruption_returns_transport_error(
        self, sample_nvidia_tool: ToolDescriptor, workspace_root: Path
    ) -> None:
        """Area 12: MCP socket or protocol disruption returns TRANSPORT_ERROR."""
        reg = DefaultToolRegistry([sample_nvidia_tool])
        reg.register_target(
            ExecutionTargetDescriptor(
                target_id=TargetId("mcp-target"),
                target_type=TargetType.MCP_ENDPOINT,
                is_available=True,
            )
        )
        mcp_adapter = MockMCPEndpointAdapter(transport_error=True)
        executor = DefaultToolExecutor(
            registry=reg, workspace_root=workspace_root, mcp_adapter=mcp_adapter
        )

        inv = ToolInvocation(
            tool_id=sample_nvidia_tool.tool_id,
            parameters={"onnx_path": "model.onnx"},
            target_id=TargetId("mcp-target"),
            execution_id=ExecutionId("exec-mcp-err"),
            timeout_seconds=60,
            approval_authorized=True,
        )
        res = await executor.execute(inv)
        assert res.status == ToolExecutionStatus.TRANSPORT_ERROR

    def test_mcp_candidate_discovery_does_not_create_tool_ids(self) -> None:
        """Area 12 (PM Blocker 2): MCP candidate discovery produces external descriptors without creating ToolIds."""
        adapter = MockMCPEndpointAdapter(
            server_id="vision-mcp",
            discovered_tool_candidates={
                "external_trt_engine_compile": {
                    "version": "1.0",
                    "description": "Remote TRT compiler",
                },
                "external_profiler": {
                    "version": "2.0",
                    "description": "Remote profiler",
                },
            },
        )
        candidates = adapter.discover_candidates()
        assert candidates == ("external_trt_engine_compile", "external_profiler")
        # Discovered candidate strings are NOT ToolId instances
        for c in candidates:
            assert not isinstance(c, ToolId)

    def test_mcp_candidate_resolution_and_unmapped_rejection(
        self, sample_nvidia_tool: ToolDescriptor
    ) -> None:
        """Area 12 (PM Blocker 2): Candidate resolution requires authoritative mapping; unmapped candidates fail closed."""
        adapter = MockMCPEndpointAdapter(
            server_id="vision-mcp",
            discovered_tool_candidates={
                "external_trt_engine_compile": {"version": "1.0"},
                "malicious_external_tool": {"version": "0.1"},
            },
            tool_mapping={"external_trt_engine_compile": sample_nvidia_tool.tool_id},
        )

        # Mapped candidate resolves to authoritative ToolId
        resolved = adapter.resolve_candidate("external_trt_engine_compile")
        assert resolved == sample_nvidia_tool.tool_id
        assert isinstance(resolved, ToolId)

        # Unmapped candidate is rejected fail-closed; MCP endpoint cannot synthesize a ToolId
        with pytest.raises(ToolSecurityError) as exc:
            adapter.resolve_candidate("malicious_external_tool")
        assert exc.value.code == "UNMAPPED_MCP_TOOL"
        assert "cannot synthesize new ToolIds" in str(exc.value.message)

    @pytest.mark.asyncio
    async def test_mcp_invocation_of_unmapped_tool_returns_permission_denied(
        self, sample_nvidia_tool: ToolDescriptor, workspace_root: Path
    ) -> None:
        """Area 12 (PM Blocker 2): Executing an unmapped tool on an MCP target returns PERMISSION_DENIED."""
        reg = DefaultToolRegistry([sample_nvidia_tool])
        reg.register_target(
            ExecutionTargetDescriptor(
                target_id=TargetId("mcp-target"),
                target_type=TargetType.MCP_ENDPOINT,
                is_available=True,
            )
        )
        # MCP adapter configured with a mapping that does NOT include sample_nvidia_tool
        other_tool_id = ToolId("other/tool")
        mcp_adapter = MockMCPEndpointAdapter(
            server_id="vision-mcp",
            tool_mapping={"some_external_tool": other_tool_id},
        )
        executor = DefaultToolExecutor(
            registry=reg, workspace_root=workspace_root, mcp_adapter=mcp_adapter
        )

        inv = ToolInvocation(
            tool_id=sample_nvidia_tool.tool_id,
            parameters={"onnx_path": "model.onnx"},
            target_id=TargetId("mcp-target"),
            execution_id=ExecutionId("exec-mcp-unmapped"),
            timeout_seconds=60,
            approval_authorized=True,
        )
        res = await executor.execute(inv)
        assert res.status == ToolExecutionStatus.PERMISSION_DENIED
        assert "not mapped from an authorized candidate" in str(res.error_message)


class TestDefensiveDeepImmutability:
    def test_nested_schemas_and_metadata_are_recursively_frozen(
        self, sample_local_tool: ToolDescriptor
    ) -> None:
        """Area 13: Mapping fields on ToolDescriptor are frozen and reject mutation."""
        assert isinstance(sample_local_tool.input_schema, MappingProxyType)
        assert isinstance(
            sample_local_tool.input_schema["properties"], MappingProxyType
        )

        with pytest.raises(TypeError):
            sample_local_tool.input_schema["properties"]["short"] = "MUTATED"  # type: ignore[index]

    def test_tool_result_nested_dictionaries_are_recursively_frozen(self) -> None:
        """Area 13: ToolResult structured_output and diagnostics are recursively frozen."""
        caller_output = {"metrics": {"fps": 60, "latency_ms": 16.6}}
        res = ToolResult(
            tool_id=ToolId("git-status"),
            execution_id=ExecutionId("exec-imm-1"),
            target_id=TargetId("local-host"),
            status=ToolExecutionStatus.SUCCESS,
            structured_output=caller_output,
        )

        # Mutating caller dictionary does not affect contract
        caller_output["metrics"]["fps"] = 999
        assert res.structured_output["metrics"]["fps"] == 60

        # Direct mutation of result raises TypeError
        with pytest.raises(TypeError):
            res.structured_output["metrics"]["fps"] = 123  # type: ignore[index]
