import pytest

from ragleap_memory import EmbedderError, EmbedResult, InvalidEntryError, QuotaExceededError, SQLiteStore
from ragleap_memory.sqlite_store import _from_blob
from ragleap_security import AuditError, AuditLog, Scope

from fake_embed import DIM, MODEL, FakeEmbedder, words

A = Scope("acme")
A_ALICE = Scope("acme", "alice")
B = Scope("globex")


def make(emb=None, **kw):
    emb = emb or FakeEmbedder()
    return SQLiteStore(embedder=emb, embedding_model=MODEL, embedding_dim=DIM, **kw), emb


def vector_rows(s):
    return s._db.execute("SELECT * FROM embeddings").fetchall()


def test_embeds_what_the_scope_sees_and_counts(clock):
    s, emb = make(clock=clock)
    a = [s.add(A, f"alpha entry number {i}") for i in range(3)]
    s.add(B, "globex private entry")
    r = s.embed_pending(A)
    assert isinstance(r, EmbedResult)
    assert (r.embedded, r.calls, r.remaining, r.dry_run) == (3, 1, 0, False)
    assert r.tokens == sum(e.tokens for e in a)
    assert sorted(emb.texts) == sorted(e.text for e in a)           # B's text never reached the embedder
    assert len(vector_rows(s)) == 3
    u = s.usage(A)
    assert u["embedded_tokens"] == r.tokens and u["embed_calls"] == 1
    again = s.embed_pending(A)                                       # nothing left to do, nothing sent
    assert (again.embedded, again.tokens, again.calls, again.remaining) == (0, 0, 0, 0)
    assert len(emb.calls) == 1
    assert s.embed_pending(B).embedded == 1 and len(vector_rows(s)) == 4


def test_vectors_are_stored_normalized_with_model_and_size():
    s, _ = make()
    s.add(A, "some text to embed")
    s.embed_pending(A)
    (row,) = vector_rows(s)
    assert row["model"] == MODEL and row["dim"] == DIM and len(row["vec"]) == 4 * DIM
    v = _from_blob(row["vec"])
    assert abs(sum(x * x for x in v) - 1.0) < 1e-5


def test_batches_are_as_big_as_embed_batch_size():
    s, emb = make(embed_batch_size=2)
    for i in range(5):
        s.add(A, f"entry {i}")
    r = s.embed_pending(A)
    assert [len(b) for b in emb.calls] == [2, 2, 1]
    assert r.calls == 3 and r.embedded == 5
    assert s.usage(A)["embed_calls"] == 3


def test_limit_and_remaining_and_most_recent_first(clock):
    s, emb = make(clock=clock)
    for i in range(5):
        s.add(A, f"entry {i}")
        clock.advance(seconds=1)
    r = s.embed_pending(A, limit=2)
    assert r.embedded == 2 and r.remaining == 3
    assert emb.texts == ["entry 4", "entry 3"]
    r2 = s.embed_pending(A, limit=10)
    assert r2.embedded == 3 and r2.remaining == 0


def test_a_narrow_scope_embeds_only_its_own_entries():
    s, emb = make()
    s.add(A_ALICE, "alice note")
    s.add(Scope("acme", "bob"), "bob note")
    s.embed_pending(A_ALICE)
    assert emb.texts == ["alice note"]


def test_dry_run_changes_nothing_and_calls_nothing():
    s, emb = make(embed_batch_size=2)
    es = [s.add(A, f"entry {i}") for i in range(3)]
    r = s.embed_pending(A, dry_run=True)
    assert (r.embedded, r.calls, r.remaining, r.dry_run) == (3, 2, 0, True)
    assert r.tokens == sum(e.tokens for e in es)
    assert emb.calls == [] and vector_rows(s) == []
    assert s.usage(A)["embed_calls"] == 0 and s.usage(A)["embedded_tokens"] == 0
    assert s.embed_pending(A).embedded == 3


def test_dry_run_works_without_an_embedder():
    s = SQLiteStore(embedding_model=MODEL, embedding_dim=DIM)
    s.add(A, "one two")
    assert s.embed_pending(A, dry_run=True).embedded == 1
    with pytest.raises(InvalidEntryError):
        s.embed_pending(A)


def test_superseded_and_expired_entries_are_not_embedded(clock):
    from datetime import timedelta
    s, emb = make(clock=clock)
    old = s.add(A, "old fact")
    s.supersede(A, old.id, "new fact")
    s.add(A, "short lived", expires_at=clock.now + timedelta(hours=1))
    clock.advance(hours=2)
    r = s.embed_pending(A)
    assert r.embedded == 1 and emb.texts == ["new fact"]


