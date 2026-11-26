"""Canary tokens: tripwires that DETECT and TRACE a leak. They never PREVENT copying. Standard library only.

You plant a token in a place that should not be copied from (a system prompt, a memory entry, a tool
description, a document, a seeded record). If the token later shows up somewhere you scan (a model
answer, an outbound tool argument, a log line, an export), you learn that the data moved and whose it was.

What a hit proves: the planted string reached the place you scanned. It does not prove who put it there,
when or why. A missing hit proves nothing: a model may paraphrase or drop a token, and an attacker may
transform or filter it. There is no network beacon: data opened elsewhere does not phone home.

A token is "rlc1_" plus 26 lowercase base32 characters: a random 80-bit id and a 48-bit MAC of that id.
A string that only looks like a token fails the MAC and is ignored without any store lookup. The scope of a
canary lives in the store record, not in the token; a token found in another scope's data is a "foreign"
hit. The token itself is never stored, so a leaked store does not give anyone usable tokens.

Audit events hold scope keys, canary ids, labels and source names, never the scanned text and never a
token. If an audit record cannot be written, the call fails (issue: no canary is left behind; scan: it
returns nothing)."""
from __future__ import annotations

import abc
import base64
import binascii
import hashlib
import hmac
import os
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

from .audit import AuditLog
from .keys import Key, KeyRing
from .scope import Scope, ScopeError, require_scope

PREFIX = "rlc1_"
ID_BYTES = 10
MAC_BYTES = 6
_TOKEN = re.compile(r"rlc1_[a-z2-7]{26}")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,63}\Z")
_ID_HEX = re.compile(r"[0-9a-f]{20}\Z")
_MAC_PURPOSE = b"canary-mac"
_NORMALIZED_EXPANSION = 4  # the normalised text may be at most 4 times max_text_bytes characters

DEFAULT_MAX_TEXT_BYTES = 1 << 20
CEILING_MAX_TEXT_BYTES = 16 << 20
DEFAULT_MAX_CANDIDATES = 64
CEILING_MAX_CANDIDATES = 256
DEFAULT_MAX_PER_SCOPE = 1_000
CEILING_MAX_PER_SCOPE = 100_000
MAX_OWNERS_IN_AUDIT = 16


class CanaryError(Exception):
    """A canary call was refused. `reason` is one of a fixed set of codes: invalid_argument, invalid_label,
    limit, no_such_canary, text_too_large, rate_limited, store_error, audit_failed. Messages never contain
    text, tokens or scope keys."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CanaryRecord:
    """What the store keeps about one canary. There is no token in it."""

    id: str  # 20 lowercase hex characters
    scope_key: str
    label: str
    key_id: str
    created: str


@dataclass(frozen=True)
class Canary:
    """A canary you just issued. `token` is the string to plant; it is not shown by repr()."""

    token: str = field(repr=False)
    id: str
    label: str
    scope_key: str
    created: str


@dataclass(frozen=True)
class CanaryHit:
    """One finding from scan(). kind "own": a canary of this scope (or a narrower one) was found; id, label and
    count are set. kind "foreign": a valid canary of another scope was found in this scope's data; only
    `count` is set (the number of foreign tokens seen), never an id, a label or the owner."""

    kind: str
    count: int
    canary_id: Optional[str] = None
    label: Optional[str] = None
    source: str = ""


class Hits(list):
    """The list of CanaryHit that scan() returns. `truncated` is True when more valid distinct tokens were seen
    than max_candidates, so only the first max_candidates were looked up (any valid token already counts)."""

    truncated: bool = False


class CanaryStore(abc.ABC):
    """Where canary records live. Every method must be atomic."""

    @abc.abstractmethod
    def add(self, record: CanaryRecord, limit: int) -> bool:
        """Store the record unless the scope already has `limit` canaries. Return False when refused."""

    @abc.abstractmethod
    def get(self, canary_id: str) -> Optional[CanaryRecord]:
        """The record with this id, or None."""

    @abc.abstractmethod
    def remove(self, canary_id: str, scope_key: str) -> bool:
        """Delete the record only if it belongs to exactly this scope key. False for a wrong scope or unknown id."""

    @abc.abstractmethod
    def count(self, scope_key: str) -> int:
        """How many canaries this scope has."""


class MemoryCanaryStore(CanaryStore):
    """In memory, thread-safe. Lost when the process ends."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: Dict[str, CanaryRecord] = {}
        self._per_scope: Dict[str, int] = {}

    def add(self, record: CanaryRecord, limit: int) -> bool:
        with self._lock:
            if self._per_scope.get(record.scope_key, 0) >= limit or record.id in self._rows:
                return False
            self._rows[record.id] = record
            self._per_scope[record.scope_key] = self._per_scope.get(record.scope_key, 0) + 1
            return True

    def get(self, canary_id: str) -> Optional[CanaryRecord]:
        with self._lock:
            return self._rows.get(canary_id)

    def remove(self, canary_id: str, scope_key: str) -> bool:
        with self._lock:
            rec = self._rows.get(canary_id)
            if rec is None or rec.scope_key != scope_key:
                return False
            del self._rows[canary_id]
            self._per_scope[scope_key] -= 1
            return True

    def count(self, scope_key: str) -> int:
        with self._lock:
            return self._per_scope.get(scope_key, 0)


