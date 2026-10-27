"""
ragleap_integrations.governance

Owner-side policy for the tools an MCP server offers. Standard library only.

An MCP server describes each tool with optional annotations (readOnlyHint,
destructiveHint, idempotentHint, openWorldHint). The MCP specification says a
client must treat those hints as untrusted unless they come from a trusted
server: a server can lie, or simply be wrong. So here the hints can only make
policy STRICTER, never looser, unless the owner explicitly says otherwise:

- Every tool is flagged requires_approval (in Tool.metadata["mcp"]) unless the
  owner lists it in McpPolicy.pre_approved, or the owner set
  McpServerConfig.trust_annotations for that server AND the tool declares
  readOnlyHint: true without also claiming destructiveHint: true (a
  contradiction keeps approval required). destructiveHint never relaxes
  anything.
- The flag is advice for a caller such as an agent loop with an approval gate.
  This package enforces it only when the owner supplies McpPolicy.approve: a
  call to a flagged tool then runs only if approve(server, tool, arguments)
  returns exactly True (an exception or any other value denies it).
  approve_scoped(scope, server, tool, arguments) is the same with the caller's scope (see
  tenancy.McpHub): when it is set it is used instead of approve, and without a scope it denies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


@dataclass
class McpPolicy:
    """approve: optional callable (server, tool, arguments) -> bool, run before
    a flagged tool is called (it may block, for example on a human; the
    network deadlines do not cover it). pre_approved: exact "server.tool"
    entries that never need approval."""

    approve: Optional[Callable[[str, str, Dict[str, Any]], bool]] = None
    pre_approved: List[str] = field(default_factory=list)
    approve_scoped: Optional[Callable[..., bool]] = None

    def __post_init__(self) -> None:
        if self.approve is not None and not callable(self.approve):
            raise ValueError("approve must be callable")
        if self.approve_scoped is not None and not callable(self.approve_scoped):
            raise ValueError("approve_scoped must be callable")
        if not isinstance(self.pre_approved, (list, tuple)):
            raise ValueError("pre_approved must be a list of 'server.tool' entries")
        cleaned: List[str] = []
        for entry in self.pre_approved:
            if not isinstance(entry, str) or "." not in entry or entry.startswith(".") or entry.endswith("."):
                raise ValueError("pre_approved entries must look like 'server.tool'")
            if entry not in cleaned:
                cleaned.append(entry)
        self.pre_approved = cleaned


def hints_from(annotations: Any) -> Dict[str, bool]:
    """Only the four boolean hints survive, and only as booleans. Nothing else
    a server puts in annotations (titles, free text, unknown keys) is kept."""
    if not isinstance(annotations, dict):
        return {}
    return {k: annotations[k] for k in HINT_KEYS if isinstance(annotations.get(k), bool)}


def requires_approval(key: str, hints: Dict[str, bool], trust_annotations: bool, policy: Optional[McpPolicy]) -> bool:
    """True unless the owner relaxed it (see the module docstring)."""
    if policy is not None and key in policy.pre_approved:
        return False
    if trust_annotations and hints.get("readOnlyHint") is True and hints.get("destructiveHint") is not True:
        return False
    return True
