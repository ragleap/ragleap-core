"""
Qdrant-backed vector storage for ragleap-rag.

LIVE-VERIFIED against a real Qdrant instance (qdrant/qdrant:latest,
via a real smoke test: init_schema, insert_document, insert_chunk x3,
search_dense with and without metadata_filter, list_documents,
get_document_filename, delete_document - all against real infra, not
mocks). Also introspected against the actual installed
qdrant-client==1.18.0 package's real source code (every method
signature and pydantic model field used below was verified directly,
not assumed from documentation).

REAL BUG FOUND AND FIXED via this live test: search_dense() previously
returned Qdrant's raw Distance.COSINE score directly as
similarity_score. Live-verified against real vectors (identical=1.0,
orthogonal=0.0, opposite=-1.0): this is RAW cosine similarity in
[-1, 1], not normalized like pgvector/weaviate/milvus's [0, 1]
convention - the same inconsistency already found and fixed once in
this codebase for MilvusBackend, previously missed here. Fixed with
the identical (x + 1) / 2 transform used there.

Design notes (same reasoning as PineconeBackend/WeaviateBackend):
- persist_directory= is REQUIRED - a remote/cloud Qdrant instance
  persists vectors regardless of local process state, so a persistent
  SQLite sidecar for chunk text is mandatory to avoid orphaned vectors.
- A SQLite sidecar stores full chunk text, the document registry, and
  a document_id/chunk_index -> Qdrant point UUID mapping. Qdrant's
  PointStruct.id type hint technically allows arbitrary strings, but
  its real runtime validation requires string IDs to actually parse as
  valid UUIDs (a well-documented gap between the type system and the
  server's actual behavior) - deterministic UUIDs are used here rather
  than trusting the permissive type hint alone.
- supports_sparse() is False even though Qdrant natively supports
  sparse vectors and hybrid search - implementing that natively is a
  real, valuable future enhancement, not done here to keep initial
  scope honest and verifiable rather than guessing at untested surface.
- Qdrant also supports a fully local, file-based mode (path= instead
  of url=) with no server at all - useful for the same "no setup"
  use case FAISSBackend covers, but not wired in here to keep this
  backend's scope focused on the managed/remote use case Pinecone and
  Weaviate's cloud offerings also cover; local Qdrant is a reasonable
  future addition.
"""
import json
import logging
import os
import sqlite3
import threading
import uuid as uuid_module
from typing import Dict, List, Optional

from ragleap.vectorstores.base import VectorBackend

logger = logging.getLogger(__name__)


