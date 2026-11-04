"""A small MCP server (JSON-RPC over Streamable HTTP, JSON responses, stateless). Standard library only.

It exposes only what PhysicalTools allows: three read tools and one propose tool. Safe defaults:
loopback bind, Host and Origin checks (DNS rebinding), body size cap, optional bearer token, no logging of
request bodies. A non-loopback bind needs a token. It does not terminate TLS by default: RagLeap's MCP
client is HTTPS-only [verified: ragleap-core README], so use ``ssl_context`` or a TLS proxy in front.
Protocol details below were written from memory of the MCP spec [unverified]; the interop test with the
official SDK client is the check.
"""
from __future__ import annotations

import hmac
import json
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterable, Mapping, Optional

from .errors import ConfigError
from .tools import PhysicalTools

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26")
MAX_BODY = 64 * 1024
SERVER_NAME = "ragleap-physical"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _rpc_error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(tools: PhysicalTools, msg: Any) -> Optional[dict]:
    """Handle one JSON-RPC message. Returns the response, or None for notifications and replies."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "invalid request")
    method, msg_id = msg.get("method"), msg.get("id")
    if method is None:
        return None  # a response from the client; nothing to answer
    if "id" not in msg:
        return None  # notification, for example notifications/initialized
    if isinstance(msg_id, bool) or not isinstance(msg_id, (int, str)) or not isinstance(method, str):
        return _rpc_error(None, -32600, "invalid request")
    params = msg.get("params")
    params = params if isinstance(params, dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "0.0.3"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools.list_tools()}}
    if method == "tools/call":
        result = tools.call(params.get("name"), params.get("arguments"))
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": json.dumps(result, sort_keys=True, allow_nan=False)}],
            "isError": not result["ok"]}}
    return _rpc_error(msg_id, -32601, "method not found")


def make_server(tools: PhysicalTools, host: str = "127.0.0.1", port: int = 0, *, auth_token: Optional[str] = None,
                allowed_origins: Iterable[str] = (), ssl_context: Optional[ssl.SSLContext] = None) -> HTTPServer:
    """Build (not start) the HTTP server. Call ``serve_forever()`` and ``shutdown()`` on the result."""
    if host not in _LOOPBACK and not auth_token:
        raise ConfigError("a non-loopback bind needs an auth token")
    if auth_token is not None and len(auth_token) < 16:
        raise ConfigError("auth token is too short (use at least 16 characters)")
    origins = frozenset(allowed_origins)

    class Handler(BaseHTTPRequestHandler):
        timeout = 10  # a slow client cannot hold the server for long
        server_version = "ragleap-physical"
        sys_version = ""

        def log_message(self, *args: Any) -> None:  # never log bodies or headers
            pass

        def _drain(self) -> None:
            """Read and discard a small unread request body before a refusal, so the client gets the answer and not a connection reset
            (Windows clients report WinError 10053 when a server closes a socket that still holds unread data)."""
            if getattr(self, "_body_done", False):
                return
            self._body_done = True
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return
            if 0 < n <= MAX_BODY:
                try:
                    self.rfile.read(n)
                    self.server.drained_bytes = getattr(self.server, "drained_bytes", 0) + n
                except Exception:
                    pass

        def _send(self, status: int, body: Optional[Mapping[str, Any]] = None, extra: Optional[dict] = None) -> None:
            if status >= 400:
                self._drain()
            data = b"" if body is None else json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Connection", "close")
            if body is not None:
                self.send_header("Content-Type", "application/json")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self) -> bool:
            hostname = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            port = self.server.server_address[1]
            if host in _LOOPBACK:
                return (self.headers.get("Host") or "") in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
            return hostname == host

        def do_POST(self) -> None:
            if self.path != "/mcp":
                return self._send(404, _rpc_error(None, -32600, "not found"))
            if not self._host_ok():
                return self._send(403, _rpc_error(None, -32600, "host not allowed"))
            origin = self.headers.get("Origin")
            if origin is not None and origin not in origins:
                return self._send(403, _rpc_error(None, -32600, "origin not allowed"))
            if auth_token is not None:
                given = self.headers.get("Authorization", "")
                if not hmac.compare_digest(given.encode(), f"Bearer {auth_token}".encode()):
                    return self._send(401, _rpc_error(None, -32600, "unauthorized"), {"WWW-Authenticate": "Bearer"})
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0:
                return self._send(411, _rpc_error(None, -32600, "length required"))
            if length > MAX_BODY:
                return self._send(413, _rpc_error(None, -32600, "body too large"))
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                return self._send(415, _rpc_error(None, -32600, "content type must be application/json"))
            try:
                self._body_done = True
                msg = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError, RecursionError):  # deeply nested JSON raises RecursionError
                return self._send(400, _rpc_error(None, -32700, "parse error"))
            if isinstance(msg, list):
                return self._send(400, _rpc_error(None, -32600, "batches are not supported"))
            try:
                reply = handle_message(tools, msg)
            except Exception:  # never leak internals
                return self._send(500, _rpc_error(None, -32603, "internal error"))
            if reply is None:
                return self._send(202)
            self._send(200, reply)

        def _no(self) -> None:
            self._send(405, _rpc_error(None, -32600, "method not allowed"), {"Allow": "POST"})

        do_GET = do_PUT = do_DELETE = do_PATCH = _no

    server = HTTPServer((host, port), Handler)  # one request at a time: the bridge is not thread-safe
    if ssl_context is not None:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    return server
