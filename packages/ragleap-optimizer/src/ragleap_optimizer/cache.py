"""ExactCache: an in-memory cache that only returns an answer for the SAME scope and the SAME request."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from ragleap_security import Scope, require_scope

from .model import InvalidRequestError, Request

DEFAULT_MAX_VALUE_BYTES = 1024 * 1024
_UNSET: Any = object()


def _positive_int_or_none(name: str, value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError(f"{name} must be a positive int or None")
    return value


class ExactCache:
    """Key = sha256 of the scope and the whole request (model, messages, tools, parameters), so anything that
    can change the answer is part of the key. A scope is required for every call. Entries expire after
    `ttl_seconds`. Values are copied in and out. It lives in this process only: nothing is written to disk.

    Budgets (0.1.1). `max_entries` limits the whole cache. When it is full, eviction="fair" (the default)
    removes the least recently used entry of the scope that holds the most entries, so one noisy tenant evicts
    itself first; eviction="lru" removes the least recently used entry of all (the 0.1.0 order).
    `max_entries_per_scope` makes a scope at its limit lose its own oldest entry. Sizes are the length of the value as
    JSON in UTF-8: `max_value_bytes` (default 1 MiB, None turns it off) refuses a bigger value, `max_bytes_per_scope`
    and `max_bytes` limit the totals. With any size limit a value must be JSON serializable. put() returns False when
    a limit refused the value; nothing is stored and nothing is evicted. Entries that have expired are removed when
    they are read or evicted, so they count against the limits until then."""

    def __init__(self, *, ttl_seconds: float, max_entries: int = 10_000, clock: Callable[[], float] = time.monotonic,
                 eviction: str = "fair", max_entries_per_scope: Optional[int] = None,
                 max_value_bytes: Optional[int] = _UNSET, max_bytes_per_scope: Optional[int] = None,
                 max_bytes: Optional[int] = None):
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or not ttl_seconds > 0:
            raise InvalidRequestError("ttl_seconds must be a positive number")
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise InvalidRequestError("max_entries must be a positive int")
        if eviction not in ("fair", "lru"):
            raise InvalidRequestError('eviction must be "fair" or "lru"')
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.eviction = eviction
        self.max_entries_per_scope = _positive_int_or_none("max_entries_per_scope", max_entries_per_scope)
        self.max_value_bytes = _positive_int_or_none(
            "max_value_bytes", DEFAULT_MAX_VALUE_BYTES if max_value_bytes is _UNSET else max_value_bytes)
        self.max_bytes_per_scope = _positive_int_or_none("max_bytes_per_scope", max_bytes_per_scope)
        self.max_bytes = _positive_int_or_none("max_bytes", max_bytes)
        self._sized = any(v is not None for v in (self.max_value_bytes, self.max_bytes_per_scope, self.max_bytes))
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (expires_at, value, scope key, size); oldest first, so the global LRU is the first item
        self._data: "OrderedDict[str, Tuple[float, Any, str, int]]" = OrderedDict()
        self._by_scope: "Dict[str, OrderedDict[str, None]]" = {}
        self._scope_bytes: Dict[str, int] = {}
        self._total_bytes = 0
        self._scope_hits: Dict[str, int] = {}
        self._scope_misses: Dict[str, int] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(scope: Any, request: Request) -> "Tuple[str, str]":
        scope = require_scope(scope)
        try:
            blob = json.dumps({"scope": scope.key, "request": request}, sort_keys=True)
        except (TypeError, ValueError):
            raise InvalidRequestError("request must be JSON serializable") from None
        return hashlib.sha256(blob.encode("utf-8")).hexdigest(), scope.key

    def _size(self, value: Any) -> int:
        if not self._sized:
            return 0
        try:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError):
            raise InvalidRequestError("value must be JSON serializable when a size limit is set") from None

    def _drop(self, key: str) -> None:
        _, _, skey, size = self._data.pop(key)
        keys = self._by_scope.get(skey)
        if keys is not None:
            keys.pop(key, None)
            if not keys:
                del self._by_scope[skey]
        self._total_bytes -= size
        left = self._scope_bytes.get(skey, 0) - size
        if left > 0:
            self._scope_bytes[skey] = left
        else:
            self._scope_bytes.pop(skey, None)

    def _victim(self, protect: str, by_bytes: bool) -> Optional[str]:
        """The key to evict from the whole cache, never `protect` (the entry that was just stored)."""
        if self.eviction == "lru":  # the oldest entry of all; the one just stored is the newest, so it is safe
            return next(iter(self._data), None)
        best: Optional[str] = None
        best_load = -1
        for skey, keys in self._by_scope.items():
            oldest = next((k for k in keys if k != protect), None)
            if oldest is None:
                continue
            load = self._scope_bytes.get(skey, 0) if by_bytes else len(keys)
            if load > best_load:
                best, best_load = oldest, load
        return best

    def _enforce(self, key: str, skey: str) -> None:
        keys = self._by_scope[skey]
        if self.max_entries_per_scope is not None:
            while len(keys) > self.max_entries_per_scope:
                self._drop(next(iter(keys)))
        if self.max_bytes_per_scope is not None:
            while self._scope_bytes.get(skey, 0) > self.max_bytes_per_scope:
                self._drop(next(k for k in self._by_scope[skey] if k != key))
        while len(self._data) > self.max_entries:
            victim = self._victim(key, by_bytes=False)
            if victim is None:  # pragma: no cover - max_entries >= 1, so another entry always exists
                break
            self._drop(victim)
        if self.max_bytes is not None:
            while self._total_bytes > self.max_bytes:
                victim = self._victim(key, by_bytes=True)
                if victim is None:  # pragma: no cover - the new value alone fits, checked in put()
                    break
                self._drop(victim)

    def get(self, scope: Scope, request: Request) -> "Tuple[bool, Any]":
        """(True, value) on a hit, (False, None) otherwise."""
        key, skey = self._key(scope, request)
        with self._lock:
            item = self._data.get(key)
            if item is None or item[0] <= self._clock():
                if item is not None:
                    self._drop(key)
                self.misses += 1
                self._scope_misses[skey] = self._scope_misses.get(skey, 0) + 1
                return False, None
            self._data.move_to_end(key)
            self._by_scope[skey].move_to_end(key)
            self.hits += 1
            self._scope_hits[skey] = self._scope_hits.get(skey, 0) + 1
            return True, copy.deepcopy(item[1])

    def put(self, scope: Scope, request: Request, value: Any) -> bool:
        """Store a value. Returns False, storing nothing and evicting nothing, if a size limit refuses it."""
        key, skey = self._key(scope, request)
        stored = copy.deepcopy(value)
        size = self._size(stored)
        if self._sized and ((self.max_value_bytes is not None and size > self.max_value_bytes)
                            or (self.max_bytes_per_scope is not None and size > self.max_bytes_per_scope)
                            or (self.max_bytes is not None and size > self.max_bytes)):
            return False
        with self._lock:
            if key in self._data:
                self._drop(key)
            self._data[key] = (self._clock() + self.ttl_seconds, stored, skey, size)
            self._by_scope.setdefault(skey, OrderedDict())[key] = None
            self._scope_bytes[skey] = self._scope_bytes.get(skey, 0) + size
            self._total_bytes += size
            self._enforce(key, skey)
            return True

    def erase(self, scope: Scope) -> int:
        """Remove every entry of exactly this scope, and its counters; returns how many entries."""
        scope = require_scope(scope)
        with self._lock:
            keys = list(self._by_scope.get(scope.key, ()))
            for k in keys:
                self._drop(k)
            self._scope_hits.pop(scope.key, None)
            self._scope_misses.pop(scope.key, None)
            return len(keys)

    def stats(self, scope: Scope) -> Dict[str, int]:
        """Entries, bytes (0 when no size limit is set), hits and misses of exactly this scope. Never another's."""
        scope = require_scope(scope)
        with self._lock:
            return {"entries": len(self._by_scope.get(scope.key, ())), "bytes": self._scope_bytes.get(scope.key, 0),
                    "hits": self._scope_hits.get(scope.key, 0), "misses": self._scope_misses.get(scope.key, 0)}

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