class SQLiteCanaryStore(CanaryStore):
    """In an SQLite file (mode 0600), so several processes on one machine share it. One atomic transaction per
    call, parameterised queries only. Not for network filesystems."""

    def __init__(self, path: Union[str, "os.PathLike[str]"]):
        self.path = os.fspath(path)
        os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
        conn = self._connect()
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:  # pragma: no cover - a filesystem that cannot do WAL keeps the default mode
                pass
            conn.execute("CREATE TABLE IF NOT EXISTS canaries (id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, "
                         "label TEXT NOT NULL, key_id TEXT NOT NULL, created TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS canaries_scope ON canaries (scope_key)")
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5.0, isolation_level=None)

    def add(self, record: CanaryRecord, limit: int) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("SELECT COUNT(*) FROM canaries WHERE scope_key = ?", (record.scope_key,)).fetchone()[0]
                if n >= limit or conn.execute("SELECT 1 FROM canaries WHERE id = ?", (record.id,)).fetchone():
                    conn.execute("COMMIT")
                    return False
                conn.execute("INSERT INTO canaries (id, scope_key, label, key_id, created) VALUES (?, ?, ?, ?, ?)",
                             (record.id, record.scope_key, record.label, record.key_id, record.created))
                conn.execute("COMMIT")
                return True
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            conn.close()

    def get(self, canary_id: str) -> Optional[CanaryRecord]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT id, scope_key, label, key_id, created FROM canaries WHERE id = ?",
                               (canary_id,)).fetchone()
        finally:
            conn.close()
        return None if row is None else CanaryRecord(*row)

    def remove(self, canary_id: str, scope_key: str) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute("DELETE FROM canaries WHERE id = ? AND scope_key = ?", (canary_id, scope_key))
                conn.execute("COMMIT")
                return cur.rowcount == 1
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            conn.close()

    def count(self, scope_key: str) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM canaries WHERE scope_key = ?", (scope_key,)).fetchone()[0]
        finally:
            conn.close()


