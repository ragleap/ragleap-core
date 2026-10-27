"""
ragleap_integrations.tenancy

Multi-tenant use of the MCP client. Standard library only (ragleap-security is
optional).

    hub = McpHub(config, token_provider=get_token, limit=RateLimit(60, 60))
    conn = hub.connect(Scope(tenant="acme", subject="u1"))   # a Connection, as from connect()
    conn.toolkit.call(name_from_model, arguments_from_model)  # the model never sees the scope

The rules, each covered by a test:

- A scope is required and is never a tool argument. It is closed over by the
  tenant's own client, so a model cannot name another tenant.
- Each scope gets its own McpClient: its own protocol-era cache, tool snapshot
  and token. Nothing mutable is shared between scopes.
- token_provider(scope, server_name) is called for every request. Nothing is
  cached, a provider failure gives a constant message, and the token is never
  put in an event, a result, an error or the report.
- allow_for(scope) can only narrow the owner's allowlist, never widen it.
- policy_for(scope) picks a per-tenant McpPolicy; approve_scoped receives the
  scope. A call with approve_scoped but no scope is denied.
- RateLimit counts calls per TENANT (all of a tenant's scopes share it), before
  any network call.
- Events carry the scope; arguments and results are never in them. If an
  AuditLog (ragleap-security) or anything with append(event, data, scope) is
  given, each event is appended with the scope.

Scope: when ragleap-security is installed its Scope is used (and, like there,
only that exact class is accepted); otherwise a small built-in Scope with the
same fields and key format.
"""

from __future__ import annotations

import collections
import dataclasses
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from ragleap_integrations.connect import Connection, connect_config
from ragleap_integrations.governance import McpPolicy
from ragleap_integrations.mcp import McpClient, McpConfig, McpConfigError, Transport

logger = logging.getLogger("ragleap_integrations")

_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
_FIELDS = ("tenant", "subject", "agent", "session")


class _FallbackScopeError(ValueError):
    """An invalid scope, or something that is not a Scope where one is required."""


@dataclass(frozen=True)
class _FallbackScope:
    """Who a call belongs to: a tenant, and optionally a subject, agent and session.
    Each part is 1 to 128 characters of letters, digits and _ . : @ -, starting with a
    letter or digit ('/' and '=' are not allowed, so `key` is unambiguous). The same rules
    and key format as ragleap_security.Scope."""

    tenant: str
    subject: Optional[str] = None
    agent: Optional[str] = None
    session: Optional[str] = None

    def __post_init__(self) -> None:
        for f in _FIELDS:
            v = getattr(self, f)
            if v is None:
                if f == "tenant":
                    raise _FallbackScopeError("tenant is required")
                continue
            if not isinstance(v, str) or not _PART.match(v):
                raise _FallbackScopeError("invalid %s" % f)

    @property
    def key(self) -> str:
        return "/".join("%s=%s" % (f, getattr(self, f)) for f in _FIELDS if getattr(self, f) is not None)


try:  # the optional extra: pip install "ragleap-integrations[security]"
    from ragleap_security import Scope, ScopeError  # type: ignore

    HAS_SECURITY = True
except ImportError:  # pragma: no cover - exercised when the extra is not installed
    Scope = _FallbackScope  # type: ignore
    ScopeError = _FallbackScopeError  # type: ignore
    HAS_SECURITY = False


def require_scope(value: Any) -> Any:
    """Return value if it is exactly a Scope. Strings, dicts, None and subclasses are
    refused, so a caller cannot pass a forged object that claims to be someone else."""
    if type(value) is not Scope:
        raise ScopeError("a Scope is required")
    return value


@dataclass(frozen=True)
class RateLimit:
    """At most `calls` tool calls per `per_seconds` seconds, per tenant."""

    calls: int
    per_seconds: float

    def __post_init__(self) -> None:
        if isinstance(self.calls, bool) or not isinstance(self.calls, int) or self.calls < 1:
            raise McpConfigError("RateLimit.calls must be a positive integer")
        if isinstance(self.per_seconds, bool) or not isinstance(self.per_seconds, (int, float)) or self.per_seconds <= 0:
            raise McpConfigError("RateLimit.per_seconds must be a positive number")


class _Window:
    """A sliding window of call times for one tenant."""

    def __init__(self, limit: RateLimit, clock: Callable[[], float]):
        self._limit = limit
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: "collections.deque[float]" = collections.deque()

    def take(self) -> bool:
        now = self._clock()
        with self._lock:
            cutoff = now - self._limit.per_seconds
            while self._hits and self._hits[0] <= cutoff:
                self._hits.popleft()
            if len(self._hits) >= self._limit.calls:
                return False
            self._hits.append(now)
            return True


