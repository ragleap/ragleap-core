"""
ragleap_integrations.connect

One call from a URL to ready-to-use tools:

    from ragleap_integrations import connect

    conn = connect("https://mcp.example.com/mcp", allow=["list_orders", "get_order"], token=MY_TOKEN)
    print(conn.report())                       # what is ready, what was skipped and why
    result = conn.toolkit.call(name_from_model, arguments_from_model)   # never raises
    provider_tools = conn.toolkit.to_openai_schema()

connect() keeps every rule of the lower-level API: the owner names the exact
tools (allow has no wildcard), https to a public host only, the tool list is a
snapshot taken once, and nothing is read from the environment. For several
servers, build an McpConfig yourself and call connect_config().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ragleap_tools import Tool, Toolkit

from ragleap_integrations.governance import McpPolicy
from ragleap_integrations.mcp import (
    McpClient,
    McpConfig,
    McpConfigError,
    McpServerConfig,
    McpToolSpec,
    Transport,
    make_mcp_tools,
)

_HINTS = {
    "inputSchema is too large": "raise max_schema_chars",
    "not offered by the server": "check the tool name",
}


@dataclass
class Connection:
    config: McpConfig
    client: McpClient
    tools: List[Tool]
    skipped: List[Tuple[str, str]]
    toolkit: Toolkit = field(init=False)

    def __post_init__(self) -> None:
        self.toolkit = Toolkit(self.tools)

    def report(self) -> str:
        """A short readable summary: the ready tools (with whether approval is
        flagged) and every skipped tool with its reason and, where there is
        one, the setting that controls it."""
        lines = ["%d tool(s) ready, %d skipped" % (len(self.tools), len(self.skipped))]
        for t in self.tools:
            flagged = t.metadata.get("mcp", {}).get("requires_approval", True)
            lines.append("  ready: %s (approval: %s)" % (t.name, "required" if flagged else "not required"))
        for key, reason in self.skipped:
            hint = _HINTS.get(reason)
            lines.append("  skipped: %s - %s%s" % (key, reason, " (%s)" % hint if hint else ""))
        return "\n".join(lines)


def connect_config(config: McpConfig, *, specs: Optional[List[McpToolSpec]] = None,
                   transport: Optional[Transport] = None, client: Optional[McpClient] = None) -> Connection:
    """Discover (or use the owner-supplied specs) and build the tools for any
    McpConfig. Raises McpConfigError or McpDiscoveryError: setup problems."""
    client = client or McpClient(config, transport=transport)
    if specs is None:
        discovery = client.discover()
        specs, skipped = discovery.tools, list(discovery.skipped)
    else:
        skipped = []
    tools = make_mcp_tools(config, specs=specs, client=client)
    return Connection(config=config, client=client, tools=tools, skipped=skipped)


def connect(url: str, *, allow: Sequence[str], name: str = "mcp", token: Optional[str] = None,
            trust_annotations: bool = False,
            approve: Optional[Callable[[str, str, Dict[str, Any]], bool]] = None,
            pre_approved: Sequence[str] = (),
            on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
            transport: Optional[Transport] = None, **limits: Any) -> Connection:
    """Connect to one MCP server. allow is a non-empty list of exact tool names
    (no wildcard). name prefixes the exposed tool names (name__tool). approve
    and pre_approved build an McpPolicy; limits (max_result_chars,
    max_schema_chars, total_timeout, ...) go to McpConfig."""
    if isinstance(allow, str) or not isinstance(allow, (list, tuple)) or not allow \
            or not all(isinstance(t, str) for t in allow):
        raise McpConfigError("allow must be a non-empty list of tool names")
    if isinstance(pre_approved, str) or not all(isinstance(t, str) for t in pre_approved):
        raise McpConfigError("pre_approved must be a list of tool names")
    server = McpServerConfig(name=name, url=url, token=token, trust_annotations=trust_annotations)
    policy = None
    if approve is not None or pre_approved:
        try:
            policy = McpPolicy(approve=approve, pre_approved=["%s.%s" % (name, t) for t in pre_approved])
        except ValueError as exc:
            raise McpConfigError(str(exc)) from None
    config = McpConfig(servers=[server], allowed_tools=["%s.%s" % (name, t) for t in allow],
                       policy=policy, on_event=on_event, **limits)
    return connect_config(config, transport=transport)
