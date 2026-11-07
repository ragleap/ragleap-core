"""Token-bucket rate limits keyed by a Scope key or a string. Standard library only.

RateLimiter keeps state in memory (one process). SQLiteRateLimiter shares state between processes on
one machine. Both fail closed: when they cannot decide, they refuse."""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Union

from .scope import Scope

MAX_KEY_CHARS = 256
_EVICT_SCAN = 32


class RateLimitError(Exception):
    """A request was refused. `retry_after` is in seconds (inf if it can never succeed) and `reason`
    is "limited", "too_many_keys" or "unavailable"."""

    def __init__(self, message: str, retry_after: float = 0.0, reason: str = "limited"):
        super().__init__(message)
        self.retry_after = retry_after
        self.reason = reason


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: float
    retry_after: float
    reason: str = ""


def _norm_key(key: object) -> str:
    if type(key) is Scope:
        return key.key
    if isinstance(key, str) and 1 <= len(key) <= MAX_KEY_CHARS:
        return key
    raise ValueError("key must be a Scope or a string of 1 to 256 characters")


def _positive(value: object, name: str, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be a finite number of at least {minimum}")
    return float(value)


class _Limiter:
    rate: float
    burst: float

    def _setup(self, rate: float, burst: float, max_keys: int) -> None:
        self.rate = _positive(rate, "rate", 1e-9)
        self.burst = _positive(burst, "burst", 1.0)
        if isinstance(max_keys, bool) or not isinstance(max_keys, int) or max_keys < 1:
            raise ValueError("max_keys must be a positive int")
        self.max_keys = max_keys

    def _cost(self, cost: object) -> float:
        return _positive(cost, "cost", 1e-9)

    def check(self, key: Union[str, Scope], cost: float = 1) -> Decision:
        raise NotImplementedError

    def require(self, key: Union[str, Scope], cost: float = 1) -> Decision:
        """Like check(), but raises RateLimitError when the request is refused."""
        d = self.check(key, cost)
        if not d.allowed:
            raise RateLimitError(f"rate limit: {d.reason}", d.retry_after, d.reason)
        return d

    def _deny(self, tokens: float, c: float) -> Decision:
        retry = math.inf if c > self.burst else (c - tokens) / self.rate
        return Decision(False, tokens, retry, "limited")


class RateLimiter(_Limiter):
    """In-memory token bucket: `burst` tokens at most, refilled at `rate` per second. At most
    `max_keys` keys are tracked. A key's state is dropped only once its bucket is full again (that
    loses nothing), so flooding with new keys cannot reset a throttled key; when no full bucket can be
    dropped, new keys are refused instead."""

    def __init__(self, rate: float, burst: float, *, max_keys: int = 10_000,
                 clock: Callable[[], float] = time.monotonic):
        self._setup(rate, burst, max_keys)
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: "OrderedDict[str, list]" = OrderedDict()

    def _evict(self, now: float) -> bool:
        for i, (k, (tokens, last)) in enumerate(self._buckets.items()):
            if i >= _EVICT_SCAN:
                break
            if tokens + max(0.0, now - last) * self.rate >= self.burst:
                del self._buckets[k]
                return True
        return False

    def check(self, key: Union[str, Scope], cost: float = 1) -> Decision:
        k = _norm_key(key)
        c = self._cost(cost)
        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(k)
            if bucket is None:
                if len(self._buckets) >= self.max_keys and not self._evict(now):
                    return Decision(False, 0.0, self.burst / self.rate, "too_many_keys")
                tokens = self.burst
            else:
                tokens = min(self.burst, bucket[0] + max(0.0, now - bucket[1]) * self.rate)
            if c > tokens:
                self._buckets[k] = [tokens, now]
                self._buckets.move_to_end(k)
                return self._deny(tokens, c)
            tokens -= c
            self._buckets[k] = [tokens, now]
            self._buckets.move_to_end(k)
            return Decision(True, tokens, 0.0)


class SQLiteRateLimiter(_Limiter):
    """Token bucket stored in an SQLite file, so several processes on one machine share it. One atomic
    transaction per check. It uses wall-clock time (time.time): a clock set forward refills early, a
    clock set back adds no tokens. The file is created with mode 0600. Not for network filesystems."""

    def __init__(self, path: Union[str, "os.PathLike[str]"], rate: float, burst: float, *,
                 max_keys: int = 100_000, clock: Callable[[], float] = time.time):
        self._setup(rate, burst, max_keys)
        self._clock = clock
        self.path = os.fspath(path)
        os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
        conn = self._connect()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS buckets (key TEXT PRIMARY KEY, tokens REAL NOT NULL, updated REAL NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS buckets_updated ON buckets (updated)")
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5.0, isolation_level=None)

    def _run(self, conn: sqlite3.Connection, k: str, c: float) -> Decision:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = self._clock()
            row = conn.execute("SELECT tokens, updated FROM buckets WHERE key = ?", (k,)).fetchone()
            if row is None:
                count = conn.execute("SELECT COUNT(*) FROM buckets").fetchone()[0]
                if count >= self.max_keys:
                    conn.execute(
                        "DELETE FROM buckets WHERE key IN (SELECT key FROM (SELECT key, tokens, updated FROM buckets "
                        "ORDER BY updated LIMIT ?) WHERE tokens + MAX(0, ? - updated) * ? >= ?)",
                        (_EVICT_SCAN, now, self.rate, self.burst))
                    count = conn.execute("SELECT COUNT(*) FROM buckets").fetchone()[0]
                    if count >= self.max_keys:
                        conn.execute("COMMIT")
                        return Decision(False, 0.0, self.burst / self.rate, "too_many_keys")
                tokens = self.burst
            else:
                tokens = min(self.burst, row[0] + max(0.0, now - row[1]) * self.rate)
            if c > tokens:
                decision = self._deny(tokens, c)
            else:
                tokens -= c
                decision = Decision(True, tokens, 0.0)
            conn.execute("INSERT OR REPLACE INTO buckets (key, tokens, updated) VALUES (?, ?, ?)", (k, tokens, now))
            conn.execute("COMMIT")
            return decision
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def check(self, key: Union[str, Scope], cost: float = 1) -> Decision:
        k = _norm_key(key)
        c = self._cost(cost)
        try:
            conn = self._connect()
            try:
                return self._run(conn, k, c)
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            return Decision(False, 0.0, 1.0, "unavailable")
