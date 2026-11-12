"""Tenant safety of rag's Pinecone, Qdrant, Milvus and Weaviate backends (fake clients, no servers) and of tenant ids."""
import sys
import types
from types import SimpleNamespace

import pytest

from ragleap.tenant import TenantRagLeap

VEC = [1.0, 0.1, 0.0]
EVIL = {"document_id": "evil", "document_name": "evil.txt", "chunk_index": 99, "tenant_id": "acme", "page": 2}
KINDS = ["pinecone", "qdrant", "milvus", "weaviate"]


@pytest.fixture(autouse=True)
def _client_libs(monkeypatch):
    """The Pinecone and Milvus constructors only check that their client library imports; a stand-in module is
    used only when the real one is missing."""
    for name in ("pinecone", "pymilvus"):
        try:
            __import__(name)
        except ImportError:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))


class _FakePineconeIndex:
    def __init__(self):
        self.payload = None

    def upsert(self, vectors):
        self.payload = vectors[0][2]


class _FakeQdrant:
    def __init__(self):
        self.payload = None
        self.query_kw = None

    def upsert(self, collection_name, points):
        self.payload = points[0].payload

    def query_points(self, **kw):
        self.query_kw = kw
        return SimpleNamespace(points=[])


class _FakeMilvus:
    def __init__(self):
        self.payload = None
        self.search_kw = None

    def insert(self, collection_name, data):
        self.payload = data[0]

    def search(self, **kw):
        self.search_kw = kw
        return [[]]


class _FakeWeaviate:
    def __init__(self):
        self.payload = None
        self.query_kw = None
        self.data = SimpleNamespace(insert=self._insert)
        self.query = SimpleNamespace(near_vector=self._near)

    def _insert(self, properties, vector, uuid):
        self.payload = properties

    def _near(self, **kw):
        self.query_kw = kw
        return SimpleNamespace(objects=[])


def make(kind, tmp_path):
    d = str(tmp_path / kind)
    if kind == "pinecone":
        from ragleap.vectorstores.pinecone_backend import PineconeBackend
        b = PineconeBackend(persist_directory=d, api_key="example-key")
        b._index = fake = _FakePineconeIndex()
    elif kind == "qdrant":
        pytest.importorskip("qdrant_client")
        from ragleap.vectorstores.qdrant_backend import QdrantBackend
        b = QdrantBackend(persist_directory=d, url="http://127.0.0.1:1")
        b._client = fake = _FakeQdrant()
    elif kind == "milvus":
        from ragleap.vectorstores.milvus_backend import MilvusBackend
        b = MilvusBackend(persist_directory=d, uri="http://127.0.0.1:1")
        b._client = fake = _FakeMilvus()
    else:
        pytest.importorskip("weaviate")
        from ragleap.vectorstores.weaviate_backend import WeaviateBackend
        b = WeaviateBackend(persist_directory=d)
        b._collection = fake = _FakeWeaviate()
    return b, fake


# ---------- stored fields must win over caller metadata ----------

@pytest.mark.parametrize("kind", KINDS)
def test_caller_metadata_cannot_overwrite_stored_fields(kind, tmp_path):
    b, fake = make(kind, tmp_path)
    b.insert_chunk("doc1", "a.txt", 3, "t", 1, VEC, dict(EVIL))
    p = fake.payload
    assert p["document_id"] == "doc1" and p["document_name"] == "a.txt" and p["chunk_index"] == 3, p
    assert p["tenant_id"] == "acme" and p["page"] == 2


@pytest.mark.parametrize("kind", KINDS)
def test_insert_without_metadata_still_stores_the_chunk_fields(kind, tmp_path):
    b, fake = make(kind, tmp_path)
    b.insert_chunk("doc1", "a.txt", 2, "t", 1, VEC, None)
    assert fake.payload["document_id"] == "doc1" and fake.payload["chunk_index"] == 2


# ---------- the tenant filter reaches the client (these work today: pinned) ----------

def test_pinecone_tenant_filter_shape(tmp_path):
    b, _ = make("pinecone", tmp_path)
    assert b._build_filter({"tenant_id": "acme"}) == {"tenant_id": {"$eq": "acme"}}


def test_qdrant_tenant_filter_reaches_the_query(tmp_path):
    b, fake = make("qdrant", tmp_path)
    b.search_dense(VEC, 5, {"tenant_id": "acme"})
    cond = fake.query_kw["query_filter"].must[0]
    assert cond.key == "tenant_id" and cond.match.value == "acme"


