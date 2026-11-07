import math
import os
import stat
import threading

import pytest

from ragleap_security import Decision, RateLimiter, RateLimitError, Scope, SQLiteRateLimiter


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(params=["memory", "sqlite"])
def make(request, tmp_path):
    def build(rate=1.0, burst=3, clock=None, max_keys=100):
        clock = clock or Clock()
        if request.param == "memory":
            return RateLimiter(rate, burst, max_keys=max_keys, clock=clock), clock
        return SQLiteRateLimiter(tmp_path / f"rl{os.urandom(3).hex()}.db", rate, burst, max_keys=max_keys, clock=clock), clock
    return build


def test_burst_then_refuse_with_exact_numbers(make):
    lim, clock = make(rate=2.0, burst=3)
    for left in (2, 1, 0):
        d = lim.check("a")
        assert d == Decision(True, left, 0.0, "")
    d = lim.check("a")
    assert (d.allowed, d.remaining, d.retry_after, d.reason) == (False, 0.0, 0.5, "limited")


def test_refill_over_time_and_cap(make):
    lim, clock = make(rate=2.0, burst=3)
    for _ in range(3):
        lim.check("a")
    clock.t += 0.5
    d = lim.check("a")
    assert d.allowed and d.remaining == 0.0
    assert not lim.check("a").allowed
    clock.t += 1000
    d = lim.check("a")
    assert d.allowed and d.remaining == 2.0  # capped at burst, not 2000


def test_partial_refill_is_kept(make):
    lim, clock = make(rate=1.0, burst=2)
    lim.check("a"), lim.check("a")
    clock.t += 0.25
    d = lim.check("a")
    assert not d.allowed and d.remaining == 0.25 and d.retry_after == 0.75
    clock.t += 0.75
    assert lim.check("a").allowed


def test_keys_are_independent(make):
    lim, _ = make(rate=1.0, burst=1)
    assert lim.check("a").allowed and not lim.check("a").allowed
    assert lim.check("b").allowed


def test_scope_key_and_its_string_form_are_the_same_bucket(make):
    lim, _ = make(rate=1.0, burst=1)
    sc = Scope("acme", subject="u1")
    assert lim.check(sc).allowed
    assert not lim.check(sc.key).allowed
    assert lim.check(Scope("acme", subject="u2")).allowed


def test_cost(make):
    lim, _ = make(rate=1.0, burst=5)
    assert lim.check("a", 2).remaining == 3
    assert lim.check("a", 2.5).remaining == 0.5
    d = lim.check("a", 1)
    assert not d.allowed and d.remaining == 0.5 and d.retry_after == 0.5


def test_cost_above_burst_can_never_pass(make):
    lim, _ = make(rate=1.0, burst=2)
    d = lim.check("a", 3)
    assert not d.allowed and d.retry_after == math.inf and d.reason == "limited"
    assert lim.check("a", 2).allowed  # the refused call did not spend anything


def test_exact_cost_equal_to_tokens_is_allowed(make):
    lim, _ = make(rate=1.0, burst=2)
    assert lim.check("a", 2).allowed


@pytest.mark.parametrize("cost", [0, -1, math.nan, math.inf, True, "1", None])
def test_bad_cost(make, cost):
    lim, _ = make()
    with pytest.raises(ValueError):
        lim.check("a", cost)


@pytest.mark.parametrize("key", ["", "x" * 257, None, 5, b"a", ["a"]])
def test_bad_key(make, key):
    lim, _ = make()
    with pytest.raises(ValueError):
        lim.check(key)


def test_key_length_boundary(make):
    lim, _ = make()
    assert lim.check("k" * 256).allowed


def test_require_raises_with_details(make):
    lim, _ = make(rate=1.0, burst=1)
    assert lim.require("a").allowed
    with pytest.raises(RateLimitError) as e:
        lim.require("a")
    assert e.value.retry_after == 1.0 and e.value.reason == "limited"


def test_clock_going_backwards_gives_no_extra_tokens(make):
    lim, clock = make(rate=1.0, burst=2)
    lim.check("a"), lim.check("a")
    clock.t -= 500
    assert not lim.check("a").allowed
    clock.t += 1.0
    assert lim.check("a").allowed


@pytest.mark.parametrize("rate,burst", [(0, 1), (-1, 1), (math.nan, 1), (math.inf, 1), (True, 1), ("1", 1),
                                        (1, 0.5), (1, 0), (1, math.nan), (1, math.inf), (1, True)])
