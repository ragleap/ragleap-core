"""
Tests for OpenSearchBackend. Uses a real OpenSearch instance with the
opensearch-knn plugin - unlike Chroma/LanceDB (embedded, always available)
and RedisBackend's dev-container-assumed-present convention, this backend
now runs on the developer's local machine (moved off the shared VPS after
a real CPU-spike incident, see the vectorstores handoff), so these tests
gate on OPENSEARCH_TEST_URL and skip cleanly (not fail) when it isn't
set - same pattern as the Upstash tests gating on
UPSTASH_VECTOR_REST_URL/TOKEN, rather than Redis's dev-container
assumption that a server is always present.

Set OPENSEARCH_TEST_URL (e.g. "http://localhost:9200") to run these for
real. Each test gets its own random index_name so parallel/repeated runs
never collide, and both indices (chunks + documents registry) are dropped
in teardown.
"""
import os
import uuid

import pytest

from ragleap_vectorstores.opensearch_backend import OpenSearchBackend

OPENSEARCH_TEST_URL = os.environ.get("OPENSEARCH_TEST_URL")

pytestmark = pytest.mark.skipif(
    not OPENSEARCH_TEST_URL,
    reason="OPENSEARCH_TEST_URL not set - skipping OpenSearchBackend tests "
           "that require a real running OpenSearch instance",
)


@pytest.fixture
def backend():
    suffix = uuid.uuid4().hex[:8]
    b = OpenSearchBackend(
        opensearch_url=OPENSEARCH_TEST_URL,
        index_name=f"test_idx_{suffix}",
    )
    b.init_schema(dimensions=3)
    yield b
    try:
        b._client.indices.delete(index=b.index_name, ignore=[404])
        b._client.indices.delete(index=b.documents_index_name, ignore=[404])
    except Exception:
        pass


def _seed(backend):
    backend.insert_document("doc1", "report.pdf", {"source": "upload"})
    backend.insert_chunk("doc1", "report.pdf", 0, "The cat sat on the mat.", 6, [0.1, 0.2, 0.3], {})
    backend.insert_chunk("doc1", "report.pdf", 1, "Dogs bark loudly outside.", 5, [0.9, 0.1, 0.1], {})
    backend.insert_document("doc2", "notes.txt", {"source": "manual"})
    backend.insert_chunk("doc2", "notes.txt", 0, "Unrelated content here.", 4, [0.5, 0.5, 0.5], {})


def test_requires_opensearch_url():
    with pytest.raises(ValueError):
        OpenSearchBackend(opensearch_url="")


def test_insert_and_search_dense_shape(backend):
    _seed(backend)
    results = backend.search_dense([0.1, 0.2, 0.3], top_k=5)
    assert results, "expected at least one result"
    assert set(results[0].keys()) == {
        "chunk_id", "text", "similarity_score", "document_id", "document_name", "chunk_index",
    }
    assert results[0]["document_id"] == "doc1"
    assert results[0]["chunk_index"] == 0
    assert results[0]["text"] == "The cat sat on the mat."


def test_search_dense_exact_match_score_near_one(backend):
    """Regression guard for the real live-verified score formula: for
    space_type='cosinesimil' with engine='lucene', score = (1 +
    cosine_similarity) / 2. An exact-match query vector should score
    very close to 1.0, not 1.0 exactly (floating point) and NOT close
    to 0.0 the way a raw '1 - distance' backend's identical convention
    would also show for a match - this pins the actual formula, not
    just "high score for exact match"."""
    backend.insert_document("doc1", "x.txt", {})
    backend.insert_chunk("doc1", "x.txt", 0, "exact match chunk", 3, [1.0, 0.0, 0.0], {})
    results = backend.search_dense([1.0, 0.0, 0.0], top_k=1)
    assert results[0]["similarity_score"] > 0.999


def test_search_dense_metadata_filter_arbitrary_keys(backend):
    """Genuine capability difference from Redis: OpenSearch's query DSL
    supports arbitrary term filters over any mapped field, not just
    document_id - live-verified in the OpenSearchBackend's own smoke
    test, pinned here as a permanent regression guard."""
    _seed(backend)
    results = backend.search_dense(
        [0.1, 0.2, 0.3], top_k=5, metadata_filter={"document_name": "notes.txt"}
    )
    assert results
    assert all(r["document_name"] == "notes.txt" for r in results)


