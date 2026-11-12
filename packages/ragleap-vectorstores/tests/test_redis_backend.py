"""
Tests for RedisBackend. Uses a real Redis Stack instance (RediSearch module
loaded) running in an isolated dev container on port 6380 - separate from
any production Redis used for caching/Celery. Each test gets its own
random index_name/key_prefix so parallel/repeated runs never collide, and
the index is dropped in teardown.

Unlike Chroma/LanceDB (embedded, no live-credentials gap to skip around),
this backend needs a real running server - REDIS_TEST_URL below must point
at one with the 'search' module loaded, or every test here will fail at
init_schema() with the same RuntimeError users would see in production.
"""
import uuid

import os

import pytest

from ragleap_vectorstores.redis_backend import RedisBackend

REDIS_TEST_URL = os.environ.get("REDIS_TEST_URL", "redis://localhost:6380/0")
# A separate, real production-shaped Redis with NO search module loaded -
# used only by test_missing_search_module_raises() to prove the real error
# path, exactly as encountered live on srv1477778's production Redis 6.0.16.
REDIS_NO_SEARCH_URL = "redis://localhost:6379/2"


@pytest.fixture
def backend(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    b = RedisBackend(
        redis_url=REDIS_TEST_URL,
        index_name=f"test_idx_{suffix}",
        key_prefix=f"test:chunk:{suffix}:",
        registry_path=str(tmp_path / "registry.sqlite3"),
    )
    b.init_schema(dimensions=3)
    yield b
    try:
        b._client.ft(b.index_name).dropindex(delete_documents=True)
    except Exception:
        pass


def _seed(backend):
    backend.insert_document("doc1", "report.pdf", {"source": "upload"})
    backend.insert_chunk("doc1", "report.pdf", 0, "The cat sat on the mat.", 6, [0.1, 0.2, 0.3], {})
    backend.insert_chunk("doc1", "report.pdf", 1, "Dogs bark loudly outside.", 5, [0.9, 0.1, 0.1], {})
    backend.insert_document("doc2", "notes.txt", {"source": "manual"})
    backend.insert_chunk("doc2", "notes.txt", 0, "Unrelated content here.", 4, [0.5, 0.5, 0.5], {})


def test_requires_redis_url():
    with pytest.raises(ValueError):
        RedisBackend(redis_url="")


def test_missing_search_module_raises(tmp_path):
    """Regression guard for the real gotcha encountered live: production
    Redis 6.0.16 on srv1477778 has no RediSearch module (MODULE LIST
    returned []). init_schema() must raise a clear RuntimeError rather
    than failing confusingly later at query time."""
    b = RedisBackend(
        redis_url=REDIS_NO_SEARCH_URL,
        index_name="should_never_be_created",
        registry_path=str(tmp_path / "registry.sqlite3"),
    )
    with pytest.raises(RuntimeError, match="search"):
        b.init_schema(dimensions=3)


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
    assert results[0]["similarity_score"] == 1.0


def test_search_dense_document_id_filter(backend):
    _seed(backend)
    results = backend.search_dense([0.1, 0.2, 0.3], top_k=5, metadata_filter={"document_id": "doc2"})
    assert results
    assert all(r["document_id"] == "doc2" for r in results)


def test_document_id_with_hyphens_is_escaped(backend):
    """Regression guard for the real TAG-escaping gotcha (live-verified):
    an un-escaped UUID-style document_id inside a TAG query silently
    returns zero matches, since RediSearch parses hyphens as query syntax
    rather than literal characters."""
    uuid_doc_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    backend.insert_document(uuid_doc_id, "hyphenated.txt", {})
    backend.insert_chunk(uuid_doc_id, "hyphenated.txt", 0, "hyphen test", 2, [0.2, 0.2, 0.2], {})
    results = backend.search_dense([0.2, 0.2, 0.2], top_k=5, metadata_filter={"document_id": uuid_doc_id})
    assert len(results) == 1
    assert results[0]["document_id"] == uuid_doc_id


def test_search_dense_unsupported_filter_keys_raise(backend):
    """RediSearch only has document_id and tenant_id as filterable fields. Any other key used to be ignored silently,
    which let a tenant filter search every tenant; it now raises instead."""
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
    assert backend.delete_document("doc1") is False  # already gone

    docs = backend.list_documents(limit=10, offset=0)
    assert [d["document_id"] for d in docs] == ["doc2"]

    remaining = backend.search_dense([0.1, 0.2, 0.3], top_k=5)
    assert all(r["document_id"] != "doc1" for r in remaining)


def test_insert_chunk_upserts_not_duplicates(backend):
    """Regression guard: HSET on the same deterministic key
    (key_prefix+document_id:chunk_index) overwrites the hash in place -
    Redis's native upsert behavior, no extra logic needed, unlike
    LanceDB's explicit merge_insert() path."""
    _seed(backend)
    backend.insert_chunk("doc2", "notes.txt", 0, "UPDATED TEXT", 2, [0.5, 0.5, 0.5], {})
    results = backend.search_dense([0.5, 0.5, 0.5], top_k=5)
    assert results[0]["text"] == "UPDATED TEXT"
    docs = backend.list_documents(limit=10, offset=0)
    by_id = {d["document_id"]: d for d in docs}
    assert by_id["doc2"]["chunk_count"] == 1


def test_init_schema_idempotent(backend):
    """Calling init_schema again (e.g. on reconnect) should reuse the
    existing index via ft().info(), not error or reset it."""
    _seed(backend)
    backend.init_schema(dimensions=3)
    results = backend.search_dense([0.1, 0.2, 0.3], top_k=5)
    assert len(results) == 3


def test_default_key_prefix_isolates_different_indexes(tmp_path):
    """Regression guard for a real bug, live-verified: key_prefix used to
    default to the same literal string regardless of index_name, so two
    RedisBackend instances with different index_name but both left at the
    default key_prefix silently shared the same Redis keyspace - creating
    the second index caused RediSearch to auto-index the first instance's
    leftover chunks too, and a fresh KNN query could return stale data
    from a completely unrelated instance. Reproduced live: three separate
    verification runs with different index_names, default key_prefix,
    all landed in "ragleap:chunk:*", and a top_k=1 query on the newest
    one returned an old chunk from an earlier one. This test creates two
    instances with different index_name and NO explicit key_prefix, and
    verifies each only ever sees its own chunks."""
    suffix_a = uuid.uuid4().hex[:8]
    suffix_b = uuid.uuid4().hex[:8]
    a = RedisBackend(
        redis_url=REDIS_TEST_URL,
        index_name=f"isolation_a_{suffix_a}",
        registry_path=str(tmp_path / "registry_a.sqlite3"),
    )
    b = RedisBackend(
        redis_url=REDIS_TEST_URL,
        index_name=f"isolation_b_{suffix_b}",
        registry_path=str(tmp_path / "registry_b.sqlite3"),
    )
    try:
        a.init_schema(dimensions=3)
        b.init_schema(dimensions=3)

        assert a.key_prefix != b.key_prefix, "default key_prefix must differ when index_name differs"

        a.insert_document("doc_a", "a.txt", {})
        a.insert_chunk("doc_a", "a.txt", 0, "chunk from instance A", 3, [0.1, 0.1, 0.1], {})
        b.insert_document("doc_b", "b.txt", {})
        b.insert_chunk("doc_b", "b.txt", 0, "chunk from instance B", 3, [0.1, 0.1, 0.1], {})

        results_a = a.search_dense([0.1, 0.1, 0.1], top_k=5)
        results_b = b.search_dense([0.1, 0.1, 0.1], top_k=5)

        assert len(results_a) == 1, "instance A must only see its own chunk, not instance B's"
        assert results_a[0]["document_id"] == "doc_a"
        assert len(results_b) == 1, "instance B must only see its own chunk, not instance A's"
        assert results_b[0]["document_id"] == "doc_b"
    finally:
        for inst in (a, b):
            try:
                inst._client.ft(inst.index_name).dropindex(delete_documents=True)
            except Exception:
                pass


def test_search_dense_scores_are_on_the_zero_to_one_scale(backend):
    """RediSearch returns a cosine DISTANCE in [0, 2]. The similarity is mapped to [0, 1] like the other backends
    (identical 1.0, orthogonal 0.5, opposite 0.0), not left as the raw cosine in [-1, 1]."""
    import time

    backend.insert_document("scale", "scale.txt", {})
    for i, vec in enumerate([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]]):
        backend.insert_chunk("scale", "scale.txt", i, "chunk %d" % i, 1, vec, {})
    by_index = {}
    for _ in range(50):
        results = backend.search_dense([1.0, 0.0, 0.0], top_k=3)
        by_index = {r["chunk_index"]: r["similarity_score"] for r in results}
        if len(by_index) == 3:
            break
        time.sleep(0.1)
    assert by_index == {0: 1.0, 1: 0.5, 2: 0.0}


