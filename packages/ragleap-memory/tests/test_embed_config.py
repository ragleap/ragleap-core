import pytest

from ragleap_memory import SCHEMA_VERSION, InvalidEntryError, SQLiteStore
from ragleap_security import Scope

from fake_embed import DIM, MODEL, FakeEmbedder

A = Scope("acme")


def test_schema_version_is_3():
    assert SCHEMA_VERSION == 3


def test_a_store_without_an_embedder_behaves_like_0_2_0(store):
    assert store.embedding_model is None and store.embedding_dim is None
    store.add(A, "hello brave world")
    assert [h.entry.text for h in store.search(A, "brave")] == ["hello brave world"]
    with pytest.raises(InvalidEntryError):
        store.embed_pending(A)
    with pytest.raises(InvalidEntryError):
        store.search(A, "brave", mode="vector")
    with pytest.raises(InvalidEntryError):
        store.search(A, "brave", mode="hybrid")


def test_usage_has_the_embedding_counters(store):
    u = store.usage(A)
    assert u["embedded_tokens"] == 0 and u["embed_calls"] == 0


@pytest.mark.parametrize("kw", [
    {"embedding_model": MODEL},                       # model without a size
    {"embedding_dim": DIM},                           # size without a model
    {"embedder": FakeEmbedder()},                     # embedder without model and size
    {"embedder": FakeEmbedder(), "embedding_model": MODEL},
])
def test_incomplete_embedding_options_are_refused(kw):
    with pytest.raises(InvalidEntryError):
        SQLiteStore(**kw)


@pytest.mark.parametrize("kw", [
    {"embedding_model": "", "embedding_dim": 8},
    {"embedding_model": "has space", "embedding_dim": 8},
    {"embedding_model": "-starts-bad", "embedding_dim": 8},
    {"embedding_model": "x" * 65, "embedding_dim": 8},
    {"embedding_model": 5, "embedding_dim": 8},
    {"embedding_model": MODEL, "embedding_dim": 0},
    {"embedding_model": MODEL, "embedding_dim": 4097},
    {"embedding_model": MODEL, "embedding_dim": True},
    {"embedding_model": MODEL, "embedding_dim": "8"},
    {"embedding_model": MODEL, "embedding_dim": 8, "embedder": "not callable"},
    {"embedding_model": MODEL, "embedding_dim": 8, "embed_quota_tokens": 0},
    {"embedding_model": MODEL, "embedding_dim": 8, "embed_quota_tokens": True},
    {"embedding_model": MODEL, "embedding_dim": 8, "embed_quota_tokens": "5"},
    {"embedding_model": MODEL, "embedding_dim": 8, "embed_batch_size": 0},
    {"embedding_model": MODEL, "embedding_dim": 8, "embed_batch_size": 257},
    {"embedding_model": MODEL, "embedding_dim": 8, "embed_batch_size": True},
    {"embedding_model": MODEL, "embedding_dim": 8, "max_vector_candidates": 0},
    {"embedding_model": MODEL, "embedding_dim": 8, "max_vector_candidates": 100001},
    {"embedding_model": MODEL, "embedding_dim": 8, "max_vector_candidates": True},
])
def test_bad_embedding_options_are_refused(kw):
    with pytest.raises(InvalidEntryError):
        SQLiteStore(**kw)


@pytest.mark.parametrize("kw", [
    {"embedding_model": MODEL, "embedding_dim": 1},
    {"embedding_model": MODEL, "embedding_dim": 4096},
    {"embedding_model": "a", "embedding_dim": 8},
    {"embedding_model": "x" * 64, "embedding_dim": 8},
    {"embedding_model": "text-embed.v2:small@2026/10", "embedding_dim": 8},
    {"embedding_model": MODEL, "embedding_dim": 8, "embed_batch_size": 1},
    {"embedding_model": MODEL, "embedding_dim": 8, "embed_batch_size": 256},
    {"embedding_model": MODEL, "embedding_dim": 8, "max_vector_candidates": 1},
    {"embedding_model": MODEL, "embedding_dim": 8, "max_vector_candidates": 100000},
    {"embedding_model": MODEL, "embedding_dim": 8, "embed_quota_tokens": 1},
])
def test_boundary_embedding_options_are_accepted(kw):
    SQLiteStore(**kw).close()


def test_the_query_size_limit_applies_to_vector_search_too():
    from ragleap_security import LimitError
    s = SQLiteStore(embedder=FakeEmbedder(), embedding_model=MODEL, embedding_dim=DIM)
    with pytest.raises(LimitError):
        s.search(A, "x" * 1001, mode="vector")
    s.search(A, "x" * 1000, mode="vector")
