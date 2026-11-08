from datetime import timedelta

import pytest

from ragleap_memory import InvalidEntryError, QuotaExceededError, SQLiteStore
from ragleap_security import LimitError, Scope

from fake_embed import DIM, MODEL, FakeEmbedder, embed_text, words

A = Scope("acme")


def make(emb=None, path=":memory:", **kw):
    emb = emb or FakeEmbedder()
    return SQLiteStore(path, embedder=emb, embedding_model=MODEL, embedding_dim=DIM, **kw), emb


def test_vector_search_finds_a_paraphrase_that_keyword_search_misses():
    s, _ = make()
    car = s.add(A, "the red automobile is parked outside")
    s.add(A, "the weather is sunny today")
    s.embed_pending(A)
    assert s.search(A, "car") == []                                   # no shared word
    hits = s.search(A, "car", mode="vector")
    assert hits[0].entry.id == car.id and 0 < hits[0].score <= 1.0 + 1e-6
    assert hits[0].tokens == car.tokens


def test_vector_hits_are_ordered_by_similarity():
    s, _ = make()
    s.add(A, "automobile")
    s.add(A, "automobile weather sunny river")
    s.add(A, "weather sunny river")
    s.embed_pending(A)
    hits = s.search(A, "car", mode="vector", k=3)
    assert [h.entry.text for h in hits][:2] == ["automobile", "automobile weather sunny river"]
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_entries_without_a_vector_are_not_in_vector_results():
    s, _ = make()
    s.add(A, "automobile one")
    s.embed_pending(A)
    s.add(A, "automobile two")                                        # added later, never embedded
    assert [h.entry.text for h in s.search(A, "car", mode="vector", k=5)] == ["automobile one"]


def test_equal_scores_come_back_newest_first(clock):
    s, _ = make(clock=clock)
    first = s.add(A, "same words here")
    clock.advance(seconds=1)
    second = s.add(A, "same words here")
    s.embed_pending(A)
    hits = s.search(A, "same words here", mode="vector", k=2)
    assert [h.entry.id for h in hits] == [second.id, first.id]


def test_a_query_vector_needs_no_embedder(tmp_path):
    path = tmp_path / "mem.db"
    s1, _ = make(path=path)
    car = s1.add(A, "an automobile")
    s1.add(A, "something else entirely")
    s1.embed_pending(A)
    s1.close()
    s2 = SQLiteStore(path, embedding_model=MODEL, embedding_dim=DIM)   # no embedder here
    calls_before = s2.usage(A)["embed_calls"]                          # 1: from embed_pending above
    hits = s2.search(A, "", mode="vector", query_vector=embed_text("car"))
    assert hits[0].entry.id == car.id
    assert s2.usage(A)["embed_calls"] == calls_before


def test_vector_search_without_embedder_or_query_vector_is_refused():
    s = SQLiteStore(embedding_model=MODEL, embedding_dim=DIM)
    with pytest.raises(InvalidEntryError):
        s.search(A, "car", mode="vector")


def test_a_blank_query_returns_nothing_and_embeds_nothing():
    s, emb = make()
    s.add(A, "automobile")
    s.embed_pending(A)
    calls = len(emb.calls)
    assert s.search(A, "   ", mode="vector") == []
    assert s.search(A, "", mode="hybrid") == []
    assert len(emb.calls) == calls
    assert s.usage(A)["searches"] == 2 and s.usage(A)["embed_calls"] == 1


def test_the_query_costs_one_embedder_call():
    s, emb = make(token_estimator=words)
    s.add(A, "automobile")
    s.embed_pending(A)
    before = s.usage(A)
    s.search(A, "fast red car", mode="vector")
    after = s.usage(A)
    assert emb.calls[-1] == ["fast red car"]
    assert after["embed_calls"] == before["embed_calls"] + 1
    assert after["embedded_tokens"] == before["embedded_tokens"] + 3


def test_the_cap_stops_a_query_before_the_embedder_is_called():
    s, emb = make(embed_quota_tokens=2, token_estimator=words)
    s.add(A, "automobile")
    s.embed_pending(A)                                                # 1 token spent
    calls = len(emb.calls)
    with pytest.raises(QuotaExceededError):
        s.search(A, "fast red car", mode="vector")                    # needs 3, 1 left
    assert len(emb.calls) == calls


def test_a_query_vector_is_not_counted_against_the_cap():
    s, _ = make(embed_quota_tokens=1, token_estimator=words)
    s.add(A, "automobile")
    s.embed_pending(A)
    s.search(A, "fast red car", mode="vector", query_vector=embed_text("car"))
    assert s.usage(A)["embed_calls"] == 1


def test_served_tokens_and_searches_are_recorded():
    s, _ = make()
    e = s.add(A, "automobile")
    s.embed_pending(A)
    s.search(A, "car", mode="vector")
    u = s.usage(A)
    assert u["searches"] == 1 and u["served_tokens"] == e.tokens


# -- filters work the same as in keyword search ----------------------------------------------------

