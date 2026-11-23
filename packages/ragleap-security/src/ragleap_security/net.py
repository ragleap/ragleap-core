"""ragleap_security.net - outbound HTTP that an attacker cannot point at your own network (design: issue #1506).

    fetcher = SafeFetcher(lambda scope: FetchPolicy(allow_hosts=("api.example.com",)), audit=log)
    res = fetcher.get(Scope("acme"), "https://api.example.com/v1/items")

What it does, in this order, on every request and on every redirect hop:
 1. strict URL parsing (https only unless the policy says http; no userinfo, no number-like hosts, ASCII only);
 2. a host allowlist that the TENANT's policy provides (deny by default);
 3. one DNS lookup, and EVERY returned address must pass the address rule (global addresses only, after unwrapping
    an embedded IPv4; multicast and the special ranges are blocked);
 4. the connection goes to the validated IP, never to the name again (so DNS rebinding between check and connect
    does not work), with the original name as TLS server name and Host header;
 5. redirects are followed by this module, never by the HTTP library, and every hop starts again at 1;
 6. one deadline for connect, TLS and reading together, a body cap, no content encoding, no proxy or .netrc from
    the environment, TLS verification always on.

What it does NOT do (see the design issue for the full list): it cannot tell a trusted allowlisted host from a
compromised one, it cannot detect a DNS answer that is consistently wrong (an allowed name pointed at an attacker's
public server), it does not protect other egress, and the response is untrusted data. Standard library only.
Not independently audited.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from ragleap_security.audit import AuditLog
from ragleap_security.limits import Deadline
from ragleap_security.ratelimit import RateLimitError
from ragleap_security.scope import Scope, require_scope

MAX_URL_CHARS = 2048
MAX_BODY_CEILING = 16 * 1024 * 1024
MAX_REDIRECT_CEILING = 5
MAX_ADDRESSES = 16
MAX_TRIES = 4
MAX_HEADERS_FROM_CALLER = 20
USER_AGENT = "ragleap-security-net/0.3.0"

REASONS = frozenset({
    "bad_url", "scheme_not_allowed", "userinfo", "ip_literal", "bad_host", "host_not_allowed", "port_not_allowed",
    "no_policy", "bad_policy", "bad_header", "dns_failed", "address_blocked", "connect_failed", "tls_failed", "timeout",
    "bad_response", "content_encoding", "response_too_large", "too_many_redirects", "bad_redirect",
})

_HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\Z")
_PATTERN = re.compile(r"(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\Z")
_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/?]*)(.*)\Z")
_HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}\Z")
_FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection", "accept-encoding", "upgrade",
                                "te", "trailer", "expect", "proxy-authorization", "proxy-connection"})
_REDIRECTS = (301, 302, 303, 307, 308)

_V4_BLOCK = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4")]
_V6_BLOCK = [ipaddress.ip_network(n) for n in (
    "::/96", "64:ff9b:1::/48", "100::/64", "2001::/32", "2001:db8::/32", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8")]
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


class NetError(Exception):
    """A request was refused or failed. str(error) and error.reason are one fixed code from REASONS: never a URL,
    a header, a body or the text of another exception."""

    def __init__(self, reason: str, host: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.host = host  # the validated host name, when it is known (for the audit record only)


@dataclass(frozen=True)
class FetchPolicy:
    """What one tenant may fetch. allow_hosts: exact names or '*.suffix' (a whole-label suffix), at least two labels,
    required. Everything else is a limit with a hard ceiling."""

    allow_hosts: Tuple[str, ...]
    allow_ports: Tuple[int, ...] = (443,)
    allow_http: bool = False
    max_bytes: int = 1024 * 1024
    timeout: float = 10.0
    max_redirects: int = 3
    max_header_bytes: int = 16 * 1024

    def __post_init__(self) -> None:
        hosts = self.allow_hosts
        if isinstance(hosts, str) or not isinstance(hosts, (tuple, list)) or not hosts:
            raise ValueError("allow_hosts must be a non-empty tuple of host names")
        clean = []
        for h in hosts:
            if not isinstance(h, str) or not _PATTERN.match(h.lower()):
                raise ValueError("allow_hosts entries are names like 'api.example.com' or '*.example.com'")
            last = h.lower().rsplit(".", 1)[-1]
            if last.isdigit() or last.startswith("0x"):
                raise ValueError("allow_hosts takes host names, not IP addresses")
            clean.append(h.lower())
        object.__setattr__(self, "allow_hosts", tuple(clean))
        ports = self.allow_ports
        if isinstance(ports, (str, int)) or not isinstance(ports, (tuple, list)) or not ports or not all(
                isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535 for p in ports):
            raise ValueError("allow_ports must be a non-empty tuple of ports 1 to 65535")
        object.__setattr__(self, "allow_ports", tuple(ports))
        if not isinstance(self.allow_http, bool):
            raise ValueError("allow_http must be True or False")
        for name, lo, hi in (("max_bytes", 1, MAX_BODY_CEILING), ("max_redirects", 0, MAX_REDIRECT_CEILING),
                             ("max_header_bytes", 1024, 64 * 1024)):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError("%s must be an integer from %d to %d" % (name, lo, hi))
        t = self.timeout
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 120:
            raise ValueError("timeout must be a number of seconds above 0 and at most 120")


@dataclass(frozen=True)
class FetchResult:
    status: int
    headers: Dict[str, str]  # lower-case names; repeated headers are joined with ", "
    body: bytes
    host: str  # the host of the last hop
    port: int
    redirects: int


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    target: str  # path and query, starting with /


def _v4_ok(ip: "ipaddress.IPv4Address") -> bool:
    return ip.is_global and not ip.is_multicast and not any(ip in n for n in _V4_BLOCK)


def address_allowed(text: str) -> bool:
    """True only for an address a public server could have. An embedded IPv4 (IPv4-mapped, NAT64 64:ff9b::/96, 6to4
    2002::/16) is judged as that IPv4; the Python ipaddress flags for mapped addresses differ between versions, so they
    are not used on those. Everything else must be global, not multicast and outside the extra blocklist."""
    if not isinstance(text, str) or "%" in text:
        return False
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if ip.version == 4:
        return _v4_ok(ip)
    mapped = ip.ipv4_mapped
    if mapped is not None:
        return _v4_ok(mapped)
    n = int(ip)
    if ip in _NAT64:
        return _v4_ok(ipaddress.IPv4Address(n & 0xFFFFFFFF))
    if ip in _SIX_TO_FOUR:
        return _v4_ok(ipaddress.IPv4Address((n >> 80) & 0xFFFFFFFF))
    if any(ip in net for net in _V6_BLOCK):
        return False
    return ip.is_global and not ip.is_multicast


def _host_allowed(host: str, patterns: Tuple[str, ...]) -> bool:
    for p in patterns:
        if p.startswith("*."):
            suffix = p[1:]
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif host == p:
            return True
    return False


def _parse(url: Any, policy: FetchPolicy) -> _Target:
    if not isinstance(url, str) or not 1 <= len(url) <= MAX_URL_CHARS:
        raise NetError("bad_url")
    if any(not 0x21 <= ord(c) <= 0x7E for c in url) or "\\" in url or "#" in url:
        raise NetError("bad_url")  # spaces, control characters, non-ASCII, backslashes and fragments
    m = _URL.match(url)
    if not m:
        raise NetError("bad_url")
    scheme = m.group(1).lower()
    if scheme not in ("https", "http") or (scheme == "http" and not policy.allow_http):
        raise NetError("scheme_not_allowed")
    authority, rest = m.group(2), m.group(3)
    if "@" in authority:
        raise NetError("userinfo")
    if authority.startswith("["):
        raise NetError("ip_literal")
    host, sep, port_s = authority.partition(":")
    if ":" in port_s:
        raise NetError("bad_url")
    if sep:
        if not port_s.isdigit() or len(port_s) > 5 or not 1 <= int(port_s) <= 65535:
            raise NetError("bad_url")
        port = int(port_s)
    else:
        port = 443 if scheme == "https" else 80
    host = host.lower()
    if not host or len(host) > 253 or not _HOST.match(host):
        raise NetError("bad_host")
    last = host.rsplit(".", 1)[-1]
    if last.isdigit() or last.startswith("0x"):
        raise NetError("ip_literal")  # 2130706433, 0x7f.1, 127.1: the system resolver accepts these as addresses
    if not _host_allowed(host, policy.allow_hosts):
        raise NetError("host_not_allowed", host)
    if port not in policy.allow_ports:
        raise NetError("port_not_allowed", host)
    target = rest or "/"
    if target[0] == "?":
        target = "/" + target
    if target[0] != "/":
        raise NetError("bad_url", host)
    return _Target(scheme, host, port, target)


def _check_headers(headers: Any) -> Dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict) or len(headers) > MAX_HEADERS_FROM_CALLER:
        raise NetError("bad_header")
    out: Dict[str, str] = {}
    for k, v in headers.items():
        if (not isinstance(k, str) or not _HEADER_NAME.match(k) or k.lower() in _FORBIDDEN_HEADERS
                or not isinstance(v, str) or len(v) > 1024 or any(not 0x20 <= ord(c) <= 0x7E for c in v)):
            raise NetError("bad_header")
        out[k] = v
    return out


def _default_resolver(host: str, port: int) -> List[str]:
    return [i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _default_connector(ip: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((ip, port), timeout=timeout)


class SafeFetcher:
    """policy_for(scope) returns the FetchPolicy of that tenant (or None for 'no fetching'). audit: an AuditLog.
    rate_limiter: anything with check(scope) -> Decision, asked once per call before any lookup. The underscore
    arguments are a test seam (a resolver, a connector and an address rule): they exist so that tests can talk to a
    local server, which the real address rule forbids. Do not use them in production."""

    def __init__(
        self,
        policy_for: Callable[[Scope], Optional[FetchPolicy]],
        *,
        audit: Optional[AuditLog] = None,
        rate_limiter: Optional[Any] = None,
        clock: Callable[[], float] = time.monotonic,
        _resolver: Optional[Callable[[str, int], List[str]]] = None,
        _connector: Optional[Callable[[str, int, float], socket.socket]] = None,
        _address_rule: Optional[Callable[[str], bool]] = None,
    ) -> None:
        if not callable(policy_for):
            raise ValueError("policy_for must be callable")
        if audit is not None and not isinstance(audit, AuditLog):
            raise ValueError("audit must be a ragleap_security AuditLog")
        if rate_limiter is not None and not callable(getattr(rate_limiter, "check", None)):
            raise ValueError("rate_limiter must have a check(scope) method")
        if not callable(clock):
            raise ValueError("clock must be callable")
        for fn in (_resolver, _connector, _address_rule):
            if fn is not None and not callable(fn):
                raise ValueError("the test seam arguments must be callable")
        self._policy_for = policy_for
        self._audit = audit
        self._limiter = rate_limiter
        self._clock = clock
        self._resolver = _resolver or _default_resolver
        self._connector = _connector or _default_connector
        self._address_rule = _address_rule or address_allowed
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self._ctx = ctx

    # ---- public ----

    def get(self, scope: Scope, url: str, headers: Optional[Dict[str, str]] = None) -> FetchResult:
        return self._fetch("GET", scope, url, headers)

    def head(self, scope: Scope, url: str, headers: Optional[Dict[str, str]] = None) -> FetchResult:
        return self._fetch("HEAD", scope, url, headers)

    # ---- one request ----

    def _fetch(self, method: str, scope: Scope, url: str, headers: Optional[Dict[str, str]]) -> FetchResult:
        scope = require_scope(scope)
        self._limit(scope)
        hops, host, port = 0, None, 0
        try:
            policy = self._policy(scope)
            caller_headers = _check_headers(headers)
            deadline = Deadline(policy.timeout, self._clock)
            current, first, left_first_origin = url, None, False
            while True:
                target = _parse(current, policy)
                host, port = target.host, target.port
                origin = (target.scheme, target.host, target.port)
                if first is None:
                    first = origin
                elif origin != first:
                    left_first_origin = True  # caller headers go to the first origin only, and never again after leaving it
                if hops:
                    self._record(scope, "net.redirect", host=host, port=port, hops=hops)
                status, resp_headers, body = self._hop(method, target, {} if left_first_origin else caller_headers,
                                                       policy, deadline)
                if status in _REDIRECTS and resp_headers.get("location"):
                    if hops >= policy.max_redirects:
                        raise NetError("too_many_redirects", host)
                    current = self._next_url(current, resp_headers["location"], host)
                    hops += 1
                    continue
                result = FetchResult(status, resp_headers, body, host, port, hops)
                self._record(scope, "net.request", host=host, port=port, status=status, bytes=len(body), hops=hops)
                return result
        except NetError as e:
            self._record(scope, "net.blocked", reason=e.reason, host=e.host or host or "", hops=hops)
            raise

    @staticmethod
    def _next_url(current: str, location: str, host: str) -> str:
        if not isinstance(location, str) or not 1 <= len(location) <= MAX_URL_CHARS or any(
                not 0x21 <= ord(c) <= 0x7E for c in location) or "\\" in location:
            raise NetError("bad_redirect", host)
        return urljoin(current, location)

    def _hop(self, method: str, t: _Target, headers: Dict[str, str], policy: FetchPolicy,
             deadline: Deadline) -> Tuple[int, Dict[str, str], bytes]:
        if deadline.expired():
            raise NetError("timeout", t.host)
        try:
            found = list(dict.fromkeys(str(a) for a in self._resolver(t.host, t.port)))
        except (OSError, UnicodeError, ValueError):
            raise NetError("dns_failed", t.host) from None
        if not found or len(found) > MAX_ADDRESSES:
            raise NetError("dns_failed", t.host)
        if not all(self._address_rule(a) for a in found):  # EVERY address must pass: a mixed answer is refused
            raise NetError("address_blocked", t.host)
        sock: Optional[socket.socket] = None
        last: Optional[BaseException] = None
        for ip in found[:MAX_TRIES]:
            if deadline.expired():
                raise NetError("timeout", t.host)
            try:
                sock = self._connector(ip, t.port, max(0.001, deadline.remaining()))
                break
            except OSError as e:
                last = e
        if sock is None:
            raise NetError("timeout" if isinstance(last, TimeoutError) else "connect_failed", t.host)
        conn = http.client.HTTPConnection(t.host, t.port)
        resp = None
        try:
            if t.scheme == "https":
                sock.settimeout(max(0.001, deadline.remaining()))
                try:
                    sock = self._ctx.wrap_socket(sock, server_hostname=t.host)
                except TimeoutError:
                    raise NetError("timeout", t.host) from None
                except (ssl.SSLError, OSError):
                    raise NetError("tls_failed", t.host) from None
            conn.sock = sock
            sock.settimeout(max(0.001, deadline.remaining()))
            default = 443 if t.scheme == "https" else 80
            conn.putrequest(method, t.target, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", t.host if t.port == default else "%s:%d" % (t.host, t.port))
            conn.putheader("Accept-Encoding", "identity")
            conn.putheader("User-Agent", USER_AGENT)
            conn.putheader("Connection", "close")
            for k, v in headers.items():
                conn.putheader(k, v)
            conn.endheaders()
            resp = conn.getresponse()
            return self._read(resp, sock, method, t, policy, deadline)
        except NetError:
            raise
        except TimeoutError:
            raise NetError("timeout", t.host) from None
        except (http.client.HTTPException, ValueError, OSError):
            raise NetError("bad_response", t.host) from None
        finally:
            if resp is not None:
                resp.close()
            conn.close()

    def _read(self, resp: http.client.HTTPResponse, sock: socket.socket, method: str, t: _Target, policy: FetchPolicy,
              deadline: Deadline) -> Tuple[int, Dict[str, str], bytes]:
        pairs = resp.getheaders()
        if sum(len(k) + len(v) + 4 for k, v in pairs) > policy.max_header_bytes:
            raise NetError("bad_response", t.host)
        hdrs: Dict[str, str] = {}
        for k, v in pairs:
            k = k.lower()
            hdrs[k] = v if k not in hdrs else hdrs[k] + ", " + v
        enc = hdrs.get("content-encoding")
        if enc is not None and enc.strip().lower() not in ("", "identity"):
            raise NetError("content_encoding", t.host)
        status = resp.status
        if status in _REDIRECTS or method == "HEAD":
            return status, hdrs, b""
        length = hdrs.get("content-length", "")
        if length.isdigit() and int(length) > policy.max_bytes:
            raise NetError("response_too_large", t.host)
        chunks: List[bytes] = []
        total = 0
        while not resp.isclosed():  # the response closes itself once it has read its whole declared length
            remaining = deadline.remaining()
            if remaining <= 0:
                raise NetError("timeout", t.host)
            sock.settimeout(remaining)
            chunk = resp.read1(min(65536, policy.max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > policy.max_bytes:
                raise NetError("response_too_large", t.host)
            chunks.append(chunk)
        return status, hdrs, b"".join(chunks)

    # ---- helpers ----

    def _policy(self, scope: Scope) -> FetchPolicy:
        try:
            policy = self._policy_for(scope)
        except Exception:  # noqa: BLE001 - nothing of the exception reaches the caller or the audit log
            raise NetError("bad_policy") from None
        if policy is None:
            raise NetError("no_policy")
        if type(policy) is not FetchPolicy:
            raise NetError("bad_policy")
        return policy

    def _limit(self, scope: Scope) -> None:
        if self._limiter is None:
            return
        decision = self._limiter.check(scope)
        if not decision.allowed:
            raise RateLimitError("rate limit reached", decision.retry_after, decision.reason or "limited")

    def _record(self, scope: Scope, event: str, **data: Any) -> None:
        if self._audit is not None:
            self._audit.append(event, data, scope)
