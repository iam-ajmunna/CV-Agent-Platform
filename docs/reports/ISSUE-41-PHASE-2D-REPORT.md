# Issue #41 — Phase 2D Implementation Report

**Issue:** [#41](https://github.com/iam-ajmunna/CV-Agent-Platform/issues/41)
**PR:** [#42](https://github.com/iam-ajmunna/CV-Agent-Platform/pull/42)
**Branch:** `feature/ajmunna/phase-2d-tools-mcp`
**Status:** Implemented; PR #42 review corrections applied; all 208 tests pass (including 35 Phase 2D contract tests); all quality gates pass.

## Overview

This report documents the implementation of the Phase 2D Tool & MCP Protocol Subsystem under Issue #41 according to the frozen ADR-0005 specification, the approved boundary decisions established with the Official PM, and the subsequent resolution of the review findings raised during the Official PM Review of PR #42.

The Tools/MCP subsystem provides the execution substrate for computer vision engineering tasks while maintaining clean separation from Capability Registry (ADR-0001) and Orchestration (ADR-0003). It enforces operational cataloging, schema validation, transport decoupling, execution safety (argv-only, workspace containment, secret isolation), pre-execution approval gating, asynchronous job handoff, and untrusted MCP transport boundary mediation.

## Official PM Review Findings & Resolution

During the Official PM Review of PR #42, six contract-level items were identified and resolved:

### 1. Fail-Closed Shell Metacharacter Rejection (Finding 1 — Critical)
- `validate_argv_safety()` now actively inspects every argument against `_SHELL_INJECTION_CHARS = frozenset({";", "&", "|", "`", "$", "(", ")", "\n", "\r"})`.
- Any argument containing any forbidden character fails closed with `ToolSecurityError("POLICY_VIOLATION", ...)` before process invocation.
- Added comprehensive regression tests proving that injection attempts (e.g. `status; rm -rf /`, `echo hello && rm -rf`, `cat `id``, `echo $USER`, etc.) are blocked fail-closed.

### 2. MCP Discovered-Tool Authority Boundary (Finding 2 — Critical)
- Implemented the full candidate discovery and authority resolution boundary on `MockMCPEndpointAdapter`:
  - `discover_candidates()` returns discovered external candidate names as strings without creating authoritative `ToolId` instances.
  - `register_tool_mapping(external_candidate_name, authoritative_tool_id)` explicitly binds external names to project-authoritative `ToolId`s.
  - `resolve_candidate(external_candidate_name)` resolves mapped candidates, and raises `ToolSecurityError("UNMAPPED_MCP_TOOL", ...)` for unmapped candidates, preventing external MCP servers from synthesizing new `ToolId`s.
  - `execute()` verifies that the invoked `tool_id` is mapped on the MCP server, returning `PERMISSION_DENIED` for unmapped tools.

### 3. Distinct `SCHEMA_VIOLATION` Outcome (Finding 3 — Important)
- Added `SCHEMA_VIOLATION = "schema_violation"` to `ToolExecutionStatus` in `contracts.py`.
- Updated `MockMCPEndpointAdapter.execute()` to return `status=ToolExecutionStatus.SCHEMA_VIOLATION` when an MCP response violates the declared `output_schema`.
- Updated tests to assert `res.status == ToolExecutionStatus.SCHEMA_VIOLATION`.

### 4. Safe Local Transport Command Resolution (Finding 4 — Important)
- Eliminated unsafe fallback conversion `argv = [str(tool.tool_id)]`.
- `LocalProcessAdapter` and `DefaultToolExecutor` support explicit `command_bindings: Mapping[ToolId, Sequence[str]]`.
- If neither `argv_builder`, nor `"argv"` parameter, nor `command_bindings` is configured, execution fails closed with `POLICY_VIOLATION` ("Conversion of logical ToolId to executable process name is strictly prohibited").
- Added regression tests verifying fail-closed rejection when unconfigured, and successful execution when bound via `command_bindings`.

### 5. Deterministic ActiveJobHandoff Expiry Evaluation (Finding 5 — Important)
- `ActiveJobHandoff.__post_init__` strictly validates `expires_at` as a timezone-aware ISO-8601 string, rejecting malformed or naive timestamps with `ValueError`.
- Added `is_expired(self, now: datetime | None = None) -> bool` helper evaluating `now >= expires_at`.
- Preserved the fundamental ADR-0005 §3.8 invariant: `expired != failed` (handoff status remains unchanged upon expiry).

### 6. Recursive Secret Isolation Inspection Across Sequences (Finding 6 — Important)
- Updated `validate_secret_isolation()` to recursively walk mappings, sequences (lists, tuples), and sets.
- Secret keys nested within lists of dicts (e.g. `{"options": [{"api_token": "..."}]}`) or deep tuples are detected and rejected fail-closed with `POLICY_VIOLATION`.
- Added regression tests for nested sequences containing credential patterns.

## Subsystem Architecture & Implementation

The implementation resides under `cv_agent/tools/`:

- `contracts.py`: Typed immutable data structures (`TargetId`, `ExecutionId`, `MCPServerId`, `TargetType`, `ToolRiskLevel`, `ToolExecutionStatus`, `ExecutionTargetDescriptor`, `ToolDescriptor`, `ActiveJobHandoff`, `ToolInvocation`, `ToolResult`, `ToolAvailabilityInfo`, `ToolExecutor`, `ToolRegistry`, `deep_freeze`).
- `security.py`: Security policy validation (`validate_argv_safety`, `validate_workspace_containment`, `validate_secret_isolation`, `check_approval_precondition`, `ToolSecurityError`).
- `registry.py`: Operational tool and target catalog (`DefaultToolRegistry`) with duplicate rejection, capability-safe lookup, filtering, and hardware-aware availability probing.
- `adapters.py`: Transport decoupling layer (`LocalProcessAdapter`, `MockRemoteTargetAdapter`, `MockMCPEndpointAdapter`).
- `executor.py`: Unified execution coordinator (`DefaultToolExecutor`) orchestrating target availability, approval gating, secret isolation, schema validation, and transport dispatch.
- `__init__.py`: Clean public interface with explicit `__all__` export list.

## Acceptance Criteria & Test Coverage (All 13 Areas)

The test suite in `tests/test_tools_boundary.py` provides 35 comprehensive contract tests covering all 13 required areas and the 6 PM review findings:

1. **ToolId Authoritative Integration:** Rejection of unknown ToolIds with `TOOL_NOT_FOUND`.
2. **Operational Catalog & Target Descriptor:** Target registration, duplicate target rejection, and multi-target cataloging.
3. **Execution Target Abstraction:** Target routing across LOCAL, REMOTE, and MCP_ENDPOINT, plus `TARGET_UNAVAILABLE` on type mismatch.
4. **Hardware-Aware Probing (macOS / NVIDIA):** Known-but-unavailable reporting for NVIDIA tools on macOS workstation per D-022.
5. **Tool Registry Immutability & Duplicate Rejection:** Deterministic rejection of duplicate tool IDs and deep-freeze immutability.
6. **Execution Safety — Zero Shell:** Structured argv execution (`shell=False`), rejection of shell metacharacters, and command bindings.
7. **Execution Safety — Workspace Containment:** Traversal attacks (`../`) and external absolute paths rejected with `POLICY_VIOLATION`.
8. **Execution Safety — Secret Isolation:** Credential keys rejected across nested mappings and sequences with `POLICY_VIOLATION`.
9. **Parameter Schema Validation:** Missing required properties or invalid enums fail fast with `INVALID_PARAMETERS`.
10. **Pre-Execution Approval Gating:** `requires_approval=True` tools fail fast with `PERMISSION_DENIED` when unapproved.
11. **Asynchronous Execution & ActiveJobHandoff:** Dispatch returns `SUCCESS` + `ActiveJobHandoff`; ISO-8601 validation; `is_expired()`; expiry is not failure.
12. **MCP Untrusted Boundary:** Output schema validation producing `SCHEMA_VIOLATION`, candidate discovery without ToolIds, mapping resolution, and unmapped candidate rejection.
13. **Subsystem Isolation:** Zero LangGraph nodes, zero workflow state machine, zero external provider SDK imports in `cv_agent/tools/`.

## Quality Gates & Verification Results

| Gate | Command | Result |
|---|---|---|
| Ruff Lint | `ruff check .` | Pass (All checks passed) |
| Ruff Format | `ruff format --check . [excludes]` | Pass (89 files already formatted) |
| Mypy (Default) | `mypy cv_agent` | Pass (Success: no issues found in 34 source files) |
| Mypy (Python 3.10) | `mypy --python-version 3.10 cv_agent/tools` | Pass (Success: no issues found in 6 source files) |
| Pytest Test Suite | `pytest -q` | Pass — 208 passed in 3.42s (0 warnings) |
| CLI Health Check | `python -m cv_agent` | Pass — Runtime status OK, 20 capabilities registered |
| Git Diff Check | `git diff --check` | Pass (Zero whitespace or conflict errors) |
