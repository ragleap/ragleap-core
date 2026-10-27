"""
ragleap_integrations - connectors that return ragleap_tools.Tool objects.

v0.1.0: an MCP client over Streamable HTTP (see docs/design/mcp-client.md).

v0.1.1: the default response-size cap is 1 MiB (it was 256 KiB, which a real
15-tool server exceeded), and the "response too large" error names the
max_response_bytes setting.

v0.2.0: easy to use and governed. connect(url, allow=[...]) returns ready tools and a
Toolkit in one call; McpPolicy and McpServerConfig.trust_annotations decide which tools
are flagged requires_approval (server hints are untrusted and can only tighten policy);
McpPolicy.approve enforces the flag; on_event receives small audit events. See
docs/design/mcp-governance.md.

v0.3.0: multi-tenant. McpHub gives each Scope (ragleap-security's, or a built-in one) its own
connection with per-request tokens, a narrowed allowlist, its own policy, a per-tenant RateLimit
and scoped audit events. See docs/design/multi-tenant.md.
"""

__version__ = "0.3.0"

from ragleap_integrations.connect import Connection, connect, connect_config
from ragleap_integrations.governance import McpPolicy
from ragleap_integrations.tenancy import HAS_SECURITY, McpHub, RateLimit, Scope, ScopeError, require_scope
from ragleap_integrations.mcp import (
    Discovery,
    McpClient,
    McpConfig,
    McpConfigError,
    McpDiscoveryError,
    McpServerConfig,
    McpToolSpec,
    make_mcp_tools,
)

__all__ = [
    "__version__",
    "Discovery",
    "McpClient",
    "McpConfig",
    "McpConfigError",
    "McpDiscoveryError",
    "McpServerConfig",
    "McpToolSpec",
    "McpPolicy",
    "Connection",
    "connect",
    "connect_config",
    "make_mcp_tools",
    "McpHub",
    "RateLimit",
    "Scope",
    "ScopeError",
    "require_scope",
    "HAS_SECURITY",
]
