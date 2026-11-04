import threading

import pytest

from ragleap_optimizer import ExactCache, InvalidRequestError
from ragleap_security import Scope, ScopeError

A = Scope("acme")
B = Scope("globex")
A_ALICE = Scope("acme", subject="alice")


def base():
    return {"model": "m1", "temperature": 0, "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
            "tools": [{"type": "function", "function": {"name": "f"}}]}


@pytest.fixture
def cache(clock):
    return ExactCache(ttl_seconds=60, max_entries=100, clock=clock)


def test_hit_for_same_scope_and_same_request(cache):
    cache.put(A, base(), {"answer": "42"})
    assert cache.get(A, base()) == (True, {"answer": "42"})


def test_no_hit_across_scopes_even_for_a_narrower_scope_of_the_same_tenant(cache):
    cache.put(A, base(), "secret of acme")
    assert cache.get(B, base()) == (False, None)
    assert cache.get(A_ALICE, base()) == (False, None)
    cache.put(A_ALICE, base(), "alice only")
    assert cache.get(A, base()) == (True, "secret of acme")


@pytest.mark.parametrize("change", [
    lambda r: r.update(model="m2"), lambda r: r.update(temperature=1),
    lambda r: r["messages"][0].update(content="other system prompt"), lambda r: r["messages"][1].update(content="q2"),
    lambda r: r["tools"][0]["function"].update(name="g"), lambda r: r.update(extra="field"),
    lambda r: r["messages"].append({"role": "user", "content": "more"})])
def test_anything_that_can_change_the_answer_is_part_of_the_key(cache, change):
    cache.put(A, base(), "old")
    other = base()
    change(other)
    assert cache.get(A, other) == (False, None)


def test_key_ignores_dict_key_order(cache):
    cache.put(A, {"a": 1, "b": 2}, "v")
    assert cache.get(A, {"b": 2, "a": 1}) == (True, "v")


def test_values_are_copied_in_and_out(cache):
    value = {"list": [1, 2]}
    cache.put(A, base(), value)
    value["list"].append(3)
    got = cache.get(A, base())[1]
    assert got == {"list": [1, 2]}
    got["list"].append(9)
    assert cache.get(A, base())[1] == {"list": [1, 2]}


def test_none_is_a_valid_cached_value(cache):
    cache.put(A, base(), None)
    assert cache.get(A, base()) == (True, None)


def test_entries_expire_at_the_ttl(cache, clock):
    cache.put(A, base(), "v")
    clock.advance(59.9)
    assert cache.get(A, base())[0] is True
    clock.advance(0.1)                                   # exactly at the ttl: gone
    assert cache.get(A, base()) == (False, None)
    assert len(cache) == 0


def test_putting_again_refreshes_the_ttl_and_keeps_one_entry(cache, clock):
    cache.put(A, base(), "v1")
    clock.advance(50)
    cache.put(A, base(), "v2")
    clock.advance(50)
    assert cache.get(A, base()) == (True, "v2") and len(cache) == 1


def test_least_recently_used_goes_first(clock):
    c = ExactCache(ttl_seconds=60, max_entries=2, clock=clock)
    c.put(A, {"k": "a"}, 1)
    c.put(A, {"k": "b"}, 2)
    c.get(A, {"k": "a"})                                 # a is now the most recent
    c.put(A, {"k": "c"}, 3)
    assert c.get(A, {"k": "b"})[0] is False
    assert c.get(A, {"k": "a"})[0] is True and c.get(A, {"k": "c"})[0] is True and len(c) == 2


def test_erase_removes_only_that_scope(cache):
    cache.put(A, {"k": 1}, "a1")
    cache.put(A, {"k": 2}, "a2")
    cache.put(B, {"k": 1}, "b1")
    cache.put(A_ALICE, {"k": 1}, "alice")
    assert cache.erase(A) == 2
    assert cache.get(A, {"k": 1})[0] is False and cache.get(B, {"k": 1})[0] is True and cache.get(A_ALICE, {"k": 1})[0] is True
    assert cache.erase(A) == 0


def test_erase_after_eviction_and_expiry_stays_consistent(clock):
    c = ExactCache(ttl_seconds=10, max_entries=1, clock=clock)
    c.put(A, {"k": 1}, 1)
    c.put(A, {"k": 2}, 2)                                # evicts the first
    assert c.erase(A) == 1
    c.put(A, {"k": 3}, 3)
    clock.advance(10)
    assert c.get(A, {"k": 3})[0] is False
    assert c.erase(A) == 0


def test_counters(cache):
    cache.get(A, base())
    cache.put(A, base(), "v")
    cache.get(A, base())
    cache.get(A, base())
    assert (cache.hits, cache.misses) == (2, 1)


def test_a_real_scope_is_required(cache):
    for call in (lambda: cache.get("acme", base()), lambda: cache.put("acme", base(), 1), lambda: cache.erase("acme"),
                 lambda: cache.get(None, base())):
        with pytest.raises(ScopeError):
            call()


def test_a_request_that_is_not_json_is_refused(cache):
    with pytest.raises(InvalidRequestError):
        cache.put(A, {"x": {1, 2}}, "v")
    with pytest.raises(InvalidRequestError):
        cache.get(A, {"x": object()})


@pytest.mark.parametrize("bad", [0, -1, True, "60", None])
def test_bad_ttl_is_refused(bad):
    with pytest.raises(InvalidRequestError):
        ExactCache(ttl_seconds=bad)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "5"])
def test_bad_max_entries_is_refused(bad):
    with pytest.raises(InvalidRequestError):
        ExactCache(ttl_seconds=60, max_entries=bad)


def test_threads_do_not_break_it_or_exceed_the_cap():
    c = ExactCache(ttl_seconds=60, max_entries=50)
    errors = []

    def work(i):
        try:
            for n in range(200):
                c.put(A, {"k": (i * 7 + n) % 80}, n)
                c.get(A, {"k": n % 80})
        except Exception as e:  # pragma: no cover
            errors.append(e)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == [] and len(c) <= 50


def test_putting_an_entry_again_makes_it_the_most_recent(clock):
    c = ExactCache(ttl_seconds=60, max_entries=2, clock=clock)
    c.put(A, {"k": "a"}, 1)
    c.put(A, {"k": "b"}, 2)
    c.put(A, {"k": "a"}, 11)                            # refreshes a: now b is the oldest
    c.put(A, {"k": "c"}, 3)
    assert c.get(A, {"k": "b"})[0] is False and c.get(A, {"k": "a"}) == (True, 11)
