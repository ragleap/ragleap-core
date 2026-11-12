"""
OpenSearch-backed vector storage for ragleap-vectorstores (opensearch-knn).

Live-verified against the actually installed opensearch-py==3.2.0 package
and a real local OpenSearch 3.8.0 instance (Docker, single-node) - the
index-creation mapping, insert/search calls, and score semantics below
were introspected and exercised for real before being written here, not
assumed from tutorials or documentation. Two things that differ from what
most OpenSearch k-NN tutorials show:

- The `nmslib` k-NN engine shown in most tutorials/docs is deprecated and
  is REJECTED at index-creation time on OpenSearch 3.0+ - live-verified:
  attempting `method.engine: "nmslib"` on this OpenSearch 3.8.0 instance
  raised `mapper_parsing_exception` with the message "nmslib engine is
  deprecated in OpenSearch and cannot be used for new index creation in
  OpenSearch from 3.0.0." `engine: "lucene"` (OpenSearch's current
  built-in default, no extra plugin required) is used here instead, and
  was live-verified end-to-end: index creation, insert, k-NN search, and
  correct nearest-neighbor ordering all succeeded with it.
- `Indices` is NOT importable directly as `from opensearchpy.client import
  Indices` - live-verified via real introspection of the installed
  package. It's accessed as the `.indices` attribute on an OpenSearch
  client instance (an `IndicesClient`), which is how this module uses it
  throughout (`self._client.indices.create(...)`, etc.).
- The k-NN score returned for `space_type: "cosinesimil"` (with the
  `lucene` engine) is NOT raw cosine similarity - live-verified by
  computing actual cosine similarity for the smoke-test vectors used
  during development and comparing to the score OpenSearch returned:
  the real relationship is `score = (1 + cosine_similarity) / 2`, i.e.
  OpenSearch maps a similarity in [-1, 1] onto a score in [0, 1]. This
  mirrors Upstash's "already normalized, don't reconvert" case rather
  than Chroma/Redis's distance-based case (1 - distance / 2) - documented here so it isn't
  silently double-converted later.

Design notes:
- opensearch_url is REQUIRED (e.g. "http://localhost:9200"). No embedded/
  local mode exists for OpenSearch - callers must point this at a real
  running OpenSearch instance with the opensearch-knn plugin available
  (bundled by default in the official Docker image).
- Unlike RedisBackend (which needs a SQLite sidecar for the document
  registry because Redis HASHes have no native ORDER BY/LIMIT-OFFSET),
  OpenSearch's own query DSL handles sorting/paging natively - so the
  document registry is stored as a second real OpenSearch index
  (`<index_name>_documents`) rather than a sidecar. This is a genuine
  capability difference from Redis, not just a stylistic choice.
- Metadata filtering: metadata_filter is applied as a `bool.filter` of `term`
  queries, but only on indexed fields (document_id, document_name, tenant_id,
  chunk_index, token_count). Other caller metadata is stored as an unindexed
  JSON string, so any other filter key raises ValueError instead of silently
  matching nothing. tenant_id is also stored as a keyword field and mapped on
  existing indexes when they are opened.
- insert_chunk/insert_document/delete_document all pass refresh=True.
  This trades a little indexing throughput for immediate read-after-write
  consistency, which matters for tests and for RagLeap's own
  insert-then-search usage pattern; a high-throughput bulk-ingest path
  can be added later using the bulk API without refresh=True per call,
  but is not implemented here (see known limitations).
- supports_sparse() returns False. OpenSearch *does* support real
  full-text/BM25 search via `match` queries on text-mapped fields, and
  even hybrid dense+sparse search via search pipelines, but neither
  surface has been implemented or tested here - same "don't claim a
  capability that isn't there" rule as every other backend in this
  package (LanceDB's tantivy note, Redis's TFIDF/BM25 note, Upstash's
  hybrid-index note).

Known limitations (stated honestly, not silently ignored):
- No bulk-insert path yet - insert_chunk() indexes one document at a
  time via the single-document index API, refreshed per call. Fine for
  the moderate chunk counts this package's other backends are tested
  with, but not optimized for very large ingests.
- delete_document() issues a delete_by_query against the chunks index
  plus a direct delete against the documents-registry index; like
  RedisBackend's manual scan-then-delete, this is two real requests
  rather than one atomic operation.
"""
import datetime
import json
import logging
from typing import Dict, List, Optional

from ragleap.vectorstores.base import VectorBackend

logger = logging.getLogger(__name__)


