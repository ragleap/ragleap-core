"""
ragleap_integrations.utcp

A client for UTCP (Universal Tool Calling Protocol) manuals that returns
ragleap_tools.Tool objects. Standard library plus ragleap-tools.
See docs/design/utcp.md.

    from ragleap_integrations import connect_utcp

    conn = connect_utcp("https://api.example.com/utcp", allow=["get_order"], token=MY_TOKEN)
    print(conn.report())
    result = conn.toolkit.call(name_from_model, arguments_from_model)   # never raises

Trust model (the same as the MCP client in mcp.py):
- The OWNER names the manual URL, an exact allowlist of tool names (no
  wildcard) and the approved origins. The model only supplies a tool's JSON
  arguments; it never chooses a URL, a header, a token or a tool outside the
  allowlist.
- The manual is read ONCE (a snapshot). A manual that changes later has no
  effect, because nothing is re-read when a tool is called.
- Only the "http" call template is supported, and only with an https URL on an
  approved origin (the manual's own origin unless the owner lists more). The
  origin is checked at discovery and again on every call. Every other template
  type is skipped with a stated reason.
- No ${NAME} or $NAME placeholder is ever substituted, and nothing is read from
  the environment: a tool whose template contains one is skipped. Credentials
  come only from the owner's code (token).
- Everything a manual says is untrusted text. It is not screened for prompt
  injection. Server error text is never put in a result; failures carry a
  constant message plus, at most, an HTTP status.

Not supported in this version: OpenAPI documents (only UTCP manuals), call
template types other than http, header_fields, auth blocks in a tool's
template (the owner's token is sent as a bearer token to approved origins),
non-JSON request bodies, variables, OAuth, streaming.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode, urlsplit

from ragleap_tools import Tool, ToolResult

from ragleap_integrations import _net
from ragleap_integrations.connect import Connection
from ragleap_integrations.governance import McpPolicy
from ragleap_integrations.governance import requires_approval as _requires_approval
from ragleap_integrations.mcp import (
    MAX_NAME_CHARS,
    McpConfig,
    McpConfigError,
    McpDiscoveryError,
    _SERVER_NAME_RE,
    _has_control_chars,
    _tool_name,
)

logger = logging.getLogger("ragleap_integrations")

HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")
_BODY_METHODS = ("POST", "PUT", "PATCH")
_PLACEHOLDER = re.compile(r"\$\{|\$[A-Za-z_]")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH_PARAM = re.compile(r"\{([^{}]*)\}")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# Static headers a manual may not set: the owner's token, routing and framing are ours.
_FORBIDDEN_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "host", "content-length", "transfer-encoding",
    "connection", "upgrade", "te", "trailer", "expect", "accept-encoding", "content-type", "accept",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip",
})
# The template fields this version understands. Any other field (query_params, a fixed body, a timeout, a
# response format ...) would change what a call means, and ignoring it silently would mis-call the API, so a
# template that carries one is skipped.
_KNOWN_TEMPLATE_FIELDS = frozenset({
    "call_template_type", "http_method", "url", "content_type", "body_field", "headers", "header_fields",
    "auth", "auth_tools", "name",
})
MAX_STATIC_HEADERS = 16
MAX_HEADER_VALUE_CHARS = 512

Transport = Callable[..., "_net.HttpResponse"]


class UtcpConfigError(McpConfigError):
    """Invalid UTCP configuration (raised at setup)."""


class UtcpDiscoveryError(McpDiscoveryError):
    """The manual could not be read (raised at setup, constant message)."""


_FAILURES = {
    "not_allowlisted": "UTCP tool is not allowlisted",
    "bad_arguments": "UTCP tool arguments are not valid",
    "http_status": "UTCP server returned an HTTP error",
    "bad_response": "UTCP server sent an unusable response",
    "not_approved": "UTCP call was not approved",
    "origin": "UTCP request is outside the approved origins",
}


class _Failure(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail  # an HTTP status, nothing else

    @property
    def message(self) -> str:
        base = _FAILURES.get(self.code, "UTCP call failed")
        return f"{base} ({self.detail})" if self.detail else base


class _Skip(Exception):
    """A tool that cannot be offered; the text is always one of ours."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _origin_key(origin: str) -> Tuple[str, int]:
    """'https://host[:port]' -> (host, port). Raises UtcpConfigError."""
    if not isinstance(origin, str):
        raise UtcpConfigError("origins entries must be strings like 'https://api.example.com'")
    try:
        p = urlsplit(origin)
        if p.scheme != "https" or p.path not in ("", "/") or p.query or p.fragment or not p.netloc:
            raise UtcpConfigError("origins entries must look like 'https://host' or 'https://host:port'")
        host, port, _ = _net.validate_url(origin.rstrip("/") + "/")
    except _net.TransportError as e:
        raise UtcpConfigError(f"bad origin ({e.message})") from None
    except ValueError:
        raise UtcpConfigError("origins entries must look like 'https://host' or 'https://host:port'") from None
    return host, port


