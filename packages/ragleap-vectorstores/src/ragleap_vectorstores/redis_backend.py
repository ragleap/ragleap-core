"""
Redis-backed vector storage for ragleap-vectorstores (RediSearch/Redis Stack).

Live-verified against the actually installed redis-py==7.3.0 package and a
real local Redis Stack instance (redis/redis-stack-server, RediSearch module
v21020) - every import path, constructor signature, and query syntax below
was introspected and exercised for real before being written here, not
assumed from tutorials or documentation. Two things that differ from what
most Redis vector-search tutorials show:

- `IndexDefinition`/`IndexType` live at `redis.commands.search.index_definition`
  in this installed version, NOT `redis.commands.search.indexDefinition` or
  `.definition` as older/newer docs sometimes show. Verified via
  pkgutil.iter_modules() against the real installed package.
- `Query` takes only a query_string in its constructor; dialect, paging,
  sorting, and return fields are all chained methods. KNN vector search is
  expressed as RediSearch query-string syntax
  (`*=>[KNN k @field $vec AS score]`) plus an explicit `.dialect(2)` call -
  live-verified: omitting dialect(2) does not raise an error but silently
  returns 0 results in some redis-py/RediSearch version combinations, so it
  is always set explicitly here rather than relying on a default.

Design notes:
- redis_url is REQUIRED (e.g. "redis://localhost:6380/0"). This backend
  targets its own Redis instance/db, separate from any Redis already used
  for caching/Celery elsewhere in a deployment - callers are responsible
  for pointing this at a Redis Stack instance (with the `search` module
  loaded); a plain Redis without RediSearch will fail at init_schema()
  with a clear error rather than a confusing one at query time.
- Chunk data (text, vector, document_id, chunk_index, token_count) is
  stored natively in a Redis HASH per chunk - no SQLite sidecar needed for
  chunks, same "no sidecar needed" pattern as Chroma/LanceDB. Arbitrary
  extra metadata IS still stored (as a JSON string field) so it round-trips
  through search_dense()'s results, but see the filtering limitation below.
- Document registry (for list_documents/delete_document/get_document_filename)
  uses a small SQLite sidecar, same pattern as ChromaBackend - Redis HASHes
  have no native "ORDER BY uploaded_at, LIMIT/OFFSET" concept as clean as
  SQL, and duplicating that logic in Redis (sorted sets, etc.) would add
  real complexity for no benefit over the sidecar approach already proven
  in ChromaBackend.
- KNOWN LIMITATION, stated honestly rather than silently ignored: RediSearch
  requires fields to be declared in the index schema up front - unlike
  Chroma's `where=` which accepts arbitrary metadata keys, this backend can
  only filter search_dense() by `document_id` and `tenant_id` (both TAG fields).
  Any other key in metadata_filter raises ValueError: such keys used to be
  ignored silently, which is unsafe for tenant isolation. An index created
  before tenant_id existed has no such field; filtering by tenant_id on it
  raises RuntimeError (use a new index_name).
- TAG field values must have RediSearch's special characters escaped in
  query strings (verified: unescaped hyphens in a UUID document_id inside a
  TAG query silently return zero results rather than erroring, since RediSearch
  treats them as query syntax rather than literal characters). _escape_tag()
  below handles this for every TAG-field query.
- supports_sparse() returns False. RediSearch *does* support real full-text
  search via its TextField/TFIDF and BM25 scorers, but that surface has not
  been implemented or tested here, so - same rule as LanceDB's tantivy note -
  it is honestly reported as unsupported rather than assumed to work.
"""
import datetime
import json
import logging
import os
import sqlite3
import threading
from typing import Dict, List, Optional

import numpy as np

from ragleap.vectorstores.base import VectorBackend

logger = logging.getLogger(__name__)

_TAG_SPECIAL_CHARS = r',.<>{}[]"\':;!@#$%^&*()-+=~| '


def _escape_tag(value: str) -> str:
    """Escape RediSearch TAG special characters. Live-verified: an
    un-escaped UUID document_id (which contains hyphens) inside a TAG
    query silently returns zero matches rather than erroring, because
    RediSearch parses the hyphen as query syntax, not a literal character."""
    return "".join(f"\\{c}" if c in _TAG_SPECIAL_CHARS else c for c in str(value))


