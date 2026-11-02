import pytest

from ragleap_memory import InvalidEntryError, SQLiteStore, estimate_tokens
from ragleap_security import Scope

A = Scope("acme")


def by_suffix(text: str) -> int:
    """Test estimator: the entry's token count is the number its text ends with."""
    return int(text.split()[-1])


@pytest.mark.parametrize("text,expected", [("a", 1), ("abc", 1), ("abcd", 2), ("abcdefghi", 3), ("x" * 10, 4),
                                           ("\u65e5\u672c\u8a9e", 3), ("\U0001F600", 2)])
def test_estimate_values(text, expected):
    assert estimate_tokens(text) == expected  # one token per 3 UTF-8 bytes, rounded up


def test_estimate_is_conservative_for_english():
    text = "the quick brown fox jumps over the lazy dog " * 20
    assert estimate_tokens(text) >= len(text) / 4  # never below the usual 4 bytes per token


def test_tokens_are_stored_and_reported(store):
    e = store.add(A, "abcdefghi")
    assert e.tokens == 3
    assert store.get(A, e.id).tokens == 3
    hit = store.search(A, "abcdefghi")[0]
    assert hit.tokens == 3 and hit.entry.tokens == 3
    assert store.export(A)[0]["tokens"] == 3


def test_custom_estimator_is_used_for_add_and_supersede():
    s = SQLiteStore(token_estimator=by_suffix)
    e = s.add(A, "note 7")
    assert e.tokens == 7
    assert s.supersede(A, e.id, "note 11").tokens == 11


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.5, "3", None])
def test_estimator_result_is_validated(bad):
    s = SQLiteStore(token_estimator=lambda t: bad)
    with pytest.raises(InvalidEntryError):
        s.add(A, "some text")
    assert s.count(A) == 0


def test_estimator_that_raises_is_refused_without_leaking_text():
    def boom(text):
        raise RuntimeError(text)
    s = SQLiteStore(token_estimator=boom)
    with pytest.raises(InvalidEntryError) as ei:
        s.add(A, "private words here")
    assert "private" not in str(ei.value) and s.count(A) == 0


def test_estimator_must_be_callable():
    with pytest.raises(InvalidEntryError):
        SQLiteStore(token_estimator="nope")


@pytest.mark.parametrize("budget", list(range(1, 60)) + [100, 250, 300, 400])
def test_budget_is_never_exceeded(budget):
    s = SQLiteStore(token_estimator=by_suffix)
    for i in range(60):
        s.add(A, "common " * (i % 5 + 1) + str(1 + (i * 7) % 23))
    hits = s.search(A, "common", k=50, max_tokens=budget)
    assert sum(h.tokens for h in hits) <= budget


def test_rank_order_is_kept_and_an_entry_that_does_not_fit_is_skipped():
    s = SQLiteStore(token_estimator=by_suffix)
    big = s.add(A, "invoice invoice invoice 40")
    b = s.add(A, "invoice payment 5")
    c = s.add(A, "invoice paid today 6")
    d = s.add(A, "invoice invoice 50")
    order = [h.entry.id for h in s.search(A, "invoice")]
    assert order[0] == big.id  # the best match is also the one that will not fit
    hits = s.search(A, "invoice", max_tokens=12)
    assert [h.entry.id for h in hits] == [i for i in order if i in (b.id, c.id)]
    assert d.id not in [h.entry.id for h in hits]  # 40 and 50 skipped, 5 + 6 = 11 used
    assert len(hits) == 2


def test_budget_holds_with_1500_entries_and_a_positive_control():
    s = SQLiteStore()
    for i in range(1500):
        s.add(A, f"common shared words number {i}")
    free = s.search(A, "common", k=50)
    capped = s.search(A, "common", k=50, max_tokens=300)
    assert sum(h.tokens for h in free) > 300  # control: without a budget it is bigger
    assert 0 < sum(h.tokens for h in capped) <= 300


def test_without_a_budget_search_is_unchanged(store):
    for i in range(4):
        store.add(A, f"common {i}")
    assert len(store.search(A, "common", k=3)) == 3
    assert len(store.search(A, "common", k=3, max_tokens=None)) == 3


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "3"])
def test_bad_max_tokens_refused(store, bad):
    with pytest.raises(InvalidEntryError):
        store.search(A, "x", max_tokens=bad)


def test_skipping_may_look_past_the_first_k_candidates():
    s = SQLiteStore(token_estimator=by_suffix)
    for _ in range(4):
        s.add(A, "alpha alpha alpha 100")
    small = [s.add(A, "alpha 1") for _ in range(2)]
    hits = s.search(A, "alpha", k=2, max_tokens=10)
    assert sorted(h.entry.id for h in hits) == sorted(e.id for e in small)


def test_k_still_limits_results_under_a_budget():
    s = SQLiteStore(token_estimator=by_suffix)
    for i in range(10):
        s.add(A, f"item {i} 1")
    assert len(s.search(A, "item", k=3, max_tokens=100)) == 3
