"""Governed, auditable, multi-tenant dispatch for a Toolkit (opt-in).

GovernedToolkit wraps a Toolkit and decides, per call and per scope, which
tools may run, which need an approval, how often a tenant may call, and what
is written to the audit hook. The Toolkit itself is not changed: using it
directly behaves exactly as before. See docs/design/governed-toolkit.md for
the design, the rejected alternatives and the limits.

What this layer does NOT do: it does not authenticate anybody (the host
passes a Scope it has already established), it cannot make a handler that is
shared between tenants tenant-safe, and its rate limit is per process.
"""
from __future__ import annotations

import collections
import copy
import dataclasses
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterable, Optional

from ragleap_tools.base import ToolResult
from ragleap_tools.toolkit import Toolkit

_LOG = logging.getLogger("ragleap_tools")

_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
_FIELDS = ("tenant", "subject", "agent", "session")


class GovernanceConfigError(ValueError):
    """A GovernedToolkit, RateLimit or Scope was configured wrongly. Raised
    while building them, never from call()."""


class _FallbackScopeError(ValueError):
    """An invalid scope."""


@dataclass(frozen=True)
class _FallbackScope:
    """Who a call belongs to: a tenant, and optionally a subject, agent and
    session. Each part is 1 to 128 characters of letters, digits and _ . : @ -,
    starting with a letter or digit ('/' and '=' are not allowed, so `key` is
    unambiguous). The same rules, messages and key format as
    ragleap_security.Scope, which is used instead when it is installed."""

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


try:  # the optional extra: pip install "ragleap-tools[security]"
    from ragleap_security import Scope, ScopeError  # type: ignore

    HAS_SECURITY = True
except ImportError:  # pragma: no cover - exercised when the extra is not installed
    Scope = _FallbackScope  # type: ignore
    ScopeError = _FallbackScopeError  # type: ignore
    HAS_SECURITY = False


@dataclass(frozen=True)
class RateLimit:
    """At most `calls` calls per `per_seconds` seconds, per tenant."""

    calls: int
    per_seconds: float

    def __post_init__(self) -> None:
        if isinstance(self.calls, bool) or not isinstance(self.calls, int) or self.calls < 1:
            raise GovernanceConfigError("RateLimit.calls must be a positive integer")
        seconds = self.per_seconds
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise GovernanceConfigError("RateLimit.per_seconds must be a positive number")


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


def _refuse(text: str) -> ToolResult:
    return ToolResult(success=False, error=text)