def test_replace_stale_re_embeds_vectors_of_another_model(tmp_path):
    path = tmp_path / "mem.db"
    s1 = SQLiteStore(path, embedder=FakeEmbedder(), embedding_model="model-one", embedding_dim=DIM)
    s1.add(A, "keep me")
    s1.embed_pending(A)
    s1.close()
    emb2 = FakeEmbedder()
    s2 = SQLiteStore(path, embedder=emb2, embedding_model="model-two", embedding_dim=DIM)
    assert s2.embed_pending(A).embedded == 0                        # it has a vector, just another model's
    r = s2.embed_pending(A, replace_stale=True)
    assert r.embedded == 1 and emb2.texts == ["keep me"]
    assert [row["model"] for row in vector_rows(s2)] == ["model-two"]
    assert s2.embed_pending(A, replace_stale=True).embedded == 0    # now current


def test_replace_stale_also_covers_another_size(tmp_path):
    path = tmp_path / "mem.db"
    s1 = SQLiteStore(path, embedder=FakeEmbedder(8), embedding_model=MODEL, embedding_dim=8)
    s1.add(A, "keep me")
    s1.embed_pending(A)
    s1.close()
    s2 = SQLiteStore(path, embedder=FakeEmbedder(DIM), embedding_model=MODEL, embedding_dim=DIM)
    assert s2.embed_pending(A, replace_stale=True).embedded == 1
    assert [row["dim"] for row in vector_rows(s2)] == [DIM]


# -- the token cap ---------------------------------------------------------------------------------

def test_the_daily_cap_trims_the_batch_and_then_refuses(clock):
    s, emb = make(clock=clock, embed_quota_tokens=5, token_estimator=words)
    s.add(A, "a b c")          # 3 tokens, oldest
    clock.advance(seconds=1)
    s.add(A, "d e")            # 2 tokens
    clock.advance(seconds=1)
    s.add(A, "f g h i")        # 4 tokens, newest: embedded first
    r = s.embed_pending(A)
    assert (r.embedded, r.tokens, r.remaining) == (1, 4, 2)
    assert emb.texts == ["f g h i"]
    calls_before = len(emb.calls)
    with pytest.raises(QuotaExceededError):                          # 1 token left, the next entry needs 2
        s.embed_pending(A)
    assert len(emb.calls) == calls_before                            # refused BEFORE the embedder was called
    clock.advance(days=1)                                            # a new UTC day, a new allowance
    r2 = s.embed_pending(A)
    assert r2.embedded == 2 and r2.tokens == 5 and r2.remaining == 0


def test_exactly_at_the_cap_is_allowed():
    s, _ = make(embed_quota_tokens=4, token_estimator=words)
    s.add(A, "one two three four")
    assert s.embed_pending(A).embedded == 1
    assert s.usage(A)["embedded_tokens"] == 4


def test_one_token_over_the_cap_is_refused():
    s, emb = make(embed_quota_tokens=3, token_estimator=words)
    s.add(A, "one two three four")
    with pytest.raises(QuotaExceededError):
        s.embed_pending(A)
    assert emb.calls == [] and s.usage(A)["embed_calls"] == 0


def test_the_cap_is_per_tenant_and_shared_by_its_scopes():
    s, _ = make(embed_quota_tokens=2, token_estimator=words)
    s.add(A_ALICE, "alice one")      # 2 tokens
    s.add(Scope("acme", "bob"), "bob one")
    s.add(B, "globex one")
    assert s.embed_pending(A_ALICE).embedded == 1
    with pytest.raises(QuotaExceededError):                           # same tenant, other scope: shares the cap
        s.embed_pending(Scope("acme", "bob"))
    assert s.embed_pending(B).embedded == 1                           # another tenant has its own


def test_dry_run_reports_instead_of_raising_when_nothing_fits():
    s, emb = make(embed_quota_tokens=1, token_estimator=words)
    s.add(A, "two words")
    r = s.embed_pending(A, dry_run=True)
    assert (r.embedded, r.tokens, r.calls, r.remaining) == (0, 0, 0, 1)
    assert emb.calls == []


def test_no_cap_means_no_limit():
    s, _ = make(token_estimator=words)
    for i in range(3):
        s.add(A, "w " * 50)
    assert s.embed_pending(A).embedded == 3


# -- a misbehaving embedder -------------------------------------------------------------------------

def test_an_embedder_that_raises_is_reported_without_its_message():
    def boom(texts):
        raise ValueError("secret-text-zzz")
    s, _ = make(boom)
    s.add(A, "entry one")
    with pytest.raises(EmbedderError) as ei:
        s.embed_pending(A)
    assert "zzz" not in str(ei.value) and "entry one" not in str(ei.value)
    assert vector_rows(s) == []
    u = s.usage(A)
    assert u["embed_calls"] == 1 and u["embedded_tokens"] > 0         # the attempt stays counted


