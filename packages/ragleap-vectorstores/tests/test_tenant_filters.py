"""Tenant filter contract for every backend: a metadata_filter key must either filter or raise, never be ignored.

Chroma and LanceDB run for real (embedded). Redis, OpenSearch and Upstash use fake clients that record what the
backend sends, so no server is needed.
"""
import importlib
import sys
import types

import pytest

from ragleap_vectorstores.chroma_backend import ChromaBackend
from ragleap_vectorstores.lancedb_backend import LanceDBBackend
from ragleap_vectorstores.opensearch_backend import OpenSearchBackend
from ragleap_vectorstores.redis_backend import RedisBackend
from ragleap_vectorstores.upstash_backend import UpstashBackend

VEC = [1.0, 0.1, 0.0]


class _StubClient:
    def __init__(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def _client_libs_stubbed(monkeypatch):
    """CI installs only the chroma, lancedb and redis extras. The Upstash and OpenSearch tests use fake clients, so
    a stand-in module is put in place only when the real client library is missing; the backend constructors then
    work and the tests run everywhere."""
    for name, attrs in (("upstash_vector", {"Index": _StubClient}), ("opensearchpy", {"OpenSearch": _StubClient})):
        try:
            importlib.import_module(name)
        except ImportError:
            module = types.ModuleType(name)
            for key, value in attrs.items():
                setattr(module, key, value)
            monkeypatch.setitem(sys.modules, name, module)


def _seed_two_tenants(b):
    for tenant in ("acme", "globex"):
        b.insert_document("doc-" + tenant, tenant + ".txt", {"tenant_id": tenant})
        b.insert_chunk("doc-" + tenant, tenant + ".txt", 0, "same text", 2, VEC, {"tenant_id": tenant})


# ---------------- Chroma (real) ----------------

@pytest.fixture
def chroma(tmp_path):
    pytest.importorskip("chromadb")
    b = ChromaBackend(persist_directory=str(tmp_path / "c"))
    b.init_schema(3)
    return b


def test_chroma_tenant_filter_returns_only_that_tenant(chroma):
    _seed_two_tenants(chroma)
    res = chroma.search_dense(VEC, 10, {"tenant_id": "acme"})
    assert res and {r["document_id"] for r in res} == {"doc-acme"}
    assert {r["document_id"] for r in chroma.search_dense(VEC, 10)} == {"doc-acme", "doc-globex"}


def test_chroma_caller_metadata_cannot_override_stored_fields(chroma):
    chroma.insert_document("doc1", "a.txt", {})
    chroma.insert_chunk("doc1", "a.txt", 0, "t", 1, VEC, {"document_id": "evil", "document_name": "x", "chunk_index": 99})
    res = chroma.search_dense(VEC, 5)
    assert res and res[0]["document_id"] == "doc1" and res[0]["document_name"] == "a.txt" and res[0]["chunk_index"] == 0


# ---------------- LanceDB (real) ----------------

@pytest.fixture
def lance(tmp_path):
    pytest.importorskip("lancedb")
    b = LanceDBBackend(uri=str(tmp_path / "l"))
    b.init_schema(3)
    return b


def test_lancedb_tenant_filter_returns_only_that_tenant(lance):
    _seed_two_tenants(lance)
    res = lance.search_dense(VEC, 10, {"tenant_id": "acme"})
    assert res and {r["document_id"] for r in res} == {"doc-acme"}
    assert {r["document_id"] for r in lance.search_dense(VEC, 10)} == {"doc-acme", "doc-globex"}


def test_lancedb_filter_key_cannot_inject_sql(lance):
    _seed_two_tenants(lance)
    with pytest.raises(ValueError):
        lance.search_dense(VEC, 10, {"document_id = 'doc-acme' OR document_id": "zzz"})


# ---------------- Redis (fake client, no server) ----------------

class _FakeFT:
    def __init__(self, log):
        self.log = log

    def search(self, q, query_params=None):
        self.log.append(q.query_string())
        return types.SimpleNamespace(docs=[], total=0)


class _FakeRedis:
    def __init__(self):
        self.queries, self.hashes = [], []

    def ft(self, name):
        return _FakeFT(self.queries)

    def hset(self, key, mapping=None):
        self.hashes.append((key, mapping))


@pytest.fixture
def redis_backend(tmp_path):
    b = RedisBackend(redis_url="redis://127.0.0.1:1/0", index_name="t_idx", registry_path=str(tmp_path / "r.sqlite3"))
    b._client = _FakeRedis()
    return b


def test_redis_document_id_filter_still_applies(redis_backend):
    redis_backend.search_dense(VEC, 5, {"document_id": "doc1"})
    assert "document_id" in redis_backend._client.queries[-1]


def test_redis_stores_tenant_id_as_a_field(redis_backend):
    redis_backend.insert_chunk("doc1", "a.txt", 0, "t", 1, VEC, {"tenant_id": "acme"})
    assert redis_backend._client.hashes[-1][1].get("tenant_id") == "acme"


def test_redis_search_filters_on_tenant_id(redis_backend):
    redis_backend.search_dense(VEC, 5, {"tenant_id": "acme"})
    q = redis_backend._client.queries[-1]
    assert "tenant_id" in q and "acme" in q


def test_redis_unsupported_filter_key_raises_instead_of_being_ignored(redis_backend):
    with pytest.raises(ValueError):
        redis_backend.search_dense(VEC, 5, {"foo": "bar"})


# ---------------- OpenSearch (fake client, no server) ----------------

class _Indices:
    def __init__(self, existing=()):
        self.existing, self.created, self.put = set(existing), [], []

    def exists(self, index):
        return index in self.existing

    def create(self, index, body):
        self.created.append((index, body))
        self.existing.add(index)

    def get_mapping(self, index):
        return {index: {"mappings": {"properties": {"embedding": {"dimension": 3}}}}}

    def put_mapping(self, index, body):
        self.put.append((index, body))


class _FakeOS:
    def __init__(self, existing=()):
        self.indices = _Indices(existing)
        self.indexed, self.searches = [], []

    def index(self, index, id, body, refresh=None):
        self.indexed.append((index, id, body))

    def search(self, index, body):
        self.searches.append(body)
        return {"hits": {"hits": []}}


def _os_backend(existing=False):
    b = OpenSearchBackend(opensearch_url="http://127.0.0.1:1", index_name="t_idx")
    b._client = _FakeOS([b.index_name, b.documents_index_name] if existing else [])
    return b


def test_opensearch_new_index_maps_tenant_id_as_keyword():
    b = _os_backend()
    b.init_schema(3)
    props = dict(b._client.indices.created)[b.index_name]["mappings"]["properties"]
    assert props.get("tenant_id") == {"type": "keyword"}


def test_opensearch_existing_index_gets_the_tenant_id_mapping():
    b = _os_backend(existing=True)
    b.init_schema(3)
    assert any(body.get("properties", {}).get("tenant_id") == {"type": "keyword"} for _, body in b._client.indices.put)


def test_opensearch_stores_tenant_id_as_a_top_level_field():
    b = _os_backend()
    b.insert_chunk("doc1", "a.txt", 0, "t", 1, VEC, {"tenant_id": "acme"})
    assert b._client.indexed[-1][2].get("tenant_id") == "acme"


def test_opensearch_search_filters_on_tenant_id():
    b = _os_backend()
    b.search_dense(VEC, 5, {"tenant_id": "acme"})
    assert {"term": {"tenant_id": "acme"}} in b._client.searches[-1]["query"]["bool"]["filter"]


def test_opensearch_unsupported_filter_key_raises_instead_of_matching_nothing():
    b = _os_backend()
    with pytest.raises(ValueError):
        b.search_dense(VEC, 5, {"foo": "bar"})


# ---------------- Upstash (fake index) ----------------

class _FakeUpstash:
    def __init__(self):
        self.queries = []

    def query(self, **kw):
        self.queries.append(kw)
        return []


@pytest.fixture
def upstash(tmp_path):
    b = UpstashBackend(url="https://example.upstash.io", token="example-token", namespace="ns1",
                       registry_path=str(tmp_path / "u.sqlite3"))
    b._index = _FakeUpstash()
    return b


def test_upstash_filters_on_tenant_id_inside_its_namespace(upstash):
    upstash.search_dense(VEC, 5, {"tenant_id": "acme"})
    kw = upstash._index.queries[-1]
    assert kw["filter"] == "tenant_id = 'acme'" and kw["namespace"] == "ns1"


def test_upstash_filter_key_cannot_inject_a_condition(upstash):
    with pytest.raises(ValueError):
        upstash.search_dense(VEC, 5, {"a = 1 OR b": "x"})


# ---------------- extra coverage ----------------

def test_lancedb_reopens_an_old_table_and_adds_the_tenant_column(tmp_path):
    lancedb = pytest.importorskip("lancedb")
    import pyarrow as pa
    uri = str(tmp_path / "old")
    lancedb.connect(uri).create_table("ragleap", schema=pa.schema([
        pa.field("id", pa.string()), pa.field("vector", pa.list_(pa.float32(), 3)), pa.field("text", pa.string()),
        pa.field("document_id", pa.string()), pa.field("document_name", pa.string()),
        pa.field("chunk_index", pa.int64()), pa.field("token_count", pa.int64()),
    ]))
    b = LanceDBBackend(uri=uri)
    b.init_schema(3)
    _seed_two_tenants(b)
    res = b.search_dense(VEC, 10, {"tenant_id": "acme"})
    assert res and {r["document_id"] for r in res} == {"doc-acme"}


def test_lancedb_untagged_chunks_never_match_a_tenant_filter(lance):
    lance.insert_document("old", "old.txt", {})
    lance.insert_chunk("old", "old.txt", 0, "same text", 2, VEC, {})
    lance.insert_document("doc-acme", "acme.txt", {"tenant_id": "acme"})
    lance.insert_chunk("doc-acme", "acme.txt", 0, "same text", 2, VEC, {"tenant_id": "acme"})
    assert {r["document_id"] for r in lance.search_dense(VEC, 10, {"tenant_id": "acme"})} == {"doc-acme"}
    assert {r["document_id"] for r in lance.search_dense(VEC, 10)} == {"old", "doc-acme"}


def test_lancedb_tenant_filter_without_the_column_raises(lance):
    lance._tenant_field = False
    with pytest.raises(RuntimeError):
        lance.search_dense(VEC, 10, {"tenant_id": "acme"})
    lance.search_dense(VEC, 10, {"document_id": "x"})


def test_redis_tenant_filter_on_an_index_without_the_field_raises(redis_backend):
    redis_backend._tenant_field = False
    with pytest.raises(RuntimeError):
        redis_backend.search_dense(VEC, 5, {"tenant_id": "acme"})
    redis_backend.search_dense(VEC, 5, {"document_id": "d"})


def test_opensearch_mapping_update_failure_does_not_break_init():
    b = _os_backend(existing=True)

    def boom(index, body):
        raise RuntimeError("mapping update refused")

    b._client.indices.put_mapping = boom
    b.init_schema(3)


def test_chroma_insert_without_metadata_still_stores_the_chunk_fields(chroma):
    chroma.insert_document("doc1", "a.txt", {})
    chroma.insert_chunk("doc1", "a.txt", 2, "t", 1, VEC, None)
    res = chroma.search_dense(VEC, 5)
    assert res[0]["document_id"] == "doc1" and res[0]["chunk_index"] == 2


@pytest.mark.parametrize("key", ["foo", "x", "text", "vector", "tenant"])
def test_lancedb_rejects_unknown_filter_keys(lance, key):
    with pytest.raises(ValueError, match="can only filter by"):
        lance.search_dense(VEC, 10, {key: "v"})


def test_redis_combined_filters_are_one_parenthesised_prefilter(redis_backend):
    """Found by the real-server test: two separate (...) groups before =>[KNN ...] are a RediSearch syntax error."""
    redis_backend.search_dense(VEC, 5, {"document_id": "d1", "tenant_id": "acme"})
    assert redis_backend._client.queries[-1].startswith("(@document_id:{d1} @tenant_id:{acme})=>[KNN"), \
        redis_backend._client.queries[-1]
    redis_backend.search_dense(VEC, 5, {"tenant_id": "acme"})
    assert redis_backend._client.queries[-1].startswith("(@tenant_id:{acme})=>[KNN")
