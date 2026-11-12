"""
Chroma-backed vector storage for ragleap-vectorstores.

Live-verified against the actually installed chromadb==1.5.9 package:
every method signature and return shape used below was introspected and
exercised against a real local PersistentClient during development, not
assumed from documentation.

Design notes:
- persist_directory is REQUIRED. Chroma's PersistentClient already stores
  chunk text and metadata durably on disk under this path, so - unlike
  QdrantBackend/PineconeBackend/WeaviateBackend - no SQLite sidecar is
  needed for chunk data. A small SQLite sidecar is still used, but only
  for the document registry (filename, uploaded_at, metadata), because
  Chroma has no native "parent document" concept - it only knows about
  individual (id, embedding, metadata, document-text) chunk records.
- Chroma's `where=` filter requires exactly one top-level operator per
  clause - live-verified: a plain multi-key dict like
  {"document_id": "d1", "tag": "x"} raises ValueError ("Expected where to
  have exactly one operator"). Multiple equality conditions must be
  wrapped in {"$and": [...]}. _build_where() below handles this.
- similarity_score is derived as 1 - distance / 2 (clamped to [0, 1]; the cosine distance runs from 0 to 2). Verified live with
  "hnsw:space": "cosine" (the default configured here): querying with an
  embedding identical to a stored one returns a distance ~0, consistent
  with Chroma's documented cosine distance = 1 - cosine_similarity.
- supports_sparse() is False. Chroma has no native BM25/keyword search
  surface as of 1.5.9 - only vector similarity - so hybrid search falls
  back to the VectorBackend default (dense-only), matching the honest
  "don't claim capability that isn't there" rule QdrantBackend follows.
"""
import json
import logging
import os
import sqlite3
import threading
from typing import Dict, List, Optional

from ragleap.vectorstores.base import VectorBackend

logger = logging.getLogger(__name__)


class ChromaBackend(VectorBackend):
    def __init__(
        self,
        persist_directory: str,
        collection_name: str = "ragleap",
    ):
        try:
            import chromadb  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "ChromaBackend requires the 'chroma' extra: "
                "pip install ragleap-vectorstores[chroma]"
            ) from e

        if not persist_directory:
            raise ValueError(
                "ChromaBackend requires persist_directory= - this is where "
                "both Chroma's own on-disk index and the document-registry "
                "SQLite sidecar are stored."
            )

        self.persist_directory = persist_directory
        self.collection_name = collection_name
        self._client = None
        self._collection = None
        self._dimensions = None
        self._lock = threading.Lock()

        os.makedirs(persist_directory, exist_ok=True)
        self._sqlite_path = os.path.join(persist_directory, "chroma_documents.sqlite3")
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

    def _build_where(self, metadata_filter: Optional[Dict]):
        """Chroma requires exactly one top-level operator per where-clause
        (live-verified) - a plain multi-key dict raises ValueError. Wrap
        multiple equality conditions in $and; pass single conditions
        through directly."""
        if not metadata_filter:
            return None
        if len(metadata_filter) == 1:
            return dict(metadata_filter)
        return {"$and": [{k: v} for k, v in metadata_filter.items()]}

    def init_schema(self, dimensions: int) -> None:
        import chromadb

        self._dimensions = dimensions
        self._client = chromadb.PersistentClient(path=self.persist_directory)
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            f"ChromaBackend: using collection '{self.collection_name}' "
            f"(dimensions={dimensions}) at {self.persist_directory}"
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
        # Stored fields come last so caller metadata can never overwrite them.
        payload = {
            **(metadata or {}),
            "document_id": document_id, "document_name": document_name,
            "chunk_index": chunk_index, "token_count": token_count or 0,
        }
        with self._lock:
            self._collection.upsert(
                ids=[vector_key],
                embeddings=[embedding],
                metadatas=[payload],
                documents=[text],
            )

    def search_dense(self, embedding: List[float], top_k: int, metadata_filter: Optional[Dict] = None) -> List[Dict]:
        if not embedding or self._collection is None:
            return []

        result = self._collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            where=self._build_where(metadata_filter),
            include=["documents", "metadatas", "distances"],
        )

        ids = result.get("ids") or [[]]
        docs = result.get("documents") or [[]]
        metas = result.get("metadatas") or [[]]
        dists = result.get("distances") or [[]]
        if not ids or not ids[0]:
            return []

        results = []
        for chunk_id, text, meta, distance in zip(ids[0], docs[0], metas[0], dists[0]):
            meta = meta or {}
            results.append({
                "chunk_id": chunk_id,
                "text": text,
                "similarity_score": round(max(0.0, min(1.0, 1.0 - float(distance) / 2.0)), 4),
                "document_id": meta.get("document_id"),
                "document_name": meta.get("document_name"),
                "chunk_index": meta.get("chunk_index"),
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
            if self._collection is not None:
                chunk_rows = self._collection.get(where={"document_id": doc_id}, include=[])
                chunk_count = len((chunk_rows or {}).get("ids", []))
            results.append({
                "document_id": doc_id, "filename": filename, "uploaded_at": uploaded_at,
                "metadata": json.loads(metadata_json), "chunk_count": chunk_count,
            })
        return results

    def delete_document(self, document_id: str) -> bool:
        with self._lock:
            if self._collection is not None:
                self._collection.delete(where={"document_id": document_id})
            cur = self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    def get_document_filename(self, document_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT filename FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        return row[0] if row else None
