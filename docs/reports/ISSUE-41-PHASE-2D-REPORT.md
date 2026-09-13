# Issue #41 — Phase 2D Implementation Report

**Issue:** [#41](https://github.com/iam-ajmunna/CV-Agent-Platform/issues/41)
**PR:** [#42](https://github.com/iam-ajmunna/CV-Agent-Platform/pull/42)
**Branch:** `feature/ajmunna/phase-2d-tools-mcp`
**Status:** Implemented; all 200 tests pass (including 27 new Phase 2D contract tests); all quality gates pass.

## Overview

This report documents the implementation of the Phase 2D Tool & MCP Protocol Subsystem under Issue #41 according to the frozen ADR-0005 specification and the approved boundary decisions established with the Official PM.

The Tools/MCP subsystem provides the execution substrate for computer vision engineering tasks while maintaining clean separation from Capability Registry (ADR-0001) and Orchestration (ADR-0003). It enforces operational cataloging, schema validation, transport decoupling, execution safety (argv-only, workspace containment, secret isolation), pre-execution approval gating, asynchronous job handoff, and untrusted MCP transport boundary mediation.

## Architectural Boundaries & Design Decisions

### 1. Authoritative Boundary Partitioning
- **Capability Registry (ADR-0001):** Authoritative owner of `ToolId` tokens and capability-to-tool selection.
- **Tools/MCP (ADR-0005):** Owns operational tool cataloging, execution target descriptors, schema validation, transport mediation, and execution dispatch. Never invents or validates semantic capabilities.
- **Orchestration (ADR-0003):** Owns workflow execution loops, checkpointing, human approval request lifecycle, and reconciliation of asynchronous active jobs. ADR-0005 strictly executes; it does not orchestrate.

### 2. Execution Safety Guarantees (ADR-0005 §3.7)
- **Zero Shell Interpretation:** Local execution (`LocalProcessAdapter`) strictly uses structured `argv: list[str]` with `shell=False`. Raw shell command strings, shell meta-characters (`;`, `&`, `|`, `` ` ``, `$`, `\n`), and unparsed text are blocked fail-closed before execution.
- **Workspace Containment:** All filesystem paths passed to tool invocations are resolved against `workspace_root` via `validate_workspace_containment()`. Attempts to traverse outside the workspace (`../`, symlinks, or absolute external paths) are rejected with `POLICY_VIOLATION`.
- **Secret Isolation:** Parameters containing sensitive key patterns (`api_key`, `secret`, `password`, `token`, `credential`, `auth_key`, `private_key`) are blocked fail-closed with `POLICY_VIOLATION`.
- **Pre-Execution Approval Gate:** Tools declaring `requires_approval=True` (such as hardware flashing, cloud instance spin-up, or destructive benchmark sweeps) demand `invocation.approval_authorized=True`. Invocations lacking authorization fail fast with `PERMISSION_DENIED` with zero process execution.

### 3. Asynchronous Job Handoff Semantics (ADR-0005 §3.8)
- For asynchronous tools (e.g. distributed multi-GPU training, long-running profiling), `ToolResult.status == SUCCESS` denotes **successful dispatch/acceptance**, not job completion.
- Returns an immutable `ActiveJobHandoff` containing `job_id`, `job_type`, `target`, and `heartbeat_timeout_seconds`.
- **Handoff Expiry:** An expired handoff invalidates further tracking but is explicitly **not** an execution failure (`is_expired()` helper distinguishes expiry from failure).

### 4. Untrusted MCP Boundary Mediation (ADR-0005 §3.6)
- MCP endpoints are treated as untrusted external transports.
- MCP servers cannot invent or synthesize `ToolId`s; external tool candidates must map to an authoritative `ToolId` registered in the catalog.
- Outputs returned from MCP endpoints are strictly validated against the tool's declared `output_schema`. Malformed outputs produce `SCHEMA_VIOLATION`.

### 5. Hardware-Aware Tool Probing (D-014, D-022, ADR-0005 §3.4)
- Tools requiring NVIDIA/CUDA runtimes (e.g. `trt-build`, `tao-train`) probed against host-local macOS workstations are reported honestly as **known-but-unavailable** (`is_available=False`, `status=TOOL_UNAVAILABLE`) with D-022 rationale, without raising exceptions or crashing.

## Subsystem Architecture & Implementation

The implementation resides under `cv_agent/tools/`:

- `contracts.py`: Typed immutable data structures (`TargetId`, `ExecutionId`, `MCPServerId`, `TargetType`, `ToolRiskLevel`, `ToolExecutionStatus`, `ExecutionTargetDescriptor`, `ToolDescriptor`, `ActiveJobHandoff`, `ToolInvocation`, `ToolResult`, `ToolAvailabilityInfo`, `ToolExecutor`, `ToolRegistry`, `deep_freeze`).
- `security.py`: Security policy validation (`validate_argv_safety`, `validate_workspace_containment`, `validate_secret_isolation`, `check_approval_precondition`, `ToolSecurityError`).
- `registry.py`: Operational tool and target catalog (`DefaultToolRegistry`) with duplicate rejection, capability-safe lookup, filtering, and hardware-aware availability probing.
- `adapters.py`: Transport decoupling layer (`LocalProcessAdapter`, `MockRemoteTargetAdapter`, `MockMCPEndpointAdapter`).
- `executor.py`: Unified execution coordinator (`DefaultToolExecutor`) orchestrating target availability, approval gating, secret isolation, schema validation, and transport dispatch.
- `__init__.py`: Clean public interface with explicit `__all__` export list.

## Acceptance Criteria & Test Coverage (All 13 Areas)

The test suite in `tests/test_tools_boundary.py` provides 27 comprehensive contract tests covering all 13 required areas:

1. **ToolId Authoritative Integration:** Rejection of unknown ToolIds with `TOOL_NOT_FOUND`.
2. **Operational Catalog & Target Descriptor:** Target registration, duplicate target rejection, and multi-target cataloging.
3. **Execution Target Abstraction:** Target routing across LOCAL, REMOTE, and MCP_ENDPOINT, plus `TARGET_UNAVAILABLE` on type mismatch.
4. **Hardware-Aware Probing (macOS / NVIDIA):** Known-but-unavailable reporting for NVIDIA tools on macOS workstation per D-022.
5. **Tool Registry Immutability & Duplicate Rejection:** Deterministic rejection of duplicate tool IDs and deep-freeze immutability.
6. **Execution Safety — Zero Shell:** Structured argv execution (`shell=False`) and rejection of shell meta-characters.
7. **Execution Safety — Workspace Containment:** Traversal attacks (`../`) and external absolute paths rejected with `POLICY_VIOLATION`.
8. **Execution Safety — Secret Isolation:** Credential keys rejected before dispatch with `POLICY_VIOLATION`.
9. **Parameter Schema Validation:** Missing required properties or invalid enums fail fast with `INVALID_PARAMETERS`.
10. **Pre-Execution Approval Gating:** `requires_approval=True` tools fail fast with `PERMISSION_DENIED` when unapproved.
11. **Asynchronous Execution & ActiveJobHandoff:** Dispatch returns `SUCCESS` + `ActiveJobHandoff`; expiry is not failure.
12. **MCP Untrusted Boundary:** Output schema validation against MCP payloads, rejecting malformed responses with `SCHEMA_VIOLATION`.
13. **Subsystem Isolation:** Zero LangGraph nodes, zero workflow state machine, zero external provider SDK imports in `cv_agent/tools/`.

## Quality Gates & Verification Results

| Gate | Command | Result |
|---|---|---|
| Ruff Lint | `ruff check .` | Pass (All checks passed) |
| Ruff Format | `ruff format --check . [excludes]` | Pass (88 files already formatted) |
| Mypy (Default) | `mypy cv_agent` | Pass (Success: no issues found in 34 source files) |
| Mypy (Python 3.10) | `mypy --python-version 3.10 cv_agent/tools` | Pass (Success: no issues found in 6 source files) |
| Pytest Test Suite | `pytest -q` | Pass — 200 passed in 3.11s (0 warnings) |
| CLI Health Check | `python -m cv_agent` | Pass — Runtime status OK, 20 capabilities registered |
| Git Diff Check | `git diff --check` | Pass (Zero whitespace or conflict errors) |
