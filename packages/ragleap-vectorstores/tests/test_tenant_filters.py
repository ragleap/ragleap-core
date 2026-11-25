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


def _hit(doc_id, tenant, key=None, name="a.txt", idx=0):
    return types.SimpleNamespace(
        id=key or f"k:{doc_id}:{idx}", text="t", score="0.1", document_id=doc_id, document_name=name,
        chunk_index=str(idx), tenant_id=tenant,
    )


class _HitsFT:
    def __init__(self, docs, log):
        self.docs, self.log = docs, log

    def search(self, q, query_params=None):
        self.log.append(q.query_string())
        return types.SimpleNamespace(docs=list(self.docs), total=len(self.docs))


class _HitsRedis:
    def __init__(self, docs):
        self.docs, self.queries, self.deleted = docs, [], []

    def ft(self, name):
        return _HitsFT(self.docs, self.queries)

    def delete(self, *keys):
        self.deleted.extend(keys)


def _backend_with_hits(tmp_path, docs):
    b = RedisBackend(redis_url="redis://127.0.0.1:1/0", index_name="t_idx", registry_path=str(tmp_path / "r.sqlite3"))
    b._client = _HitsRedis(docs)
    return b


def test_redis_search_drops_a_chunk_of_a_tenant_whose_id_differs_only_in_case(tmp_path):
    # an index made by 0.7.0 or older returns both of these for the filter tenant_id=ACME
    b = _backend_with_hits(tmp_path, [_hit("d1", "acme"), _hit("d2", "ACME"), _hit("d3", None)])
    got = b.search_dense(VEC, 5, {"tenant_id": "ACME"})
    assert [r["document_id"] for r in got] == ["d2"]


def test_redis_search_exact_check_also_handles_bytes_values(tmp_path):
    doc = _hit("d1", b"ACME")
    b = _backend_with_hits(tmp_path, [doc, _hit("d2", b"acme")])
    assert [r["document_id"] for r in b.search_dense(VEC, 5, {"tenant_id": "ACME"})] == ["d1"]


def test_redis_search_exact_check_applies_to_document_id(tmp_path):
    b = _backend_with_hits(tmp_path, [_hit("Doc1", "acme"), _hit("doc1", "acme")])
    got = b.search_dense(VEC, 5, {"document_id": "doc1", "tenant_id": "acme"})
    assert [r["document_id"] for r in got] == ["doc1"]


def test_redis_search_without_filter_returns_everything_and_no_extra_keys(tmp_path):
    b = _backend_with_hits(tmp_path, [_hit("d1", "acme"), _hit("d2", None)])
    got = b.search_dense(VEC, 5)
    assert len(got) == 2
    assert set(got[0]) == {"chunk_id", "text", "similarity_score", "document_id", "document_name", "chunk_index"}


def test_redis_search_asks_for_the_tenant_id_field(tmp_path):
    b = _backend_with_hits(tmp_path, [])
    seen = {}
    real = _HitsFT.search

    def spy(self, q, query_params=None):
        seen["fields"] = list(q._return_fields)
        return real(self, q, query_params)

    _HitsFT.search = spy
    try:
        b.search_dense(VEC, 5, {"tenant_id": "acme"})
    finally:
        _HitsFT.search = real
    assert "tenant_id" in seen["fields"]


def test_redis_delete_removes_only_chunks_with_exactly_that_document_id(tmp_path):
    b = _backend_with_hits(tmp_path, [_hit("Doc1", "x", key="k:upper"), _hit("doc1", "x", key="k:lower")])
    b.delete_document("doc1")
    assert b._client.deleted == ["k:lower"]


def test_redis_new_index_has_case_sensitive_tenant_and_document_id_fields(tmp_path, monkeypatch):
    import redis

    created = {}

    class _FT:
        def info(self):
            raise RuntimeError("no such index")

        def create_index(self, schema, definition=None):
            created["schema"] = schema

    class _Client:
        def execute_command(self, *a):
            return [[b"name", b"search"]]

        def ft(self, name):
            return _FT()

    monkeypatch.setattr(redis, "from_url", lambda *a, **k: _Client())
    b = RedisBackend(redis_url="redis://127.0.0.1:1/0", index_name="t_idx", registry_path=str(tmp_path / "r.sqlite3"))
    b.init_schema(3)
    tags = {f.name: [str(x) for x in f.redis_args()] for f in created["schema"] if "TAG" in [str(x) for x in f.redis_args()]}
    assert set(tags) == {"document_id", "tenant_id"}
    assert all("CASESENSITIVE" in args for args in tags.values())


@pytest.mark.parametrize("info,expected", [
    ({"attributes": [[b"identifier", b"tenant_id", b"attribute", b"tenant_id", b"type", b"TAG", b"SEPARATOR", b",", b"CASESENSITIVE"]]}, True),
    ({"attributes": [[b"identifier", b"tenant_id", b"attribute", b"tenant_id", b"type", b"TAG", b"SEPARATOR", b","]]}, False),
    ({b"attributes": [{b"identifier": b"tenant_id", b"type": b"TAG", b"flags": [b"CASESENSITIVE"]}]}, True),
    ({"attributes": [{"identifier": "tenant_id", "type": "TAG", "flags": []}]}, False),
    ({"attributes": [[b"identifier", b"document_id", b"type", b"TAG", b"CASESENSITIVE"]]}, None),
    ({"attributes": [[b"identifier", b"tenant_id", b"type", b"TEXT", b"CASESENSITIVE"]]}, None),
    ({}, None),
    ("not a dict", None),
])
def test_tag_case_sensitive_reads_ft_info_in_either_shape(info, expected):
    from ragleap_vectorstores.redis_backend import _tag_case_sensitive
    assert _tag_case_sensitive(info, "tenant_id") is expected


@pytest.mark.parametrize("info,warns", [
    ({"attributes": [[b"identifier", b"tenant_id", b"type", b"TAG", b"SEPARATOR", b","]]}, True),
    ({"attributes": [[b"identifier", b"tenant_id", b"type", b"TAG", b"CASESENSITIVE"]]}, False),
    ({"attributes": [[b"identifier", b"tenant_id", b"mystery", b"shape"]]}, True),  # flags cannot be read
])
def test_redis_existing_index_warns_unless_tenant_id_is_known_case_sensitive(tmp_path, monkeypatch, caplog, info, warns):
    import logging
    import redis

    class _FT:
        def info(self):
            return info

    class _Client:
        def execute_command(self, *a):
            return [[b"name", b"search"]]

        def ft(self, name):
            return _FT()

    monkeypatch.setattr(redis, "from_url", lambda *a, **k: _Client())
    b = RedisBackend(redis_url="redis://127.0.0.1:1/0", index_name="t_idx", registry_path=str(tmp_path / "r.sqlite3"))
    with caplog.at_level(logging.WARNING):
        b.init_schema(3)
    assert ("without regard to case" in caplog.text) is warns


def test_redis_delete_asks_for_the_document_id_field(tmp_path):
    b = _backend_with_hits(tmp_path, [])
    seen = {}
    real = _HitsFT.search

    def spy(self, q, query_params=None):
        seen["fields"] = list(q._return_fields)
        return real(self, q, query_params)

    _HitsFT.search = spy
    try:
        b.delete_document("doc1")
    finally:
        _HitsFT.search = real
    assert "document_id" in seen["fields"]


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
