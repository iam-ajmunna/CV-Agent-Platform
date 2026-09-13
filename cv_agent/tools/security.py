"""cv_agent.tools.security — Security, path containment, and secret isolation policies.

Implements ADR-0005 §3.7 execution safety guarantees:
1. Prohibition of raw shell interpretation (structured argv only).
2. Workspace path containment (blocking path traversal and external paths).
3. Secret isolation (rejecting sensitive credential parameters).
4. Approval precondition enforcement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cv_agent.tools.contracts import ToolDescriptor, ToolInvocation

_SENSITIVE_KEY_PATTERNS = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "password",
        "token",
        "credential",
        "auth_key",
        "private_key",
    }
)

_SHELL_INJECTION_CHARS = frozenset({";", "&", "|", "`", "$", "(", ")", "\n", "\r"})


class ToolSecurityError(Exception):
    """Raised when a tool invocation breaches execution safety constraints."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def validate_workspace_containment(
    path: Path | str,
    workspace_root: Path,
) -> Path:
    """Validate that a filesystem path resolves strictly within the workspace root.

    Blocks path traversal (e.g. '../', '..\\\\') and unauthorized access outside the workspace.
    """
    root = workspace_root.resolve()
    target = (
        (root / Path(path)).resolve()
        if not Path(path).is_absolute()
        else Path(path).resolve()
    )

    try:
        target.relative_to(root)
    except ValueError:
        raise ToolSecurityError(
            "POLICY_VIOLATION",
            f"Path {str(path)!r} resolves to {str(target)!r} outside workspace root {str(root)!r}",
        ) from None

    return target


def validate_secret_isolation(data: Any) -> None:
    """Validate that invocation parameters do not contain sensitive credential keys.

    Per ADR-0005 §3.7, credentials must resolve from a target-bound secrets vault
    and MUST NOT appear in invocation payloads, logs, or results.
    Recursively inspects mappings, sequences (lists, tuples), and sets.
    """
    if isinstance(data, Mapping):
        for key, val in data.items():
            lower = str(key).lower()
            if any(pat in lower for pat in _SENSITIVE_KEY_PATTERNS):
                raise ToolSecurityError(
                    "POLICY_VIOLATION",
                    f"Invocation parameter {key!r} contains sensitive credential pattern violating secret isolation",
                )
            validate_secret_isolation(val)
    elif isinstance(data, (list, tuple, set, frozenset)):
        for item in data:
            validate_secret_isolation(item)


def validate_argv_safety(argv: Sequence[str]) -> None:
    """Verify that structured argv vectors do not contain shell injection sequences.

    Enforces ADR-0005 §3.7 prohibition of shell interpretation.
    Blocks any argument containing shell metacharacters (; & | ` $ ( ) \n \r).
    """
    if not argv:
        raise ToolSecurityError(
            "INVALID_PARAMETERS",
            "Command argv vector cannot be empty",
        )
    for arg in argv:
        if not isinstance(arg, str):
            raise ToolSecurityError(
                "INVALID_PARAMETERS",
                f"Command argument {arg!r} must be a string",
            )
        for char in _SHELL_INJECTION_CHARS:
            if char in arg:
                raise ToolSecurityError(
                    "POLICY_VIOLATION",
                    f"Command argument {arg!r} contains forbidden shell metacharacter {char!r} violating shell prohibition",
                )


def check_approval_precondition(
    tool: ToolDescriptor,
    invocation: ToolInvocation,
) -> None:
    """Enforce that gated actions have verified orchestration approval.

    Per ADR-0005 §3.7 and §3.8, if requires_approval is True, invocation.approval_authorized
    must be True; otherwise invocation fails fast with PERMISSION_DENIED.
    """
    if tool.requires_approval and not invocation.approval_authorized:
        raise ToolSecurityError(
            "PERMISSION_DENIED",
            f"Tool {tool.tool_id!r} requires human approval, but invocation.approval_authorized is False",
        )