@dataclass
class UtcpServerConfig:
    """One UTCP manual. url is where the manual is read (https, public host,
    checked again with DNS on every request). origins lists extra https
    origins its tools may call; the manual's own origin is always approved.
    token is an optional static bearer token, sent only to approved origins."""

    name: str
    url: str
    token: Optional[str] = None
    origins: Sequence[str] = ()
    approved: frozenset = field(init=False, repr=False, default=frozenset())

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SERVER_NAME_RE.match(self.name):
            raise UtcpConfigError("server name must be 1-32 characters of letters, digits, '_' or '-'")
        try:
            host, port, _ = _net.validate_url(self.url)
        except _net.TransportError as e:
            raise UtcpConfigError(f"server {self.name!r}: bad url ({e.message})") from None
        if self.token is not None:
            if not isinstance(self.token, str) or not self.token or _has_control_chars(self.token):
                raise UtcpConfigError(f"server {self.name!r}: token must be a non-empty string without control characters")
        if isinstance(self.origins, str) or not isinstance(self.origins, (list, tuple)):
            raise UtcpConfigError("origins must be a list of 'https://host' strings")
        approved = {(host, port)}
        for origin in self.origins:
            approved.add(_origin_key(origin))
        self.approved = frozenset(approved)


@dataclass
class UtcpToolSpec:
    """What the model is told about one tool, plus the (validated) request
    shape taken from the manual snapshot."""

    server: str
    name: str
    description: str
    parameters: Dict[str, Any]
    method: str
    url: str  # https, no query, {name} only inside the path
    path_params: Tuple[str, ...] = ()
    body_field: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class UtcpDiscovery:
    tools: List[UtcpToolSpec] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # ("server.tool", reason)


@dataclass
class UtcpConfig(McpConfig):
    """servers (UtcpServerConfig) and the exact 'server.tool' allowlist, plus
    the same limits, policy and on_event as McpConfig. Nothing is read from the
    environment: the application builds this object."""

    def __post_init__(self) -> None:
        if not isinstance(self.servers, (list, tuple)) or not all(isinstance(s, UtcpServerConfig) for s in self.servers):
            raise UtcpConfigError("servers must be a list of UtcpServerConfig")
        super().__post_init__()


# --------------------------------------------------------------------------
# Reading a manual (a pure function, tested without a network)
# --------------------------------------------------------------------------

def _check_url(url: Any, server: UtcpServerConfig, props: Dict[str, Any]) -> Tuple[str, Tuple[str, ...]]:
    if not isinstance(url, str) or not url:
        raise _Skip("url is missing")
    if _PLACEHOLDER.search(url):
        raise _Skip("url contains a variable placeholder (not supported)")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise _Skip("url is not an allowed https url")
    if parts.query or parts.fragment:
        raise _Skip("url has a query string or a fragment")
    if "{" in parts.netloc or "}" in parts.netloc or "{" in parts.scheme:
        raise _Skip("url has a parameter outside the path")
    names = _PATH_PARAM.findall(parts.path)
    if "{" in _PATH_PARAM.sub("", parts.path) or "}" in _PATH_PARAM.sub("", parts.path):
        raise _Skip("url has a malformed path parameter")
    for n in names:
        if not _IDENT.match(n):
            raise _Skip("url has a malformed path parameter")
        if n not in props:
            raise _Skip("a path parameter is not an input property")
    if len(set(names)) != len(names):
        raise _Skip("url repeats a path parameter")
    probe = _PATH_PARAM.sub("x", url)
    try:
        host, port, _ = _net.validate_url(probe)
    except _net.TransportError:
        raise _Skip("url is not an allowed https url")
    if (host, port) not in server.approved:
        raise _Skip("url is outside the approved origins")
    return url, tuple(names)


def _check_headers(headers: Any) -> Dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict) or len(headers) > MAX_STATIC_HEADERS:
        raise _Skip("headers are not usable")
    out: Dict[str, str] = {}
    for k, v in headers.items():
        if not isinstance(k, str) or not isinstance(v, str) or not _HEADER_NAME.match(k) \
                or k.lower() in _FORBIDDEN_HEADERS or len(v) > MAX_HEADER_VALUE_CHARS or _has_control_chars(v):
            raise _Skip("headers are not usable")
        if _PLACEHOLDER.search(v):
            raise _Skip("a header contains a variable placeholder (not supported)")
        out[k] = v
    return out