class GovernedToolkit:
    """Calls into a Toolkit, per scope, under the owner's rules.

    toolkit: the Toolkit to dispatch to. It is used as is and never changed.
    allow_for: allow_for(scope) returns the names of the tools that scope may
        call (a list, tuple, set or frozenset of strings). Deny by default: a
        tool it does not name is not available, and so is every tool when it
        raises or returns anything else.
    require_approval: names of tools that need approve() to say yes first.
    approve: approve(scope, tool, arguments) must return exactly True. An
        exception or any other value denies. With none supplied, tools named
        in require_approval are refused. It runs after the arguments are
        validated and gets its own copy of them.
    rate_limit: a RateLimit, applied per tenant, per process.
    on_event: on_event(event) gets one small dict per call() attempt. It never
        contains arguments or results. A hook that raises is logged and
        ignored.
    clock: a monotonic clock for the rate windows (tests inject one).

    call() never raises.
    """

    def __init__(
        self,
        toolkit: Toolkit,
        *,
        allow_for: Callable[[Any], Iterable[str]],
        require_approval: Iterable[str] = (),
        approve: Optional[Callable[[Any, Any, Dict[str, Any]], bool]] = None,
        rate_limit: Optional[RateLimit] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(toolkit, Toolkit):
            raise GovernanceConfigError("toolkit must be a Toolkit")
        if not callable(allow_for):
            raise GovernanceConfigError("allow_for must be callable")
        if isinstance(require_approval, (str, bytes)) or not isinstance(
            require_approval, (list, tuple, set, frozenset)
        ):
            raise GovernanceConfigError("require_approval must be a list of tool names")
        names = frozenset(require_approval)
        for n in names:
            if not isinstance(n, str):
                raise GovernanceConfigError("require_approval must hold tool names (strings)")
            if n not in toolkit:
                raise GovernanceConfigError("require_approval names a tool that is not in the toolkit: %r" % (n[:60],))
        if approve is not None and not callable(approve):
            raise GovernanceConfigError("approve must be callable")
        if rate_limit is not None and type(rate_limit) is not RateLimit:
            raise GovernanceConfigError("rate_limit must be a RateLimit")
        if on_event is not None and not callable(on_event):
            raise GovernanceConfigError("on_event must be callable")
        if not callable(clock):
            raise GovernanceConfigError("clock must be callable")
        self._toolkit = toolkit
        self._allow_for = allow_for
        self._require_approval: FrozenSet[str] = names
        self._approve = approve
        self._limit = rate_limit
        self._on_event = on_event
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: Dict[str, _Window] = {}

    def call(self, scope: Any, name: Any, arguments: Any = None) -> ToolResult:
        started = time.monotonic()
        info: Dict[str, Any] = {"tool": None, "approval": "unchecked", "error": None}
        result = self._decide_and_run(scope, name, arguments, info)
        self._emit(scope, started, result, info)
        return result

    def forget(self, tenant: Optional[str] = None) -> int:
        """Drops the rate window of one tenant, or of all tenants when none is
        named. Returns how many windows were dropped."""
        with self._lock:
            if tenant is None:
                dropped = len(self._windows)
                self._windows.clear()
                return dropped
            return 1 if self._windows.pop(tenant, None) is not None else 0

    # -- internals -----------------------------------------------------------

    def _decide_and_run(self, scope: Any, name: Any, arguments: Any, info: Dict[str, Any]) -> ToolResult:
        if type(scope) is not Scope:
            info["error"] = "invalid_scope"
            return _refuse("a scope is required")
        allowed = self._allowed(scope)
        if allowed is None or not isinstance(name, str) or name not in allowed or name not in self._toolkit:
            info["error"] = "not_available"
            return _refuse("tool not available")
        info["tool"] = name
        if self._limit is not None and not self._window_for(scope.tenant).take():
            info["error"] = "rate_limited"
            return _refuse("rate limit exceeded")
        prepared = self._toolkit.prepare(name, arguments)
        if isinstance(prepared, ToolResult):
            info["error"] = "invalid_arguments"
            return prepared
        tool, args = prepared
        if name in self._require_approval:
            if self._approve is None:
                info["approval"] = "denied"
                info["error"] = "approval_required"
                return _refuse("approval required")
            try:
                verdict = self._approve(scope, tool, copy.deepcopy(args))
            except Exception as exc:  # noqa: BLE001 - the hook or the copy failed: deny, never raise
                _LOG.warning("approve step raised %s", type(exc).__name__)
                verdict = False
            if verdict is not True:
                info["approval"] = "denied"
                info["error"] = "approval_denied"
                return _refuse("approval denied")
            info["approval"] = "approved"
        else:
            info["approval"] = "not_required"
        result = self._toolkit.run(tool, args)
        if not result.success:
            info["error"] = "tool_failed"
        return result

    def _allowed(self, scope: Any) -> Optional[FrozenSet[str]]:
        try:
            wanted = self._allow_for(scope)
        except Exception as exc:  # noqa: BLE001 - fail closed
            _LOG.warning("allow_for hook raised %s", type(exc).__name__)
            return None
        if isinstance(wanted, (str, bytes)) or not isinstance(wanted, (list, tuple, set, frozenset)):
            _LOG.warning("allow_for returned %s, not a list of tool names", type(wanted).__name__)
            return None
        if any(not isinstance(n, str) for n in wanted):
            _LOG.warning("allow_for returned a non-string entry")
            return None
        return frozenset(wanted)

    def _window_for(self, tenant: str) -> _Window:
        with self._lock:
            w = self._windows.get(tenant)
            if w is None:
                w = self._windows[tenant] = _Window(self._limit, self._clock)  # type: ignore[arg-type]
            return w

    def _emit(self, scope: Any, started: float, result: ToolResult, info: Dict[str, Any]) -> None:
        if self._on_event is None:
            return
        event: Dict[str, Any] = {
            "event": "call",
            "tool": info["tool"],
            "ok": result.success,
            "error": info["error"],
            "approval": info["approval"],
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if type(scope) is Scope:
            event["tenant"] = scope.tenant
            event["scope"] = scope.key
        try:
            self._on_event(event)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a call
            _LOG.warning("on_event hook raised %s", type(exc).__name__)
