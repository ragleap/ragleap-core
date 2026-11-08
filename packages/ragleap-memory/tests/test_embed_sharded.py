import os

from ragleap_memory import ShardedStore
from ragleap_security import Scope

from fake_embed import DIM, MODEL, FakeEmbedder

A = Scope("acme")
B = Scope("globex")


def make(tmp_path, **kw):
    emb = FakeEmbedder()
    return ShardedStore(tmp_path / "shards", embedder=emb, embedding_model=MODEL, embedding_dim=DIM, **kw), emb


def test_each_tenant_embeds_and_searches_in_its_own_shard(tmp_path):
    s, emb = make(tmp_path)
    a = s.add(A, "acme automobile")
    b = s.add(B, "globex automobile")
    assert s.embed_pending(A).embedded == 1 and s.embed_pending(B).embedded == 1
    assert [h.entry.id for h in s.search(A, "car", mode="vector")] == [a.id]
    assert [h.entry.id for h in s.search(B, "car", mode="hybrid")] == [b.id]
    # the entries' texts (each tenant's own) and the two query texts; nothing else
    assert sorted(emb.texts) == ["acme automobile", "car", "car", "globex automobile"]
    assert s.usage(A)["embed_calls"] == 2 and s.usage(B)["embed_calls"] == 2   # each: embed_pending + its query


def test_the_cap_is_per_tenant_in_a_sharded_store(tmp_path):
    from ragleap_memory import QuotaExceededError
    import pytest
    s, _ = make(tmp_path, embed_quota_tokens=3, token_estimator=lambda t: len(t.split()))
    s.add(A, "one two three four")
    s.add(B, "one two three")
    with pytest.raises(QuotaExceededError):
        s.embed_pending(A)
    assert s.embed_pending(B).embedded == 1


def test_a_tenant_wide_erase_deletes_the_files_and_the_vectors(tmp_path):
    s, _ = make(tmp_path)
    s.add(A, "acme automobile")
    s.embed_pending(A)
    assert s.erase(A) == 1
    assert [f for f in os.listdir(tmp_path / "shards") if f.endswith((".db", ".db-wal", ".db-shm"))] == []
    s.add(A, "fresh automobile")
    assert s.embed_pending(A).embedded == 1
    assert [h.entry.text for h in s.search(A, "car", mode="vector")] == ["fresh automobile"]


def test_bad_embedding_options_are_refused_when_the_store_is_built(tmp_path):
    import pytest
    from ragleap_memory import InvalidEntryError
    with pytest.raises(InvalidEntryError):
        ShardedStore(tmp_path / "x", embedding_model=MODEL)


def test_options_reach_the_shard(tmp_path):
    s, emb = make(tmp_path)
    for i in range(3):
        s.add(A, f"acme entry {i}")
    r = s.embed_pending(A, dry_run=True)
    assert r.dry_run and r.embedded == 3 and emb.calls == []
    r2 = s.embed_pending(A, limit=2)
    assert r2.embedded == 2 and r2.remaining == 1