def _check_tool(server: UtcpServerConfig, item: Dict[str, Any], cfg: McpConfig) -> UtcpToolSpec:
    template = item.get("tool_call_template")
    if not isinstance(template, dict):
        raise _Skip("tool_call_template is missing")
    if template.get("call_template_type") != "http":
        raise _Skip("call template type is not supported (only http)")
    if any(k not in _KNOWN_TEMPLATE_FIELDS for k in template):
        raise _Skip("the template has fields this version does not understand")
    method = template.get("http_method")
    if method not in HTTP_METHODS:
        raise _Skip("http_method is missing or not supported")
    if template.get("auth") is not None or template.get("auth_tools") is not None:
        raise _Skip("the template declares auth (not supported; the owner token is sent as a bearer token)")
    header_fields = template.get("header_fields")
    if header_fields not in (None, []):
        raise _Skip("header_fields is not supported")
    schema = item.get("inputs", {"type": "object", "properties": {}})
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise _Skip("inputs is not an object schema")
    if len(json.dumps(schema)) > cfg.max_schema_chars:
        raise _Skip("inputs schema is too large")
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        raise _Skip("inputs is not an object schema")
    url, path_params = _check_url(template.get("url"), server, props)
    body_field = template.get("body_field")
    if body_field is not None:
        if not isinstance(body_field, str) or body_field not in props or body_field in path_params:
            raise _Skip("body_field is not an input property")
        if method not in _BODY_METHODS:
            raise _Skip("body_field is not allowed with this http_method")
        ctype = template.get("content_type", "application/json")
        if not isinstance(ctype, str) or ctype.split(";")[0].strip().lower() != "application/json":
            raise _Skip("only application/json request bodies are supported")
    headers = _check_headers(template.get("headers"))
    description = item.get("description")
    description = description[: cfg.max_description_chars] if isinstance(description, str) else ""
    return UtcpToolSpec(server=server.name, name=item["name"], description=description, parameters=schema,
                        method=method, url=url, path_params=path_params, body_field=body_field, headers=headers)


def parse_manual(server: UtcpServerConfig, manual: Any, wanted: Sequence[str], cfg: McpConfig) -> UtcpDiscovery:
    """The allowlisted tools of one manual: the ones that can be offered, and
    the skipped ones with a constant reason. Raises UtcpDiscoveryError if the
    document is not a UTCP manual (an OpenAPI document is not accepted)."""
    if not isinstance(manual, dict) or not isinstance(manual.get("utcp_version"), str) \
            or not isinstance(manual.get("tools"), list):
        raise UtcpDiscoveryError(f"{server.name}: the document is not a UTCP manual")
    found: Dict[str, Dict[str, Any]] = {}
    for item in manual["tools"]:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"] in wanted \
                and item["name"] not in found:
            found[item["name"]] = item
    out = UtcpDiscovery()
    for tool in wanted:
        key = f"{server.name}.{tool}"
        item = found.get(tool)
        if item is None:
            out.skipped.append((key, "not offered by the manual"))
            continue
        try:
            out.tools.append(_check_tool(server, item, cfg))
        except _Skip as s:
            out.skipped.append((key, str(s)))
    return out


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------

def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _Failure("bad_arguments")
        return repr(value)
    if isinstance(value, str):
        return value
    raise _Failure("bad_arguments")


