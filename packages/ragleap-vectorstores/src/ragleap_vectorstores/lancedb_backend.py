"""
LanceDB-backed vector storage for ragleap-vectorstores.

Live-verified against the actually installed lancedb==0.37.1 package:
every method signature and return shape used below was introspected and
exercised against a real local embedded database during development,
not assumed from documentation.

Design notes:
- uri is REQUIRED. LanceDB's embedded mode (a local directory path, no
  server) already stores chunk text and metadata durably on disk, so -
  same as ChromaBackend - no SQLite sidecar is needed for chunk data. A
  small SQLite sidecar is still used, but only for the document
  registry (filename, uploaded_at, metadata), since LanceDB tables have
  no native "parent document" concept.
- similarity_score is derived as 1 - distance / 2 (clamped to [0, 1]; the cosine distance runs from 0 to 2) using explicit
  .metric("cosine") on every query - live-verified: querying with an
  embedding identical to a stored one returns a distance ~0 under
  cosine metric, consistent with cosine distance = 1 - cosine_similarity.
  LanceDB's default metric is L2 (not cosine) if unspecified - this
  backend always sets metric explicitly to avoid that mismatch.
- Unlike ChromaBackend, LanceDB's `where=` accepts a native SQL boolean
  expression, so multi-key filters combine directly with ` AND ` -
  no special wrapping needed. String values are quoted and single
  quotes escaped to build a safe literal. Only the table columns can be
  filtered (id, document_id, document_name, chunk_index, token_count,
  tenant_id); any other key raises ValueError. tenant_id is a nullable
  column, added to tables created before it existed; chunks without a
  tenant tag never match a tenant_id filter.
- insert_chunk uses merge_insert(...).when_matched_update_all()
  .when_not_matched_insert_all() for real upsert semantics - live-
  verified: re-inserting the same id updates the row in place rather
  than duplicating it.
- init_schema creates the table from an explicit pyarrow schema (empty,
  zero rows) so a table exists to search/list against even before the
  first real chunk is inserted - live-verified this table opens and
  accepts .add() afterward. Checks for an existing table via
  open_table()'s real ValueError-on-missing behavior, so this method is
  idempotent - calling it again just reopens the existing table.
- supports_sparse() is False. LanceDB does support full-text search via
  fts_columns=/tantivy, but that surface was not implemented or tested
  in this release - honestly reporting False and falling back to dense-
  only, rather than claiming untested capability, matches the same
  discipline ChromaBackend follows.
"""
import json
import logging
import os
import sqlite3
import threading
from typing import Dict, List, Optional

from ragleap.vectorstores.base import VectorBackend

logger = logging.getLogger(__name__)