def test_kinds_and_trust_filters_apply():
    s, _ = make()
    fact = s.add(A, "automobile fact", kind="fact", trust="owner")
    s.add(A, "automobile episode", kind="episode", trust="untrusted")
    s.embed_pending(A)
    assert [h.entry.id for h in s.search(A, "car", mode="vector", kinds=["fact"])] == [fact.id]
    assert [h.entry.id for h in s.search(A, "car", mode="vector", min_trust="owner")] == [fact.id]
    assert len(s.search(A, "car", mode="vector", k=5)) == 2


def test_superseded_and_expired_entries_are_hidden_unless_asked(clock):
    s, _ = make(clock=clock)
    old = s.add(A, "automobile old")
    s.embed_pending(A)
    new = s.supersede(A, old.id, "automobile new")
    s.embed_pending(A)
    soon = s.add(A, "automobile soon gone", expires_at=clock.now + timedelta(hours=1))
    s.embed_pending(A)
    clock.advance(hours=2)
    ids = lambda **kw: {h.entry.id for h in s.search(A, "car", mode="vector", k=5, **kw)}
    assert ids() == {new.id}
    assert ids(include_superseded=True) == {new.id, old.id}
    assert ids(include_expired=True) == {new.id, soon.id}


def test_k_and_the_token_budget_apply():
    s, _ = make(token_estimator=words)
    for i in range(4):
        s.add(A, f"automobile number{i}")                             # 2 tokens each
    s.embed_pending(A)
    assert len(s.search(A, "car", mode="vector", k=2)) == 2
    hits = s.search(A, "car", mode="vector", k=4, max_tokens=5)
    assert len(hits) == 2 and sum(h.tokens for h in hits) <= 5


def test_an_entry_that_does_not_fit_the_budget_is_skipped_not_fatal():
    s, _ = make(token_estimator=words)
    s.add(A, "automobile " + "pad " * 9)                              # 10 tokens, the best match is not it
    small = s.add(A, "automobile")                                    # 1 token
    s.embed_pending(A)
    hits = s.search(A, "car", mode="vector", k=5, max_tokens=3)
    assert [h.entry.id for h in hits] == [small.id]


def test_the_candidate_cap_scans_newest_entries_first(clock):
    s, _ = make(clock=clock, max_vector_candidates=2)
    best = s.add(A, "automobile")                                     # oldest and the best match
    for i in range(2):
        clock.advance(seconds=1)
        s.add(A, f"weather river{i}")
    s.embed_pending(A)
    assert best.id not in {h.entry.id for h in s.search(A, "car", mode="vector", k=5)}
    s.max_vector_candidates = 3
    assert best.id in {h.entry.id for h in s.search(A, "car", mode="vector", k=5)}


def test_vectors_of_another_model_are_ignored(tmp_path):
    path = tmp_path / "mem.db"
    s1 = SQLiteStore(path, embedder=FakeEmbedder(), embedding_model="model-one", embedding_dim=DIM)
    s1.add(A, "automobile")
    s1.embed_pending(A)
    s1.close()
    s2 = SQLiteStore(path, embedder=FakeEmbedder(), embedding_model="model-two", embedding_dim=DIM)
    assert s2.search(A, "car", mode="vector") == []
    s2.embed_pending(A, replace_stale=True)
    assert len(s2.search(A, "car", mode="vector")) == 1


def test_a_damaged_vector_is_skipped_not_fatal():
    s, _ = make()
    s.add(A, "automobile")
    s.add(A, "truck")
    s.embed_pending(A)
    s._db.execute("UPDATE embeddings SET vec = x'0102' WHERE rowid = (SELECT MIN(rowid) FROM embeddings)")
    assert len(s.search(A, "car", mode="vector", k=5)) == 1


# -- hybrid -----------------------------------------------------------------------------------------

def test_hybrid_combines_keyword_and_vector_results():
    s, _ = make()
    only_vector = s.add(A, "red sports automobile")
    both = s.add(A, "car park open late")
    s.add(A, "xylophone recital")
    s.embed_pending(A)
    only_keyword = [s.add(A, text).id for text in ("car wash", "car hire", "car show")]   # added after: keyword only
    ids = [h.entry.id for h in s.search(A, "car", mode="hybrid", k=10)]
    assert set(ids) >= {only_vector.id, both.id, *only_keyword}
    assert ids[0] == both.id                                          # in both lists: the highest fused score
    assert len(ids) == len(set(ids))


def test_hybrid_scores_are_reciprocal_rank_fusion():
    s, _ = make()
    e = s.add(A, "automobile")
    s.embed_pending(A)
    (hit,) = s.search(A, "car", mode="hybrid")
    assert hit.entry.id == e.id and abs(hit.score - 1 / 61) < 1e-12   # vector rank 1 only; keyword finds nothing
    (hit2,) = s.search(A, "automobile", mode="hybrid")
    assert abs(hit2.score - 2 / 61) < 1e-12                            # rank 1 in both lists


def test_hybrid_records_one_search_not_two():
    s, _ = make()
    s.add(A, "automobile")
    s.embed_pending(A)
    s.search(A, "automobile", mode="hybrid")
    assert s.usage(A)["searches"] == 1