class QdrantBackend(VectorBackend):
    def __init__(
        self,
        persist_directory: str,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        collection_name: str = "ragleap",
    ):
        try:
            import qdrant_client  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "QdrantBackend requires the 'qdrant' extra: pip install ragleap-rag[qdrant]"
            ) from e

        if not persist_directory:
            raise ValueError(
                "QdrantBackend requires persist_directory= - a remote Qdrant "
                "instance persists vectors regardless of local process state, so a "
                "persistent SQLite sidecar for chunk text is mandatory to avoid "
                "orphaned vectors after a restart."
            )

        self.url = url or os.environ.get("QDRANT_URL")
        if not self.url:
            raise ValueError(
                "No url for QdrantBackend. Pass url= explicitly, or set "
                "QDRANT_URL in your environment - e.g. a Qdrant Cloud cluster URL "
                "or a self-hosted instance's address."
            )
        self.api_key = api_key or os.environ.get("QDRANT_API_KEY")
        self.collection_name = collection_name
        self.persist_directory = persist_directory
        self._client = None
        self._dimensions = None
        self._lock = threading.Lock()

        os.makedirs(persist_directory, exist_ok=True)
        self._sqlite_path = os.path.join(persist_directory, "qdrant_meta.sqlite3")
        self._conn = sqlite3.connect(self._sqlite_path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, metadata TEXT NOT NULL, uploaded_at TEXT NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS chunks (
                vector_key TEXT PRIMARY KEY, qdrant_id TEXT NOT NULL, document_id TEXT NOT NULL,
                document_name TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL, token_count INTEGER, metadata TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def _vector_key(self, document_id: str, chunk_index: int) -> str:
        return f"{document_id}:{chunk_index}"

    def _deterministic_uuid(self, vector_key: str) -> str:
        """Qdrant's PointStruct.id type hint technically allows any str,
        but real runtime validation requires it to parse as a valid
        UUID - deriving deterministically from vector_key also makes
        re-running the same insert idempotent, matching Weaviate's
        approach here for the same underlying reason."""
        return str(uuid_module.uuid5(uuid_module.NAMESPACE_DNS, vector_key))

    def init_schema(self, dimensions: int) -> None:
        from qdrant_client import QdrantClient
        from qdrant_client.http.models import VectorParams, Distance

        self._dimensions = dimensions
        self._client = QdrantClient(url=self.url, api_key=self.api_key)

        if not self._client.collection_exists(self.collection_name):
            logger.info(f"QdrantBackend: creating collection '{self.collection_name}' (dimensions={dimensions})")
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=dimensions, distance=Distance.COSINE),
            )
        else:
            logger.info(f"QdrantBackend: using existing collection '{self.collection_name}'")

    def _build_filter(self, metadata_filter: Optional[Dict]):
        if not metadata_filter:
            return None
        from qdrant_client.http.models import Filter, FieldCondition, MatchValue

        conditions = [FieldCondition(key=k, match=MatchValue(value=v)) for k, v in metadata_filter.items()]
        return Filter(must=conditions)

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
        from qdrant_client.http.models import PointStruct

        vector_key = self._vector_key(document_id, chunk_index)
        qdrant_id = self._deterministic_uuid(vector_key)

        with self._lock:
            self._conn.execute(
                "INSERT INTO chunks (vector_key, qdrant_id, document_id, document_name, chunk_index, text, token_count, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (vector_key, qdrant_id, document_id, document_name, chunk_index, text, token_count, json.dumps(metadata or {})),
            )
            self._conn.commit()

            # Stored fields come last so caller metadata can never overwrite them.
            payload = {
                **(metadata or {}),
                "document_id": document_id, "document_name": document_name,
                "chunk_index": chunk_index,
            }
            self._client.upsert(
                collection_name=self.collection_name,
                points=[PointStruct(id=qdrant_id, vector=embedding, payload=payload)],
            )

    def search_dense(self, embedding: List[float], top_k: int, metadata_filter: Optional[Dict] = None) -> List[Dict]:
        if not embedding or self._client is None:
            return []
        if self._dimensions and len(embedding) != self._dimensions:
            logger.warning(f"Query embedding dim={len(embedding)} != expected {self._dimensions}; skipping search")
            return []

        response = self._client.query_points(
            collection_name=self.collection_name, query=embedding, limit=top_k,
            query_filter=self._build_filter(metadata_filter), with_payload=False,
        )

        results = []
        for point in response.points:
            row = self._conn.execute(
                "SELECT document_id, document_name, chunk_index, text FROM chunks WHERE qdrant_id = ?",
                (str(point.id),),
            ).fetchone()
            if row is None:
                logger.warning(f"QdrantBackend: point '{point.id}' has no matching local text row, skipping")
                continue
            document_id, document_name, chunk_index, text = row
            # Qdrant with Distance.COSINE returns RAW cosine similarity in
            # [-1, 1] (live-verified: identical=1.0, orthogonal=0.0,
            # opposite=-1.0), not normalized like pgvector/weaviate/milvus's
            # [0, 1] convention. Normalized here via the same (x + 1) / 2
            # transform already used in MilvusBackend for the identical
            # inconsistency, so callers filtering similarity_score behave
            # consistently across every backend.
            similarity_score = round((float(point.score) + 1) / 2, 4)
            results.append({
                "chunk_id": str(point.id), "text": text, "similarity_score": similarity_score,
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
            qdrant_ids = [r[0] for r in self._conn.execute(
                "SELECT qdrant_id FROM chunks WHERE document_id = ?", (document_id,)
            ).fetchall()]
            if qdrant_ids and self._client is not None:
                self._client.delete(collection_name=self.collection_name, points_selector=qdrant_ids)
            cur = self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            self._conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    def get_document_filename(self, document_id: str) -> Optional[str]:
        row = self._conn.execute("SELECT filename FROM documents WHERE id = ?", (document_id,)).fetchone()
        return row[0] if row else None