def _wait_for_chunks(backend, expected):
    """RediSearch indexes asynchronously: poll the unfiltered search until every inserted chunk is visible."""
    import time

    seen = set()
    for _ in range(50):
        seen = {r["document_id"] for r in backend.search_dense([1.0, 0.1, 0.0], top_k=20)}
        if len(seen) >= expected:
            return seen
        time.sleep(0.1)
    return seen


def test_redis_tenant_filter_isolates_tenants(backend):
    vec = [1.0, 0.1, 0.0]
    for tenant in ("acme", "globex"):
        backend.insert_document("doc-" + tenant, tenant + ".txt", {"tenant_id": tenant})
        backend.insert_chunk("doc-" + tenant, tenant + ".txt", 0, "same text", 2, vec, {"tenant_id": tenant})
    backend.insert_document("doc-legacy", "legacy.txt", {})
    backend.insert_chunk("doc-legacy", "legacy.txt", 0, "same text", 2, vec, {})
    assert _wait_for_chunks(backend, 3) == {"doc-acme", "doc-globex", "doc-legacy"}
    assert {r["document_id"] for r in backend.search_dense(vec, top_k=20, metadata_filter={"tenant_id": "acme"})} == {"doc-acme"}
    assert {r["document_id"] for r in backend.search_dense(vec, top_k=20, metadata_filter={"tenant_id": "globex"})} == {"doc-globex"}
    assert backend.search_dense(vec, top_k=20, metadata_filter={"tenant_id": "nobody"}) == []