class McpHub:
    """One owner-level configuration, one Connection per scope.

    token_provider(scope, server_name) -> str | wrapped token with .reveal() | None
    policy_for(scope) -> McpPolicy | None (None: the config's own policy)
    allow_for(scope) -> list of 'server.tool' | None (None: the whole allowlist)
    limit: a RateLimit applied per tenant
    audit: anything with append(event, data, scope), e.g. ragleap_security.AuditLog
    max_scopes: how many scopes stay cached (least recently used are dropped)"""

    def __init__(self, config: McpConfig, *, token_provider: Optional[Callable[[Any, str], Any]] = None,
                 policy_for: Optional[Callable[[Any], Optional[McpPolicy]]] = None,
                 allow_for: Optional[Callable[[Any], Optional[Sequence[str]]]] = None,
                 limit: Optional[RateLimit] = None, audit: Any = None,
                 transport: Optional[Transport] = None, max_scopes: int = 256,
                 clock: Callable[[], float] = time.monotonic):
        if not isinstance(config, McpConfig):
            raise McpConfigError("config must be an McpConfig")
        for name, fn in (("token_provider", token_provider), ("policy_for", policy_for), ("allow_for", allow_for)):
            if fn is not None and not callable(fn):
                raise McpConfigError("%s must be callable" % name)
        if limit is not None and not isinstance(limit, RateLimit):
            raise McpConfigError("limit must be a RateLimit")
        if audit is not None and not callable(getattr(audit, "append", None)):
            raise McpConfigError("audit must have an append(event, data, scope) method")
        if isinstance(max_scopes, bool) or not isinstance(max_scopes, int) or max_scopes < 1:
            raise McpConfigError("max_scopes must be a positive integer")
        self._config = config
        self._token_provider = token_provider
        self._policy_for = policy_for
        self._allow_for = allow_for
        self._limit = limit
        self._audit = audit
        self._transport = transport
        self._max = max_scopes
        self._clock = clock
        self._lock = threading.Lock()
        self._conns: "collections.OrderedDict[str, tuple]" = collections.OrderedDict()
        self._builds: Dict[str, threading.Lock] = {}
        self._windows: Dict[str, _Window] = {}

    @property
    def scopes(self) -> List[str]:
        """The keys of the cached scopes, least recently used first."""
        with self._lock:
            return list(self._conns)

    def connect(self, scope: Any) -> Connection:
        """The Connection for this scope (built and cached on first use). Raises
        ScopeError without an exact Scope, and McpConfigError or McpDiscoveryError
        for setup problems."""
        scope = require_scope(scope)
        key = scope.key
        with self._lock:
            hit = self._conns.get(key)
            if hit is not None:
                self._conns.move_to_end(key)
                return hit[1]
            build_lock = self._builds.setdefault(key, threading.Lock())
        try:
            with build_lock:
                with self._lock:
                    hit = self._conns.get(key)
                    if hit is not None:
                        self._conns.move_to_end(key)
                        return hit[1]
                conn = self._build(scope)
                with self._lock:
                    self._conns[key] = (scope, conn)
                    self._conns.move_to_end(key)
                    while len(self._conns) > self._max:
                        self._conns.popitem(last=False)
                return conn
        finally:
            with self._lock:
                self._builds.pop(key, None)

    def forget(self, scope: Any = None, *, tenant: Optional[str] = None) -> int:
        """Drop one scope's cached Connection, or every scope of one tenant (and that
        tenant's rate window). Returns how many Connections were dropped."""
        if (scope is None) == (tenant is None):
            raise ValueError("give exactly one of scope or tenant")
        with self._lock:
            if scope is not None:
                scope = require_scope(scope)
                return 1 if self._conns.pop(scope.key, None) is not None else 0
            doomed = [k for k, (s, _c) in self._conns.items() if s.tenant == tenant]
            for k in doomed:
                del self._conns[k]
            self._windows.pop(tenant, None)
            return len(doomed)

    # -- internals -----------------------------------------------------------

    def _window_for(self, tenant: str) -> _Window:
        with self._lock:
            w = self._windows.get(tenant)
            if w is None:
                w = self._windows[tenant] = _Window(self._limit, self._clock)
            return w

    def _allowed_for(self, scope: Any) -> List[str]:
        allowed = list(self._config.allowed_tools)
        if self._allow_for is not None:
            try:
                wanted = self._allow_for(scope)
            except Exception:
                logger.error("allow_for failed")
                raise McpConfigError("allow_for failed") from None
            if wanted is not None:
                if isinstance(wanted, str) or not isinstance(wanted, (list, tuple, set, frozenset)):
                    raise McpConfigError("allow_for must return a list of 'server.tool' entries or None")
                wanted_set = set(wanted)
                allowed = [t for t in allowed if t in wanted_set]
        if not allowed:
            raise McpConfigError("no tools are allowed for this scope")
        return allowed

    def _policy_of(self, scope: Any, allowed: List[str]) -> Optional[McpPolicy]:
        chosen = self._config.policy
        if self._policy_for is not None:
            try:
                picked = self._policy_for(scope)
            except Exception:
                logger.error("policy_for failed")
                raise McpConfigError("policy_for failed") from None
            if picked is not None:
                if not isinstance(picked, McpPolicy):
                    raise McpConfigError("policy_for must return an McpPolicy or None")
                chosen = picked
        if chosen is not None:
            keep = set(allowed)
            chosen = dataclasses.replace(chosen, pre_approved=[e for e in chosen.pre_approved if e in keep])
        return chosen

    def _build(self, scope: Any) -> Connection:
        allowed = self._allowed_for(scope)
        policy = self._policy_of(scope, allowed)
        owner_hook = self._config.on_event
        audit = self._audit

        def hook(event: Dict[str, Any]) -> None:
            if owner_hook is not None:
                try:
                    owner_hook(event)
                except Exception:
                    logger.exception("on_event hook failed")
            if audit is not None:
                try:
                    audit.append(str(event.get("event", "event")), {k: v for k, v in event.items() if k != "event"}, scope)
                except Exception:
                    logger.exception("audit append failed")

        scoped = dataclasses.replace(self._config, allowed_tools=allowed, policy=policy, on_event=hook)
        provider = self._token_provider
        token_for = (lambda server: provider(scope, server)) if provider is not None else None
        guard = self._window_for(scope.tenant).take if self._limit is not None else None
        client = McpClient(scoped, transport=self._transport, token_for=token_for, scope=scope,
                           guard=guard, lock=threading.RLock())
        return connect_config(scoped, transport=self._transport, client=client)