class RedisBackend(VectorBackend):
    def __init__(
        self,
        redis_url: str,
        index_name: str = "ragleap_idx",
        key_prefix: Optional[str] = None,
        registry_path: Optional[str] = None,
    ):
        try:
            import redis  # noqa: F401
            from redis.commands.search.field import VectorField  # noqa: F401
            from redis.commands.search.index_definition import IndexDefinition, IndexType  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "RedisBackend requires the 'redis' extra: "
                "pip install ragleap-vectorstores[redis]"
            ) from e

        if not redis_url:
            raise ValueError(
                "RedisBackend requires redis_url= - e.g. "
                "'redis://localhost:6379/0'. This must point at a Redis "
                "Stack instance (or plain Redis with the RediSearch module "
                "loaded) - init_schema() will raise a clear error if the "
                "'search' module is not available."
            )

        self.redis_url = redis_url
        self.index_name = index_name
        # REAL BUG FIX, live-verified: key_prefix used to default to the
        # same literal string ("ragleap:chunk:") regardless of index_name.
        # Two RedisBackend instances with different index_name but both
        # left at the default key_prefix would silently share the same
        # Redis keyspace - creating a second index over that prefix makes
        # RediSearch auto-index every existing key there, including chunks
        # inserted by a completely unrelated earlier instance. Reproduced
        # live: three separate verification runs with different
        # index_names all wrote into "ragleap:chunk:*", and a fresh
        # top_k=1 KNN query on the newest instance returned a stale chunk
        # from an unrelated earlier run. Default now derives from
        # index_name so two different indices never collide unless the
        # caller explicitly passes the same key_prefix on purpose.
        self.key_prefix = key_prefix or f"ragleap:{index_name}:chunk:"
        self._client = None
        self._dimensions = None
        self._tenant_field = True  # False only when an existing index was created without the tenant_id field
        self._lock = threading.Lock()

        registry_path = registry_path or os.path.join(
            os.path.expanduser("~"), ".ragleap_vectorstores", "redis_documents.sqlite3"
        )
        os.makedirs(os.path.dirname(registry_path), exist_ok=True)
        self._sqlite_path = registry_path
        self._conn = sqlite3.connect(self._sqlite_path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL,
                metadata TEXT NOT NULL, uploaded_at TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def _vector_key(self, document_id: str, chunk_index: int) -> str:
        return f"{self.key_prefix}{document_id}:{chunk_index}"

    def init_schema(self, dimensions: int) -> None:
        import redis
        from redis.commands.search.field import NumericField, TagField, TextField, VectorField
        from redis.commands.search.index_definition import IndexDefinition, IndexType

        self._dimensions = dimensions
        self._client = redis.from_url(self.redis_url, decode_responses=False)

        # MODULE LIST's response shape is NOT stable - live-verified twice
        # against this exact client construction (redis.from_url()) on the
        # same server instance, with the same redis-py version, returning
        # two different shapes on different occasions:
        #   1) a list of dicts with bytes keys: {b"name": b"search", ...}
        #   2) a list of flat key-value lists: [b"name", b"search", ...]
        # This is almost certainly RESP2/RESP3 protocol negotiation
        # happening beneath redis-py (map replies parse as dicts under
        # RESP3, as flat arrays under RESP2) rather than anything this
        # code controls - so both shapes are handled explicitly instead
        # of assuming either one generalizes.
        modules = set()
        for m in self._client.execute_command("MODULE", "LIST"):
            if isinstance(m, dict):
                name = m.get(b"name", m.get("name"))
            else:
                name = None
                for i, field in enumerate(m):
                    if field in (b"name", "name"):
                        name = m[i + 1]
                        break
            if name is not None:
                modules.add(name.decode() if isinstance(name, bytes) else name)
        if "search" not in modules:
            raise RuntimeError(
                f"RedisBackend: no 'search' module loaded on {self.redis_url}. "
                "This backend requires Redis Stack (or plain Redis + the "
                "RediSearch module) - live-verified via MODULE LIST, not assumed."
            )

        try:
            info = self._client.ft(self.index_name).info()
            self._tenant_field = "tenant_id" in repr(info)
            logger.info(f"RedisBackend: index '{self.index_name}' already exists, reusing it")
            return
        except Exception:
            pass  # index doesn't exist yet - fall through and create it

        schema = (
            TagField("document_id"),
            TagField("tenant_id"),
            TextField("document_name"),
            NumericField("chunk_index"),
            NumericField("token_count"),
            TextField("text"),
            VectorField(
                "vector", "HNSW",
                {"TYPE": "FLOAT32", "DIM": dimensions, "DISTANCE_METRIC": "COSINE"},
            ),
        )
        self._client.ft(self.index_name).create_index(
            schema,
            definition=IndexDefinition(prefix=[self.key_prefix], index_type=IndexType.HASH),
        )
        logger.info(
            f"RedisBackend: created index '{self.index_name}' "
            f"(dimensions={dimensions}) at {self.redis_url}"
        )

    def insert_document(self, document_id: str, filename: str, metadata: Dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO documents (id, filename, metadata, uploaded_at) VALUES (?, ?, ?, ?)",
                (document_id, filename, json.dumps(metadata or {}), datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()),
            )
            self._conn.commit()

    def insert_chunk(
        self, document_id: str, document_name: str, chunk_index: int,
        text: str, token_count: int, embedding: List[float], metadata: Dict,
    ) -> None:
        key = self._vector_key(document_id, chunk_index)
        vector_bytes = np.array(embedding, dtype=np.float32).tobytes()
        with self._lock:
            self._client.hset(
                key,
                mapping={
                    "document_id": document_id,
                    "document_name": document_name,
                    "chunk_index": chunk_index,
                    "token_count": token_count or 0,
                    "text": text,
                    "vector": vector_bytes,
                    "metadata": json.dumps(metadata or {}),
                    **({"tenant_id": str(metadata["tenant_id"])} if metadata and metadata.get("tenant_id") is not None else {}),
                },
            )

    def search_dense(self, embedding: List[float], top_k: int, metadata_filter: Optional[Dict] = None) -> List[Dict]:
        from redis.commands.search.query import Query

        if not embedding or self._client is None:
            return []

        filter_clause = "*"
        if metadata_filter:
            unknown = sorted(set(metadata_filter) - {"document_id", "tenant_id"})
            if unknown:
                raise ValueError(
                    f"RedisBackend can only filter by document_id and tenant_id, got {unknown}; "
                    "other keys used to be ignored silently, which is unsafe for tenant isolation"
                )
            if "tenant_id" in metadata_filter and not self._tenant_field:
                raise RuntimeError(
                    f"RedisBackend: index '{self.index_name}' was created without a tenant_id field; "
                    "use a new index_name to enable tenant filtering"
                )
            # One parenthesised prefilter group: RediSearch rejects two groups in front of =>[KNN ...]
            filter_clause = "(" + " ".join(
                f"@{key}:{{{_escape_tag(metadata_filter[key])}}}"
                for key in ("document_id", "tenant_id") if key in metadata_filter
            ) + ")"

        query_vec = np.array(embedding, dtype=np.float32).tobytes()
        q = (
            Query(f"{filter_clause}=>[KNN {top_k} @vector $vec AS score]")
            .sort_by("score")
            .return_fields("text", "score", "document_id", "document_name", "chunk_index")
            .paging(0, top_k)
            .dialect(2)
        )
        res = self._client.ft(self.index_name).search(q, query_params={"vec": query_vec})

        results = []
        for doc in res.docs:
            key = doc.id if isinstance(doc.id, str) else doc.id.decode()
            results.append({
                "chunk_id": key,
                "text": doc.text if isinstance(doc.text, str) else doc.text,
                # RediSearch\'s vector "score" here is a COSINE DISTANCE: 0 = identical, 1 = orthogonal,
                # 2 = opposite (live-verified on Redis Stack 7.4.7). 1 - distance would be the raw cosine in
                # [-1, 1], so it is mapped onto [0, 1] like the other backends: 1 - distance / 2.
                "similarity_score": round(max(0.0, min(1.0, 1.0 - float(doc.score) / 2.0)), 4),
                "document_id": doc.document_id,
                "document_name": doc.document_name,
                "chunk_index": int(doc.chunk_index),
            })
        return results

    def supports_sparse(self) -> bool:
        return False

    def list_documents(self, limit: int, offset: int) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT id, filename, uploaded_at, metadata FROM documents "
            "ORDER BY uploaded_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

        results = []
        for doc_id, filename, uploaded_at, metadata_json in rows:
            chunk_count = 0
            if self._client is not None:
                from redis.commands.search.query import Query
                q = Query(f"(@document_id:{{{_escape_tag(doc_id)}}})").paging(0, 0).dialect(2)
                chunk_count = self._client.ft(self.index_name).search(q).total
            results.append({
                "document_id": doc_id, "filename": filename, "uploaded_at": uploaded_at,
                "metadata": json.loads(metadata_json), "chunk_count": chunk_count,
            })
        return results

    def delete_document(self, document_id: str) -> bool:
        with self._lock:
            if self._client is not None:
                from redis.commands.search.query import Query
                q = Query(f"(@document_id:{{{_escape_tag(document_id)}}})").paging(0, 10000).dialect(2)
                res = self._client.ft(self.index_name).search(q)
                keys = [doc.id if isinstance(doc.id, str) else doc.id.decode() for doc in res.docs]
                if keys:
                    self._client.delete(*keys)
            cur = self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    def get_document_filename(self, document_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT filename FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        return row[0] if row else None