def test_redis_document_id_and_tenant_id_filters_combine(backend):
    vec = [1.0, 0.1, 0.0]
    for tenant in ("acme", "globex"):
        backend.insert_document("doc-" + tenant, tenant + ".txt", {"tenant_id": tenant})
        backend.insert_chunk("doc-" + tenant, tenant + ".txt", 0, "same text", 2, vec, {"tenant_id": tenant})
    assert _wait_for_chunks(backend, 2) == {"doc-acme", "doc-globex"}
    own = backend.search_dense(vec, top_k=20, metadata_filter={"document_id": "doc-acme", "tenant_id": "acme"})
    assert {r["document_id"] for r in own} == {"doc-acme"}
    other = backend.search_dense(vec, top_k=20, metadata_filter={"document_id": "doc-acme", "tenant_id": "globex"})
    assert other == []


def test_redis_index_created_before_tenant_id_raises_on_a_tenant_filter(tmp_path):
    import uuid

    from redis.commands.search.field import NumericField, TagField, TextField, VectorField
    from redis.commands.search.index_definition import IndexDefinition, IndexType

    suffix = uuid.uuid4().hex[:8]
    name, prefix = "old_idx_" + suffix, "test:oldchunk:" + suffix + ":"
    b = RedisBackend(redis_url=REDIS_TEST_URL, index_name=name, key_prefix=prefix, registry_path=str(tmp_path / "r.sqlite3"))
    b.init_schema(dimensions=3)
    try:
        b._client.ft(name).dropindex(delete_documents=True)
        old_schema = (
            TagField("document_id"), TextField("document_name"), NumericField("chunk_index"),
            NumericField("token_count"), TextField("text"),
            VectorField("vector", "HNSW", {"TYPE": "FLOAT32", "DIM": 3, "DISTANCE_METRIC": "COSINE"}),
        )
        b._client.ft(name).create_index(old_schema, definition=IndexDefinition(prefix=[prefix], index_type=IndexType.HASH))
        b2 = RedisBackend(redis_url=REDIS_TEST_URL, index_name=name, key_prefix=prefix, registry_path=str(tmp_path / "r2.sqlite3"))
        b2.init_schema(dimensions=3)
        with pytest.raises(RuntimeError, match="without a tenant_id field"):
            b2.search_dense([1.0, 0.1, 0.0], top_k=5, metadata_filter={"tenant_id": "acme"})
        b2.search_dense([1.0, 0.1, 0.0], top_k=5, metadata_filter={"document_id": "x"})
    finally:
        try:
            b._client.ft(name).dropindex(delete_documents=True)
        except Exception:
            pass