def test_search_dense_document_id_filter(backend):
    _seed(backend)
    results = backend.search_dense([0.1, 0.2, 0.3], top_k=5, metadata_filter={"document_id": "doc2"})
    assert results
    assert all(r["document_id"] == "doc2" for r in results)


def test_search_dense_unsupported_filter_keys_raise(backend):
    """Other caller metadata is stored unindexed, so a filter on it would silently match nothing: it raises instead."""
    _seed(backend)
    with pytest.raises(ValueError, match="can only filter by"):
        backend.search_dense([0.1, 0.2, 0.3], top_k=5, metadata_filter={"nonexistent_field": "x"})


def test_search_dense_empty_embedding_returns_empty(backend):
    assert backend.search_dense([], top_k=5) == []


def test_search_sparse_not_supported(backend):
    _seed(backend)
    assert backend.search_sparse("cat", top_k=5) == []


def test_search_hybrid_falls_back_to_dense(backend):
    _seed(backend)
    hybrid = backend.search_hybrid("cat", [0.1, 0.2, 0.3], top_k=5)
    dense = backend.search_dense([0.1, 0.2, 0.3], top_k=5)
    assert [(r["document_id"], r["chunk_index"]) for r in hybrid] == \
           [(r["document_id"], r["chunk_index"]) for r in dense]


def test_supports_sparse_is_false(backend):
    assert backend.supports_sparse() is False


def test_list_documents(backend):
    _seed(backend)
    docs = backend.list_documents(limit=10, offset=0)
    assert len(docs) == 2
    by_id = {d["document_id"]: d for d in docs}
    assert by_id["doc1"]["chunk_count"] == 2
    assert by_id["doc2"]["chunk_count"] == 1
    assert by_id["doc1"]["filename"] == "report.pdf"
    assert by_id["doc1"]["metadata"] == {"source": "upload"}


def test_get_document_filename(backend):
    _seed(backend)
    assert backend.get_document_filename("doc1") == "report.pdf"
    assert backend.get_document_filename("nonexistent") is None


def test_delete_document_removes_registry_entry_and_vectors(backend):
    _seed(backend)
    assert backend.delete_document("doc1") is True

    docs = backend.list_documents(limit=10, offset=0)
    assert [d["document_id"] for d in docs] == ["doc2"]

    remaining = backend.search_dense([0.1, 0.2, 0.3], top_k=5)
    assert all(r["document_id"] != "doc1" for r in remaining)


def test_insert_chunk_upserts_not_duplicates(backend):
    """Regression guard: indexing with the same deterministic id
    (document_id:chunk_index) overwrites the document in place -
    OpenSearch's native index-by-id upsert behavior, no extra logic
    needed, same pattern as RedisBackend's HSET upsert."""
    _seed(backend)
    backend.insert_chunk("doc2", "notes.txt", 0, "UPDATED TEXT", 2, [0.5, 0.5, 0.5], {})
    results = backend.search_dense([0.5, 0.5, 0.5], top_k=5)
    assert results[0]["text"] == "UPDATED TEXT"
    docs = backend.list_documents(limit=10, offset=0)
    by_id = {d["document_id"]: d for d in docs}
    assert by_id["doc2"]["chunk_count"] == 1


def test_init_schema_idempotent(backend):
    """Calling init_schema again (e.g. on reconnect) should reuse the
    existing index rather than erroring or resetting it."""
    _seed(backend)
    backend.init_schema(dimensions=3)
    results = backend.search_dense([0.1, 0.2, 0.3], top_k=5)
    assert len(results) == 3


def test_init_schema_dimension_mismatch_raises():
    """Regression guard for a real check this backend adds beyond
    RedisBackend: reusing an existing index with a different embedding
    dimension must raise a clear RuntimeError rather than silently
    accepting mismatched vectors or failing confusingly at insert/search
    time."""
    suffix = uuid.uuid4().hex[:8]
    b = OpenSearchBackend(opensearch_url=OPENSEARCH_TEST_URL, index_name=f"dim_mismatch_{suffix}")
    try:
        b.init_schema(dimensions=3)
        with pytest.raises(RuntimeError, match="dimension"):
            b.init_schema(dimensions=5)
    finally:
        b._client.indices.delete(index=b.index_name, ignore=[404])
        b._client.indices.delete(index=b.documents_index_name, ignore=[404])
