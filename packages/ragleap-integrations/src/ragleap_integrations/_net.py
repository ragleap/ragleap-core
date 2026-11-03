"""
ragleap_integrations._net

A bounded HTTPS request - POST for MCP servers, GET/POST/PUT/DELETE/PATCH for UTCP -
standard library only.

Every property below is enforced in code, not just documented:
- https only, no credentials in the URL, no IP-literal hosts.
- DNS is resolved ONCE; every returned address must be globally routable
  (IPv4-mapped IPv6 addresses are unwrapped first); the TCP connection is
  then made to that exact IP and TLS (minimum 1.2) is verified against the
  hostname, so DNS rebinding cannot swap in an internal address between the
  check and the connection.
- Redirects are never followed (a 3xx status is returned to the caller).
- Accept-Encoding: identity, so there is no compressed-response expansion.
- The response is read in pieces and capped at max_bytes.
- A wall-clock deadline covers the whole exchange (TLS handshake, headers
  and body). A watchdog timer shuts the socket down at the deadline and
  sets a flag, because after a shutdown the read looks like a normal end
  of response - the flag is what turns it into a deadline error. A check
  between chunks alone is not enough: a server that sends one byte just
  inside the per-operation timeout would otherwise hold the call open.
- Failures raise TransportError carrying a short code; the wording comes
  from a constant table, so no library exception text reaches a caller.

See docs/design/mcp-client.md.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

MAX_URL_CHARS = 2048
ALLOWED_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")
DEFAULT_TOTAL_TIMEOUT = 30.0
DEFAULT_OP_TIMEOUT = 10.0
DEFAULT_MAX_BYTES = 1024 * 1024
READ_CHUNK = 8192
USER_AGENT = "ragleap-integrations/0.1"

_REASONS = {
    "url_length": "bad url length",
    "url_chars": "invalid characters in url",
    "scheme": "https only",
    "creds": "credentials in the url are not allowed",
    "port": "bad port",
    "host": "bad host",
    "ip_literal": "ip addresses are not allowed as hosts",
    "no_resolve": "host did not resolve",
    "non_public": "host resolves to a non-public address",
    "connect": "could not connect",
    "tls": "tls failure",
    "deadline": "response too slow",
    "too_large": "response too large; raise max_response_bytes",
    "io": "network error",
    "method": "http method not allowed",
}


class TransportError(Exception):
    """A refused or failed request. .code is a key of the constant table."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    @property
    def message(self) -> str:
        return _REASONS.get(self.code, "request failed")


@dataclass
class HttpResponse:
    status: int
    headers: Dict[str, str]  # lower-cased names
    body: bytes
    content_type: str  # media type only, lower-cased


def validate_url(url: str) -> Tuple[str, int, str]:
    """Returns (host, port, path_with_query) or raises TransportError."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise TransportError("url_length")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise TransportError("url_chars")
    p = urlsplit(url)
    if p.scheme != "https":
        raise TransportError("scheme")
    if p.username or p.password or "@" in p.netloc:
        raise TransportError("creds")
    try:
        port = p.port
    except ValueError:
        raise TransportError("port")
    if port is None:
        port = 443
    if not 1 <= port <= 65535:
        raise TransportError("port")
    host = (p.hostname or "").lower().rstrip(".")
    if not host or not host.isascii():
        raise TransportError("host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise TransportError("ip_literal")
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    return host, port, path


def resolve_public(host: str, port: int, resolver: Optional[Callable] = None) -> str:
    """Resolve once; every address must be public. Returns the IP to pin."""
    resolver = resolver or socket.getaddrinfo
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise TransportError("no_resolve")
    ips = []
    for info in infos:
        ip = str(info[4][0]).split("%")[0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise TransportError("non_public")
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            addr = mapped
        if not addr.is_global or addr.is_multicast:
            raise TransportError("non_public")
        ips.append(ip)
    if not ips:
        raise TransportError("no_resolve")
    return ips[0]


def _default_connect(ip: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((ip, port), timeout=timeout)


def _default_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def https_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    *,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    op_timeout: float = DEFAULT_OP_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    stop: Optional[Callable[[bytes], bool]] = None,
    resolver: Optional[Callable] = None,
    connect: Optional[Callable[[str, int, float], socket.socket]] = None,
    ssl_context: Optional[ssl.SSLContext] = None,
) -> HttpResponse:
    """One request (method is one of ALLOWED_METHODS) to the pinned IP. Raises TransportError; never follows
    redirects. stop(buffer_so_far) -> True ends the read early (used to
    stop at the final response of an event stream). resolver, connect and
    ssl_context exist so tests can run without real DNS or certificates;
    production callers leave them alone."""
    if method not in ALLOWED_METHODS:
        raise TransportError("method")
    host, port, path = validate_url(url)
    ip = resolve_public(host, port, resolver)

    start = time.monotonic()
    deadline_hit = threading.Event()
    holder: Dict[str, socket.socket] = {}

    def _kill() -> None:
        deadline_hit.set()
        sock = holder.get("sock")
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(total_timeout, _kill)
    timer.daemon = True
    timer.start()
    conn: Optional[http.client.HTTPSConnection] = None
    raw: Optional[socket.socket] = None
    phase = "connect"
    try:
        remaining = max(0.1, total_timeout - (time.monotonic() - start))
        raw = (connect or _default_connect)(ip, port, min(op_timeout, remaining))
        phase = "tls"
        ctx = ssl_context or _default_context()
        tls = ctx.wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
        raw = None  # ownership moved to tls
        holder["sock"] = tls
        tls.settimeout(op_timeout)
        if deadline_hit.is_set():
            raise TransportError("deadline")
        tls.do_handshake()

        phase = "io"
        conn = http.client.HTTPSConnection(host, port, timeout=op_timeout)
        conn.sock = tls
        send_headers = {
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": USER_AGENT,
        }
        send_headers.update(headers)
        conn.request(method, path, body=body, headers=send_headers)
        resp = conn.getresponse()
        status = resp.status
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        ctype = resp_headers.get("content-type", "").split(";")[0].strip().lower()

        buf = bytearray()
        stopped = False
        while True:
            chunk = resp.read1(READ_CHUNK)
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_bytes:
                raise TransportError("too_large")
            if deadline_hit.is_set() or time.monotonic() - start > total_timeout:
                raise TransportError("deadline")
            if stop is not None and stop(bytes(buf)):
                stopped = True
                break
        if deadline_hit.is_set():
            raise TransportError("deadline")
        if not stopped and resp.length:
            # Fewer bytes than Content-Length promised: a cut-off body must
            # not look like a complete one.
            raise TransportError("io")
        return HttpResponse(status=status, headers=resp_headers, body=bytes(buf), content_type=ctype)
    except TransportError:
        raise
    except Exception as exc:
        if deadline_hit.is_set():
            raise TransportError("deadline")
        if isinstance(exc, ssl.SSLError):
            raise TransportError("tls")
        if phase == "connect":
            raise TransportError("connect")
        if phase == "tls":
            raise TransportError("tls")
        raise TransportError("io")
    finally:
        timer.cancel()
        if conn is not None:
            conn.close()
        elif holder.get("sock") is not None:
            try:
                holder["sock"].close()
            except OSError:
                pass
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass


def https_post(url: str, headers: Dict[str, str], body: bytes, **kwargs) -> HttpResponse:
    """One POST (what the MCP client sends); see https_request for the options."""
    return https_request("POST", url, headers, body, **kwargs)
