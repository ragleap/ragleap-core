import pytest

from ragleap_memory import SQLiteStore
from ragleap_security import Scope

from fake_embed import DIM, MODEL, FakeEmbedder, embed_text

A = Scope("acme")
B = Scope("globex")
A_ALICE = Scope("acme", "alice")
A_BOB = Scope("acme", "bob")


def make(**kw):
    return SQLiteStore(embedder=FakeEmbedder(), embedding_model=MODEL, embedding_dim=DIM, **kw)


def vectors(s):
    return s._db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]


@pytest.mark.parametrize("mode", ["vector", "hybrid"])
def test_a_tenant_never_sees_another_tenants_entries_even_with_identical_vectors(mode):
    s = make()
    mine = s.add(A, "the automobile is mine")
    theirs = s.add(B, "the automobile is mine")                       # same text, same vector
    s.embed_pending(A)
    s.embed_pending(B)
    assert [h.entry.id for h in s.search(A, "car", mode=mode, k=10)] == [mine.id]
    assert [h.entry.id for h in s.search(B, "car", mode=mode, k=10)] == [theirs.id]


@pytest.mark.parametrize("mode", ["vector", "hybrid"])
def test_a_tenant_with_no_entries_gets_nothing_back(mode):
    s = make()
    s.add(A, "automobile")
    s.embed_pending(A)
    assert s.search(Scope("initech"), "car", mode=mode, k=10) == []
    assert s.search(Scope("initech"), "car", mode=mode, query_vector=embed_text("car")) == []


@pytest.mark.parametrize("mode", ["vector", "hybrid"])
def test_narrower_scopes_see_only_their_own_entries(mode):
    s = make()
    alice = s.add(A_ALICE, "alice automobile")
    bob = s.add(A_BOB, "bob automobile")
    shared = s.add(A, "tenant automobile")                            # no subject: only a tenant-wide scope sees it
    s.embed_pending(A)
    ids = lambda scope: {h.entry.id for h in s.search(scope, "car", mode=mode, k=10)}
    assert ids(A_ALICE) == {alice.id}
    assert ids(A_BOB) == {bob.id}
    assert ids(A) == {alice.id, bob.id, shared.id}


def test_embedding_one_scope_leaves_the_others_without_vectors():
    s = make()
    s.add(A_ALICE, "alice note")
    s.add(A_BOB, "bob note")
    s.add(B, "globex note")
    s.embed_pending(A_ALICE)
    assert vectors(s) == 1
    assert s.search(A_BOB, "note", mode="vector") == []
    assert s.search(B, "note", mode="vector") == []


def test_the_embedder_is_never_given_another_tenants_text():
    emb = FakeEmbedder()
    s = SQLiteStore(embedder=emb, embedding_model=MODEL, embedding_dim=DIM)
    s.add(A, "acme secret plan")
    s.add(B, "globex secret plan")
    s.embed_pending(B)
    assert emb.texts == ["globex secret plan"]


def test_forget_removes_the_vector():
    s = make()
    e = s.add(A, "automobile")
    keep = s.add(A, "truck")
    s.embed_pending(A)
    assert vectors(s) == 2
    assert s.forget(A, [e.id]) == 1
    assert vectors(s) == 1
    assert [h.entry.id for h in s.search(A, "car", mode="vector", k=5)] == [keep.id]


def test_forget_cannot_remove_another_tenants_vector():
    s = make()
    theirs = s.add(B, "automobile")
    s.embed_pending(B)
    assert s.forget(A, [theirs.id]) == 0
    assert vectors(s) == 1


def test_erase_removes_only_the_scopes_vectors():
    s = make()
    s.add(A, "acme automobile")
    keep = s.add(B, "globex automobile")
    s.embed_pending(A)
    s.embed_pending(B)
    assert vectors(s) == 2
    assert s.erase(A) == 1
    assert vectors(s) == 1
    assert [h.entry.id for h in s.search(B, "car", mode="vector")] == [keep.id]


def test_a_narrow_erase_keeps_the_rest_of_the_tenant():
    s = make()
    s.add(A_ALICE, "alice automobile")
    bob = s.add(A_BOB, "bob automobile")
    s.embed_pending(A)
    s.erase(A_ALICE)
    assert vectors(s) == 1
    assert [h.entry.id for h in s.search(A, "car", mode="vector")] == [bob.id]


def test_sweeping_expired_entries_removes_their_vectors(clock):
    from datetime import timedelta
    s = make(clock=clock)
    s.add(A, "automobile", expires_at=clock.now + timedelta(hours=1))
    s.add(A, "truck")
    s.embed_pending(A)
    clock.advance(hours=2)
    assert s.sweep_expired(A) == 1
    assert vectors(s) == 1
