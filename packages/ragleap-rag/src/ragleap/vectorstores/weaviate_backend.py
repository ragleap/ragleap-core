"""
Weaviate-backed vector storage for ragleap-rag.

LIVE-VERIFIED against a real Weaviate instance (cr.weaviate.io/semitechnologies/weaviate:1.27.0,
via a real smoke test: init_schema, insert_document, insert_chunk x3,
search_dense with and without metadata_filter, list_documents,
get_document_filename, delete_document - all against real infra, not
mocks). Also introspected against the actual installed
weaviate-client (4.22.0 during initial development, 4.23.1 during
live verification) package's real source code - the current GA Python
client (v3 is deprecated).

TWO REAL BUGS FOUND AND FIXED via this live test:
1. init_schema()'s else branch called weaviate.connect_to_local() with
   no arguments; that function's real signature hardcodes
   host="localhost", port=8080, grpc_port=50051 with no override -
   any self-hosted Weaviate not on the exact default port was
   unreachable. Fixed by adding optional local_host=/local_port=/
   local_grpc_port= constructor parameters.
2. Configure.Vectors.self_provided() with no name= creates a NAMED
   vector called "default" (confirmed via the real collection config),
   not the legacy unnamed vector space - insert_chunk() and
   search_dense() both needed to reference it explicitly
   (vector={"default": [...]}, target_vector="default"), or
   search_dense() silently returned zero results. Additionally, the
   existing distance-to-similarity conversion (1.0 - distance)
   produced raw cosine similarity in [-1, 1] (live-verified: identical/
   orthogonal/opposite vectors scored 1.0/0.0/-1.0) instead of
   pgvector's [0, 1] convention (1 - distance/2) - the same class of
   bug already found and fixed for MilvusBackend and QdrantBackend.
   Fixed to match pgvector's exact formula.

REAL GAP FOUND AND FIXED via live-testing against a real local Weaviate
instance: init_schema()'s else branch called weaviate.connect_to_local()
with no arguments, and that function's real installed signature
(weaviate-client==4.22.0) hardcodes host="localhost", port=8080,
grpc_port=50051 with no way to override them through this backend's
constructor - so any self-hosted Weaviate not on the exact default port
was unreachable. Fixed by adding optional local_host=/local_port=/
local_grpc_port= constructor parameters, defaulting to connect_to_local()'s
own defaults so existing behavior is unchanged when unset.

REAL BUG FOUND AND FIXED via the same live-testing: Configure.Vectors.self_provided()
with no name= creates a NAMED vector called "default" (confirmed via
the real collection config: vector_config={'default': _NamedVectorConfig(...)}),
not the legacy unnamed vector space. Empirically confirmed via three
live test cases against a real Weaviate instance: passing a plain list
to data.insert(vector=...) with no target_vector on the query returned
0 results; adding target_vector="default" to the query ALONE still
returned 0; only passing vector={"default": [...]} to insert AND
target_vector="default" to the query together returned matches. Both
insert_chunk() and search_dense() are fixed to use the named vector.

Design notes (same reasoning as PineconeBackend):
- persist_directory= is REQUIRED - a Weaviate Cloud instance persists
  vectors remotely regardless of local process state, so a persistent
  SQLite sidecar for chunk text is mandatory to avoid orphaned vectors.
- A SQLite sidecar stores full chunk text, the document registry, and
  a document_id/chunk_index -> Weaviate UUID mapping (Weaviate assigns
  its own UUIDs on insert - unlike Pinecone, you can't just supply a
  custom string ID directly as the primary key).
- Vectors are "self-provided" (Configure.Vectors.self_provided()) -
  ragleap-rag always brings its own embeddings; Weaviate's built-in
  vectorizer integrations are intentionally not used here, consistent
  with this library's BYOK philosophy.
- supports_sparse() is False even though Weaviate natively supports
  BM25/hybrid search - implementing that natively is a real, valuable
  future enhancement, not done here to keep initial scope honest and
  verifiable rather than guessing at a larger untested surface area.
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


class WeaviateBackend(VectorBackend):
    # Configure.Vectors.self_provided() with no name= creates a named
    # vector called "default" (live-verified against the real installed
    # weaviate-client==4.22.0) - both insert and query must reference it
    # explicitly, or search_dense() silently returns zero results.
    _VECTOR_NAME = "default"

    def __init__(
        self,
        persist_directory: str,
        cluster_url: Optional[str] = None,
        api_key: Optional[str] = None,
        collection_name: str = "RagleapChunk",
        local_host: Optional[str] = None,
        local_port: Optional[int] = None,
        local_grpc_port: Optional[int] = None,
    ):
        try:
            import weaviate  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "WeaviateBackend requires the 'weaviate' extra: pip install ragleap-rag[weaviate]"
            ) from e

        if not persist_directory:
            raise ValueError(
                "WeaviateBackend requires persist_directory= - a remote Weaviate "
                "instance persists vectors regardless of local process state, so a "
                "persistent SQLite sidecar for chunk text is mandatory to avoid "
                "orphaned vectors after a restart."
            )

        self.cluster_url = cluster_url or os.environ.get("WEAVIATE_CLUSTER_URL")
        self.api_key = api_key or os.environ.get("WEAVIATE_API_KEY")
        # No self-hosted equivalent of WEAVIATE_CLUSTER_URL existed before -
        # connect_to_local()'s real defaults (host="localhost", port=8080,
        # grpc_port=50051) are only overridable here, not via env vars, to
        # match how cluster_url/api_key are the two Cloud-facing settings.
        self.local_host = local_host or "localhost"
        self.local_port = local_port or 8080
        self.local_grpc_port = local_grpc_port or 50051
        # collection_name must start with an uppercase letter per Weaviate's
        # naming convention - normalize rather than fail confusingly later.
        self.collection_name = collection_name[0].upper() + collection_name[1:] if collection_name else "RagleapChunk"
        self.persist_directory = persist_directory
        self._client = None
        self._collection = None
        self._dimensions = None
        self._lock = threading.Lock()

        os.makedirs(persist_directory, exist_ok=True)
        self._sqlite_path = os.path.join(persist_directory, "weaviate_meta.sqlite3")
        self._conn = sqlite3.connect(self._sqlite_path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, metadata TEXT NOT NULL, uploaded_at TEXT NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS chunks (
                vector_key TEXT PRIMARY KEY, weaviate_uuid TEXT NOT NULL, document_id TEXT NOT NULL,
                document_name TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL, token_count INTEGER, metadata TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def _vector_key(self, document_id: str, chunk_index: int) -> str:
        return f"{document_id}:{chunk_index}"

    def _deterministic_uuid(self, vector_key: str) -> str:
        """Weaviate requires its own UUID as the object identifier -
        deriving it deterministically from our own vector_key (rather
        than a random uuid4) means re-running the same insert is
        idempotent at the Weaviate layer too, matching how FAISS/
        Pinecone's IDs are derived from document_id+chunk_index."""
        return str(uuid_module.uuid5(uuid_module.NAMESPACE_DNS, vector_key))

    def init_schema(self, dimensions: int) -> None:
        import weaviate
        from weaviate.classes.init import Auth
        from weaviate.classes.config import Configure

        self._dimensions = dimensions

        if self.cluster_url:
            auth = Auth.api_key(self.api_key) if self.api_key else None
            self._client = weaviate.connect_to_weaviate_cloud(cluster_url=self.cluster_url, auth_credentials=auth)
        else:
            self._client = weaviate.connect_to_local(
                host=self.local_host, port=self.local_port, grpc_port=self.local_grpc_port,
            )

        if not self._client.collections.exists(self.collection_name):
            logger.info(f"WeaviateBackend: creating collection '{self.collection_name}' (dimensions={dimensions})")
            self._client.collections.create(
                name=self.collection_name,
                vector_config=Configure.Vectors.self_provided(),
            )
        else:
            logger.info(f"WeaviateBackend: using existing collection '{self.collection_name}'")

        self._collection = self._client.collections.get(self.collection_name)

    def _build_filter(self, metadata_filter: Optional[Dict]):
        if not metadata_filter:
            return None
        from weaviate.classes.query import Filter

        conditions = [Filter.by_property(k).equal(v) for k, v in metadata_filter.items()]
        combined = conditions[0]
        for cond in conditions[1:]:
            combined = combined & cond
        return combined

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
        weaviate_uuid = self._deterministic_uuid(vector_key)

        with self._lock:
            self._conn.execute(
                "INSERT INTO chunks (vector_key, weaviate_uuid, document_id, document_name, chunk_index, text, token_count, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (vector_key, weaviate_uuid, document_id, document_name, chunk_index, text, token_count, json.dumps(metadata or {})),
            )
            self._conn.commit()

            # Stored fields come last so caller metadata can never overwrite them.
            properties = {
                **(metadata or {}),
                "document_id": document_id, "document_name": document_name,
                "chunk_index": chunk_index,
            }
            self._collection.data.insert(properties=properties, vector={self._VECTOR_NAME: embedding}, uuid=weaviate_uuid)

    def search_dense(self, embedding: List[float], top_k: int, metadata_filter: Optional[Dict] = None) -> List[Dict]:
        if not embedding or self._collection is None:
            return []
        if self._dimensions and len(embedding) != self._dimensions:
            logger.warning(f"Query embedding dim={len(embedding)} != expected {self._dimensions}; skipping search")
            return []

        response = self._collection.query.near_vector(
            near_vector=embedding, limit=top_k,
            target_vector=self._VECTOR_NAME,
            filters=self._build_filter(metadata_filter),
            return_metadata=["distance"],
        )

        results = []
        for obj in response.objects:
            row = self._conn.execute(
                "SELECT document_id, document_name, chunk_index, text FROM chunks WHERE weaviate_uuid = ?",
                (str(obj.uuid),),
            ).fetchone()
            if row is None:
                logger.warning(f"WeaviateBackend: object '{obj.uuid}' has no matching local text row, skipping")
                continue
            document_id, document_name, chunk_index, text = row
            # Weaviate's cosine distance ranges [0, 2] (0=identical,
            # 2=opposite) - live-verified: identical/orthogonal/opposite
            # vectors returned distance 0.0/1.0/2.0. The previous
            # `1.0 - distance` conversion produced raw cosine similarity
            # in [-1, 1] (1.0/0.0/-1.0), inconsistent with pgvector's own
            # `1 - distance/2` convention and the [0, 1] range every other
            # backend uses (the same class of bug already found and fixed
            # for MilvusBackend and QdrantBackend). Matching pgvector's
            # exact formula here for consistency across the ecosystem.
            distance = obj.metadata.distance if obj.metadata and obj.metadata.distance is not None else None
            similarity_score = round(1.0 - distance / 2, 4) if distance is not None else None
            results.append({
                "chunk_id": str(obj.uuid), "text": text, "similarity_score": similarity_score,
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
            weaviate_uuids = [r[0] for r in self._conn.execute(
                "SELECT weaviate_uuid FROM chunks WHERE document_id = ?", (document_id,)
            ).fetchall()]
            if weaviate_uuids and self._collection is not None:
                for wid in weaviate_uuids:
                    self._collection.data.delete_by_id(wid)
            cur = self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            self._conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    def get_document_filename(self, document_id: str) -> Optional[str]:
        row = self._conn.execute("SELECT filename FROM documents WHERE id = ?", (document_id,)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        """Weaviate's v4 client holds an open connection that should be
        explicitly closed when done - unlike Pinecone's REST-based
        client, this isn't automatic. Not called automatically by
        RagLeap; callers managing WeaviateBackend's lifecycle directly
        should call this when finished."""
        if self._client is not None:
            self._client.close()
