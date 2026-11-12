"""
Pinecone-backed vector storage for ragleap-rag - a managed, serverless
vector database.

**NOT LIVE-VERIFIED** - no Pinecone account was available to test
against during development (same honest caveat already applied to
Mistral/Together/Cohere/Voyage AI in the embedding module). This is
code-complete based on Pinecone's public Python SDK v3 documentation,
confirmed current via live web search during development (not from
possibly-stale training data - Pinecone's SDK has changed significantly
across versions: pod-based -> serverless, multiple client rewrites).
Treat this backend as best-effort until confirmed live by someone with
a real account.

Design notes:
- Like FAISSBackend, a SQLite sidecar stores full chunk text and the
  document registry - Pinecone caps vector metadata at 40KB and has no
  native "list all documents" query, so full text was never a good fit
  for Pinecone metadata anyway.
- Unlike FAISSBackend, persist_directory= is REQUIRED, not optional.
  Pinecone vectors persist remotely regardless of local process state -
  an in-memory-only SQLite sidecar would create orphaned vectors
  (searchable in Pinecone, but with no matching text) the moment the
  process restarts. Requiring persistence here avoids that footgun.
- The caller-supplied metadata dict is stored natively in Pinecone's
  vector metadata (not just SQLite), so metadata_filter uses Pinecone's
  real native filtering - not a post-filter like FAISSBackend has to do.
- Serverless indexes are not instantly queryable after creation -
  init_schema() polls readiness with a timeout, a well-documented
  Pinecone requirement that's easy to miss and would cause confusing
  failures on first use if skipped.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

from ragleap.vectorstores.base import VectorBackend

logger = logging.getLogger(__name__)


class PineconeBackend(VectorBackend):
    def __init__(
        self,
        persist_directory: str,
        api_key: Optional[str] = None,
        index_name: str = "ragleap",
        cloud: str = "aws",
        region: str = "us-east-1",
    ):
        try:
            import pinecone  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "PineconeBackend requires the 'pinecone' extra: pip install ragleap-rag[pinecone]"
            ) from e

        if not persist_directory:
            raise ValueError(
                "PineconeBackend requires persist_directory= - unlike FAISSBackend, "
                "this cannot be optional. Pinecone vectors persist remotely regardless "
                "of local state; without a persistent SQLite sidecar for chunk text, "
                "a process restart would leave searchable vectors with no matching text."
            )

        self.api_key = api_key or os.environ.get("PINECONE_API_KEY")
        if not self.api_key:
            raise ValueError(
                "No API key for PineconeBackend. Pass api_key= explicitly, or set "
                "PINECONE_API_KEY in your environment."
            )

        self.index_name = index_name
        self.cloud = cloud
        self.region = region
        self.persist_directory = persist_directory
        self._pc = None
        self._index = None
        self._dimensions = None
        self._lock = threading.Lock()

        os.makedirs(persist_directory, exist_ok=True)
        self._sqlite_path = os.path.join(persist_directory, "pinecone_meta.sqlite3")
        self._conn = sqlite3.connect(self._sqlite_path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, metadata TEXT NOT NULL, uploaded_at TEXT NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS chunks (
                vector_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                document_name TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL, token_count INTEGER, metadata TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def init_schema(self, dimensions: int) -> None:
        from pinecone import Pinecone, ServerlessSpec

        self._dimensions = dimensions
        self._pc = Pinecone(api_key=self.api_key)

        # IndexList.names is the real field (verified against the installed
        # pinecone SDK's actual source - list_indexes() returns a msgspec
        # Struct, not a plain list of dicts, and attribute access is
        # required throughout, not .get()/dict-subscript).
        existing = self._pc.list_indexes().names
        if self.index_name not in existing:
            logger.info(f"PineconeBackend: creating serverless index '{self.index_name}' (dimensions={dimensions})")
            self._pc.create_index(
                name=self.index_name,
                dimension=dimensions,
                metric="cosine",
                spec=ServerlessSpec(cloud=self.cloud, region=self.region),
            )
            # Serverless indexes are not instantly queryable - poll readiness,
            # a well-documented Pinecone requirement, not optional. status is
            # an IndexStatus struct with a real .ready attribute.
            deadline = time.time() + 60
            while time.time() < deadline:
                status = self._pc.describe_index(self.index_name).status
                if status.ready:
                    break
                time.sleep(2)
            else:
                logger.warning(
                    f"PineconeBackend: index '{self.index_name}' did not report ready "
                    f"within 60s - proceeding anyway, but early queries may fail."
                )
        else:
            logger.info(f"PineconeBackend: using existing index '{self.index_name}'")

        host = self._pc.describe_index(self.index_name).host
        self._index = self._pc.Index(host=host)

    def _vector_id(self, document_id: str, chunk_index: int) -> str:
        return f"{document_id}:{chunk_index}"

    def _build_filter(self, metadata_filter: Optional[Dict]) -> Optional[Dict]:
        if not metadata_filter:
            return None
        return {k: {"$eq": v} for k, v in metadata_filter.items()}

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
        vector_id = self._vector_id(document_id, chunk_index)
        with self._lock:
            self._conn.execute(
                "INSERT INTO chunks (vector_id, document_id, document_name, chunk_index, text, token_count, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (vector_id, document_id, document_name, chunk_index, text, token_count, json.dumps(metadata or {})),
            )
            self._conn.commit()

            # Stored fields come last so caller metadata can never overwrite them.
            pinecone_metadata = {
                **(metadata or {}),
                "document_id": document_id, "document_name": document_name,
                "chunk_index": chunk_index,
            }
            self._index.upsert(vectors=[(vector_id, embedding, pinecone_metadata)])

    def search_dense(self, embedding: List[float], top_k: int, metadata_filter: Optional[Dict] = None) -> List[Dict]:
        if not embedding or self._index is None:
            return []
        if self._dimensions and len(embedding) != self._dimensions:
            logger.warning(f"Query embedding dim={len(embedding)} != expected {self._dimensions}; skipping search")
            return []

        response = self._index.query(
            vector=embedding, top_k=top_k, include_metadata=False,
            filter=self._build_filter(metadata_filter),
        )

        results = []
        for match in response.matches:
            row = self._conn.execute(
                "SELECT document_id, document_name, chunk_index, text FROM chunks WHERE vector_id = ?",
                (match.id,),
            ).fetchone()
            if row is None:
                # Orphaned vector - present in Pinecone but its text row is
                # missing locally (e.g. SQLite sidecar was deleted/moved
                # without also clearing the remote index). Skip rather than
                # crash or return a chunk with no text.
                logger.warning(f"PineconeBackend: vector '{match.id}' has no matching local text row, skipping")
                continue
            document_id, document_name, chunk_index, text = row
            results.append({
                "chunk_id": match.id, "text": text, "similarity_score": round((float(match.score) + 1) / 2, 4),
                "document_id": document_id, "document_name": document_name, "chunk_index": chunk_index,
            })
        return results

    def supports_sparse(self) -> bool:
        return False

    def list_documents(self, limit: int, offset: int) -> List[Dict]:
        rows = self._conn.execute(
            """SELECT d.id, d.filename, d.uploaded_at, d.metadata, COUNT(c.vector_id) AS chunk_count
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
            vector_ids = [r[0] for r in self._conn.execute(
                "SELECT vector_id FROM chunks WHERE document_id = ?", (document_id,)
            ).fetchall()]
            if vector_ids and self._index is not None:
                self._index.delete(ids=vector_ids)
            cur = self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            self._conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    def get_document_filename(self, document_id: str) -> Optional[str]:
        row = self._conn.execute("SELECT filename FROM documents WHERE id = ?", (document_id,)).fetchone()
        return row[0] if row else None
