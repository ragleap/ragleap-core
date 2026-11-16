"""ExactCache budgets (design: #1423): fair eviction, per-scope limits, size limits and per-scope stats."""
import json
import random
import threading

import pytest

from ragleap_optimizer import ExactCache, InvalidRequestError
from ragleap_security import Scope

A, B, C = Scope("acme"), Scope("globex"), Scope("initech")
A_ALICE = Scope("acme", subject="alice")


def rq(i):
    return {"model": "m", "messages": [{"role": "user", "content": f"q{i}"}]}


def jlen(v):
    return len(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def held(c, scope, ids):
    return sum(c.get(scope, rq(i))[0] for i in ids)


def check_invariants(c):
    """The books must match the entries: counts per scope, bytes per scope and in total."""
    assert sum(len(k) for k in c._by_scope.values()) == len(c._data)
    for skey, keys in c._by_scope.items():
        assert keys and all(c._data[k][2] == skey for k in keys)
        assert c._scope_bytes.get(skey, 0) == sum(c._data[k][3] for k in keys)
    assert c._total_bytes == sum(v[3] for v in c._data.values())
    assert set(c._scope_bytes) <= set(c._by_scope)


# ---------------------------------------------------------------- the two reproduced gaps

def test_one_tenant_writing_a_lot_no_longer_evicts_another_tenants_entries():
    c = ExactCache(ttl_seconds=60, max_entries=10)
    for i in range(3):
        c.put(A, rq(i), f"a{i}")
    for i in range(100, 130):
        c.put(B, rq(i), "b")
    assert held(c, A, range(3)) == 3 and len(c) == 10          # globex evicted itself, not acme
    check_invariants(c)


def test_the_old_global_order_is_still_there_as_lru():
    c = ExactCache(ttl_seconds=60, max_entries=10, eviction="lru")
    for i in range(3):
        c.put(A, rq(i), "a")
    for i in range(100, 130):
        c.put(B, rq(i), "b")
    assert held(c, A, range(3)) == 0 and len(c) == 10        # 0.1.0 behaviour: acme lost everything


def test_a_huge_value_from_one_tenant_is_refused_by_default():
    c = ExactCache(ttl_seconds=60)
    assert c.max_value_bytes == 1024 * 1024
    for i in range(5):
        assert c.put(B, rq(i), {"text": "x" * (2 * 1024 * 1024)}) is False
    assert len(c) == 0 and c.stats(B)["bytes"] == 0


# ---------------------------------------------------------------- fair eviction

def test_the_scope_holding_the_most_entries_loses_its_oldest_first():
    c = ExactCache(ttl_seconds=60, max_entries=6)
    for i in range(3):
        c.put(A, rq(i), "a")
    for i in range(10, 12):
        c.put(B, rq(i), "b")
    c.put(C, rq(20), "c")                                        # full: acme 3, globex 2, initech 1
    c.put(C, rq(21), "c")                                        # over: acme holds the most, so acme's oldest goes
    assert held(c, A, [0]) == 0 and held(c, A, [1, 2]) == 2
    assert held(c, B, [10, 11]) == 2 and held(c, C, [20, 21]) == 2
    check_invariants(c)


def test_inside_the_chosen_scope_the_least_recently_used_goes():
    c = ExactCache(ttl_seconds=60, max_entries=4)
    for i in range(3):
        c.put(A, rq(i), "a")
    c.put(B, rq(10), "b")
    c.get(A, rq(0))                                              # 0 is now the most recent of acme
    c.put(B, rq(11), "b")                                        # over: acme (3) is the largest, its oldest is 1
    assert held(c, A, [1]) == 0 and held(c, A, [0, 2]) == 2


def test_the_entry_just_stored_is_never_the_one_evicted():
    for eviction in ("fair", "lru"):
        c = ExactCache(ttl_seconds=60, max_entries=2, eviction=eviction)
        c.put(A, rq(1), 1)
        c.put(B, rq(2), 2)
        c.put(C, rq(3), 3)                                       # three scopes with one entry each: a tie
        assert c.get(C, rq(3)) == (True, 3) and len(c) == 2
        check_invariants(c)


def test_in_lru_mode_a_hit_refreshes_the_global_order():
    c = ExactCache(ttl_seconds=60, max_entries=2, eviction="lru")
    c.put(A, rq(1), 1)
    c.put(B, rq(2), 2)
    c.get(A, rq(1))                                              # acme's entry is now the most recent of all
    c.put(C, rq(3), 3)
    assert held(c, A, [1]) == 1 and held(c, B, [2]) == 0 and held(c, C, [3]) == 1
    check_invariants(c)


def test_a_full_cache_of_one_scope_behaves_like_an_ordinary_lru():
    c = ExactCache(ttl_seconds=60, max_entries=2)
    c.put(A, rq(1), 1)
    c.put(A, rq(2), 2)
    c.get(A, rq(1))
    c.put(A, rq(3), 3)
    assert held(c, A, [2]) == 0 and held(c, A, [1, 3]) == 2


# ---------------------------------------------------------------- per-scope entry limit

def test_a_scope_at_its_limit_loses_only_its_own_entries():
    c = ExactCache(ttl_seconds=60, max_entries=100, max_entries_per_scope=3)
    for i in range(3):
        c.put(B, rq(100 + i), "b")
    for i in range(10):
        c.put(A, rq(i), "a")
    assert c.stats(A)["entries"] == 3 and c.stats(B)["entries"] == 3
    assert held(c, A, [7, 8, 9]) == 3 and held(c, A, range(7)) == 0       # the newest three stay
    assert held(c, B, [100, 101, 102]) == 3
    check_invariants(c)


def test_the_per_scope_limit_follows_least_recently_used():
    c = ExactCache(ttl_seconds=60, max_entries_per_scope=2)
    c.put(A, rq(1), 1)
    c.put(A, rq(2), 2)
    c.get(A, rq(1))
    c.put(A, rq(3), 3)
    assert held(c, A, [2]) == 0 and held(c, A, [1, 3]) == 2


def test_a_narrower_scope_of_the_same_tenant_has_its_own_limit():
    c = ExactCache(ttl_seconds=60, max_entries_per_scope=1)
    c.put(A, rq(1), 1)
    c.put(A_ALICE, rq(1), 2)
    assert c.stats(A)["entries"] == 1 and c.stats(A_ALICE)["entries"] == 1


# ---------------------------------------------------------------- size limits

def test_a_value_is_refused_only_above_max_value_bytes():
    c = ExactCache(ttl_seconds=60, max_value_bytes=10)
    assert jlen("x" * 8) == 10 and jlen("x" * 9) == 11
    assert c.put(A, rq(1), "x" * 8) is True and c.put(A, rq(2), "x" * 9) is False
    assert held(c, A, [1]) == 1 and held(c, A, [2]) == 0 and len(c) == 1


def test_sizes_are_counted_as_utf8_json():
    c = ExactCache(ttl_seconds=60, max_value_bytes=100)
    c.put(A, rq(1), {"k": "é€"})                                  # two non-ASCII characters: 2 + 3 bytes
    assert c.stats(A)["bytes"] == jlen({"k": "é€"}) == len('{"k":"') + 5 + len('"}')


def test_a_refused_value_stores_nothing_and_evicts_nothing():
    c = ExactCache(ttl_seconds=60, max_entries=2, max_value_bytes=10)
    c.put(A, rq(1), 1)
    c.put(B, rq(2), 2)
    assert c.put(C, rq(3), "x" * 50) is False
    assert held(c, A, [1]) == 1 and held(c, B, [2]) == 1 and len(c) == 2


def test_a_refused_overwrite_keeps_the_old_value():
    c = ExactCache(ttl_seconds=60, max_value_bytes=10)
    c.put(A, rq(1), "ok")
    assert c.put(A, rq(1), "x" * 50) is False
    assert c.get(A, rq(1)) == (True, "ok")


def test_the_value_limit_can_be_turned_off_and_then_any_copyable_value_is_stored():
    c = ExactCache(ttl_seconds=60, max_value_bytes=None)
    assert c.put(A, rq(1), {"text": "x" * (3 * 1024 * 1024)}) is True
    assert c.put(A, rq(2), {1, 2, 3}) is True and c.get(A, rq(2)) == (True, {1, 2, 3})
    assert c.stats(A)["bytes"] == 0                              # nothing is measured


def test_with_a_size_limit_a_value_must_be_json():
    c = ExactCache(ttl_seconds=60)
    with pytest.raises(InvalidRequestError, match="JSON serializable"):
        c.put(A, rq(1), {1, 2, 3})
    assert len(c) == 0


def test_a_scope_over_its_byte_limit_loses_its_own_oldest_entries():
    c = ExactCache(ttl_seconds=60, max_bytes_per_scope=30, max_value_bytes=None)
    c.put(B, rq(100), "b" * 20)
    for i in range(4):
        c.put(A, rq(i), "a" * 10)                                # each is 12 bytes
    assert c.stats(A)["bytes"] == 24 and c.stats(A)["entries"] == 2
    assert held(c, A, [2, 3]) == 2 and held(c, B, [100]) == 1
    check_invariants(c)


def test_a_value_bigger_than_the_per_scope_limit_is_refused():
    c = ExactCache(ttl_seconds=60, max_bytes_per_scope=10, max_value_bytes=None)
    assert c.put(A, rq(1), "x" * 20) is False and len(c) == 0


def test_the_total_byte_limit_evicts_from_the_scope_holding_the_most_bytes():
    c = ExactCache(ttl_seconds=60, max_bytes=60, max_value_bytes=None)
    c.put(B, rq(100), "b" * 8)                                   # 10 bytes
    for i in range(5):
        c.put(A, rq(i), "a" * 8)                                 # 10 bytes each: 60 in total
    c.put(A, rq(5), "a" * 8)                                     # over: acme holds the most bytes
    assert held(c, B, [100]) == 1 and held(c, A, [0]) == 0 and held(c, A, [5]) == 1
    assert c._total_bytes <= 60
    check_invariants(c)


def test_by_bytes_the_scope_with_the_most_bytes_loses_not_the_one_with_the_most_entries():
    c = ExactCache(ttl_seconds=60, max_bytes=130, max_value_bytes=None)
    for i in range(6):
        c.put(B, rq(100 + i), "b")                               # six tiny entries: 18 bytes
    c.put(A, rq(1), "a" * 38)                                    # two big entries: 80 bytes
    c.put(A, rq(2), "a" * 38)
    c.put(C, rq(5), "c" * 38)                                    # 40 bytes more: 98 + 40 > 130, acme holds the most bytes
    assert held(c, A, [1]) == 0 and held(c, A, [2]) == 1 and held(c, B, range(100, 106)) == 6
    assert c._total_bytes <= 130
    check_invariants(c)


def test_by_bytes_the_entry_just_stored_survives_even_when_its_scope_holds_the_most():
    c = ExactCache(ttl_seconds=60, max_bytes=60, max_value_bytes=None)
    c.put(B, rq(1), "b" * 18)                                    # 20 bytes
    c.put(A, rq(2), "a" * 48)                                    # 50 bytes: 70 in total, acme is largest but has one entry
    assert c.get(A, rq(2)) == (True, "a" * 48) and held(c, B, [1]) == 0
    check_invariants(c)


def test_a_value_bigger_than_the_total_limit_is_refused_and_lru_mode_uses_global_order():
    c = ExactCache(ttl_seconds=60, max_bytes=30, max_value_bytes=None, eviction="lru")
    assert c.put(A, rq(1), "x" * 40) is False
    c.put(A, rq(1), "a" * 8)
    c.put(B, rq(2), "b" * 8)
    c.put(B, rq(3), "b" * 8)
    c.put(B, rq(4), "b" * 8)                                     # 40 bytes in total: the global oldest (acme's) goes
    assert held(c, A, [1]) == 0 and held(c, B, [2, 3, 4]) == 3


def test_overwriting_a_key_updates_the_bytes():
    c = ExactCache(ttl_seconds=60)
    c.put(A, rq(1), "x" * 5)
    first = c.stats(A)["bytes"]
    c.put(A, rq(1), "x" * 50)
    assert first == jlen("x" * 5) and c.stats(A)["bytes"] == jlen("x" * 50) and c.stats(A)["entries"] == 1
    check_invariants(c)


# ---------------------------------------------------------------- stats and erase

def test_stats_belong_to_one_exact_scope():
    c = ExactCache(ttl_seconds=60)
    c.put(A, rq(1), "a")
    c.put(B, rq(1), "bb")
    c.get(A, rq(1))
    c.get(A, rq(9))
    c.get(A_ALICE, rq(1))
    assert c.stats(A) == {"entries": 1, "bytes": jlen("a"), "hits": 1, "misses": 1}
    assert c.stats(B) == {"entries": 1, "bytes": jlen("bb"), "hits": 0, "misses": 0}
    assert c.stats(A_ALICE) == {"entries": 0, "bytes": 0, "hits": 0, "misses": 1}
    assert (c.hits, c.misses) == (1, 2)                           # the whole-cache counters are unchanged
    assert c.stats(C) == {"entries": 0, "bytes": 0, "hits": 0, "misses": 0}


def test_stats_needs_a_real_scope():
    c = ExactCache(ttl_seconds=60)
    for bad in ("acme", None, {"tenant": "acme"}):
        with pytest.raises(Exception):
            c.stats(bad)


def test_erase_clears_the_entries_bytes_and_counters_of_that_scope_only():
    c = ExactCache(ttl_seconds=60)
    c.put(A, rq(1), "a")
    c.put(B, rq(1), "b")
    c.get(A, rq(1))
    assert c.erase(A) == 1
    assert c.stats(A) == {"entries": 0, "bytes": 0, "hits": 0, "misses": 0} and c.stats(B)["entries"] == 1
    check_invariants(c)


def test_expired_entries_leave_the_books_when_they_are_read(clock):
    c = ExactCache(ttl_seconds=10, clock=clock)
    c.put(A, rq(1), "a")
    clock.advance(11)
    assert c.get(A, rq(1)) == (False, None)
    assert c.stats(A)["entries"] == 0 and c.stats(A)["bytes"] == 0 and c.stats(A)["misses"] == 1
    check_invariants(c)


# ---------------------------------------------------------------- the rest of the interface

def test_put_returns_a_bool_and_overwrites():
    c = ExactCache(ttl_seconds=60)
    assert c.put(A, rq(1), 1) is True and c.put(A, rq(1), 2) is True and c.get(A, rq(1)) == (True, 2)


@pytest.mark.parametrize("name", ["max_entries_per_scope", "max_value_bytes", "max_bytes_per_scope", "max_bytes"])
@pytest.mark.parametrize("bad", [0, -1, True, False, "5", 1.5])
def test_bad_limits_are_refused(name, bad):
    with pytest.raises(InvalidRequestError):
        ExactCache(ttl_seconds=60, **{name: bad})


@pytest.mark.parametrize("bad", ["FAIR", "random", None, 1, ""])
def test_a_bad_eviction_mode_is_refused(bad):
    with pytest.raises(InvalidRequestError):
        ExactCache(ttl_seconds=60, eviction=bad)


def test_defaults():
    c = ExactCache(ttl_seconds=60)
    assert (c.eviction, c.max_entries_per_scope, c.max_bytes_per_scope, c.max_bytes) == ("fair", None, None, None)


# ---------------------------------------------------------------- many operations and threads

def test_the_books_stay_right_after_many_random_operations():
    rng = random.Random(1423)
    c = ExactCache(ttl_seconds=1000, max_entries=25, max_entries_per_scope=12, max_bytes_per_scope=300,
                   max_bytes=500, max_value_bytes=80)
    scopes = [A, B, C, A_ALICE]
    for _ in range(3000):
        s, i = rng.choice(scopes), rng.randrange(60)
        op = rng.random()
        if op < 0.6:
            c.put(s, rq(i), "v" * rng.randrange(1, 100))
        elif op < 0.95:
            c.get(s, rq(i))
        else:
            c.erase(s)
        assert len(c) <= 25 and c._total_bytes <= 500
        assert all(len(k) <= 12 for k in c._by_scope.values())
        assert all(b <= 300 for b in c._scope_bytes.values())
    check_invariants(c)


def test_threads_keep_the_books_and_a_quiet_tenant_keeps_its_entries():
    c = ExactCache(ttl_seconds=1000, max_entries=40)
    for i in range(3):
        c.put(C, rq(i), "quiet")
    errors = []

    def noisy(n):
        try:
            for k in range(300):
                c.put(A if n % 2 else B, rq(1000 * n + k), "x")
                c.get(A, rq(k))
        except Exception as e:  # pragma: no cover
            errors.append(e)
    ts = [threading.Thread(target=noisy, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == [] and len(c) <= 40
    assert held(c, C, range(3)) == 3
    check_invariants(c)
