"""
ragleap_integrations.mcp

An MCP client over Streamable HTTP that returns ragleap_tools.Tool objects.
Standard library plus ragleap-tools. See docs/design/mcp-client.md.

Trust model (the same as the app's core/mcp_client.py, made explicit):
- The OWNER configures servers (URL, optional static bearer token) and an
  exact allowlist of "server.tool" entries. The model only ever supplies a
  tool's JSON arguments; it never chooses a URL, a token or a tool outside
  the allowlist.
- Everything a server says is untrusted text: tool descriptions, input
  schemas and results flow into a model's context. They are not screened for
  prompt injection. The structural limits are the allowlist, a snapshot of
  the tool list taken once (a server that changes a description later has no
  effect), length caps, and optional owner-supplied specs that skip discovery.
- Server-supplied error text is never put in a result; failures carry a
  constant message plus, at most, an HTTP status or a JSON-RPC error code.

Governance (v0.2.0, see governance.py): a server's tool annotations are
untrusted hints that can only make policy stricter. Every tool is flagged
requires_approval in Tool.metadata["mcp"] unless the owner pre-approves it (or
trusts a server's readOnlyHint); the flag is enforced only when the owner
supplies McpPolicy.approve. An optional on_event hook receives small audit
events (never arguments or results).

Protocol: the current revision (2026-07-28) is tried first - every request is
its own POST with MCP-Protocol-Version, Mcp-Method and Mcp-Name headers and
the version/client info/capabilities in params._meta, with no handshake and
no session. A server that is not a modern server is detected as the spec's
Backward Compatibility section describes: a 4xx without a recognized modern
JSON-RPC error body (-32020 HeaderMismatch, -32021 MissingRequiredClient-
Capability, -32022 UnsupportedProtocolVersion) identifies a legacy server -
including a legacy server's own JSON-RPC rejection such as -32600, which a
live DeepWiki server returned - and it is then spoken to with the legacy
initialize handshake (2025-06-18 only; an explicit allowlist, not "whatever
the server answers").

Not supported in this version: stdio, HTTP+SSE, sampling/elicitation/roots
(a server asking for client input gets a clear "unsupported" result),
resources, prompts, subscriptions, OAuth, and the optional retry after a
HeaderMismatch error.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ragleap_tools import Tool, ToolResult

from ragleap_integrations import _net
from ragleap_integrations.governance import HINT_KEYS, McpPolicy, hints_from
from ragleap_integrations.governance import requires_approval as _requires_approval

logger = logging.getLogger("ragleap_integrations")

MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-06-18",)
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_INFO = "io.modelcontextprotocol/clientInfo"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
MAX_LIST_PAGES = 10
MAX_NAME_CHARS = 128
MAX_TOOL_NAME_CHARS = 64  # exposed function name; chat APIs restrict this

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_TCHAR_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_PRIMITIVE_TYPES = ("integer", "string", "boolean")
_SAFE_INT = 2 ** 53 - 1

# The codes the 2026-07-28 Streamable HTTP rules call recognized modern errors.
# A 400 carrying any other body means a legacy server: fall back to initialize.
_HEADER_MISMATCH = -32020
_MISSING_CAPABILITY = -32021
_UNSUPPORTED_VERSION = -32022
_MODERN_ERRORS = (_HEADER_MISMATCH, _MISSING_CAPABILITY, _UNSUPPORTED_VERSION)


class McpConfigError(ValueError):
    """Invalid configuration or owner-supplied tool spec (raised at setup)."""


class McpDiscoveryError(RuntimeError):
    """tools/list failed for a server (raised at setup, constant message)."""


# --------------------------------------------------------------------------
# Failures: constant wording, never server-supplied text
# --------------------------------------------------------------------------

_FAILURES = {
    "not_allowlisted": "MCP tool is not allowlisted",
    "bad_arguments": "MCP tool arguments are not valid",
    "http_status": "MCP server returned an HTTP error",
    "bad_response": "MCP server sent an unusable response",
    "rpc_error": "MCP server returned an error",
    "no_common_version": "MCP server and client share no protocol version",
    "legacy_transport": "MCP server does not use a transport this client supports",
    "input_required": "MCP server asked for client input, which is not supported",
    "not_approved": "MCP call was not approved",
    "token_unavailable": "MCP token is unavailable",
    "rate_limited": "MCP call limit reached",
}


class _Failure(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail  # an HTTP status or a JSON-RPC code, nothing else

    @property
    def message(self) -> str:
        base = _FAILURES.get(self.code, "MCP call failed")
        return f"{base} ({self.detail})" if self.detail else base


class _UseLegacy(Exception):
    def __init__(self, code: Optional[int] = None):
        super().__init__("use legacy")
        self.code = code  # the JSON-RPC code of the modern attempt's reply, if any


class _UnsupportedVersion(Exception):
    def __init__(self, supported: List[str]):
        super().__init__("unsupported version")
        self.supported = supported


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _has_control_chars(value: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


@dataclass
class McpServerConfig:
    """One remote MCP server. url must be https to a public host (checked
    again, with DNS, on every call). token is an optional static bearer
    token, sent only to this URL."""

    name: str
    url: str
    token: Optional[str] = None
    trust_annotations: bool = False  # True: this server's readOnlyHint: true may relax approval

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SERVER_NAME_RE.match(self.name):
            raise McpConfigError("server name must be 1-32 characters of letters, digits, '_' or '-'")
        try:
            _net.validate_url(self.url)
        except _net.TransportError as e:
            raise McpConfigError(f"server {self.name!r}: bad url ({e.message})")
        if self.token is not None:
            if not isinstance(self.token, str) or not self.token or _has_control_chars(self.token):
                raise McpConfigError(f"server {self.name!r}: token must be a non-empty string without control characters")
        if not isinstance(self.trust_annotations, bool):
            raise McpConfigError(f"server {self.name!r}: trust_annotations must be True or False")


@dataclass
class McpToolSpec:
    """What the model is told about one tool. Discovered, or supplied by the
    owner to skip discovery (and any server-controlled text) entirely."""

    server: str
    name: str
    description: str
    parameters: Dict[str, Any]
    annotations: Dict[str, bool] = field(default_factory=dict)  # the four boolean MCP hints; untrusted


@dataclass
class McpConfig:
    """servers and the exact 'server.tool' allowlist, plus limits. Nothing is
    read from the environment: the application builds this object."""

    servers: List[McpServerConfig]
    allowed_tools: List[str]
    max_result_chars: int = 4000
    max_description_chars: int = 500
    max_schema_chars: int = 10000
    total_timeout: float = _net.DEFAULT_TOTAL_TIMEOUT
    op_timeout: float = _net.DEFAULT_OP_TIMEOUT
    max_response_bytes: int = _net.DEFAULT_MAX_BYTES
    policy: Optional[McpPolicy] = None
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None

    def __post_init__(self) -> None:
        if not self.servers:
            raise McpConfigError("at least one server is required")
        names = [s.name for s in self.servers]
        if len(set(names)) != len(names):
            raise McpConfigError("server names must be unique")
        if not self.allowed_tools:
            raise McpConfigError("allowed_tools must not be empty")
        cleaned: List[str] = []
        for entry in self.allowed_tools:
            if not isinstance(entry, str) or "." not in entry:
                raise McpConfigError("allowed_tools entries must look like 'server.tool'")
            server, tool = entry.split(".", 1)
            if server not in names:
                raise McpConfigError(f"allowed_tools entry {entry!r} names an unknown server")
            if not tool or len(tool) > MAX_NAME_CHARS or _has_control_chars(tool):
                raise McpConfigError(f"allowed_tools entry {entry!r} has a bad tool name")
            if entry not in cleaned:
                cleaned.append(entry)
        self.allowed_tools = cleaned
        for attr in ("max_result_chars", "max_description_chars", "max_schema_chars",
                     "total_timeout", "op_timeout", "max_response_bytes"):
            value = getattr(self, attr)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise McpConfigError(f"{attr} must be a positive number")
        if self.policy is not None:
            if not isinstance(self.policy, McpPolicy):
                raise McpConfigError("policy must be an McpPolicy")
            for entry in self.policy.pre_approved:
                if entry not in self.allowed_tools:
                    raise McpConfigError(f"pre_approved entry {entry!r} is not in allowed_tools")
        if self.on_event is not None and not callable(self.on_event):
            raise McpConfigError("on_event must be callable")


@dataclass
class Discovery:
    tools: List[McpToolSpec] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # ("server.tool", reason)


# --------------------------------------------------------------------------
# Header value encoding and x-mcp-header handling (spec: Value Encoding)
# --------------------------------------------------------------------------

def _encode_header_value(value: str) -> str:
    """Plain if it is safe visible ASCII with no leading/trailing space and
    does not look like the sentinel; otherwise the Base64 sentinel form."""
    plain = (
        all(0x20 <= ord(c) <= 0x7E for c in value)
        and value == value.strip(" ")
        and not (value.startswith("=?base64?") and value.endswith("?="))
    )
    if plain:
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


class _BadSchema(Exception):
    pass


def _count_annotations(node: Any, props_map: bool = False) -> int:
    n = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if props_map:
                n += _count_annotations(value)
            elif key == "x-mcp-header":
                n += 1
            elif key == "properties":
                n += _count_annotations(value, True)
            else:
                n += _count_annotations(value)
    elif isinstance(node, list):
        n += sum(_count_annotations(item) for item in node)
    return n


def _header_params(schema: Dict[str, Any]) -> List[Tuple[Tuple[str, ...], str]]:
    """The (property path, header name) pairs of a tool's inputSchema, or
    _BadSchema if any x-mcp-header annotation breaks the spec's constraints
    (the spec requires such a tool to be rejected)."""
    found: List[Tuple[Tuple[str, ...], Any, Any]] = []

    def walk(node: Dict[str, Any], path: Tuple[str, ...]) -> None:
        props = node.get("properties")
        if not isinstance(props, dict):
            return
        for pname, psch in props.items():
            if not isinstance(psch, dict):
                continue
            if "x-mcp-header" in psch:
                found.append((path + (pname,), psch["x-mcp-header"], psch.get("type")))
            walk(psch, path + (pname,))

    walk(schema, ())
    if _count_annotations(schema) != len(found):
        raise _BadSchema("annotation outside the properties chain")
    seen = set()
    out: List[Tuple[Tuple[str, ...], str]] = []
    for path, name, ptype in found:
        if not isinstance(name, str) or not _TCHAR_RE.match(name):
            raise _BadSchema("invalid header name")
        if name.lower() in seen:
            raise _BadSchema("duplicate header name")
        seen.add(name.lower())
        if ptype not in _PRIMITIVE_TYPES:
            raise _BadSchema("header parameter is not integer, string or boolean")
        out.append((path, name))
    return out


def _header_values(header_params: List[Tuple[Tuple[str, ...], str]], arguments: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for path, hname in header_params:
        cur: Any = arguments
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                cur = None
                break
        if cur is None:
            continue
        if isinstance(cur, bool):
            out[hname] = "true" if cur else "false"
        elif isinstance(cur, int):
            if abs(cur) > _SAFE_INT:
                raise _Failure("bad_arguments")
            out[hname] = str(cur)
        elif isinstance(cur, str):
            out[hname] = cur
        else:
            raise _Failure("bad_arguments")
    return out


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------

def _sse_find(data: bytes, rpc_id: int, final: bool) -> Optional[Dict[str, Any]]:
    """The JSON-RPC response with this id inside an event stream, or None.
    With final=False the last, possibly incomplete event is ignored."""
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    chunks = text.split("\n\n")
    if not final:
        chunks = chunks[:-1]
    for chunk in chunks:
        parts = []
        for line in chunk.split("\n"):
            if line.startswith("data:"):
                value = line[5:]
                parts.append(value[1:] if value.startswith(" ") else value)
        if not parts:
            continue
        try:
            obj = json.loads("\n".join(parts))
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("id") == rpc_id and ("result" in obj or "error" in obj):
            return obj
    return None


def _rpc_from_response(resp: "_net.HttpResponse", rpc_id: int) -> Dict[str, Any]:
    obj: Any = None
    if resp.content_type == "text/event-stream":
        obj = _sse_find(resp.body, rpc_id, True)
    elif resp.content_type == "application/json":
        try:
            obj = json.loads(resp.body)
        except ValueError:
            obj = None
        if not (isinstance(obj, dict) and obj.get("id") == rpc_id and ("result" in obj or "error" in obj)):
            obj = None
    if obj is None:
        raise _Failure("bad_response")
    return obj


def _unwrap(obj: Dict[str, Any]) -> Dict[str, Any]:
    err = obj.get("error")
    if err is not None:
        code = err.get("code") if isinstance(err, dict) else None
        raise _Failure("rpc_error", f"code {code}" if isinstance(code, int) else "")
    result = obj.get("result")
    if not isinstance(result, dict):
        raise _Failure("bad_response")
    return result


def _jsonrpc_error(resp: "_net.HttpResponse") -> Optional[Dict[str, Any]]:
    """The error object when the body is a JSON-RPC error response (what the
    spec calls a recognized modern error), else None."""
    try:
        obj = json.loads(resp.body)
    except ValueError:
        return None
    if (isinstance(obj, dict) and obj.get("jsonrpc") == "2.0" and isinstance(obj.get("error"), dict)
            and isinstance(obj["error"].get("code"), int)):
        return obj["error"]
    return None


def _supported_versions(err: Dict[str, Any]) -> Optional[List[str]]:
    for holder in (err.get("data"), err):
        if isinstance(holder, dict):
            sup = holder.get("supported")
            if isinstance(sup, list) and all(isinstance(v, str) for v in sup):
                return sup
    return None


def _to_tool_result(result: Dict[str, Any], cap: int) -> ToolResult:
    if "inputRequests" in result:
        raise _Failure("input_required")
    content = result.get("content")
    if not isinstance(content, list):
        raise _Failure("bad_response")
    texts: List[str] = []
    omitted = 0
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            texts.append(item["text"])
        else:
            omitted += 1
    text = "\n".join(texts)
    if omitted:
        text += f"\n[{omitted} non-text content item(s) omitted]"
    if len(text) > cap:
        text = text[:cap] + "\n[truncated]"
    if result.get("isError") is True:
        return ToolResult(success=False, error=text or "the tool reported an error")
    return ToolResult(success=True, result={"text": text})


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------

Transport = Callable[..., "_net.HttpResponse"]


class McpClient:
    """transport(url, headers, body_bytes, stop) -> HttpResponse is the one
    network seam; the default is the pinned, bounded _net.https_post. Tests
    inject a fake. Not safe to share between threads while a call is running
    (the protocol era per server is cached on first success)."""

    def __init__(self, config: McpConfig, transport: Optional[Transport] = None, *,
                 token_for: Optional[Callable[[str], Any]] = None, scope: Any = None,
                 guard: Optional[Callable[[], bool]] = None, lock: Any = None):
        # token_for, scope, guard and lock are set by ragleap_integrations.tenancy.McpHub:
        # a per-request token, the scope events carry, a rate-limit check and a lock that
        # serializes this scope's requests. Without them nothing changes.
        self.config = config
        self._token_for = token_for
        self._scope = scope
        self._guard = guard
        self._lock = lock
        self._servers = {s.name: s for s in config.servers}
        self._allowed = set(config.allowed_tools)
        self._ids = itertools.count(1)
        self._eras: Dict[str, str] = {}
        self._transport = transport or self._default_transport

    # -- transport ---------------------------------------------------------

    def _default_transport(self, url, headers, body, stop=None):
        c = self.config
        return _net.https_post(
            url, headers, body,
            total_timeout=c.total_timeout, op_timeout=c.op_timeout,
            max_bytes=c.max_response_bytes, stop=stop,
        )

    def _post(self, server: str, headers: Dict[str, str], obj: Dict[str, Any], rpc_id: Optional[int] = None):
        cfg = self._servers[server]
        sent = dict(headers)
        token = self._resolve_token(server) if self._token_for is not None else cfg.token
        if token:
            sent["Authorization"] = "Bearer " + token
        stop = None
        if rpc_id is not None:
            stop = lambda buf: _sse_find(buf, rpc_id, False) is not None  # noqa: E731
        return self._transport(cfg.url, sent, json.dumps(obj).encode("utf-8"), stop)

    def _resolve_token(self, server: str) -> Optional[str]:
        """The per-request token from the owner's provider. None means no token (the
        static token is NOT used then). Any failure, or an unusable value, gives a
        constant message; the exception text and the token are never kept."""
        try:
            value = self._token_for(server)
            if value is not None and not isinstance(value, str) and callable(getattr(value, "reveal", None)):
                value = value.reveal()
        except Exception as exc:
            logger.error("token provider failed for server %s (%s)", server, type(exc).__name__)
            raise _Failure("token_unavailable")
        if value is None:
            return None
        if not isinstance(value, str) or not value or _has_control_chars(value):
            raise _Failure("token_unavailable")
        return value

    # -- modern (2026-07-28) -------------------------------------------------

    @staticmethod
    def _meta() -> Dict[str, Any]:
        from ragleap_integrations import __version__
        return {
            META_VERSION: MODERN_VERSION,
            META_INFO: {"name": "ragleap-integrations", "version": __version__},
            META_CAPS: {},
        }

    def _modern(self, server, method, params, name, param_headers) -> Dict[str, Any]:
        rpc_id = next(self._ids)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MODERN_VERSION,
            "Mcp-Method": method,
        }
        if name is not None:
            headers["Mcp-Name"] = _encode_header_value(name)
        for hname, hvalue in (param_headers or {}).items():
            headers["Mcp-Param-" + hname] = _encode_header_value(hvalue)
        body = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": dict(params, _meta=self._meta())}
        resp = self._post(server, headers, body, rpc_id)
        if 200 <= resp.status < 300:
            return _unwrap(_rpc_from_response(resp, rpc_id))
        if resp.status in (400, 404, 405):
            err = _jsonrpc_error(resp)
            if err is None:
                raise _UseLegacy()
            code = err["code"]
            if resp.status == 400 and code not in _MODERN_ERRORS:
                # A legacy server rejecting the modern headers with its own
                # JSON-RPC error (a live DeepWiki server answered -32600).
                raise _UseLegacy(code)
            if code == _UNSUPPORTED_VERSION:
                supported = _supported_versions(err)
                if supported is not None:
                    raise _UnsupportedVersion(supported)
                raise _Failure("no_common_version")
            raise _Failure("rpc_error", f"code {code}")
        raise _Failure("http_status", f"HTTP {resp.status}")

    # -- legacy (2025-06-18) -------------------------------------------------

    def _legacy(self, server, method, params) -> Dict[str, Any]:
        base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        init_id = next(self._ids)
        init = {
            "jsonrpc": "2.0", "id": init_id, "method": "initialize",
            "params": {
                "protocolVersion": LEGACY_VERSIONS[0],
                "capabilities": {},
                "clientInfo": self._meta()[META_INFO],
            },
        }
        resp = self._post(server, base, init, init_id)
        if not 200 <= resp.status < 300:
            if resp.status in (400, 404, 405) and _jsonrpc_error(resp) is None:
                raise _Failure("legacy_transport")
            raise _Failure("http_status", f"HTTP {resp.status}")
        result = _unwrap(_rpc_from_response(resp, init_id))
        negotiated = result.get("protocolVersion")
        if negotiated not in LEGACY_VERSIONS:
            raise _Failure("no_common_version")
        headers = dict(base)
        headers["MCP-Protocol-Version"] = negotiated
        session = resp.headers.get("mcp-session-id")
        if session:
            headers["Mcp-Session-Id"] = session
        note = self._post(server, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        if not 200 <= note.status < 300:
            raise _Failure("http_status", f"HTTP {note.status}")
        rpc_id = next(self._ids)
        body = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        resp = self._post(server, headers, body, rpc_id)
        if not 200 <= resp.status < 300:
            raise _Failure("http_status", f"HTTP {resp.status}")
        return _unwrap(_rpc_from_response(resp, rpc_id))

    # -- one request, either era -------------------------------------------

    def _request(self, server, method, params, name=None, param_headers=None) -> Dict[str, Any]:
        if self._lock is None:
            return self._request_unlocked(server, method, params, name, param_headers)
        with self._lock:
            return self._request_unlocked(server, method, params, name, param_headers)

    def _request_unlocked(self, server, method, params, name=None, param_headers=None) -> Dict[str, Any]:
        first_code: Optional[int] = None
        if self._eras.get(server) != "legacy":
            try:
                result = self._modern(server, method, params, name, param_headers)
                self._eras[server] = "modern"
                return result
            except _UseLegacy as fallback:
                first_code = fallback.code
            except _UnsupportedVersion as unsupported:
                if not set(unsupported.supported) & set(LEGACY_VERSIONS):
                    raise _Failure("no_common_version")
        try:
            result = self._legacy(server, method, params)
        except _Failure as failure:
            if first_code is None:
                raise
            # Keep the modern attempt's reply visible: a modern server that
            # answered 400 for a reason of its own (for example invalid
            # params) would otherwise be reported only as a legacy failure.
            extra = f"first reply code {first_code}"
            raise _Failure(failure.code, f"{failure.detail}; {extra}" if failure.detail else extra)
        self._eras[server] = "legacy"
        return result

    # -- events --------------------------------------------------------------

    def _emit(self, event: Dict[str, Any]) -> None:
        """Hands a small event dict (never arguments or results) to the owner's
        on_event hook. A failing hook can never break a call."""
        hook = self.config.on_event
        if hook is None:
            return
        event = dict(event)
        if self._scope is not None:
            event["tenant"] = self._scope.tenant
            event["scope"] = self._scope.key
        try:
            hook(event)
        except Exception:
            logger.exception("on_event hook failed")

    # -- discovery -----------------------------------------------------------

    def discover(self) -> Discovery:
        """tools/list for the allowlisted tools only. Raises
        McpDiscoveryError if a server cannot be listed."""
        started = time.monotonic()
        try:
            out = self._discover()
        except McpDiscoveryError as e:
            self._emit({"event": "discover", "ok": False, "error": str(e),
                        "duration_ms": int((time.monotonic() - started) * 1000)})
            raise
        self._emit({"event": "discover", "ok": True, "tools": len(out.tools), "skipped": len(out.skipped),
                    "duration_ms": int((time.monotonic() - started) * 1000)})
        return out

    def _discover(self) -> Discovery:
        out = Discovery()
        cfg = self.config
        wanted: Dict[str, List[str]] = {}
        for entry in cfg.allowed_tools:
            server, tool = entry.split(".", 1)
            wanted.setdefault(server, []).append(tool)
        for server, tools in wanted.items():
            found: Dict[str, Dict[str, Any]] = {}
            cursor: Optional[str] = None
            try:
                for _page in range(MAX_LIST_PAGES):
                    result = self._request(server, "tools/list", {"cursor": cursor} if cursor else {})
                    listed = result.get("tools")
                    if not isinstance(listed, list):
                        raise _Failure("bad_response")
                    for item in listed:
                        if isinstance(item, dict) and isinstance(item.get("name"), str) \
                                and item["name"] in tools and item["name"] not in found:
                            found[item["name"]] = item
                    cursor = result.get("nextCursor")
                    if not isinstance(cursor, str) or not cursor or len(found) == len(tools):
                        break
            except _Failure as f:
                raise McpDiscoveryError(f"{server}: {f.message}")
            except _net.TransportError as e:
                raise McpDiscoveryError(f"{server}: MCP server request failed ({e.message})")
            for tool in tools:
                key = f"{server}.{tool}"
                item = found.get(tool)
                if item is None:
                    out.skipped.append((key, "not offered by the server"))
                    continue
                schema = item.get("inputSchema", {"type": "object", "properties": {}})
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    out.skipped.append((key, "inputSchema is not an object schema"))
                    continue
                if len(json.dumps(schema)) > cfg.max_schema_chars:
                    out.skipped.append((key, "inputSchema is too large"))
                    continue
                try:
                    _header_params(schema)
                except _BadSchema:
                    out.skipped.append((key, "invalid x-mcp-header annotation"))
                    continue
                description = item.get("description")
                description = description[: cfg.max_description_chars] if isinstance(description, str) else ""
                out.tools.append(McpToolSpec(server=server, name=tool, description=description, parameters=schema,
                                             annotations=hints_from(item.get("annotations"))))
        for key, reason in out.skipped:
            logger.warning("MCP tool %s skipped: %s", key, reason)
        return out

    # -- calling -------------------------------------------------------------

    def call_tool(self, server: str, tool: str, arguments: Optional[Dict[str, Any]] = None,
                  parameters: Optional[Dict[str, Any]] = None,
                  requires_approval: Optional[bool] = None) -> ToolResult:
        """Call one allowlisted tool. Never raises. parameters is the tool's
        inputSchema, used only to mirror x-mcp-header parameters. requires_approval
        says whether policy.approve must agree first (None means unknown, which
        is treated as True); it has an effect only when the owner set
        McpPolicy.approve."""
        started = time.monotonic()
        info: Dict[str, Any] = {}
        result = self._call_tool(server, tool, arguments, parameters, requires_approval, info)
        error = None if result.success else ("tool reported an error" if info.get("tool_error") else result.error)
        self._emit({"event": "call", "server": server, "tool": tool, "ok": result.success, "error": error,
                    "approval": info.get("approval", "unchecked"),
                    "duration_ms": int((time.monotonic() - started) * 1000)})
        return result

    def _check_approval(self, server: str, tool: str, arguments: Dict[str, Any],
                        requires_approval: Optional[bool], info: Dict[str, Any]) -> None:
        if requires_approval is None:
            requires_approval = True  # unknown: fail closed
        if not requires_approval:
            info["approval"] = "not_required"
            return
        policy = self.config.policy
        scoped = getattr(policy, "approve_scoped", None) if policy is not None else None
        if policy is None or (policy.approve is None and scoped is None):
            info["approval"] = "not_enforced"
            return
        try:
            safe_args = json.loads(json.dumps(arguments))
            if scoped is not None:
                answer = scoped(self._scope, server, tool, safe_args) if self._scope is not None else False
            else:
                answer = policy.approve(server, tool, safe_args)
        except Exception:
            logger.exception("approve hook failed for %s.%s", server, tool)
            answer = False
        if answer is not True:
            info["approval"] = "denied"
            raise _Failure("not_approved")
        info["approval"] = "approved"

    def _call_tool(self, server: str, tool: str, arguments: Optional[Dict[str, Any]],
                   parameters: Optional[Dict[str, Any]], requires_approval: Optional[bool],
                   info: Dict[str, Any]) -> ToolResult:
        try:
            if f"{server}.{tool}" not in self._allowed:
                raise _Failure("not_allowlisted")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise _Failure("bad_arguments")
            try:
                json.dumps(arguments)
            except (TypeError, ValueError):
                raise _Failure("bad_arguments")
            header_params: List[Tuple[Tuple[str, ...], str]] = []
            if isinstance(parameters, dict):
                try:
                    header_params = _header_params(parameters)
                except _BadSchema:
                    raise _Failure("bad_arguments")
            param_headers = _header_values(header_params, arguments)
            if self._guard is not None:
                try:
                    allowed_now = self._guard()
                except Exception:
                    logger.exception("rate-limit check failed")
                    allowed_now = False
                if not allowed_now:
                    raise _Failure("rate_limited")
            self._check_approval(server, tool, arguments, requires_approval, info)
            result = self._request(server, "tools/call", {"name": tool, "arguments": arguments},
                                   name=tool, param_headers=param_headers)
            converted = _to_tool_result(result, self.config.max_result_chars)
            info["tool_error"] = not converted.success
            return converted
        except _Failure as f:
            return ToolResult(success=False, error=f.message)
        except _net.TransportError as e:
            return ToolResult(success=False, error=f"MCP server request failed ({e.message})")
        except Exception:
            logger.exception("MCP call %s.%s failed unexpectedly", server, tool)
            return ToolResult(success=False, error="MCP call failed")


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def _tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{server}__{tool}")[:MAX_TOOL_NAME_CHARS]


def _make_handler(client: McpClient, spec: McpToolSpec,
                  requires_approval: bool = True) -> Callable[..., ToolResult]:
    # A closure, not a default argument: the model controls the keyword names
    # in **arguments, so no parameter name of ours may be shadowable by one.
    def handler(**arguments: Any) -> ToolResult:
        return client.call_tool(spec.server, spec.name, arguments, spec.parameters, requires_approval)

    return handler


def make_mcp_tools(config: McpConfig, *, specs: Optional[List[McpToolSpec]] = None,
                   client: Optional[McpClient] = None) -> List[Tool]:
    """One ragleap_tools.Tool per allowlisted MCP tool.

    Without specs, the allowlisted tools are discovered once (tools/list) and
    that snapshot is what the model sees; tools the server does not offer, or
    whose schema is unusable, are left out with a logged warning. With specs
    (owner-supplied descriptions and schemas) nothing is sent over the network
    here and no server-controlled text reaches the model. Raises McpConfigError
    or McpDiscoveryError - setup problems, not tool-calling failures."""
    client = client or McpClient(config)
    if specs is None:
        specs = client.discover().tools
    else:
        for spec in specs:
            if f"{spec.server}.{spec.name}" not in set(config.allowed_tools):
                raise McpConfigError(f"spec {spec.server}.{spec.name} is not in allowed_tools")
            if not isinstance(spec.description, str):
                raise McpConfigError(f"spec {spec.server}.{spec.name}: description must be a string")
            if not isinstance(spec.parameters, dict) or spec.parameters.get("type") != "object":
                raise McpConfigError(f"spec {spec.server}.{spec.name}: parameters must be an object schema")
            if not isinstance(spec.annotations, dict) or any(
                    k not in HINT_KEYS or not isinstance(v, bool) for k, v in spec.annotations.items()):
                raise McpConfigError(f"spec {spec.server}.{spec.name}: annotations must hold only the four boolean hints")
            try:
                _header_params(spec.parameters)
            except _BadSchema:
                raise McpConfigError(f"spec {spec.server}.{spec.name}: invalid x-mcp-header annotation")
    tools: List[Tool] = []
    names = set()
    for spec in specs:
        name = _tool_name(spec.server, spec.name)
        if name in names:
            raise McpConfigError(f"two tools would be exposed as {name!r}")
        names.add(name)

        key = f"{spec.server}.{spec.name}"
        server_cfg = next(s for s in config.servers if s.name == spec.server)
        flag = _requires_approval(key, spec.annotations, server_cfg.trust_annotations, config.policy)
        meta = {"mcp": {"server": spec.server, "tool": spec.name,
                        "annotations": dict(spec.annotations), "requires_approval": flag}}
        tools.append(Tool(name=name, description=spec.description, parameters=spec.parameters,
                          handler=_make_handler(client, spec, flag), metadata=meta))
    return tools
