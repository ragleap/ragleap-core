"""
Upstash Vector-backed vector storage for ragleap-vectorstores.

Live-verified against the actually installed upstash-vector==0.8.0 package
and a real Upstash Vector index (dimension 1536, COSINE) - every method
signature, filter syntax, and score convention below was introspected and
exercised for real before being written here, not assumed from docs.

Design notes:
- url= and token= are REQUIRED. Unlike Chroma/LanceDB (embedded) or even
  Redis (self-hostable), Upstash Vector is a managed serverless service -
  there is no local/embedded mode at all. The index itself (including its
  fixed dimension and dense-vs-hybrid type) must already exist, created
  via the Upstash console - the SDK has no create-index call. Because of
  this, init_schema() cannot create anything; it can only verify the real
  index's dimension matches what the caller expects, via info(), and
  raise a clear error on mismatch rather than a confusing one at query
  time.
- LIVE-VERIFIED GOTCHA: an Upstash Vector index can be created as either
  "dense" or "hybrid" (requiring sparse vectors on every upsert). This is
  an index-level setting from creation, not something the SDK controls.
  A hybrid-configured index rejects dense-only upserts with
  UpstashError("This index requires sparse vectors"). init_schema()
  checks dense_index/sparse_index from info() and raises a clear error
  if the index isn't a pure dense index, rather than failing confusingly
  on the first insert_chunk() call.
- Metadata filtering is a REAL differentiator from Chroma/LanceDB/Redis:
  Upstash's filter= is a SQL-like string ("key = 'value' AND key2 >= 5"),
  and since metadata is stored as a genuine JSON dict (not predeclared
  schema fields like Redis requires), arbitrary multi-key filters work
  natively - closer to Chroma's flexibility than Redis's document_id-only
  limitation, without needing Chroma's $and wrapping either.
- similarity_score: Upstash's query() score is ALREADY a normalized
  similarity in [0, 1] where 1 = identical (live-verified: an exact-match
  query returned score 1.0) - unlike Chroma/Redis, which return a
  distance requiring `1 - distance / 2` conversion. Used directly here, no
  conversion needed.
- Chunk data (text, document_id, document_name, chunk_index, token_count)
  is stored natively in Upstash's metadata dict per vector - no SQLite
  sidecar needed for chunks, same "no sidecar needed" pattern as
  Chroma/LanceDB. A small SQLite sidecar is still used, but only for the
  document registry (filename, uploaded_at), same as every other backend.
- Chunk IDs use the deterministic f"{document_id}:{chunk_index}" format.
  delete_document() and chunk-counting both use Upstash's native prefix=
  parameter on delete()/range() - a real prefix-scan delete/list, more
  convenient than Redis's manual scan-then-delete-by-key-list pattern.
- supports_sparse() returns False. Upstash Vector *does* support real
  hybrid dense+sparse search, but that surface requires a hybrid-type
  index and sparse embeddings this package doesn't generate, so it's
  honestly reported as unsupported rather than assumed to work.
"""
import datetime
import json
import logging
import os
import re
import sqlite3
import threading
from typing import Dict, List, Optional

from ragleap.vectorstores.base import VectorBackend

logger = logging.getLogger(__name__)


