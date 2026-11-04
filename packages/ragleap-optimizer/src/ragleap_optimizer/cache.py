"""ExactCache: an in-memory cache that only returns an answer for the SAME scope and the SAME request."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Set, Tuple

from ragleap_security import Scope, require_scope

from .model import InvalidRequestError, Request


class ExactCache:
    """Key = sha256 of the scope and the whole request (model, messages, tools, parameters), so anything that
    can change the answer is part of the key. A scope is required for every call. Entries expire after
    `ttl_seconds`; the least recently used entries go first beyond `max_entries`. Values are copied in and
    out. It lives in this process only: nothing is written to disk."""

    def __init__(self, *, ttl_seconds: float, max_entries: int = 10_000, clock: Callable[[], float] = time.monotonic):
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or not ttl_seconds > 0:
            raise InvalidRequestError("ttl_seconds must be a positive number")
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise InvalidRequestError("max_entries must be a positive int")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._data: "OrderedDict[str, Tuple[float, Any, str]]" = OrderedDict()
        self._by_scope: Dict[str, Set[str]] = {}
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

    def _drop(self, key: str) -> None:
        _, _, skey = self._data.pop(key)
        keys = self._by_scope.get(skey)
        if keys is not None:
            keys.discard(key)
            if not keys:
                del self._by_scope[skey]

    def get(self, scope: Scope, request: Request) -> "Tuple[bool, Any]":
        """(True, value) on a hit, (False, None) otherwise."""
        key, _ = self._key(scope, request)
        with self._lock:
            item = self._data.get(key)
            if item is None or item[0] <= self._clock():
                if item is not None:
                    self._drop(key)
                self.misses += 1
                return False, None
            self._data.move_to_end(key)
            self.hits += 1
            return True, copy.deepcopy(item[1])

    def put(self, scope: Scope, request: Request, value: Any) -> None:
        key, skey = self._key(scope, request)
        with self._lock:
            if key in self._data:
                self._drop(key)
            self._data[key] = (self._clock() + self.ttl_seconds, copy.deepcopy(value), skey)
            self._by_scope.setdefault(skey, set()).add(key)
            while len(self._data) > self.max_entries:
                self._drop(next(iter(self._data)))

    def erase(self, scope: Scope) -> int:
        """Remove every entry of exactly this scope; returns how many."""
        scope = require_scope(scope)
        with self._lock:
            keys = list(self._by_scope.get(scope.key, ()))
            for k in keys:
                self._drop(k)
            return len(keys)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
