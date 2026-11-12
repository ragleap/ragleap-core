"""
Milvus-backed vector storage for ragleap-rag.

**NOT LIVE-VERIFIED** - no Milvus instance (Zilliz Cloud or self-
hosted) was available to test against during development (same honest
caveat as PineconeBackend/WeaviateBackend/QdrantBackend/mistral/
cohere/voyage). Code-complete against the actual installed
pymilvus==3.0.1 package's real source code (every method signature
used below was introspected directly, not assumed from documentation)
via the modern MilvusClient interface, not the older ORM-style API.
Treat as best-effort until confirmed live.

Design notes:
- persist_directory= is REQUIRED - same reasoning as the other remote
  backends: a Zilliz Cloud instance (or any remote Milvus) persists
  vectors regardless of local process state, so a persistent SQLite
  sidecar for chunk text is mandatory to avoid orphaned vectors.
- Unlike Pinecone/Qdrant/Weaviate, Milvus's id_type="string" natively
  accepts arbitrary string primary keys directly (max_length=512 set
  explicitly here) - no deterministic-UUID workaround needed, since
  Milvus doesn't have the same UUID-only constraint the others do.
- enable_dynamic_field=True (MilvusClient's own default for its
  simplified schema helper) means the caller-supplied metadata dict,
  document_id, document_name, and chunk_index can all be inserted
  directly as extra fields without pre-declaring a schema for them.
- A SQLite sidecar still stores full chunk text and the document
  registry, for consistency with every other backend in this project
  and because Milvus's text/JSON field length limits make it a worse
  fit for storing large amounts of full document text than a plain
  relational sidecar.
- supports_sparse() is False even though Milvus supports sparse
  vectors/BM25/hybrid search - implementing that natively is a real,
  valuable future enhancement, not done here to keep initial scope
  honest and verifiable rather than guessing at untested surface area.
"""
import json
import logging
import os
import sqlite3
import threading
from typing import Dict, List, Optional

from ragleap.vectorstores.base import VectorBackend

logger = logging.getLogger(__name__)