@pytest.mark.parametrize("bad", [
    lambda texts: [[1.0] * DIM for _ in texts[:-1]],                  # one vector too few
    lambda texts: [[1.0] * DIM for _ in texts] + [[1.0] * DIM],       # one too many
    lambda texts: [[1.0] * (DIM - 1) for _ in texts],                 # wrong size
    lambda texts: [[1.0] * (DIM + 1) for _ in texts],
    lambda texts: [[float("nan")] + [1.0] * (DIM - 1) for _ in texts],
    lambda texts: [[float("inf")] + [1.0] * (DIM - 1) for _ in texts],
    lambda texts: [[True] + [1.0] * (DIM - 1) for _ in texts],
    lambda texts: [["1"] * DIM for _ in texts],
    lambda texts: [[None] * DIM for _ in texts],
    lambda texts: [[0.0] * DIM for _ in texts],                       # all zeros
    lambda texts: [[1e300] * DIM for _ in texts],                     # the length overflows
    lambda texts: None,
    lambda texts: 5,
    lambda texts: (v for v in [[1.0] * DIM for _ in texts]),          # a generator has no length
    lambda texts: ["x" * DIM for _ in texts],
    lambda texts: [5 for _ in texts],
])
def test_an_unusable_embedder_result_stores_nothing(bad):
    s, _ = make(bad)
    s.add(A, "entry one")
    s.add(A, "entry two")
    with pytest.raises(EmbedderError) as ei:
        s.embed_pending(A)
    assert "entry" not in str(ei.value)
    assert vector_rows(s) == []


def test_earlier_batches_are_kept_when_a_later_one_fails():
    seen = []

    def flaky(texts):
        seen.append(texts)
        if len(seen) == 2:
            raise RuntimeError("down")
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]
    s, _ = make(flaky, embed_batch_size=1)
    s.add(A, "first")
    s.add(A, "second")
    with pytest.raises(EmbedderError):
        s.embed_pending(A)
    assert len(vector_rows(s)) == 1
    assert s.usage(A)["embed_calls"] == 2


def test_an_entry_forgotten_during_embedding_gets_no_vector():
    holder = {}

    def forgetful(texts):
        holder["s"].forget(A, [holder["id"]])
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]
    s, _ = make(forgetful)
    holder["s"] = s
    holder["id"] = s.add(A, "going away").id
    r = s.embed_pending(A)
    assert r.embedded == 0 and vector_rows(s) == []


def test_ints_are_accepted_as_vector_values():
    s, _ = make(lambda texts: [[1] + [0] * (DIM - 1) for _ in texts])
    s.add(A, "x y")
    assert s.embed_pending(A).embedded == 1


# -- arguments ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [{"limit": 0}, {"limit": 1001}, {"limit": True}, {"limit": "5"},
                                {"dry_run": 1}, {"dry_run": None}, {"replace_stale": 1}, {"replace_stale": "yes"}])
def test_bad_arguments_are_refused(kw):
    s, _ = make()
    with pytest.raises(InvalidEntryError):
        s.embed_pending(A, **kw)


def test_the_limit_boundaries_are_accepted():
    s, _ = make()
    s.embed_pending(A, limit=1)
    s.embed_pending(A, limit=1000)


def test_a_real_scope_is_required():
    s, _ = make()
    with pytest.raises(Exception):
        s.embed_pending("acme")


# -- the audit log -----------------------------------------------------------------------------------

def test_embedding_is_audited_without_the_text(tmp_path):
    import json
    audit = AuditLog(tmp_path / "audit.jsonl")
    s, _ = make(audit=audit)
    s.add(A, "one two")
    s.embed_pending(A)
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines() if line.strip()]
    embeds = [r for r in records if r["event"] == "memory.embed"]
    assert len(embeds) == 1 and embeds[0]["data"] == {"count": 1, "tokens": 3}
    assert embeds[0]["scope"] == "tenant=acme"
    assert "one two" not in (tmp_path / "audit.jsonl").read_text()
    assert audit.verify()


def test_a_failing_audit_log_rolls_the_batch_back():
    class Broken:
        def append(self, event, *a, **k):
            if event == "memory.embed":
                raise AuditError("disk full")
    s, _ = make()
    s.add(A, "one two")
    s._audit = Broken()
    with pytest.raises(AuditError):
        s.embed_pending(A)
    assert vector_rows(s) == []
