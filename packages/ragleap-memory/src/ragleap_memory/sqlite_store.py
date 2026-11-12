"""SQLiteStore: durable memory in one SQLite file (or in memory), with FTS5 keyword search and optional
vector and hybrid search over vectors the caller's embedder produced.

Every operation takes a Scope and is filtered by it inside the SQL, not by the caller. A scope sees
the entries whose scope it covers: a tenant-wide scope sees everything in the tenant, a narrower scope
sees only its own subject/agent/session, and an entry without a subject is not visible to a scope that
names one."""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
import uuid
from array import array
from datetime import datetime, timezone
from operator import mul
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence

from ragleap_security import (AuditLog, LimitError, Scope, check_json_shape, check_size, require_scope)

from . import exportcrypto
from .model import (ID_RE, KINDS, SOURCE_RE, TRUSTS, EmbedderError, EmbedResult, InvalidEntryError, MemoryEntry,
                    MemoryStoreError, ImportResult, NotFoundError, QuotaExceededError, SearchHit, estimate_tokens,
                    trust_rank)

SCHEMA_VERSION = 3
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,63}\Z")
MAX_EMBEDDING_DIM = 4096
SEARCH_MODES = ("keyword", "vector", "hybrid")
RRF_K = 60
MAX_QUERY_BYTES = 1000
_SCOPE_FIELDS = ("tenant", "subject", "agent", "session")
_TOKEN = re.compile(r"\w+")
_PERIOD = re.compile(r"\d{4}-\d{2}-\d{2}\Z")

EXPORT_FORMAT = "ragleap-memory-export"
EXPORT_VERSION = 1
MAX_LINE_BYTES = 65536
_EXPORT_KEYS = frozenset({"id", "scope", "kind", "text", "source", "trust", "importance", "created_at", "expires_at",
                          "superseded_by", "derived_from", "metadata", "tokens"})
_EXPORT_REQUIRED = frozenset({"scope", "kind", "text"})

# Schema 1 (the 0.1.0 layout). Kept as it was so that a 0.1.0 file can be migrated in place.
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS memories(
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    tenant TEXT NOT NULL, subject TEXT, agent TEXT, session TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('episode','fact','summary')),
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    trust TEXT NOT NULL CHECK(trust IN ('untrusted','model','owner')),
    importance REAL NOT NULL CHECK(importance >= 0 AND importance <= 1),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    superseded_by TEXT,
    derived_from TEXT NOT NULL,
    metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_scope ON memories(tenant, subject, agent, session);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    text, content='memories', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