def _escape_filter_value(value) -> str:
    """Build one side of an Upstash filter= clause for a single value.
    Live-verified filter syntax is SQL-like: string values need single
    quotes, with any embedded single quote doubled (standard SQL escaping);
    numbers and booleans are written bare."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


class UpstashBackend(VectorBackend):
    def __init__(
        self,
        url: str,
        token: str,
        namespace: str = "",
        registry_path: Optional[str] = None,
    ):
        try:
            from upstash_vector import Index  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "UpstashBackend requires the 'upstash' extra: "
                "pip install ragleap-vectorstores[upstash]"
            ) from e

        if not url or not token:
            raise ValueError(
                "UpstashBackend requires both url= and token= - get these "
                "from the Upstash console (Vector > your index > REST API). "
                "The index itself must already exist (created via the "
                "console) as a pure dense index; UpstashBackend cannot "
                "create one."
            )

        self.url = url
        self.token = token
        # Isolation note, learned from a real RedisBackend bug this
        # project fixed: if you share ONE Upstash index across multiple
        # logically separate RagLeap deployments, always pass a distinct
        # namespace= for each - the default "" namespace is shared by
        # everyone who doesn't set one, same footgun shape as Redis's
        # key_prefix collision, just one level up (index vs namespace).
        self.namespace = namespace
        self._index = None
        self._dimensions = None
        self._lock = threading.Lock()

        registry_path = registry_path or os.path.join(
            os.path.expanduser("~"), ".ragleap_vectorstores", "upstash_documents.sqlite3"
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

    def _vector_id(self, document_id: str, chunk_index: int) -> str:
        return f"{document_id}:{chunk_index}"

    def _build_filter(self, metadata_filter: Optional[Dict]) -> str:
        """Upstash's filter= is a real SQL-like string over the actual
        metadata dict - unlike Redis, arbitrary keys work natively since
        there's no predeclared schema. Multi-key filters join with AND,
        same native-AND convenience as LanceDB, no $and wrapping needed."""
        if not metadata_filter:
            return ""
        import re as _re
        for key in metadata_filter:
            if not isinstance(key, str) or not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", key):
                raise ValueError(f"Invalid metadata filter key: {key!r}")
        clauses = [f"{key} = {_escape_filter_value(value)}" for key, value in metadata_filter.items()]
        return " AND ".join(clauses)

    def init_schema(self, dimensions: int) -> None:
        from upstash_vector import Index

        self._dimensions = dimensions
        self._index = Index(url=self.url, token=self.token)

        info = self._index.info()
        if info.sparse_index is not None or info.dense_index is None:
            raise RuntimeError(
                f"UpstashBackend: index at {self.url} is not a pure dense "
                "index (it's configured for hybrid/sparse vectors) - "
                "live-verified via info(), not assumed. Create a new "
                "index in the Upstash console with type 'Dense' instead."
            )
        if info.dimension != dimensions:
            raise RuntimeError(
                f"UpstashBackend: index at {self.url} has dimension "
                f"{info.dimension}, but {dimensions} was requested. The "
                "index's dimension is fixed at creation time in the "
                "Upstash console and cannot be changed by this backend."
            )
        logger.info(
            f"UpstashBackend: connected to existing index "
            f"(dimensions={dimensions}, namespace={self.namespace!r}) at {self.url}"
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
        vec_id = self._vector_id(document_id, chunk_index)
        payload_metadata = {
            "document_id": document_id,
            "document_name": document_name,
            "chunk_index": chunk_index,
            "token_count": token_count or 0,
            "text": text,
        }
        # Extra caller metadata is added AFTER the stored fields and can never replace them: a key named "text" or
        # "document_id" used to overwrite the real value, which broke search results and document deletes.
        for key, value in (metadata or {}).items():
            payload_metadata.setdefault(key, value)
        with self._lock:
            self._index.upsert(
                vectors=[{"id": vec_id, "vector": embedding, "metadata": payload_metadata}],
                namespace=self.namespace,
            )

    def search_dense(self, embedding: List[float], top_k: int, metadata_filter: Optional[Dict] = None) -> List[Dict]:
        if not embedding or self._index is None:
            return []

        results = self._index.query(
            vector=embedding,
            top_k=top_k,
            include_metadata=True,
            filter=self._build_filter(metadata_filter),
            namespace=self.namespace,
        )

        out = []
        for r in results:
            meta = r.metadata or {}
            out.append({
                "chunk_id": r.id,
                "text": meta.get("text"),
                # Upstash's score is ALREADY a normalized similarity in
                # [0, 1] (1 = identical) - live-verified, unlike
                # Chroma/Redis's distance-based score. Used directly.
                "similarity_score": round(float(r.score), 4),
                "document_id": meta.get("document_id"),
                "document_name": meta.get("document_name"),
                "chunk_index": meta.get("chunk_index"),
            })
        return out

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
            if self._index is not None:
                # Native prefix-scan via range() - no predeclared schema
                # or manual key-listing needed, unlike Redis's approach.
                # One range() call returns at most `limit` vectors, so follow
                # next_cursor until the scan is finished - a single call used
                # to under-count any document with more than 1000 chunks.
                cursor = ""
                while True:
                    range_result = self._index.range(
                        cursor=cursor, prefix=f"{doc_id}:", limit=1000, namespace=self.namespace,
                    )
                    chunk_count += len(range_result.vectors)
                    next_cursor = range_result.next_cursor
                    if not next_cursor or next_cursor == cursor:
                        break
                    cursor = next_cursor
            results.append({
                "document_id": doc_id, "filename": filename, "uploaded_at": uploaded_at,
                "metadata": json.loads(metadata_json), "chunk_count": chunk_count,
            })
        return results

    def delete_document(self, document_id: str) -> bool:
        with self._lock:
            if self._index is not None:
                # Native prefix-scan delete - single call, unlike Redis's
                # scan-then-delete-by-key-list pattern.
                self._index.delete(prefix=f"{document_id}:", namespace=self.namespace)
            cur = self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    def get_document_filename(self, document_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT filename FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        return row[0] if row else None