def test_bad_config(tmp_path, rate, burst):
    with pytest.raises(ValueError):
        RateLimiter(rate, burst)
    with pytest.raises(ValueError):
        SQLiteRateLimiter(tmp_path / "x.db", rate, burst)


@pytest.mark.parametrize("max_keys", [0, -1, True, 1.5, None])
def test_bad_max_keys(tmp_path, max_keys):
    with pytest.raises(ValueError):
        RateLimiter(1, 1, max_keys=max_keys)
    with pytest.raises(ValueError):
        SQLiteRateLimiter(tmp_path / "x.db", 1, 1, max_keys=max_keys)


def test_burst_of_exactly_one_is_allowed(make):
    lim, _ = make(rate=1.0, burst=1)
    assert lim.check("a").allowed


def test_new_keys_refused_when_full_of_busy_keys(make):
    lim, clock = make(rate=1.0, burst=2, max_keys=3)
    for k in ("a", "b", "c"):
        lim.check(k)
    d = lim.check("new")
    assert not d.allowed and d.reason == "too_many_keys" and d.retry_after == 2.0
    assert lim.check("a").allowed  # known keys still work


def test_flooding_with_new_keys_cannot_reset_a_throttled_key(make):
    lim, clock = make(rate=1.0, burst=2, max_keys=3)
    lim.check("victim"), lim.check("victim")
    assert not lim.check("victim").allowed
    for i in range(50):
        lim.check(f"junk{i}")
    assert not lim.check("victim").allowed


def test_idle_full_keys_are_forgotten_to_make_room(make):
    lim, clock = make(rate=1.0, burst=2, max_keys=3)
    for k in ("a", "b", "c"):
        lim.check(k)
    clock.t += 5  # everything has refilled
    assert lim.check("new").allowed
    assert lim.check("new").allowed and not lim.check("new").allowed


def test_only_the_oldest_32_keys_are_examined(make):
    lim, clock = make(rate=1.0, burst=2, max_keys=40)
    for i in range(32):
        lim.check(f"old{i}", 2)  # empty at t0
    clock.t += 0.5
    for i in range(8):
        lim.check(f"new{i}", 1)  # one token left at t0 + 0.5
    clock.t += 1.0  # old keys hold 1.5 tokens (not full); the 8 newer keys are full again
    d = lim.check("fresh")
    assert not d.allowed and d.reason == "too_many_keys"  # a full key exists, but it is beyond the 32 examined
    clock.t += 1.0  # now the oldest keys are full too
    assert lim.check("fresh").allowed


def test_sqlite_file_mode_and_shared_state(tmp_path):
    path = tmp_path / "rl.db"
    clock = Clock()
    a = SQLiteRateLimiter(path, 1.0, 1, clock=clock)
    b = SQLiteRateLimiter(path, 1.0, 1, clock=clock)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert a.check("k").allowed and not b.check("k").allowed


def test_sqlite_fails_closed_when_the_database_is_unusable(tmp_path):
    path = tmp_path / "rl.db"
    lim = SQLiteRateLimiter(path, 1.0, 5)
    assert lim.check("k").allowed
    path.write_bytes(b"this is not a database" * 100)
    d = lim.check("k")
    assert d == Decision(False, 0.0, 1.0, "unavailable")
    with pytest.raises(RateLimitError) as e:
        lim.require("k")
    assert e.value.reason == "unavailable"


def test_sqlite_threads_never_exceed_the_burst(tmp_path):
    lim = SQLiteRateLimiter(tmp_path / "rl.db", 0.001, 20)
    allowed = []

    def work():
        for _ in range(10):
            allowed.append(lim.check("k").allowed)

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(allowed) == 20 and len(allowed) == 60


def test_memory_threads_never_exceed_the_burst():
    lim = RateLimiter(0.001, 20)
    allowed = []

    def work():
        for _ in range(200):
            allowed.append(lim.check("k").allowed)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(allowed) == 20


def test_a_key_that_is_exactly_full_again_can_be_forgotten(make):
    lim, clock = make(rate=1.0, burst=2, max_keys=1)
    assert lim.check("a").allowed  # one token left
    assert lim.check("b").reason == "too_many_keys"
    clock.t += 1.0  # exactly full again
    assert lim.check("b").allowed
