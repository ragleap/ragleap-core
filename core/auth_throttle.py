"""Failed-login throttling for the API key.

After AUTH_MAX_FAILURES wrong keys from one client within AUTH_WINDOW_SECONDS the client is
refused (429 + Retry-After) for AUTH_LOCKOUT_SECONDS, even with the right key. State is in
memory and per process; a restart clears it. Requests with no key at all are not counted.

A client is the connecting address. X-Forwarded-For is believed only when the connecting
address is listed in TRUSTED_PROXIES (comma separated IPs or CIDRs); otherwise anyone could
dodge the limit by sending a different header on every request.
"""
import ipaddress
import os
import threading
import time
from collections import deque
from typing import Deque, Dict, List

MAX_TRACKED_CLIENTS = 10000
_clock = time.monotonic
_lock = threading.Lock()
_failures: Dict[str, Deque[float]] = {}
_locked_until: Dict[str, float] = {}
_rate: Dict[tuple, Deque[float]] = {}
_noted: Dict[tuple, float] = {}


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw.isdigit():
        return max(low, min(high, int(raw)))
    return default


def enabled() -> bool:
    return os.environ.get("AUTH_THROTTLE", "").strip().lower() != "off"


def max_failures() -> int:
    return _int_env("AUTH_MAX_FAILURES", 10, 1, 1000)


def window_seconds() -> int:
    return _int_env("AUTH_WINDOW_SECONDS", 300, 1, 86400)


def lockout_seconds() -> int:
    return _int_env("AUTH_LOCKOUT_SECONDS", 900, 1, 86400)


def _trusted_entries() -> List[str]:
    return [p.strip() for p in os.environ.get("TRUSTED_PROXIES", "").split(",") if p.strip()]


def _is_trusted(addr: str, entries: List[str]) -> bool:
    if not addr:
        return False
    if addr in entries:
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    for entry in entries:
        try:
            if "/" in entry:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            elif ip == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def _normalize(addr: str) -> str:
    try:
        return str(ipaddress.ip_address(addr))
    except ValueError:
        return addr[:64]


def client_id(peer, forwarded: str = "") -> str:
    """The address to rate-limit: the connecting peer, or the real client behind a trusted proxy."""
    peer = (peer or "").strip() or "unknown"
    entries = _trusted_entries()
    if not entries or not _is_trusted(peer, entries):
        return _normalize(peer)
    hops = [h.strip() for h in (forwarded or "").split(",") if h.strip()][-20:]
    for hop in reversed(hops):
        try:
            ip = ipaddress.ip_address(hop)
        except ValueError:
            return _normalize(peer)          # a garbage header: believe none of it
        if not _is_trusted(str(ip), entries):
            return str(ip)
    return _normalize(peer)


def blocked_seconds(client: str) -> int:
    """Seconds the client must still wait, or 0 if it is not locked out."""
    now = _clock()
    with _lock:
        until = _locked_until.get(client, 0.0)
        if until > now:
            return max(1, int(until - now + 0.999))
        _locked_until.pop(client, None)
    return 0


def _make_room(now: float) -> None:
    if len(_failures) + len(_locked_until) < MAX_TRACKED_CLIENTS:
        return
    window = window_seconds()
    for c in [c for c, u in _locked_until.items() if u <= now]:
        del _locked_until[c]
    for c in [c for c, q in _failures.items() if not q or now - q[-1] > window]:
        del _failures[c]
    while len(_failures) + len(_locked_until) >= MAX_TRACKED_CLIENTS and _failures:
        _failures.pop(next(iter(_failures)))


def record_failure(client: str) -> int:
    """Count a wrong key. Returns the lockout length in seconds if this failure triggered one, else 0."""
    now = _clock()
    window = window_seconds()
    with _lock:
        _make_room(now)
        q = _failures.setdefault(client, deque())
        while q and now - q[0] > window:
            q.popleft()
        q.append(now)
        if len(q) >= max_failures():
            secs = lockout_seconds()
            _locked_until[client] = now + secs
            _failures.pop(client, None)
            return secs
    return 0


def record_success(client: str) -> None:
    with _lock:
        _failures.pop(client, None)


def sso_limit() -> int:
    """Requests per client and minute allowed on each public sign-in address (AUTH_SSO_MAX_PER_MINUTE, 1 to 10000)."""
    return _int_env("AUTH_SSO_MAX_PER_MINUTE", 30, 1, 10000)


def _make_rate_room(now: float, window: int) -> None:
    if len(_rate) < MAX_TRACKED_CLIENTS:
        return
    for k in [k for k, q in _rate.items() if not q or now - q[-1] > window]:
        del _rate[k]
        _noted.pop(k, None)
    while len(_rate) >= MAX_TRACKED_CLIENTS and _rate:
        k = next(iter(_rate))
        del _rate[k]
        _noted.pop(k, None)


def _note(bucket: str, client: str, limit: int, wait: int) -> None:
    try:
        from core import audit
        audit.emit("sso_rate_limited", route=bucket, client=client, limit=limit, retry_after=wait)
    except Exception:
        pass


def rate_limited(bucket: str, client: str) -> int:
    """Count one request to a public sign-in address. Returns the seconds to wait when this client has used up its
    budget for the last minute, else 0. Refused requests are not counted, so hammering does not extend the wait.
    The first refusal per client and minute writes one sso_rate_limited audit log line."""
    now = _clock()
    limit = sso_limit()
    window = 60
    key = (bucket, client)
    note = None
    with _lock:
        _make_rate_room(now, window)
        q = _rate.setdefault(key, deque())
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            wait = max(1, int(q[0] + window - now + 0.999))
            if now - _noted.get(key, -1e9) > window:
                _noted[key] = now
                note = wait
        else:
            q.append(now)
            wait = 0
    if note is not None:
        _note(bucket, client, limit, note)
    return wait


def reset() -> None:
    with _lock:
        _failures.clear()
        _locked_until.clear()
        _rate.clear()
        _noted.clear()