def test_hybrid_respects_the_budget_and_filters(clock):
    s, _ = make(clock=clock, token_estimator=words)
    keep = s.add(A, "automobile", kind="fact", trust="owner")
    s.add(A, "automobile again", kind="episode")
    s.embed_pending(A)
    hits = s.search(A, "automobile", mode="hybrid", kinds=["fact"], min_trust="owner")
    assert [h.entry.id for h in hits] == [keep.id]
    assert len(s.search(A, "automobile", mode="hybrid", k=5, max_tokens=1)) == 1


def test_hybrid_with_a_query_vector_calls_no_embedder():
    s, emb = make()
    s.add(A, "automobile")
    s.embed_pending(A)
    calls = len(emb.calls)
    s.search(A, "car", mode="hybrid", query_vector=embed_text("car"))
    assert len(emb.calls) == calls


# -- arguments ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["", "Vector", "semantic", None, 1])
def test_bad_modes_are_refused(mode):
    s, _ = make()
    with pytest.raises(InvalidEntryError):
        s.search(A, "car", mode=mode)


def test_a_query_vector_is_refused_in_keyword_mode():
    s, _ = make()
    with pytest.raises(InvalidEntryError):
        s.search(A, "car", query_vector=embed_text("car"))


@pytest.mark.parametrize("vec", [
    [1.0] * (DIM - 1), [1.0] * (DIM + 1), [float("nan")] + [1.0] * (DIM - 1), [float("inf")] + [1.0] * (DIM - 1),
    [0.0] * DIM, ["a"] * DIM, [True] * DIM, "x" * DIM, 5, [[1.0]] * DIM,
])
def test_a_bad_query_vector_is_refused_before_the_embedder_runs(vec):
    s, emb = make()
    with pytest.raises(InvalidEntryError):
        s.search(A, "car", mode="vector", query_vector=vec)
    assert emb.calls == []


@pytest.mark.parametrize("kw", [{"k": 0}, {"k": True}, {"k": "3"}, {"max_tokens": 0}, {"max_tokens": True},
                                {"min_trust": "root"}, {"kinds": []}, {"kinds": ["nope"]}])
def test_bad_search_arguments_are_refused_in_vector_mode(kw):
    s, emb = make()
    with pytest.raises(InvalidEntryError):
        s.search(A, "car", mode="vector", **kw)
    assert emb.calls == []


def test_the_query_must_be_a_string_and_is_size_limited():
    s, emb = make()
    with pytest.raises(InvalidEntryError):
        s.search(A, 5, mode="vector")
    with pytest.raises(LimitError):
        s.search(A, "x" * 1001, mode="hybrid")
    assert emb.calls == []


def test_k_is_capped_at_max_results():
    s, _ = make(max_results=2)
    for i in range(4):
        s.add(A, f"automobile {i}")
    s.embed_pending(A)
    assert len(s.search(A, "car", mode="vector", k=50)) == 2


def test_the_embedder_is_called_with_the_query_text_only():
    s, emb = make()
    s.add(A, "automobile")
    s.embed_pending(A)
    s.search(A, "find the car please", mode="vector")
    assert emb.calls[-1] == ["find the car please"]


def test_hybrid_hides_superseded_and_expired_entries_unless_asked(clock):
    s, _ = make(clock=clock)
    old = s.add(A, "automobile old")
    new = s.supersede(A, old.id, "automobile new")
    soon = s.add(A, "automobile soon gone", expires_at=clock.now + timedelta(hours=1))
    s.embed_pending(A)
    clock.advance(hours=2)
    ids = lambda **kw: {h.entry.id for h in s.search(A, "automobile", mode="hybrid", k=10, **kw)}
    assert ids() == {new.id}
    assert ids(include_superseded=True) == {new.id, old.id}
    assert ids(include_expired=True) == {new.id, soon.id}


def test_a_hybrid_query_that_matches_nothing_by_keyword_still_uses_vectors():
    s, _ = make()
    e = s.add(A, "automobile")
    s.embed_pending(A)
    assert [h.entry.id for h in s.search(A, "van", mode="hybrid")] == [e.id]


def test_hybrid_kinds_filter_works_on_its_own():
    s, _ = make()
    fact = s.add(A, "automobile fact", kind="fact")
    episode = s.add(A, "automobile episode", kind="episode")           # same trust: only `kinds` can tell them apart
    s.embed_pending(A)
    assert [h.entry.id for h in s.search(A, "automobile", mode="hybrid", kinds=["episode"])] == [episode.id]
    assert [h.entry.id for h in s.search(A, "automobile", mode="hybrid", kinds=["fact"])] == [fact.id]


def test_hybrid_min_trust_filter_works_on_its_own():
    s, _ = make()
    trusted = s.add(A, "automobile trusted", trust="owner")
    s.add(A, "automobile untrusted", trust="untrusted")                # same kind: only `min_trust` can tell them apart
    s.embed_pending(A)
    assert [h.entry.id for h in s.search(A, "automobile", mode="hybrid", min_trust="owner")] == [trusted.id]
    assert len(s.search(A, "automobile", mode="hybrid", k=5)) == 2