def test_milvus_tenant_filter_reaches_the_search(tmp_path):
    b, fake = make("milvus", tmp_path)
    b.search_dense(VEC, 5, {"tenant_id": "acme"})
    assert fake.search_kw["filter"] == 'tenant_id == "acme"'


def test_weaviate_tenant_filter_reaches_the_query(tmp_path):
    b, fake = make("weaviate", tmp_path)
    b.search_dense(VEC, 5, {"tenant_id": "acme"})
    assert fake.query_kw["filters"] is not None
    b.search_dense(VEC, 5, None)
    assert fake.query_kw["filters"] is None


# ---------- Milvus filter expression ----------

def test_milvus_simple_filters_are_unchanged(tmp_path):
    b, _ = make("milvus", tmp_path)
    assert b._build_filter_expr({"tenant_id": "acme", "chunk_index": 3}) == 'tenant_id == "acme" and chunk_index == 3'
    assert b._build_filter_expr(None) == ""


def test_milvus_filter_value_with_a_quote_is_escaped(tmp_path):
    b, _ = make("milvus", tmp_path)
    expr = b._build_filter_expr({"tenant_id": 'acme" or tenant_id != "'})
    assert expr == 'tenant_id == "acme\\" or tenant_id != \\""'


def test_milvus_filter_value_with_a_trailing_backslash_is_escaped(tmp_path):
    b, _ = make("milvus", tmp_path)
    assert b._build_filter_expr({"tenant_id": "x\\"}) == 'tenant_id == "x\\\\"'


@pytest.mark.parametrize("key", ["a == 1 or b", "tenant id", "", "a)", "x.y"])
def test_milvus_unsafe_filter_keys_raise(tmp_path, key):
    b, _ = make("milvus", tmp_path)
    with pytest.raises(ValueError, match="filter key"):
        b._build_filter_expr({key: "v"})


@pytest.mark.parametrize("value", [{"a": 1}, ["x"], None])
def test_milvus_unsupported_filter_value_types_raise(tmp_path, value):
    b, _ = make("milvus", tmp_path)
    with pytest.raises(ValueError, match="filter value"):
        b._build_filter_expr({"tenant_id": value})


# ---------- tenant ids ----------

class _Rec:
    pass


@pytest.mark.parametrize("tid", ["acme", "a_b-c.d@e.com", "x" * 128, "123e4567-e89b-12d3-a456-426614174000", "T1"])
def test_safe_tenant_ids_are_accepted(tid):
    assert TenantRagLeap(_Rec(), tid).tenant_id == tid


@pytest.mark.parametrize("tid", ['a"b', "a'b", "a b", "a\\b", "a/b", "a\nb", "a;b", "a=b", "é", "x" * 129, "a:b", ""])
def test_unsafe_tenant_ids_are_refused(tid):
    with pytest.raises(ValueError):
        TenantRagLeap(_Rec(), tid)


# ---------- extra coverage ----------

def test_milvus_metadata_cannot_overwrite_id_or_vector(tmp_path):
    b, fake = make("milvus", tmp_path)
    b.insert_chunk("doc1", "a.txt", 3, "t", 1, VEC, {"id": "evil", "vector": [9.0, 9.0, 9.0]})
    assert fake.payload["id"] == "doc1:3" and fake.payload["vector"] == VEC


def test_milvus_boolean_filter_values(tmp_path):
    b, _ = make("milvus", tmp_path)
    assert b._build_filter_expr({"flag": True, "other": False}) == "flag == true and other == false"


def test_milvus_control_characters_in_values_are_escaped(tmp_path):
    b, _ = make("milvus", tmp_path)
    assert b._build_filter_expr({"tenant_id": "a\nb"}) == 'tenant_id == "a\\nb"'
    assert b._build_filter_expr({"tenant_id": "a\rb"}) == 'tenant_id == "a\\rb"'


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_milvus_non_finite_numbers_raise(tmp_path, value):
    b, _ = make("milvus", tmp_path)
    with pytest.raises(ValueError, match="filter value"):
        b._build_filter_expr({"x": value})


@pytest.mark.parametrize("tid", [None, 5, b"acme", ["a"], 1.5])
def test_non_string_tenant_ids_are_refused(tid):
    with pytest.raises(ValueError):
        TenantRagLeap(_Rec(), tid)