def _quote_sql_literal(value) -> str:
    """Build a safe SQL literal for LanceDB's where= expression."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


_FILTER_KEYS = {"id", "document_id", "document_name", "chunk_index", "token_count", "tenant_id"}


class LanceDBBackend(VectorBackend):
    def __init__(
        self,
        uri: str,
        table_name: str = "ragleap",
    ):
        try:
            import lancedb  # noqa: F401
            import pyarrow  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "LanceDBBackend requires the 'lancedb' extra: "
                "pip install ragleap-vectorstores[lancedb]"
            ) from e

        if not uri:
            raise ValueError(
                "LanceDBBackend requires uri= - a local directory path "
                "for embedded/local mode. Both LanceDB's own on-disk "
                "table and the document-registry SQLite sidecar are "
                "stored under this path."
            )

        self.uri = uri
        self.table_name = table_name
        self._db = None
        self._table = None
        self._dimensions = None
        self._tenant_field = True  # False only when an existing table has no tenant_id column and it could not be added
        self._lock = threading.Lock()

        os.makedirs(uri, exist_ok=True)
        self._sqlite_path = os.path.join(uri, "lancedb_documents.sqlite3")
        self._conn = sqlite3.connect(self._sqlite_path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL,
                metadata TEXT NOT NULL, uploaded_at TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def _vector_key(self, document_id: str, chunk_index: int) -> str:
        return f"{document_id}:{chunk_index}"

    def _build_where(self, metadata_filter: Optional[Dict]) -> Optional[str]:
        if not metadata_filter:
            return None
        unknown = sorted(set(metadata_filter) - _FILTER_KEYS)
        if unknown:
            raise ValueError(
                f"LanceDBBackend can only filter by {sorted(_FILTER_KEYS)}, got {unknown}: other metadata is not "
                "stored in table columns"
            )
        if "tenant_id" in metadata_filter and not self._tenant_field:
            raise RuntimeError(
                f"LanceDBBackend: table '{self.table_name}' has no tenant_id column and it could not be added; "
                "use a new table_name to enable tenant filtering"
            )
        clauses = [f"{k} = {_quote_sql_literal(v)}" for k, v in metadata_filter.items()]
        return " AND ".join(clauses)

    def init_schema(self, dimensions: int) -> None:
        import lancedb
        import pyarrow as pa

        self._dimensions = dimensions
        self._db = lancedb.connect(self.uri)

        try:
            self._table = self._db.open_table(self.table_name)
            self._tenant_field = "tenant_id" in self._table.schema.names
            if not self._tenant_field:
                try:
                    self._table.add_columns({"tenant_id": "cast(NULL as string)"})
                    self._tenant_field = "tenant_id" in self._table.schema.names
                except Exception as e:
                    logger.warning(f"LanceDBBackend: could not add the tenant_id column to '{self.table_name}': {e}")
            logger.info(f"LanceDBBackend: opened existing table '{self.table_name}'")
        except ValueError:
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dimensions)),
                pa.field("text", pa.string()),
                pa.field("document_id", pa.string()),
                pa.field("document_name", pa.string()),
                pa.field("chunk_index", pa.int64()),
                pa.field("token_count", pa.int64()),
                pa.field("tenant_id", pa.string()),
            ])
            self._table = self._db.create_table(self.table_name, schema=schema)
            logger.info(
                f"LanceDBBackend: created table '{self.table_name}' "
                f"(dimensions={dimensions}) at {self.uri}"
            )

    def insert_document(self, document_id: str, filename: str, metadata: Dict) -> None:
        import datetime
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
        vector_key = self._vector_key(document_id, chunk_index)
        row = {
            "id": vector_key,
            "vector": embedding,
            "text": text,
            "document_id": document_id,
            "document_name": document_name,
            "chunk_index": chunk_index,
            "token_count": token_count or 0,
        }
        if self._tenant_field:
            row["tenant_id"] = str(metadata["tenant_id"]) if metadata and metadata.get("tenant_id") is not None else None
        with self._lock:
            (self._table.merge_insert("id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute([row]))

    def search_dense(self, embedding: List[float], top_k: int, metadata_filter: Optional[Dict] = None) -> List[Dict]:
        if not embedding or self._table is None:
            return []

        query = self._table.search(embedding).metric("cosine").limit(top_k)
        where = self._build_where(metadata_filter)
        if where:
            query = query.where(where)

        rows = query.to_list()
        results = []
        for r in rows:
            results.append({
                "chunk_id": r.get("id"),
                "text": r.get("text"),
                "similarity_score": round(max(0.0, min(1.0, 1.0 - float(r.get("_distance", 0.0)) / 2.0)), 4),
                "document_id": r.get("document_id"),
                "document_name": r.get("document_name"),
                "chunk_index": r.get("chunk_index"),
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
            if self._table is not None:
                where = self._build_where({"document_id": doc_id})
                chunk_count = self._table.count_rows(filter=where)
            results.append({
                "document_id": doc_id, "filename": filename, "uploaded_at": uploaded_at,
                "metadata": json.loads(metadata_json), "chunk_count": chunk_count,
            })
        return results

    def delete_document(self, document_id: str) -> bool:
        with self._lock:
            if self._table is not None:
                where = self._build_where({"document_id": document_id})
                self._table.delete(where)
            cur = self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    def get_document_filename(self, document_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT filename FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        return row[0] if row else None