class OpenSearchBackend(VectorBackend):
    def __init__(
        self,
        opensearch_url: str,
        index_name: str = "ragleap_idx",
        documents_index_name: Optional[str] = None,
        use_ssl: bool = False,
        verify_certs: bool = False,
        http_auth: Optional[tuple] = None,
        **client_kwargs,
    ):
        try:
            from opensearchpy import OpenSearch  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "OpenSearchBackend requires the 'opensearch' extra: "
                "pip install ragleap-vectorstores[opensearch]"
            ) from e

        if not opensearch_url:
            raise ValueError(
                "OpenSearchBackend requires opensearch_url= - e.g. "
                "'http://localhost:9200'. This must point at a real "
                "running OpenSearch instance - there is no embedded/local "
                "mode for OpenSearch, unlike Chroma/LanceDB."
            )

        self.opensearch_url = opensearch_url
        self.index_name = index_name
        self.documents_index_name = documents_index_name or f"{index_name}_documents"
        self._dimensions = None

        parsed_host, parsed_port = self._parse_url(opensearch_url)
        from opensearchpy import OpenSearch

        self._client = OpenSearch(
            hosts=[{"host": parsed_host, "port": parsed_port}],
            http_auth=http_auth,
            use_ssl=use_ssl,
            verify_certs=verify_certs,
            **client_kwargs,
        )

    @staticmethod
    def _parse_url(url: str):
        # Minimal, dependency-free host:port parse - avoids pulling in
        # urllib for what is always a simple "http(s)://host:port" value.
        stripped = url.split("://", 1)[-1]
        host_part = stripped.split("/", 1)[0]
        if ":" in host_part:
            host, port = host_part.split(":", 1)
            return host, int(port)
        return host_part, 9200

    def init_schema(self, dimensions: int) -> None:
        self._dimensions = dimensions

        if not self._client.indices.exists(index=self.index_name):
            mapping = {
                "settings": {"index": {"knn": True}},
                "mappings": {
                    "properties": {
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": dimensions,
                            "method": {
                                "name": "hnsw",
                                # REAL, LIVE-VERIFIED: "nmslib" is rejected
                                # on index creation from OpenSearch 3.0.0
                                # onward. "lucene" is the working, built-in
                                # replacement - see module docstring.
                                "engine": "lucene",
                                "space_type": "cosinesimil",
                            },
                        },
                        "document_id": {"type": "keyword"},
                        "document_name": {"type": "keyword"},
                        "tenant_id": {"type": "keyword"},
                        "chunk_index": {"type": "integer"},
                        "token_count": {"type": "integer"},
                        "text": {"type": "text"},
                        "metadata": {"type": "object", "enabled": False},
                    }
                },
            }
            self._client.indices.create(index=self.index_name, body=mapping)
            logger.info(
                f"OpenSearchBackend: created index '{self.index_name}' "
                f"(dimensions={dimensions}) at {self.opensearch_url}"
            )
        else:
            existing = self._client.indices.get_mapping(index=self.index_name)
            existing_dim = (
                existing.get(self.index_name, {})
                .get("mappings", {})
                .get("properties", {})
                .get("embedding", {})
                .get("dimension")
            )
            if existing_dim is not None and existing_dim != dimensions:
                raise RuntimeError(
                    f"OpenSearchBackend: index '{self.index_name}' already "
                    f"exists with dimension {existing_dim}, but this call "
                    f"requested dimension {dimensions}. Use a different "
                    f"index_name or a matching dimension."
                )
            try:
                self._client.indices.put_mapping(
                    index=self.index_name, body={"properties": {"tenant_id": {"type": "keyword"}}}
                )
            except Exception as e:  # a filter on an unmapped field matches nothing, so this stays safe
                logger.warning(f"OpenSearchBackend: could not add the tenant_id mapping to '{self.index_name}': {e}")
            logger.info(f"OpenSearchBackend: index '{self.index_name}' already exists, reusing it")

        if not self._client.indices.exists(index=self.documents_index_name):
            self._client.indices.create(
                index=self.documents_index_name,
                body={
                    "mappings": {
                        "properties": {
                            "filename": {"type": "keyword"},
                            "metadata": {"type": "object", "enabled": False},
                            "uploaded_at": {"type": "date"},
                        }
                    }
                },
            )
            logger.info(
                f"OpenSearchBackend: created documents-registry index "
                f"'{self.documents_index_name}' at {self.opensearch_url}"
            )

    def insert_document(self, document_id: str, filename: str, metadata: Dict) -> None:
        self._client.index(
            index=self.documents_index_name,
            id=document_id,
            body={
                "filename": filename,
                "metadata": json.dumps(metadata or {}),
                # utcnow() is deprecated on Python 3.12+, and datetime.UTC only
                # exists on Python 3.11+ (this package supports 3.10), so use
                # timezone.utc, which works on every supported version.
                "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
            refresh=True,
        )

    def insert_chunk(
        self, document_id: str, document_name: str, chunk_index: int,
        text: str, token_count: int, embedding: List[float], metadata: Dict,
    ) -> None:
        chunk_id = f"{document_id}:{chunk_index}"
        self._client.index(
            index=self.index_name,
            id=chunk_id,
            body={
                "embedding": embedding,
                "document_id": document_id,
                "document_name": document_name,
                "chunk_index": chunk_index,
                "token_count": token_count or 0,
                "text": text,
                "metadata": json.dumps(metadata or {}),
                **({"tenant_id": str(metadata["tenant_id"])} if metadata and metadata.get("tenant_id") is not None else {}),
            },
            refresh=True,
        )

    def search_dense(self, embedding: List[float], top_k: int, metadata_filter: Optional[Dict] = None) -> List[Dict]:
        if not embedding:
            return []

        knn_query = {"embedding": {"vector": embedding, "k": top_k}}

        if metadata_filter:
            # OpenSearch's query DSL supports arbitrary term filters over
            # any mapped field - unlike RedisBackend, every key passed in
            # metadata_filter is used, not just document_id.
            allowed = {"document_id", "document_name", "tenant_id", "chunk_index", "token_count"}
            unknown = sorted(set(metadata_filter) - allowed)
            if unknown:
                raise ValueError(
                    f"OpenSearchBackend can only filter by {sorted(allowed)}, got {unknown}: other metadata is "
                    "stored unindexed, so such a filter would silently match nothing"
                )
            filters = [{"term": {key: value}} for key, value in metadata_filter.items()]
            query_body = {
                "size": top_k,
                "query": {
                    "bool": {
                        "must": [{"knn": knn_query}],
                        "filter": filters,
                    }
                },
            }
        else:
            query_body = {"size": top_k, "query": {"knn": knn_query}}

        result = self._client.search(index=self.index_name, body=query_body)

        results = []
        for hit in result["hits"]["hits"]:
            src = hit["_source"]
            results.append({
                "chunk_id": hit["_id"],
                "text": src.get("text", ""),
                # REAL, LIVE-VERIFIED (derived from actual smoke-test
                # scores, not assumed from docs): the score for
                # space_type="cosinesimil" with engine="lucene" is
                # (1 + cosine_similarity) / 2, already normalized into
                # [0, 1] - used directly, same as Upstash, NOT converted
                # via "1 - distance / 2" the way Chroma/Redis are.
                "similarity_score": round(float(hit["_score"]), 4),
                "document_id": src.get("document_id"),
                "document_name": src.get("document_name"),
                "chunk_index": int(src.get("chunk_index", 0)),
            })
        return results

    def supports_sparse(self) -> bool:
        return False

    def list_documents(self, limit: int, offset: int) -> List[Dict]:
        query_body = {
            "size": limit,
            "from": offset,
            "sort": [{"uploaded_at": {"order": "desc"}}],
            "query": {"match_all": {}},
        }
        result = self._client.search(index=self.documents_index_name, body=query_body)

        results = []
        for hit in result["hits"]["hits"]:
            src = hit["_source"]
            doc_id = hit["_id"]
            count_result = self._client.count(
                index=self.index_name,
                body={"query": {"term": {"document_id": doc_id}}},
            )
            results.append({
                "document_id": doc_id,
                "filename": src.get("filename"),
                "uploaded_at": src.get("uploaded_at"),
                "metadata": json.loads(src.get("metadata", "{}")),
                "chunk_count": count_result.get("count", 0),
            })
        return results

    def delete_document(self, document_id: str) -> bool:
        self._client.delete_by_query(
            index=self.index_name,
            body={"query": {"term": {"document_id": document_id}}},
            refresh=True,
        )
        try:
            self._client.delete(index=self.documents_index_name, id=document_id, refresh=True)
            return True
        except Exception:
            return False

    def get_document_filename(self, document_id: str) -> Optional[str]:
        try:
            result = self._client.get(index=self.documents_index_name, id=document_id)
            return result["_source"].get("filename")
        except Exception:
            return None
