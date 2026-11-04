import pytest

from ragleap_optimizer import AllFailedError, InvalidRequestError, retry, with_fallback


class Boom(Exception):
    pass


class Other(Exception):
    pass


class Clk:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def flaky(fail_times, exc=Boom):
    calls = []

    def fn():
        calls.append(1)
        if len(calls) <= fail_times:
            raise exc(f"fail {len(calls)}")
        return "ok"
    fn.calls = calls
    return fn


def test_retry_succeeds_after_failures_with_doubling_capped_delays():
    k = Clk()
    fn = flaky(4)
    assert retry(fn, attempts=5, base_delay=1, max_delay=3, sleep=k.sleep, clock=k.now, retry_on=(Boom,)) == "ok"
    assert len(fn.calls) == 5 and k.sleeps == [1, 2, 3, 3]


def test_retry_raises_the_last_error_when_attempts_run_out():
    k = Clk()
    fn = flaky(99)
    with pytest.raises(Boom, match="fail 3"):
        retry(fn, attempts=3, sleep=k.sleep, clock=k.now)
    assert len(fn.calls) == 3 and len(k.sleeps) == 2         # no sleep after the last attempt


def test_a_single_attempt_is_called_once_and_never_sleeps():
    k = Clk()
    fn = flaky(99)
    with pytest.raises(Boom):
        retry(fn, attempts=1, sleep=k.sleep, clock=k.now)
    assert len(fn.calls) == 1 and k.sleeps == []


def test_errors_not_in_retry_on_are_raised_at_once():
    k = Clk()
    fn = flaky(5, exc=Other)
    with pytest.raises(Other):
        retry(fn, attempts=5, sleep=k.sleep, clock=k.now, retry_on=(Boom,))
    assert len(fn.calls) == 1 and k.sleeps == []


def test_keyboard_interrupt_is_never_retried():
    calls = []

    def fn():
        calls.append(1)
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        retry(fn, attempts=5, sleep=lambda s: None)
    assert len(calls) == 1


def test_the_deadline_stops_retrying_before_waiting_past_it():
    k = Clk()
    fn = flaky(99)
    with pytest.raises(Boom):
        retry(fn, attempts=10, base_delay=2, max_delay=100, deadline_seconds=7, sleep=k.sleep, clock=k.now)
    assert k.sleeps == [2, 4] and len(fn.calls) == 3          # the next wait (8) would pass the deadline


def test_exactly_at_the_deadline_is_allowed():
    k = Clk()
    fn = flaky(1)
    assert retry(fn, attempts=3, base_delay=5, deadline_seconds=5, sleep=k.sleep, clock=k.now) == "ok"


@pytest.mark.parametrize("name,bad", [("attempts", 0), ("attempts", 11), ("attempts", True), ("attempts", 1.5),
                                      ("base_delay", -1), ("base_delay", True), ("base_delay", "1"),
                                      ("max_delay", -0.1), ("deadline_seconds", 0), ("deadline_seconds", True),
                                      ("deadline_seconds", "5"), ("retry_on", ()), ("retry_on", Boom),
                                      ("retry_on", (KeyboardInterrupt,)), ("retry_on", ("Boom",)), ("retry_on", (int,))])
def test_bad_retry_arguments_are_refused(name, bad):
    with pytest.raises(InvalidRequestError):
        retry(lambda: "ok", **{name: bad})


def test_fallback_returns_the_first_success_and_passes_arguments():
    seen = []

    def bad(x, y=0):
        seen.append(("bad", x, y))
        raise Boom("down")

    def good(x, y=0):
        seen.append(("good", x, y))
        return x + y
    assert with_fallback(bad, good)(1, y=2) == 3
    assert seen == [("bad", 1, 2), ("good", 1, 2)]


def test_fallback_stops_at_the_first_success():
    seen = []
    with_fallback(lambda: seen.append(1) or "a", lambda: seen.append(2) or "b")()
    assert seen == [1]


def test_all_failed_keeps_the_causes_in_order_and_hides_their_text():
    def f1():
        raise Boom("very private words")

    def f2():
        raise Other("more private words")
    with pytest.raises(AllFailedError) as ei:
        with_fallback(f1, f2)()
    assert [type(c) for c in ei.value.causes] == [Boom, Other]
    assert "private" not in str(ei.value) and "Boom" in str(ei.value) and "Other" in str(ei.value)


def test_fallback_raises_errors_outside_retry_on_without_trying_the_next():
    seen = []

    def f1():
        raise Other("x")
    with pytest.raises(Other):
        with_fallback(f1, lambda: seen.append(1), retry_on=(Boom,))()
    assert seen == []


@pytest.mark.parametrize("bad", [(), ("x",), (None,)])
def test_fallback_needs_callables(bad):
    with pytest.raises(InvalidRequestError):
        with_fallback(*bad)


def test_fallback_validates_retry_on():
    with pytest.raises(InvalidRequestError):
        with_fallback(lambda: 1, retry_on=(KeyboardInterrupt,))