class MilvusBackend(VectorBackend):
    def __init__(
        self,
        persist_directory: str,
        uri: Optional[str] = None,
        token: Optional[str] = None,
        collection_name: str = "ragleap",
    ):
        try:
            import pymilvus  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "MilvusBackend requires the 'milvus' extra: pip install ragleap-rag[milvus]"
            ) from e

        if not persist_directory:
            raise ValueError(
                "MilvusBackend requires persist_directory= - a remote Milvus/Zilliz "
                "Cloud instance persists vectors regardless of local process state, so "
                "a persistent SQLite sidecar for chunk text is mandatory to avoid "
                "orphaned vectors after a restart."
            )

        self.uri = uri or os.environ.get("MILVUS_URI")
        if not self.uri:
            raise ValueError(
                "No uri for MilvusBackend. Pass uri= explicitly, or set MILVUS_URI "
                "in your environment - e.g. a Zilliz Cloud endpoint or a self-hosted "
                "Milvus instance's address."
            )
        self.token = token or os.environ.get("MILVUS_TOKEN")
        self.collection_name = collection_name
        self.persist_directory = persist_directory
        self._client = None
        self._dimensions = None
        self._lock = threading.Lock()

        os.makedirs(persist_directory, exist_ok=True)
        self._sqlite_path = os.path.join(persist_directory, "milvus_meta.sqlite3")
        self._conn = sqlite3.connect(self._sqlite_path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, metadata TEXT NOT NULL, uploaded_at TEXT NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS chunks (
                vector_key TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                document_name TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL, token_count INTEGER, metadata TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def _vector_key(self, document_id: str, chunk_index: int) -> str:
        return f"{document_id}:{chunk_index}"

    def init_schema(self, dimensions: int) -> None:
        from pymilvus import MilvusClient

        self._dimensions = dimensions
        self._client = MilvusClient(uri=self.uri, token=self.token or "")

        if not self._client.has_collection(self.collection_name):
            logger.info(f"MilvusBackend: creating collection '{self.collection_name}' (dimensions={dimensions})")
            self._client.create_collection(
                collection_name=self.collection_name,
                dimension=dimensions,
                id_type="string",
                max_length=512,
                metric_type="COSINE",
            )
        else:
            logger.info(f"MilvusBackend: using existing collection '{self.collection_name}'")

    def _build_filter_expr(self, metadata_filter: Optional[Dict]) -> str:
        """Milvus filters are boolean expression strings, not a
        structured object like the other backends - build a simple
        AND-of-equalities expression."""
        if not metadata_filter:
            return ""
        import math
        import re

        parts = []
        for k, v in metadata_filter.items():
            if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                raise ValueError(f"Invalid metadata filter key: {k!r}")
            if isinstance(v, bool):
                value_repr = "true" if v else "false"
            elif isinstance(v, str):
                escaped = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
                value_repr = '"' + escaped + '"'
            elif isinstance(v, (int, float)) and math.isfinite(v):
                value_repr = str(v)
            else:
                raise ValueError(f"Unsupported metadata filter value for {k!r}: {v!r}")
            parts.append(f'{k} == {value_repr}')
        return " and ".join(parts)

    def insert_document(self, document_id: str, filename: str, metadata: Dict) -> None:
        import datetime
        with self._lock:
            self._conn.execute(
                "INSERT INTO documents (id, filename, metadata, uploaded_at) VALUES (?, ?, ?, ?)",
                (document_id, filename, json.dumps(metadata or {}), datetime.datetime.utcnow().isoformat()),
            )
            self._conn.commit()

    def insert_chunk(
        self, document_id: str, document_name: str, chunk_index: int,
        text: str, token_count: int, embedding: List[float], metadata: Dict,
    ) -> None:
        vector_key = self._vector_key(document_id, chunk_index)

        with self._lock:
            self._conn.execute(
                "INSERT INTO chunks (vector_key, document_id, document_name, chunk_index, text, token_count, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (vector_key, document_id, document_name, chunk_index, text, token_count, json.dumps(metadata or {})),
            )
            self._conn.commit()

            # Stored fields come last so caller metadata can never overwrite them (including id and vector).
            row = {
                **(metadata or {}),
                "id": vector_key, "vector": embedding,
                "document_id": document_id, "document_name": document_name,
                "chunk_index": chunk_index,
            }
            self._client.insert(collection_name=self.collection_name, data=[row])

    def search_dense(self, embedding: List[float], top_k: int, metadata_filter: Optional[Dict] = None) -> List[Dict]:
        if not embedding or self._client is None:
            return []
        if self._dimensions and len(embedding) != self._dimensions:
            logger.warning(f"Query embedding dim={len(embedding)} != expected {self._dimensions}; skipping search")
            return []

        response = self._client.search(
            collection_name=self.collection_name, data=[embedding], limit=top_k,
            filter=self._build_filter_expr(metadata_filter),
        )

        results = []
        # search() returns List[List[dict]] - one inner list per query
        # vector; we always send exactly one, so response[0] is ours.
        hits = response[0] if response else []
        for hit in hits:
            vector_key = hit.get("id")
            distance = hit.get("distance")
            row = self._conn.execute(
                "SELECT document_id, document_name, chunk_index, text FROM chunks WHERE vector_key = ?",
                (vector_key,),
            ).fetchone()
            if row is None:
                logger.warning(f"MilvusBackend: point '{vector_key}' has no matching local text row, skipping")
                continue
            document_id, document_name, chunk_index, text = row
            results.append({
                "chunk_id": vector_key, "text": text,
                # Milvus with metric_type="COSINE" returns raw cosine
                # similarity in [-1, 1] (verified against Milvus docs,
                # not a true distance - higher is already "more similar").
                # Normalized to [0, 1] here to match pgvector/weaviate's
                # convention, since a caller filtering on similarity_score
                # (e.g. "score > 0.5") would otherwise behave inconsistently
                # across backends for negative Milvus scores.
                "similarity_score": round((float(distance) + 1) / 2, 4) if distance is not None else None,
                "document_id": document_id, "document_name": document_name, "chunk_index": chunk_index,
            })
        return results

    def supports_sparse(self) -> bool:
        return False

    def list_documents(self, limit: int, offset: int) -> List[Dict]:
        rows = self._conn.execute(
            """SELECT d.id, d.filename, d.uploaded_at, d.metadata, COUNT(c.vector_key) AS chunk_count
               FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
               GROUP BY d.id ORDER BY d.uploaded_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [
            {"document_id": r[0], "filename": r[1], "uploaded_at": r[2], "metadata": json.loads(r[3]), "chunk_count": r[4]}
            for r in rows
        ]

    def delete_document(self, document_id: str) -> bool:
        with self._lock:
            vector_keys = [r[0] for r in self._conn.execute(
                "SELECT vector_key FROM chunks WHERE document_id = ?", (document_id,)
            ).fetchall()]
            if vector_keys and self._client is not None:
                self._client.delete(collection_name=self.collection_name, ids=vector_keys)
            cur = self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            self._conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    def get_document_filename(self, document_id: str) -> Optional[str]:
        row = self._conn.execute("SELECT filename FROM documents WHERE id = ?", (document_id,)).fetchone()
        return row[0] if row else None
