"""Capability declarations for the core's own built-in tools (Phase 0, P0-E and P0-G, draft).

Generated from the tables in core/connector_host/policy.py, so the declarations cannot disagree with the floor the
gate applies (tests/test_builtin_declarations.py compares them with the real gate sets and with core/subagents.py).
Nothing in the product imports this module yet: it describes what the built-in tools already do; it changes no
behaviour.

Each tool has its own declaration, because network facts differ per tool. The network.egress class says how far a
tool reaches: `owner_configured` for tools that send to targets the OWNER configures by name (webhook and Slack
names, the email and CRM settings, MCP servers, the approval chat), `runtime_chosen` for `fetch_page` (the address
is chosen while the run goes on, so a host list cannot be declared and the class carries the checks instead).
`delegate` (sub-agents, off by default) is declared too: it is denied unless the owner explicitly grants it, and
its `inherits` block states the limits of a delegated run.
"""
import copy
from typing import Any, Dict, List

from core.connector_host.policy import (ALWAYS_APPROVAL_OPERATIONS, BUILTIN_EFFECT, BUILTIN_EGRESS,
                                        GRANT_REQUIRED_OPERATIONS, OUTBOUND_BUILTINS, TAINTING_BUILTINS)

SCHEMA = "ragleap.capability/1"
VERSION = "1.0.0"
ID_PREFIX = "ragleap.builtin."

KIND = {"mcp_call": "mcp"}

# The limits of a delegated run. The numbers are the defaults in core/subagents.py (a test compares them with the code).
# `tools: parent_subset` is the rule the core is meant to apply from Phase 1; today a child gets the same tool list as
# any run (see the note below).
DELEGATE_INHERITS = {"max_depth": 1, "max_children": 3, "child_tokens": 8000, "tree_tokens": 30000,
                     "tools": "parent_subset", "taint": "both_ways"}

_TITLES = {
    "send_webhook": "Send a webhook", "send_slack": "Send a Slack message", "send_email": "Send an email",
    "update_crm_record": "Update a CRM record", "schedule_event": "Schedule a calendar event",
    "notify_owner": "Notify the owner", "mcp_call": "Call an MCP tool", "create_task": "Create a task ticket",
    "run_code": "Run code in the sandbox", "run_shell": "Run a shell command in the sandbox",
    "fetch_page": "Fetch a web page", "list_files": "List files in a granted folder",
    "read_file": "Read a file in a granted folder", "preview_change": "Preview a file change",
    "write_file": "Write a file in a granted folder", "delete_file": "Delete a file in a granted folder",
    "request_access": "Ask the owner for folder access", "delegate": "Hand a task to a colleague role",
}
_NOTES = {
    "send_webhook": "The target is a webhook name the owner configured in the environment.",
    "send_slack": "The target is a Slack destination the owner configured in the environment.",
    "send_email": "The target comes from the owner's email settings.",
    "update_crm_record": "The target is the CRM the owner configured.",
    "schedule_event": "The target is the calendar the owner configured.",
    "notify_owner": "Sends to the owner's approval chat.",
    "mcp_call": "Calls a tool on an MCP server that is on the owner's allow list.",
    "create_task": "Creates a task ticket in the core's own database.",
    "run_code": "Runs in the sandbox runner container, which has no network.",
    "run_shell": "Runs in the sandbox runner container, which has no network.",
    "fetch_page": "Reads a public page whose address is chosen at run time, so no host list can be declared. "
                  "The runtime_chosen class carries the checks the core must apply to every address and redirect.",
    "list_files": "Works only inside folders the owner granted.",
    "read_file": "Works only inside folders the owner granted.",
    "preview_change": "Shows a change without applying it. Works only inside folders the owner granted.",
    "write_file": "Works only inside folders the owner granted with a write grant. Off by default.",
    "delete_file": "Works only inside folders the owner granted with a delete grant. Off by default.",
    "request_access": "Asks the owner to grant a folder. Off by default.",
    "delegate": "Off unless the owner turns sub-agents on and grants this operation. A child run starts with the "
                "parent's taint, and its taint comes back to the parent. A child's tools are cut down to the parent's, and "
                "a sensitive role anywhere above a child forces approval for every child action.",
}


def _taint(tool: str) -> str:
    out, tai = tool in OUTBOUND_BUILTINS, tool in TAINTING_BUILTINS
    return "both" if (out and tai) else ("outbound" if out else ("tainting" if tai else "none"))


def builtin_declaration(tool: str) -> Dict[str, Any]:
    """The declaration of one built-in tool, as a new plain dict each time."""
    if tool not in BUILTIN_EFFECT:
        raise KeyError(tool)
    op: Dict[str, Any] = {"name": tool, "effect": BUILTIN_EFFECT[tool]}
    if tool in ALWAYS_APPROVAL_OPERATIONS:
        op["approval"] = "always"
    if _taint(tool) != "none":
        op["taint"] = _taint(tool)
    if tool in GRANT_REQUIRED_OPERATIONS:
        op["grant"] = "explicit"
    if tool == "delegate":
        op["inherits"] = dict(DELEGATE_INHERITS)
    decl: Dict[str, Any] = {"schema": SCHEMA, "id": ID_PREFIX + tool, "version": VERSION,
                            "kind": KIND.get(tool, "action"), "operations": [op]}
    if tool in BUILTIN_EGRESS:
        decl["network"] = {"egress": BUILTIN_EGRESS[tool]}
    decl["descriptive"] = {"title": _TITLES[tool], "notes": _NOTES[tool]}
    return copy.deepcopy(decl)


def builtin_declarations() -> List[Dict[str, Any]]:
    """All built-in declarations, in a fixed order, as new plain dicts."""
    return [builtin_declaration(t) for t in sorted(BUILTIN_EFFECT)]


def register_builtins(registry) -> list:
    """Register every built-in declaration in a DeclarationRegistry (call before registry.seal())."""
    return [registry.register(d) for d in builtin_declarations()]