def _int_in(value: object, name: str, default: int, ceiling: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
        raise CanaryError("invalid_argument")
    return value


def _check_label(value: object) -> str:
    if not isinstance(value, str) or not _LABEL.match(value):
        raise CanaryError("invalid_label")
    return value


def _normalise(text: str) -> str:
    """For matching only: NFKC, format characters (zero-width, bidi, soft hyphen) removed, case folded."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf").casefold()


def _encode(raw: bytes) -> str:
    return PREFIX + base64.b32encode(raw).decode("ascii").rstrip("=").lower()


def _decode(token: str) -> Optional[bytes]:
    body = token[len(PREFIX):]
    try:
        raw = base64.b32decode(body.upper() + "=" * (-len(body) % 8))
    except (binascii.Error, ValueError):
        return None
    if len(raw) != ID_BYTES + MAC_BYTES or _encode(raw) != token:  # refuse a non-canonical spelling
        return None
    return raw


class CanaryIssuer:
    """Issues, scans for and revokes canaries.

    keys: a Key or a KeyRing (new canaries use the active key; old canaries still verify while their key is in the
    ring). store: a CanaryStore. audit: an optional AuditLog. rate_limiter: an optional RateLimiter-like object
    whose check(scope) decides whether a scan may run. max_per_scope, max_text_bytes and max_candidates have
    ceilings (100,000, 16 MiB, 256)."""

    def __init__(self, keys: Union[Key, KeyRing], store: CanaryStore, *, audit: Optional[AuditLog] = None,
                 rate_limiter: object = None, max_per_scope: Optional[int] = None,
                 max_text_bytes: Optional[int] = None, max_candidates: Optional[int] = None):
        if type(keys) not in (Key, KeyRing):
            raise CanaryError("invalid_argument")
        if not isinstance(store, CanaryStore):
            raise CanaryError("invalid_argument")
        if audit is not None and not isinstance(audit, AuditLog):
            raise CanaryError("invalid_argument")
        if rate_limiter is not None and not callable(getattr(rate_limiter, "check", None)):
            raise CanaryError("invalid_argument")
        self._keys = keys
        self._store = store
        self._audit = audit
        self._limiter = rate_limiter
        self.max_per_scope = _int_in(max_per_scope, "max_per_scope", DEFAULT_MAX_PER_SCOPE, CEILING_MAX_PER_SCOPE)
        self.max_text_bytes = _int_in(max_text_bytes, "max_text_bytes", DEFAULT_MAX_TEXT_BYTES, CEILING_MAX_TEXT_BYTES)
        self.max_candidates = _int_in(max_candidates, "max_candidates", DEFAULT_MAX_CANDIDATES, CEILING_MAX_CANDIDATES)

    # ---- keys and MACs
    def _active(self) -> Key:
        return self._keys if type(self._keys) is Key else self._keys.active

    def _all_keys(self) -> List[Key]:
        if type(self._keys) is Key:
            return [self._keys]
        return [self._keys.get(k) for k in self._keys.key_ids]

    @staticmethod
    def _mac(key: Key, canary_id: bytes) -> bytes:
        return hmac.new(key.derive(_MAC_PURPOSE), b"rlc1" + canary_id, hashlib.sha256).digest()[:MAC_BYTES]

    def _mac_valid(self, canary_id: bytes, mac: bytes) -> bool:
        ok = False
        for key in self._all_keys():  # no early exit: the time does not depend on which key matched
            ok |= hmac.compare_digest(self._mac(key, canary_id), mac)
        return ok

    # ---- audit
    def _record(self, event: str, scope: Scope, data: dict) -> None:
        if self._audit is None:
            return
        try:
            self._audit.append(event, data, scope=scope)
        except Exception:  # AuditError, or anything the log raises: fail closed
            raise CanaryError("audit_failed") from None

    # ---- public API
    def issue(self, scope: Scope, label: str) -> Canary:
        """Make a canary for exactly this scope. Refused at the per-scope cap (reason "limit")."""
        try:
            scope = require_scope(scope)
        except ScopeError:
            raise CanaryError("invalid_argument") from None
        _check_label(label)
        key = self._active()
        raw_id = os.urandom(ID_BYTES)
        canary_id = raw_id.hex()
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record = CanaryRecord(canary_id, scope.key, label, key.key_id, created)
        try:
            if not self._store.add(record, self.max_per_scope):
                raise CanaryError("limit")
        except CanaryError:
            raise
        except Exception:
            raise CanaryError("store_error") from None
        try:
            self._record("canary.issued", scope, {"canary": canary_id, "label": label})
        except CanaryError:
            try:
                self._store.remove(canary_id, scope.key)  # no canary without its audit record
            except Exception:
                pass
            raise
        return Canary(_encode(raw_id + self._mac(key, raw_id)), canary_id, label, scope.key, created)

    def scan(self, text: str, scope: Scope, *, source: str = "unspecified") -> Hits:
        """Look for valid canaries in `text`, on behalf of `scope`. Returns a Hits list (empty when none).

        "own" hits are canaries inside `scope` (scope.covers(canary scope)); a "foreign" hit is a valid canary of
        another scope. Unknown, revoked and forged tokens are ignored without a trace."""
        try:
            scope = require_scope(scope)
        except ScopeError:
            raise CanaryError("invalid_argument") from None
        if not isinstance(text, str):
            raise CanaryError("invalid_argument")
        _check_label(source)
        if len(text.encode("utf-8", "surrogatepass")) > self.max_text_bytes:
            raise CanaryError("text_too_large")
        if self._limiter is not None:
            decision = self._limiter.check(scope)
            if not getattr(decision, "allowed", False):
                raise CanaryError("rate_limited")
        normalised = _normalise(text)
        if len(normalised) > _NORMALIZED_EXPANSION * self.max_text_bytes:
            raise CanaryError("text_too_large")

        counts: Dict[str, int] = {}
        for match in _TOKEN.finditer(normalised):
            token = match.group(0)
            counts[token] = counts.get(token, 0) + 1

        own: Dict[str, List[object]] = {}      # canary id -> [record, count]
        foreign_count = 0
        foreign_owners: List[str] = []
        foreign_ids: List[str] = []
        valid = 0
        truncated = False
        try:
            for token, n in counts.items():
                raw = _decode(token)
                if raw is None or not self._mac_valid(raw[:ID_BYTES], raw[ID_BYTES:]):
                    continue  # a lookalike: ignored before any store lookup
                if valid >= self.max_candidates:
                    truncated = True
                    break
                valid += 1
                record = self._store.get(raw[:ID_BYTES].hex())
                if record is None:
                    continue
                try:
                    owner = Scope.parse(record.scope_key)
                except ScopeError:
                    continue
                if scope.covers(owner):
                    entry = own.setdefault(record.id, [record, 0])
                    entry[1] += n
                else:
                    foreign_count += n
                    if record.scope_key not in foreign_owners and len(foreign_owners) < MAX_OWNERS_IN_AUDIT:
                        foreign_owners.append(record.scope_key)
                    if record.id not in foreign_ids and len(foreign_ids) < MAX_OWNERS_IN_AUDIT:
                        foreign_ids.append(record.id)
        except CanaryError:
            raise
        except Exception:
            raise CanaryError("store_error") from None

        hits = Hits()
        hits.truncated = truncated
        for record, n in own.values():
            hits.append(CanaryHit("own", n, record.id, record.label, source))
        if foreign_count:
            hits.append(CanaryHit("foreign", foreign_count, None, None, source))
        # events are written last, one per finding; if any write fails nothing is returned
        for record, n in own.values():
            self._record("canary.hit", scope, {"canary": record.id, "label": record.label, "source": source, "count": n})
        if foreign_count:
            self._record("canary.foreign", scope, {"owners": foreign_owners, "canaries": foreign_ids,
                                                   "source": source, "count": foreign_count})
        return hits

    def revoke(self, scope: Scope, canary_id: str) -> None:
        """Delete a canary that belongs to exactly this scope. A wrong scope and an unknown id both raise
        CanaryError("no_such_canary"). Afterwards the token is ignored by scan()."""
        try:
            scope = require_scope(scope)
        except ScopeError:
            raise CanaryError("invalid_argument") from None
        if not isinstance(canary_id, str) or not _ID_HEX.match(canary_id):
            raise CanaryError("no_such_canary")
        try:
            removed = self._store.remove(canary_id, scope.key)
        except Exception:
            raise CanaryError("store_error") from None
        if not removed:
            raise CanaryError("no_such_canary")
        self._record("canary.revoked", scope, {"canary": canary_id})