"""

# Schema 2 additions, run one statement at a time inside the migration transaction.
_V2_DDL = (
    "CREATE TABLE IF NOT EXISTS tenant_totals(tenant TEXT PRIMARY KEY, entries INTEGER NOT NULL,"
    " tokens INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS usage(tenant TEXT NOT NULL, period TEXT NOT NULL, served_tokens INTEGER NOT NULL,"
    " searches INTEGER NOT NULL, PRIMARY KEY(tenant, period))",
    "CREATE TRIGGER IF NOT EXISTS totals_ai AFTER INSERT ON memories BEGIN"
    " INSERT INTO tenant_totals(tenant, entries, tokens) VALUES (new.tenant, 1, new.tokens)"
    " ON CONFLICT(tenant) DO UPDATE SET entries = entries + 1, tokens = tokens + new.tokens; END",
    "CREATE TRIGGER IF NOT EXISTS totals_ad AFTER DELETE ON memories BEGIN"
    " UPDATE tenant_totals SET entries = entries - 1, tokens = tokens - old.tokens WHERE tenant = old.tenant; END",
)


# Schema 3 (0.3.0): vectors made by the caller's embedder, kept next to the entries they describe.
_V3_DDL = (
    "CREATE TABLE IF NOT EXISTS embeddings(rowid INTEGER PRIMARY KEY, model TEXT NOT NULL,"
    " dim INTEGER NOT NULL, vec BLOB NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS embeddings_ad AFTER DELETE ON memories BEGIN"
    " DELETE FROM embeddings WHERE rowid = old.rowid; END",
)


def _unit_vector(vec: Any, dim: int) -> "array[float]":
    """Check a vector (finite numbers, exactly `dim` of them, not all zero) and return it scaled to length 1."""
    try:
        n = len(vec)
    except TypeError:
        raise InvalidEntryError("a vector must be a sequence of numbers") from None
    if n != dim:
        raise InvalidEntryError(f"a vector must have exactly {dim} numbers")
    values = []
    for x in vec:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise InvalidEntryError("a vector must contain only finite numbers")
        values.append(float(x))
    norm = math.sqrt(sum(v * v for v in values))
    if not math.isfinite(norm) or norm == 0.0:
        raise InvalidEntryError("a vector must not be all zeros")
    return array("f", (v / norm for v in values))


def _to_blob(vec: "array[float]") -> bytes:
    out = array("f", vec)
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def _from_blob(blob: bytes) -> "array[float]":
    out = array("f")
    out.frombytes(blob)
    if sys.byteorder != "little":
        out.byteswap()
    return out


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _scope_clause(scope: Scope, alias: str = "") -> "tuple[str, list]":
    """SQL condition that matches exactly the entries `scope` covers."""
    p = f"{alias}." if alias else ""
    parts, params = [f"{p}tenant = ?"], [scope.tenant]
    for f in _SCOPE_FIELDS[1:]:
        v = getattr(scope, f)
        if v is not None:
            parts.append(f"{p}{f} = ?")
            params.append(v)
    return " AND ".join(parts), params


def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
    return MemoryEntry(
        id=row["id"],
        scope=Scope(row["tenant"], row["subject"], row["agent"], row["session"]),
        kind=row["kind"], text=row["text"], source=row["source"], trust=row["trust"],
        importance=row["importance"], created_at=row["created_at"], expires_at=row["expires_at"],
        superseded_by=row["superseded_by"], derived_from=tuple(json.loads(row["derived_from"])),
        metadata=json.loads(row["metadata"]), tokens=row["tokens"],
    )



def _read_export(stream: Any, max_entries: int, max_bytes: int) -> List["tuple[int, Any]"]:
    """Read and check the header and the lines of an export file. Returns (line number, parsed object)."""
    total = 0
    out: List["tuple[int, Any]"] = []
    header = None
    n = 0
    while True:
        raw = stream.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        n += 1
        total += len(raw)
        if total > max_bytes:
            raise LimitError(f"file is larger than {max_bytes} bytes")
        if len(raw) > MAX_LINE_BYTES:
            raise LimitError(f"line {n} is longer than {MAX_LINE_BYTES} bytes")
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            raise InvalidEntryError(f"line {n}: not valid JSON") from None
        if header is None:
            if (not isinstance(obj, dict) or obj.get("format") != EXPORT_FORMAT
                    or isinstance(obj.get("version"), bool) or obj.get("version") != EXPORT_VERSION
                    or isinstance(obj.get("count"), bool)
                    or not isinstance(obj.get("count"), int) or obj["count"] < 0):
                raise InvalidEntryError("not a ragleap-memory export file of a supported version")
            header = obj
            continue
        if len(out) >= max_entries:
            raise LimitError(f"file has more than {max_entries} entries")
        out.append((n, obj))
    if header is None:
        raise InvalidEntryError("not a ragleap-memory export file of a supported version")
    if len(out) != header["count"]:
        raise InvalidEntryError("entry count does not match the header (truncated or edited file)")
    return out


class SQLiteStore:
    def __init__(self, path: str = ":memory:", *, audit: Optional[AuditLog] = None,
                 clock: Optional[Callable[[], datetime]] = None, max_text_bytes: int = 8192,
                 max_results: int = 50, quota_entries: Optional[int] = None, quota_tokens: Optional[int] = None,
                 token_estimator: Optional[Callable[[str], int]] = None,
                 embedder: Optional[Callable[[List[str]], Any]] = None, embedding_model: Optional[str] = None,
                 embedding_dim: Optional[int] = None, embed_quota_tokens: Optional[int] = None,
                 embed_batch_size: int = 64, max_vector_candidates: int = 5000):
        for name, q in (("quota_entries", quota_entries), ("quota_tokens", quota_tokens),
                        ("embed_quota_tokens", embed_quota_tokens)):
            if q is not None and (isinstance(q, bool) or not isinstance(q, int) or q < 1):
                raise InvalidEntryError(f"{name} must be a positive int or None")
        if token_estimator is not None and not callable(token_estimator):
            raise InvalidEntryError("token_estimator must be callable")
        if embedder is not None and not callable(embedder):
            raise InvalidEntryError("embedder must be callable")
        if (embedding_model is None) != (embedding_dim is None):
            raise InvalidEntryError("embedding_model and embedding_dim go together")
        if embedder is not None and embedding_model is None:
            raise InvalidEntryError("an embedder needs embedding_model and embedding_dim")
        if embedding_model is not None:
            if not isinstance(embedding_model, str) or not MODEL_RE.match(embedding_model):
                raise InvalidEntryError("embedding_model must be a short id such as 'text-embed-small-1'")
            if (isinstance(embedding_dim, bool) or not isinstance(embedding_dim, int)
                    or not 1 <= embedding_dim <= MAX_EMBEDDING_DIM):
                raise InvalidEntryError(f"embedding_dim must be an int from 1 to {MAX_EMBEDDING_DIM}")
        for name, v, high in (("embed_batch_size", embed_batch_size, 256), ("max_vector_candidates", max_vector_candidates, 100000)):
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= high:
                raise InvalidEntryError(f"{name} must be an int from 1 to {high}")
        self.quota_entries = quota_entries
        self.quota_tokens = quota_tokens
        self.embed_quota_tokens = embed_quota_tokens
        self._embedder = embedder
        self.embedding_model = embedding_model
        self.embedding_dim = embedding_dim
        self.embed_batch_size = embed_batch_size
        self.max_vector_candidates = max_vector_candidates
        self._estimator = token_estimator or estimate_tokens
        self.path = os.fspath(path)
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_text_bytes = max_text_bytes
        self.max_results = max_results
        self._lock = threading.RLock()
        if self.path != ":memory:" and not os.path.exists(self.path):
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.execute("PRAGMA secure_delete = ON")
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        if sqlite3.sqlite_version_info < (3, 24, 0):
            self._db.close()
            raise MemoryStoreError("SQLite 3.24 or newer is required (upserts); found %s" % sqlite3.sqlite_version)
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self._db.close()
            raise MemoryStoreError(f"database schema version {version} is newer than this library supports")
        try:
            self._db.executescript(_SCHEMA_V1)
        except sqlite3.OperationalError as e:
            self._db.close()
            raise MemoryStoreError("SQLite with FTS5 is required: %s" % e) from None
        try:
            self._migrate()
        except BaseException:
            self._db.close()
            raise

    def _estimate(self, text: str) -> int:
        try:
            n = self._estimator(text)
        except Exception:
            raise InvalidEntryError("token_estimator failed") from None
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise InvalidEntryError("token_estimator must return a positive int")
        return n

    def _migrate(self) -> None:
        """Bring the file to schema 3 in ONE transaction: a crash leaves the old schema or the complete new one."""
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            migrating = "tokens" not in {r["name"] for r in db.execute("PRAGMA table_info(memories)")}
            if migrating:
                db.execute("ALTER TABLE memories ADD COLUMN tokens INTEGER NOT NULL DEFAULT 0")
                last = 0
                while True:
                    rows = db.execute("SELECT rowid, text FROM memories WHERE rowid > ? ORDER BY rowid LIMIT 500",
                                      [last]).fetchall()
                    if not rows:
                        break
                    db.executemany("UPDATE memories SET tokens = ? WHERE rowid = ?",
                                   [(self._estimate(r["text"]), r["rowid"]) for r in rows])
                    last = rows[-1]["rowid"]
            for ddl in _V2_DDL:
                db.execute(ddl)
            if migrating:
                db.execute("INSERT INTO tenant_totals(tenant, entries, tokens)"
                           " SELECT tenant, COUNT(*), SUM(tokens) FROM memories GROUP BY tenant")
            usage_cols = {r["name"] for r in db.execute("PRAGMA table_info(usage)")}
            for col in ("embedded_tokens", "embed_calls"):
                if col not in usage_cols:
                    db.execute(f"ALTER TABLE usage ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
            for ddl in _V3_DDL:
                db.execute(ddl)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    # -- helpers -----------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _log(self, event: str, scope: Scope, data: Dict[str, Any]) -> None:
        if self._audit is not None:  # inside the transaction: if the audit write fails, the change is rolled back
            self._audit.append(event, data, scope)

    def _visible(self, db: sqlite3.Connection, scope: Scope, entry_id: str) -> sqlite3.Row:
        clause, params = _scope_clause(scope)
        row = db.execute(f"SELECT * FROM memories WHERE id = ? AND {clause}", [entry_id, *params]).fetchone()
        if row is None:
            raise NotFoundError("no such entry")
        return row

    def _validate_new(self, scope: Any, text: Any, kind: str, source: str, trust: str, importance: Any,
                      expires_at: Any, metadata: Any) -> "tuple[Scope, str | None, str]":
        scope = require_scope(scope)
        if kind not in KINDS:
            raise InvalidEntryError("invalid kind")
        if trust not in TRUSTS:
            raise InvalidEntryError("invalid trust")
        if not isinstance(source, str) or not SOURCE_RE.match(source):
            raise InvalidEntryError("invalid source")
        if isinstance(importance, bool) or not isinstance(importance, (int, float)) or not 0 <= importance <= 1:
            raise InvalidEntryError("importance must be a number from 0 to 1")
        if not isinstance(text, str) or not text.strip():
            raise InvalidEntryError("text must be a non-empty string")
        check_size(text, self.max_text_bytes, "text")
        exp = None
        if expires_at is not None:
            if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
                raise InvalidEntryError("expires_at must be a timezone-aware datetime")
            exp = _iso(expires_at)
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise InvalidEntryError("metadata must be a dict")
        check_json_shape(metadata, max_depth=8, max_nodes=1000)
        try:
            meta_json = json.dumps(metadata, sort_keys=True)
        except (TypeError, ValueError):
            raise InvalidEntryError("metadata must be JSON serializable") from None
        check_size(meta_json, 4096, "metadata")
        return scope, exp, meta_json

    def _check_quota(self, db: sqlite3.Connection, tenant: str, tokens: int) -> None:
        """Refuse a write that would take the tenant over a quota. Exactly at the quota is allowed."""
        if self.quota_entries is None and self.quota_tokens is None:
            return
        row = db.execute("SELECT entries, tokens FROM tenant_totals WHERE tenant = ?", [tenant]).fetchone()
        entries, stored = (row["entries"], row["tokens"]) if row is not None else (0, 0)
        if self.quota_entries is not None and entries + 1 > self.quota_entries:
            raise QuotaExceededError("entry quota reached for this tenant")
        if self.quota_tokens is not None and stored + tokens > self.quota_tokens:
            raise QuotaExceededError("token quota reached for this tenant")

    def _insert(self, db: sqlite3.Connection, scope: Scope, text: str, kind: str, source: str, trust: str,
                importance: float, exp: Optional[str], derived: Sequence[str], meta_json: str,
                created: Optional[str] = None) -> MemoryEntry:
        entry_id = uuid.uuid4().hex
        created = created or _iso(self._clock())
        tokens = self._estimate(text)
        self._check_quota(db, scope.tenant, tokens)
        db.execute(
            "INSERT INTO memories(id, tenant, subject, agent, session, kind, text, source, trust, importance,"
            " created_at, expires_at, superseded_by, derived_from, metadata, tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
            [entry_id, scope.tenant, scope.subject, scope.agent, scope.session, kind, text, source, trust,
             float(importance), created, exp, json.dumps(list(derived)), meta_json, tokens])
        return _row_to_entry(db.execute("SELECT * FROM memories WHERE id = ?", [entry_id]).fetchone())

    # -- public API --------------------------------------------------------------------------------

    def add(self, scope: Scope, text: str, *, kind: str = "fact", source: str = "api", trust: str = "untrusted",
            importance: float = 0.5, expires_at: Optional[datetime] = None, derived_from: Iterable[str] = (),
            metadata: Optional[Dict[str, Any]] = None) -> MemoryEntry:
        """Store an entry in exactly `scope`. trust defaults to the lowest level ("untrusted"): say
        "model" or "owner" explicitly. Every id in derived_from must be visible to `scope`."""
        scope, exp, meta_json = self._validate_new(scope, text, kind, source, trust, importance, expires_at, metadata)
        derived = list(derived_from)
        if len(derived) > 100 or len(set(derived)) != len(derived) or not all(isinstance(d, str) and ID_RE.match(d) for d in derived):
            raise InvalidEntryError("derived_from must be up to 100 distinct entry ids")
        with self._tx() as db:
            for d in derived:
                self._visible(db, scope, d)
            entry = self._insert(db, scope, text, kind, source, trust, importance, exp, derived, meta_json)
            self._log("memory.add", scope, {"id": entry.id, "kind": kind, "trust": trust})
        return entry

    def get(self, scope: Scope, entry_id: str) -> MemoryEntry:
        scope = require_scope(scope)
        with self._lock:
            return _row_to_entry(self._visible(self._db, scope, entry_id))

    def count(self, scope: Scope) -> int:
        scope = require_scope(scope)
        clause, params = _scope_clause(scope)
        with self._lock:
            return self._db.execute(f"SELECT COUNT(*) FROM memories WHERE {clause}", params).fetchone()[0]

    def search(self, scope: Scope, query: str, *, k: int = 5, kinds: Optional[Sequence[str]] = None,
               min_trust: str = "untrusted", include_superseded: bool = False,
               include_expired: bool = False, max_tokens: Optional[int] = None, mode: str = "keyword",
               query_vector: Optional[Sequence[float]] = None) -> List[SearchHit]:
        """Search the entries `scope` can see.

        mode="keyword" (default): FTS5 bm25 keyword search, exactly as in 0.2.0.
        mode="vector": cosine similarity between the query and the stored vectors of this store's embedding
        model, scanning at most `max_vector_candidates` entries (most recent first, after all filters).
        mode="hybrid": reciprocal rank fusion (k=60) of the keyword and vector results; entries without a
        vector can still be found by keyword.

        Vector and hybrid need embedding_model and embedding_dim, and either an embedder (one embedder call
        for the query, counted against embed_quota_tokens) or a query_vector. kinds, min_trust, superseded,
        expired, k and max_tokens work the same in every mode. hit.score is bm25-based for keyword, the cosine
        for vector and the fused rank score for hybrid."""
        if mode not in SEARCH_MODES:
            raise InvalidEntryError("mode must be 'keyword', 'vector' or 'hybrid'")
        if mode == "keyword":
            if query_vector is not None:
                raise InvalidEntryError("query_vector needs mode 'vector' or 'hybrid'")
            return self._keyword_search(scope, query, k=k, kinds=kinds, min_trust=min_trust,
                                        include_superseded=include_superseded, include_expired=include_expired,
                                        max_tokens=max_tokens, record=True)
        return self._vector_search(scope, query, k=k, kinds=kinds, min_trust=min_trust,
                                   include_superseded=include_superseded, include_expired=include_expired,
                                   max_tokens=max_tokens, query_vector=query_vector, hybrid=(mode == "hybrid"))

    def _keyword_search(self, scope: Scope, query: str, *, k: int, kinds: Optional[Sequence[str]],
                        min_trust: str, include_superseded: bool, include_expired: bool,
                        max_tokens: Optional[int], record: bool) -> List[SearchHit]:
        """Keyword search (FTS5, bm25) over the entries `scope` can see. The query is split into
        words; any word may match. Operators in the query are treated as plain words.

        max_tokens: a hard budget on the estimated tokens of the returned entries, however large the store
        is. Candidates are taken in rank order; one that does not fit what is left is skipped and the next
        is tried. The budget bounds tokens, not time. Every search records the tokens it served (see usage)."""
        scope = require_scope(scope)
        if not isinstance(query, str):
            raise InvalidEntryError("query must be a string")
        check_size(query, 1000, "query")
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise InvalidEntryError("k must be a positive int")
        k = min(k, self.max_results)
        if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1):
            raise InvalidEntryError("max_tokens must be a positive int or None")
        if min_trust not in TRUSTS:
            raise InvalidEntryError("invalid min_trust")
        kinds = tuple(kinds) if kinds is not None else KINDS
        if not kinds or any(x not in KINDS for x in kinds):
            raise InvalidEntryError("invalid kinds")
        words = _TOKEN.findall(query)[:32]
        if not words:
            if record:
                self._record_served(scope.tenant, 0)
            return []
        match = " OR ".join('"%s"' % t for t in words)
        clause, params = _scope_clause(scope, "m")
        trusts = [t for t in TRUSTS if trust_rank(t) >= trust_rank(min_trust)]
        sql = (f"SELECT m.*, bm25(memories_fts) AS score FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid"
               f" WHERE memories_fts MATCH ? AND {clause}"
               f" AND m.kind IN ({','.join('?' * len(kinds))}) AND m.trust IN ({','.join('?' * len(trusts))})")
        args: list = [match, *params, *kinds, *trusts]
        if not include_superseded:
            sql += " AND m.superseded_by IS NULL"
        if not include_expired:
            sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"
            args.append(_iso(self._clock()))
        sql += " ORDER BY score, m.created_at DESC LIMIT ?"
        args.append(self.max_results if max_tokens is not None else k)  # a budget may skip entries: look further
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        hits: List[SearchHit] = []
        used = 0
        for r in rows:
            if len(hits) >= k:
                break
            if max_tokens is not None and used + r["tokens"] > max_tokens:
                continue
            used += r["tokens"]
            hits.append(SearchHit(_row_to_entry(r), -float(r["score"]), r["tokens"]))
        if record:
            self._record_served(scope.tenant, used)
        return hits

    def _period(self, period: Optional[str]) -> str:
        if period is None:
            return self._clock().astimezone(timezone.utc).strftime("%Y-%m-%d")
        if not isinstance(period, str) or not _PERIOD.match(period):
            raise InvalidEntryError("period must be a UTC day like 2026-10-31")
        try:
            datetime.strptime(period, "%Y-%m-%d")
        except ValueError:
            raise InvalidEntryError("period must be a UTC day like 2026-10-31") from None
        return period

    def _record_served(self, tenant: str, tokens: int) -> None:
        period = self._period(None)
        with self._tx() as db:
            db.execute("INSERT INTO usage(tenant, period, served_tokens, searches) VALUES (?, ?, ?, 1)"
                       " ON CONFLICT(tenant, period) DO UPDATE SET served_tokens = served_tokens + excluded.served_tokens,"
                       " searches = searches + 1", [tenant, period, tokens])

    def usage(self, scope: Scope, period: Optional[str] = None) -> Dict[str, Any]:
        """What the scope stores now (entries and estimated tokens, for the entries it covers) and what
        searches served on one UTC day (`period` like "2026-10-31", default today). The served counters are
        kept per tenant, not per narrower scope."""
        scope = require_scope(scope)
        period = self._period(period)
        clause, params = _scope_clause(scope)
        with self._lock:
            n, stored = self._db.execute(f"SELECT COUNT(*), COALESCE(SUM(tokens), 0) FROM memories WHERE {clause}",
                                         params).fetchone()
            row = self._db.execute("SELECT served_tokens, searches, embedded_tokens, embed_calls FROM usage"
                                   " WHERE tenant = ? AND period = ?", [scope.tenant, period]).fetchone()
        return {"entries": n, "stored_tokens": stored, "period": period,
                "served_tokens": row["served_tokens"] if row else 0, "searches": row["searches"] if row else 0,
                "embedded_tokens": row["embedded_tokens"] if row else 0,
                "embed_calls": row["embed_calls"] if row else 0}

    # -- embeddings (0.3.0) ------------------------------------------------------------------------

    def _need_embedding_config(self) -> None:
        if self.embedding_model is None:
            raise InvalidEntryError("this store has no embedding_model and embedding_dim")

    def _reserve_embed(self, tenant: str, tokens: int) -> None:
        """Count one embedder call and its tokens on the tenant's UTC day BEFORE the embedder is called, and
        refuse if that would take the tenant over embed_quota_tokens. A call that then fails stays counted."""
        period = self._period(None)
        with self._tx() as db:
            if self.embed_quota_tokens is not None:
                row = db.execute("SELECT embedded_tokens FROM usage WHERE tenant = ? AND period = ?",
                                 [tenant, period]).fetchone()
                if (row["embedded_tokens"] if row else 0) + tokens > self.embed_quota_tokens:
                    raise QuotaExceededError("embedding quota reached for this tenant today")
            db.execute("INSERT INTO usage(tenant, period, served_tokens, searches, embedded_tokens, embed_calls)"
                       " VALUES (?, ?, 0, 0, ?, 1) ON CONFLICT(tenant, period) DO UPDATE SET"
                       " embedded_tokens = embedded_tokens + excluded.embedded_tokens,"
                       " embed_calls = embed_calls + 1", [tenant, period, tokens])

    def _call_embedder(self, texts: List[str]) -> "List[array[float]]":
        assert self._embedder is not None and self.embedding_dim is not None
        try:
            out = self._embedder(list(texts))
        except Exception:
            raise EmbedderError("the embedder failed") from None
        try:
            if len(out) != len(texts):
                raise InvalidEntryError("wrong number of vectors")
            return [_unit_vector(v, self.embedding_dim) for v in out]
        except (InvalidEntryError, TypeError):
            raise EmbedderError("the embedder returned an unusable result") from None

    def embed_pending(self, scope: Scope, *, limit: int = 100, dry_run: bool = False,
                      replace_stale: bool = False) -> EmbedResult:
        """Give a vector to up to `limit` entries `scope` can see that have none for this store's embedding
        model (most recent first; superseded and expired entries are skipped). replace_stale=True also
        re-embeds entries whose vector came from another model or size.

        If embed_quota_tokens is set, only as many entries as fit in what is left of the tenant's quota for
        today are embedded (see `remaining` in the result); if not even one fits, QuotaExceededError is raised
        before the embedder is called. dry_run=True changes nothing, calls nothing and reports what would be
        sent. The texts of the entries are sent to YOUR embedder function; where it sends them is up to it."""
        scope = require_scope(scope)
        self._need_embedding_config()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise InvalidEntryError("limit must be an int from 1 to 1000")
        if not isinstance(dry_run, bool) or not isinstance(replace_stale, bool):
            raise InvalidEntryError("dry_run and replace_stale must be True or False")
        if self._embedder is None and not dry_run:
            raise InvalidEntryError("this store has no embedder")
        clause, params = _scope_clause(scope, "m")
        stale = " OR e.model != ? OR e.dim != ?" if replace_stale else ""
        base = (f"FROM memories m LEFT JOIN embeddings e ON e.rowid = m.rowid WHERE {clause}"
                f" AND m.superseded_by IS NULL AND (m.expires_at IS NULL OR m.expires_at > ?)"
                f" AND (e.rowid IS NULL{stale})")
        args: list = [*params, _iso(self._clock())]
        if replace_stale:
            args += [self.embedding_model, self.embedding_dim]
        with self._lock:
            rows = self._db.execute("SELECT m.rowid AS rid, m.text AS text, m.tokens AS tokens " + base
                                    + " ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?", [*args, limit]).fetchall()
            pending = self._db.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
            used = self._db.execute("SELECT embedded_tokens FROM usage WHERE tenant = ? AND period = ?",
                                    [scope.tenant, self._period(None)]).fetchone()
        chosen = list(rows)
        if self.embed_quota_tokens is not None:
            room = self.embed_quota_tokens - (used["embedded_tokens"] if used else 0)
            fits, total = [], 0
            for r in chosen:
                if total + r["tokens"] > room:
                    break
                fits.append(r)
                total += r["tokens"]
            if chosen and not fits and not dry_run:
                raise QuotaExceededError("embedding quota reached for this tenant today")
            chosen = fits
        batches = -(-len(chosen) // self.embed_batch_size)
        if dry_run:
            return EmbedResult(len(chosen), sum(r["tokens"] for r in chosen), batches, pending - len(chosen), True)
        embedded = spent = calls = 0
        for i in range(0, len(chosen), self.embed_batch_size):
            batch = chosen[i:i + self.embed_batch_size]
            batch_tokens = sum(r["tokens"] for r in batch)
            self._reserve_embed(scope.tenant, batch_tokens)
            spent += batch_tokens
            calls += 1
            vectors = self._call_embedder([r["text"] for r in batch])
            with self._tx() as db:
                stored = 0
                for r, v in zip(batch, vectors):
                    stored += db.execute(
                        "INSERT OR REPLACE INTO embeddings(rowid, model, dim, vec) SELECT ?, ?, ?, ?"
                        " WHERE EXISTS (SELECT 1 FROM memories WHERE rowid = ?)",
                        [r["rid"], self.embedding_model, self.embedding_dim, _to_blob(v), r["rid"]]).rowcount
                self._log("memory.embed", scope, {"count": stored, "tokens": batch_tokens})
            embedded += stored
        return EmbedResult(embedded, spent, calls, pending - len(chosen), False)

    def _vector_search(self, scope: Scope, query: str, *, k: int, kinds: Optional[Sequence[str]], min_trust: str,
                       include_superseded: bool, include_expired: bool, max_tokens: Optional[int],
                       query_vector: Optional[Sequence[float]], hybrid: bool) -> List[SearchHit]:
        scope = require_scope(scope)
        self._need_embedding_config()
        assert self.embedding_dim is not None
        if not isinstance(query, str):
            raise InvalidEntryError("query must be a string")
        check_size(query, MAX_QUERY_BYTES, "query")
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise InvalidEntryError("k must be a positive int")
        top = min(k, self.max_results)
        if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1):
            raise InvalidEntryError("max_tokens must be a positive int or None")
        if min_trust not in TRUSTS:
            raise InvalidEntryError("invalid min_trust")
        wanted_kinds = tuple(kinds) if kinds is not None else KINDS
        if not wanted_kinds or any(x not in KINDS for x in wanted_kinds):
            raise InvalidEntryError("invalid kinds")
        floor = trust_rank(min_trust)
        trusts = [t for t in TRUSTS if trust_rank(t) >= floor]
        if query_vector is not None:
            qvec = _unit_vector(query_vector, self.embedding_dim)
        elif not query.strip():
            self._record_served(scope.tenant, 0)
            return []
        elif self._embedder is None:
            raise InvalidEntryError("vector search needs an embedder or a query_vector")
        else:
            self._reserve_embed(scope.tenant, self._estimate(query))
            qvec = self._call_embedder([query])[0]
        clause, params = _scope_clause(scope, "m")
        sql = ("SELECT m.*, e.vec AS vec FROM embeddings e JOIN memories m ON m.rowid = e.rowid"
               f" WHERE e.model = ? AND e.dim = ? AND {clause}"
               f" AND m.kind IN ({','.join('?' * len(wanted_kinds))}) AND m.trust IN ({','.join('?' * len(trusts))})")
        args: list = [self.embedding_model, self.embedding_dim, *params, *wanted_kinds, *trusts]
        if not include_superseded:
            sql += " AND m.superseded_by IS NULL"
        if not include_expired:
            sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"
            args.append(_iso(self._clock()))
        sql += " ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?"
        args.append(self.max_vector_candidates)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        scored = []
        for r in rows:
            if len(r["vec"]) != 4 * self.embedding_dim:
                continue
            scored.append((sum(map(mul, qvec, _from_blob(r["vec"]))), r))
        scored.sort(key=lambda s: -s[0])  # stable: equal scores stay newest first
        ranked: "List[tuple[float, MemoryEntry, int]]" = [(s, _row_to_entry(r), r["tokens"]) for s, r in scored]
        if hybrid:
            fused: "Dict[str, list]" = {}
            keyword = self._keyword_search(scope, query, k=self.max_results, kinds=kinds, min_trust=min_trust,
                                           include_superseded=include_superseded, include_expired=include_expired,
                                           max_tokens=None, record=False)
            for rank, h in enumerate(keyword, 1):
                fused[h.entry.id] = [1.0 / (RRF_K + rank), h.entry, h.tokens]
            for rank, (_, entry, tokens) in enumerate(ranked[:self.max_results], 1):
                if entry.id in fused:
                    fused[entry.id][0] += 1.0 / (RRF_K + rank)
                else:
                    fused[entry.id] = [1.0 / (RRF_K + rank), entry, tokens]
            merged = list(fused.values())
            merged.sort(key=lambda f: (f[1].created_at, f[1].id), reverse=True)
            merged.sort(key=lambda f: -f[0])
            ranked = [(f[0], f[1], f[2]) for f in merged]
        hits: List[SearchHit] = []
        used = 0
        for score, entry, tokens in ranked:
            if len(hits) >= top:
                break
            if max_tokens is not None and used + tokens > max_tokens:
                continue
            used += tokens
            hits.append(SearchHit(entry, float(score), tokens))
        self._record_served(scope.tenant, used)
        return hits

    def supersede(self, scope: Scope, old_id: str, text: str, *, kind: Optional[str] = None, source: str = "api",
                  trust: str = "untrusted", importance: Optional[float] = None,
                  expires_at: Optional[datetime] = None, metadata: Optional[Dict[str, Any]] = None) -> MemoryEntry:
        """Replace an entry whose content changed. The new entry gets the old entry's exact scope; the
        old one stays in the store (see export, include_superseded) and points at its replacement."""
        scope = require_scope(scope)
        with self._tx() as db:
            old = self._visible(db, scope, old_id)
            if old["superseded_by"] is not None:
                raise InvalidEntryError("entry is already superseded")
            old_scope = Scope(old["tenant"], old["subject"], old["agent"], old["session"])
            kind = kind or old["kind"]
            importance = old["importance"] if importance is None else importance
            _, exp, meta_json = self._validate_new(old_scope, text, kind, source, trust, importance, expires_at, metadata)
            new = self._insert(db, old_scope, text, kind, source, trust, importance, exp, [old_id], meta_json)
            db.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", [new.id, old_id])
            self._log("memory.supersede", scope, {"old": old_id, "new": new.id})
        return new

    def forget(self, scope: Scope, ids: Iterable[str]) -> int:
        """Delete the listed entries that `scope` can see; returns how many were deleted. They stop
        appearing in searches at once, but traces can stay in the database file until compact()
        or erase() runs."""
        scope = require_scope(scope)
        ids = list(ids)
        if len(ids) > 1000 or not all(isinstance(i, str) and ID_RE.match(i) for i in ids):
            raise InvalidEntryError("ids must be up to 1000 entry ids")
        if not ids:
            return 0
        clause, params = _scope_clause(scope)
        with self._tx() as db:
            n = db.execute(f"DELETE FROM memories WHERE id IN ({','.join('?' * len(ids))}) AND {clause}",
                           [*ids, *params]).rowcount
            self._log("memory.forget", scope, {"count": n})
        return n

    def export(self, scope: Scope) -> List[Dict[str, Any]]:
        """Every entry `scope` can see (superseded and expired ones too) as plain dicts."""
        scope = require_scope(scope)
        clause, params = _scope_clause(scope)
        with self._tx() as db:
            rows = db.execute(f"SELECT * FROM memories WHERE {clause} ORDER BY created_at, id", params).fetchall()
            self._log("memory.export", scope, {"count": len(rows)})
        return [_row_to_entry(r).to_dict() for r in rows]

    # -- download and upload ------------------------------------------------------------------------

    def export_file(self, scope: Scope, path: "str | os.PathLike[str]", *, encrypt_with: Any = None,
                    passphrase: Any = None, context: "str | bytes | None" = None) -> int:
        """Write every entry `scope` can see (superseded and expired too) to a JSON Lines file. Returns the
        number of entries. The file is created with mode 0600 and must not exist yet.

        Without `encrypt_with` or `passphrase` the file is PLAIN TEXT: anyone who can read it can read the memory.
        Guard it like the database itself. With a Key or KeyRing (the active key is used) or a passphrase, every
        entry is encrypted by ragleap-security (needs ragleap-memory[crypto]); give one of them, never both.
        `context` (at most 1,024 bytes) is also authenticated: the same value is needed to import. This protects
        the export FILE only, not the database."""
        encrypted = exportcrypto.check_args("encrypt_with", encrypt_with, passphrase, context)
        if not encrypted:
            entries = self.export(scope)
            plan = None
        else:
            entries = self._export_encrypted_rows(scope)
            plan = exportcrypto.prepare_export(len(entries), encrypt_with, passphrase, context)
        fd = os.open(os.fspath(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            if plan is None:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps({"format": EXPORT_FORMAT, "version": EXPORT_VERSION, "count": len(entries)},
                                       sort_keys=True) + "\n")
                    for e in entries:
                        f.write(json.dumps(e, sort_keys=True) + "\n")
            else:
                with os.fdopen(fd, "wb") as f:
                    f.write(plan.header_line)
                    for i, e in enumerate(entries, 1):
                        f.write(plan.line(i, e))
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(os.fspath(path))
            raise
        return len(entries)

    def _export_encrypted_rows(self, scope: Scope) -> List[Dict[str, Any]]:
        scope = require_scope(scope)
        clause, params = _scope_clause(scope)
        with self._tx() as db:
            rows = db.execute(f"SELECT * FROM memories WHERE {clause} ORDER BY created_at, id", params).fetchall()
            self._log("memory.export", scope, {"count": len(rows), "encrypted": True})
        return [_row_to_entry(r).to_dict() for r in rows]

    def import_file(self, scope: Scope, path: "str | os.PathLike[str]", **options: Any) -> ImportResult:
        """Import an export file (plain, or encrypted with `decrypt_with` or `passphrase`). See `import_stream`."""
        with open(os.fspath(path), "rb") as f:
            return self.import_stream(scope, f, **options)

    def import_stream(self, scope: Scope, stream: Any, *, max_trust: str = "untrusted", rescope: bool = False,
                      skip_duplicates: bool = True, max_entries: int = 100_000,
                      max_bytes: int = 64 * 1024 * 1024, decrypt_with: Any = None, passphrase: Any = None,
                      context: "str | bytes | None" = None) -> ImportResult:
        """Import entries from a binary stream of an export file (for example an uploaded file).

        An uploaded file is untrusted input, so:
        - every entry is checked like a new `add` (kind, size, metadata, importance...);
        - an entry's trust label is capped at `max_trust` (default "untrusted"): a file cannot promote itself.
          Pass a higher `max_trust` only when YOU know where the file came from;
        - every entry must lie inside `scope`; one that does not stops the import. With `rescope=True` the
          file's tenant is replaced by `scope`'s tenant first (narrower parts are kept);
        - entries get new ids; supersede/derived links are remapped, and links that point outside the file or
          across scopes are dropped (counted in the result);
        - an entry with the same scope, kind and text as one already stored is skipped (`skip_duplicates`);
        - the whole import is ONE transaction: any error, including a quota, imports nothing;
        - limits: `max_entries`, `max_bytes`, 64 KiB per line.
        Encrypted files (export format version 2): give the Key or KeyRing as `decrypt_with`, or the `passphrase`,
        and the same `context` that was used to export. An encrypted file is refused without one of them, and a
        plain file is refused when one is given (no downgrade). The lines are decrypted first, then every rule
        above applies unchanged: decrypting does not make a file trusted. A file on disk may be up to twice
        `max_bytes`; the decrypted data obeys `max_bytes`.
        Error messages name the line number, never entry text."""
        scope = require_scope(scope)
        if max_trust not in TRUSTS:
            raise InvalidEntryError("invalid max_trust")
        for name, v in (("max_entries", max_entries), ("max_bytes", max_bytes)):
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise InvalidEntryError(f"{name} must be a positive int")
        stream, encrypted = exportcrypto.open_import(stream, decrypt_with, passphrase, context, max_entries, max_bytes)
        items = [self._prepare_import(n, obj, scope, max_trust, rescope)
                 for n, obj in _read_export(stream, max_entries, max_bytes)]
        imported = skipped = dropped = 0
        idmap: Dict[str, str] = {}
        scope_of: Dict[str, Scope] = {}
        fresh = []
        with self._tx() as db:
            for it in items:
                sc = it["scope"]
                existing = None
                if skip_duplicates:
                    existing = db.execute(
                        "SELECT id FROM memories WHERE tenant = ? AND subject IS ? AND agent IS ? AND session IS ?"
                        " AND kind = ? AND text = ? LIMIT 1",
                        [sc.tenant, sc.subject, sc.agent, sc.session, it["kind"], it["text"]]).fetchone()
                if existing is not None:
                    skipped += 1
                    new_id = existing["id"]
                else:
                    new_id = self._insert(db, sc, it["text"], it["kind"], it["source"], it["trust"],
                                          it["importance"], it["exp"], [], it["meta_json"],
                                          created=it["created"]).id
                    imported += 1
                    fresh.append((new_id, it))
                scope_of[new_id] = sc
                if it["old_id"] is not None:
                    idmap[it["old_id"]] = new_id
            for new_id, it in fresh:
                sc = it["scope"]
                derived = []
                for d in it["derived"]:
                    mapped = idmap.get(d)
                    if mapped is None or mapped == new_id or mapped in derived or not sc.covers(scope_of[mapped]):
                        dropped += 1
                    else:
                        derived.append(mapped)
                sup = None
                if it["superseded_by"] is not None:
                    mapped = idmap.get(it["superseded_by"])
                    if mapped is None or mapped == new_id or scope_of[mapped] != sc:
                        dropped += 1
                    else:
                        sup = mapped
                if derived or sup is not None:
                    db.execute("UPDATE memories SET derived_from = ?, superseded_by = ? WHERE id = ?",
                               [json.dumps(derived), sup, new_id])
            if encrypted:
                self._log("memory.import", scope, {"imported": imported, "skipped": skipped, "encrypted": True})
            else:
                self._log("memory.import", scope, {"imported": imported, "skipped": skipped})
        return ImportResult(imported, skipped, dropped)

    def _prepare_import(self, n: int, obj: Any, target: Scope, max_trust: str, rescope: bool) -> Dict[str, Any]:
        def bad(msg: str) -> InvalidEntryError:
            return InvalidEntryError(f"line {n}: {msg}")
        if not isinstance(obj, dict):
            raise bad("entry is not an object")
        keys = set(obj)
        if not keys <= _EXPORT_KEYS:
            raise bad("entry has unknown fields")
        if not _EXPORT_REQUIRED <= keys:
            raise bad("entry is missing scope, kind or text")
        if not isinstance(obj["scope"], str):
            raise bad("invalid scope")
        try:
            sc = Scope.parse(obj["scope"])
        except ValueError:
            raise bad("invalid scope") from None
        if rescope:
            sc = Scope(target.tenant, sc.subject, sc.agent, sc.session)
        if not target.covers(sc):
            raise bad("entry is outside the import scope")
        trust = obj.get("trust", "untrusted")
        if trust not in TRUSTS:
            raise bad("invalid trust")
        if trust_rank(trust) > trust_rank(max_trust):
            trust = max_trust

        def when(field: str) -> Optional[datetime]:
            v = obj.get(field)
            if v is None:
                return None
            try:
                d = datetime.fromisoformat(v)
            except (TypeError, ValueError):
                raise bad(f"invalid {field}") from None
            if d.tzinfo is None:
                raise bad(f"{field} must have a timezone")
            return d
        exp_dt, created_dt = when("expires_at"), when("created_at")
        try:
            sc, exp, meta_json = self._validate_new(sc, obj["text"], obj["kind"], obj.get("source", "import"), trust,
                                                    obj.get("importance", 0.5), exp_dt, obj.get("metadata"))
        except LimitError as e:       # keep the type; messages never hold entry text
            raise LimitError(f"line {n}: {e}") from None
        except InvalidEntryError as e:
            raise bad(str(e)) from None
        old_id = obj.get("id")
        if old_id is not None and (not isinstance(old_id, str) or not ID_RE.match(old_id)):
            raise bad("invalid id")
        derived = obj.get("derived_from", [])
        if (not isinstance(derived, list) or len(derived) > 100
                or not all(isinstance(d, str) and ID_RE.match(d) for d in derived)):
            raise bad("invalid derived_from")
        sup = obj.get("superseded_by")
        if sup is not None and (not isinstance(sup, str) or not ID_RE.match(sup)):
            raise bad("invalid superseded_by")
        return {"scope": sc, "kind": obj["kind"], "text": obj["text"], "source": obj.get("source", "import"),
                "trust": trust, "importance": obj.get("importance", 0.5), "exp": exp, "meta_json": meta_json,
                "created": _iso(created_dt) if created_dt is not None else None, "old_id": old_id,
                "derived": derived, "superseded_by": sup}

    def erase(self, scope: Scope) -> int:
        """Delete everything `scope` can see, then rewrite the search index and checkpoint the
        journal so the deleted text is no longer in the database file. Returns the number deleted."""
        scope = require_scope(scope)
        clause, params = _scope_clause(scope)
        with self._tx() as db:
            n = db.execute(f"DELETE FROM memories WHERE {clause}", params).rowcount
            if scope.subject is None and scope.agent is None and scope.session is None:
                db.execute("DELETE FROM usage WHERE tenant = ?", [scope.tenant])  # tenant-wide: forget the counters too
            self._log("memory.erase", scope, {"count": n})
        self.compact()
        return n

    def sweep_expired(self, scope: Scope) -> int:
        """Delete expired entries inside `scope`; returns how many."""
        scope = require_scope(scope)
        clause, params = _scope_clause(scope)
        with self._tx() as db:
            n = db.execute(f"DELETE FROM memories WHERE {clause} AND expires_at IS NOT NULL AND expires_at <= ?",
                           [*params, _iso(self._clock())]).rowcount
            self._log("memory.sweep", scope, {"count": n})
        return n

    def compact(self) -> None:
        """Rewrite the full-text index and checkpoint the journal. Needed to remove deleted text from
        the file; erase() calls it. Can take a while on a large store."""
        with self._lock:
            self._db.execute("INSERT INTO memories_fts(memories_fts) VALUES ('optimize')")
            if self.path != ":memory:":
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        with self._lock:
            self._db.close()