class UtcpClient:
    """transport(method, url, headers, body_or_None) -> HttpResponse is the one
    network seam; the default is the pinned, bounded _net.https_request. Tests
    inject a fake. The manual is read in discover() and never again: calls use
    that snapshot. Not safe to share between threads while a call is running."""

    def __init__(self, config: UtcpConfig, transport: Optional[Transport] = None):
        self.config = config
        self._servers = {s.name: s for s in config.servers}
        self._allowed = set(config.allowed_tools)
        self._specs: Dict[str, UtcpToolSpec] = {}
        self._transport = transport or self._default_transport

    def _default_transport(self, method, url, headers, body=None):
        c = self.config
        return _net.https_request(method, url, headers, body, total_timeout=c.total_timeout,
                                  op_timeout=c.op_timeout, max_bytes=c.max_response_bytes)

    def _emit(self, event: Dict[str, Any]) -> None:
        hook = self.config.on_event
        if hook is None:
            return
        event = dict(event)
        event["protocol"] = "utcp"
        try:
            hook(event)
        except Exception:
            logger.exception("on_event hook failed")

    # -- discovery -----------------------------------------------------------

    def discover(self) -> UtcpDiscovery:
        """Reads each manual once; keeps the allowlisted tools. Raises
        UtcpDiscoveryError if a manual cannot be read."""
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

    def _discover(self) -> UtcpDiscovery:
        out = UtcpDiscovery()
        cfg = self.config
        wanted: Dict[str, List[str]] = {}
        for entry in cfg.allowed_tools:
            server, tool = entry.split(".", 1)
            wanted.setdefault(server, []).append(tool)
        self._specs = {}
        for server, tools in wanted.items():
            sc = self._servers[server]
            headers = {"Accept": "application/json"}
            if sc.token:
                headers["Authorization"] = "Bearer " + sc.token
            try:
                resp = self._transport("GET", sc.url, headers, None)
            except _net.TransportError as e:
                raise UtcpDiscoveryError(f"{server}: UTCP server request failed ({e.message})") from None
            if resp.status != 200:
                raise UtcpDiscoveryError(f"{server}: {_Failure('http_status', str(resp.status)).message}")
            try:
                manual = json.loads(resp.body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise UtcpDiscoveryError(f"{server}: {_FAILURES['bad_response']}") from None
            found = parse_manual(sc, manual, tools, cfg)
            out.tools.extend(found.tools)
            out.skipped.extend(found.skipped)
        for spec in out.tools:
            self._specs[f"{spec.server}.{spec.name}"] = spec
        for key, reason in out.skipped:
            logger.warning("UTCP tool %s skipped: %s", key, reason)
        return out

    # -- calling -------------------------------------------------------------

    def call_tool(self, server: str, tool: str, arguments: Optional[Dict[str, Any]] = None,
                  requires_approval: Optional[bool] = None) -> ToolResult:
        """Call one allowlisted tool from the snapshot. Never raises.
        requires_approval says whether policy.approve must agree first (None
        means unknown, which is treated as True)."""
        started = time.monotonic()
        info: Dict[str, Any] = {}
        result = self._call_tool(server, tool, arguments, requires_approval, info)
        self._emit({"event": "call", "server": server, "tool": tool, "ok": result.success,
                    "error": None if result.success else result.error,
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
        if policy is None or policy.approve is None:
            info["approval"] = "not_enforced"
            return
        try:
            answer = policy.approve(server, tool, json.loads(json.dumps(arguments)))
        except Exception:
            logger.exception("approve hook failed for %s.%s", server, tool)
            answer = False
        if answer is not True:
            info["approval"] = "denied"
            raise _Failure("not_approved")
        info["approval"] = "approved"

    def _build(self, spec: UtcpToolSpec, arguments: Dict[str, Any]) -> Tuple[str, str, Dict[str, str], Optional[bytes]]:
        sc = self._servers[spec.server]

        def fill(match: "re.Match[str]") -> str:
            text = _scalar(arguments.get(match.group(1)))  # None, lists and objects are refused here
            if text in ("", ".", ".."):
                raise _Failure("bad_arguments")
            return quote(text, safe="")

        url = _PATH_PARAM.sub(fill, spec.url)
        body: Optional[bytes] = None
        headers = {"Accept": "application/json"}
        headers.update(spec.headers)
        if spec.body_field is not None and arguments.get(spec.body_field) is not None:
            body = json.dumps(arguments[spec.body_field]).encode("utf-8")
            headers["Content-Type"] = "application/json"
        pairs: List[Tuple[str, str]] = []
        for key, value in arguments.items():
            if key in spec.path_params or key == spec.body_field or value is None:
                continue
            if not isinstance(key, str) or isinstance(value, (dict, list)):
                raise _Failure("bad_arguments")
            pairs.append((key, _scalar(value)))
        if pairs:
            url += "?" + urlencode(pairs, quote_via=quote, safe="")
        try:
            host, port, _ = _net.validate_url(url)
        except _net.TransportError:
            raise _Failure("bad_arguments") from None
        if (host, port) not in sc.approved:
            raise _Failure("origin")
        if sc.token:
            headers["Authorization"] = "Bearer " + sc.token
        return spec.method, url, headers, body

    def _call_tool(self, server: str, tool: str, arguments: Optional[Dict[str, Any]],
                   requires_approval: Optional[bool], info: Dict[str, Any]) -> ToolResult:
        try:
            key = f"{server}.{tool}"
            spec = self._specs.get(key)
            if key not in self._allowed or spec is None:
                raise _Failure("not_allowlisted")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise _Failure("bad_arguments")
            try:
                json.dumps(arguments)
            except (TypeError, ValueError):
                raise _Failure("bad_arguments")
            method, url, headers, body = self._build(spec, arguments)
            self._check_approval(server, tool, arguments, requires_approval, info)
            resp = self._transport(method, url, headers, body)
            if not 200 <= resp.status < 300:
                raise _Failure("http_status", str(resp.status))
            text = resp.body.decode("utf-8", errors="replace")
            cap = self.config.max_result_chars
            if len(text) > cap:
                text = text[:cap] + "\n[truncated]"
            return ToolResult(success=True, result={"text": text})
        except _Failure as f:
            return ToolResult(success=False, error=f.message)
        except _net.TransportError as e:
            return ToolResult(success=False, error=f"UTCP server request failed ({e.message})")
        except Exception:
            logger.exception("UTCP call %s.%s failed unexpectedly", server, tool)
            return ToolResult(success=False, error="UTCP call failed")


# --------------------------------------------------------------------------
# Tools and connect_utcp
# --------------------------------------------------------------------------

def _make_handler(client: UtcpClient, spec: UtcpToolSpec, requires_approval: bool) -> Callable[..., ToolResult]:
    # A closure, not a default argument: the model controls the keyword names
    # in **arguments, so no parameter name of ours may be shadowable by one.
    def handler(**arguments: Any) -> ToolResult:
        return client.call_tool(spec.server, spec.name, arguments, requires_approval)

    return handler


def make_utcp_tools(config: UtcpConfig, client: UtcpClient, specs: List[UtcpToolSpec]) -> List[Tool]:
    """One ragleap_tools.Tool per discovered tool. Every tool is flagged
    requires_approval in Tool.metadata['utcp'] unless the owner pre-approved it
    (a manual carries no hints that this package trusts)."""
    tools: List[Tool] = []
    names = set()
    for spec in specs:
        name = _tool_name(spec.server, spec.name)
        if name in names:
            raise UtcpConfigError(f"two tools would be exposed as {name!r}")
        names.add(name)
        flag = _requires_approval(f"{spec.server}.{spec.name}", {}, False, config.policy)
        meta = {"utcp": {"server": spec.server, "tool": spec.name, "method": spec.method,
                         "requires_approval": flag}}
        tools.append(Tool(name=name, description=spec.description, parameters=spec.parameters,
                          handler=_make_handler(client, spec, flag), metadata=meta))
    return tools


def connect_utcp(url: str, *, allow: Sequence[str], name: str = "utcp", token: Optional[str] = None,
                 origins: Sequence[str] = (),
                 approve: Optional[Callable[[str, str, Dict[str, Any]], bool]] = None,
                 pre_approved: Sequence[str] = (),
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                 transport: Optional[Transport] = None, **limits: Any) -> Connection:
    """Read one UTCP manual and return its allowlisted http tools. allow is a
    non-empty list of exact tool names (no wildcard). name prefixes the exposed
    tool names (name__tool). origins lists extra https origins the tools may
    call (the manual's own origin is always approved). approve and
    pre_approved build an McpPolicy; limits go to the config. Raises
    McpConfigError (UtcpConfigError) or McpDiscoveryError (UtcpDiscoveryError):
    setup problems."""
    if isinstance(allow, str) or not isinstance(allow, (list, tuple)) or not allow \
            or not all(isinstance(t, str) and t and len(t) <= MAX_NAME_CHARS and not _has_control_chars(t) for t in allow):
        raise UtcpConfigError("allow must be a non-empty list of tool names")
    if isinstance(pre_approved, str) or not all(isinstance(t, str) for t in pre_approved):
        raise UtcpConfigError("pre_approved must be a list of tool names")
    server = UtcpServerConfig(name=name, url=url, token=token, origins=origins)
    policy = None
    if approve is not None or pre_approved:
        try:
            policy = McpPolicy(approve=approve, pre_approved=["%s.%s" % (name, t) for t in pre_approved])
        except ValueError as exc:
            raise UtcpConfigError(str(exc)) from None
    config = UtcpConfig(servers=[server], allowed_tools=["%s.%s" % (name, t) for t in allow],
                        policy=policy, on_event=on_event, **limits)
    client = UtcpClient(config, transport=transport)
    discovery = client.discover()
    tools = make_utcp_tools(config, client, discovery.tools)
    return Connection(config=config, client=client, tools=tools, skipped=list(discovery.skipped))
