import random

import pytest

from ragleap_optimizer import InvalidRequestError, Optimizer, evaluate, steps

BIG = "p" * 300


def make_cases(n, dup=True):
    out = []
    for i in range(n):
        msgs = [{"role": "user", "content": f"question {i}"}]
        if dup:
            msgs += [{"role": "tool", "content": BIG}, {"role": "tool", "content": BIG}]
        out.append({"request": {"messages": msgs}, "expected": f"answer {i}"})
    return out


def echo_run(request):
    return request["messages"][0]["content"].replace("question", "answer")


def exact_judge(case, response):
    return response == case["expected"]


OPT = Optimizer([steps.dedupe_context()])


def test_numbers_match_a_hand_computation():
    t = evaluate(make_cases(40), echo_run, OPT, exact_judge)
    one = OPT.run(make_cases(1)[0]["request"]).report
    assert (t.n, t.n_errors) == (40, 0)
    assert t.tokens_before == 40 * one.tokens_before and t.tokens_after == 40 * one.tokens_after
    assert (t.mean_baseline, t.mean_optimized, t.delta) == (1.0, 1.0, 0.0)
    assert (t.delta_low, t.delta_high) == (0.0, 0.0) and t.regressions == () and t.lossy_cases == 0 and t.enough


def test_a_summary_with_enough_cases_shows_the_savings_and_the_interval():
    t = evaluate(make_cases(40), echo_run, OPT, exact_judge)
    s = t.summary()
    one = OPT.run(make_cases(1)[0]["request"]).report
    pct = 100.0 * (1 - one.tokens_after / one.tokens_before)
    assert f"{pct:.1f}% fewer" in s and "95% interval" in s and "Input tokens only" in s
    assert "optimized answer scored lower: none" in s


def test_too_few_cases_gives_no_figures():
    t = evaluate(make_cases(5), echo_run, OPT, exact_judge)
    s = t.summary()
    assert not t.enough and "Too few cases" in s and "% fewer" not in s and "interval" not in s
    assert "need 30" in s
    assert evaluate(make_cases(5), echo_run, OPT, exact_judge, min_cases=5).enough


def test_no_cases_at_all():
    t = evaluate([], echo_run, OPT, exact_judge)
    assert (t.n, t.tokens_before) == (0, 0) and "Too few cases" in t.summary()


def test_regressions_are_listed_by_case_index():
    def run(request):                                     # the optimizer removed a duplicate: answer breaks for odd cases
        n = sum(1 for m in request["messages"] if m["role"] == "tool")
        text = request["messages"][0]["content"].replace("question", "answer")
        return text if n == 2 or int(text.split()[1]) % 2 == 0 else "wrong"
    t = evaluate(make_cases(40), run, OPT, exact_judge)
    assert t.regressions == tuple(i for i in range(40) if i % 2 == 1)
    assert t.delta == pytest.approx(-0.5) and t.delta_high < 0 and "scored lower: 1, 3, 5" in t.summary()


def test_improvements_are_not_regressions():
    def run(request):
        n = sum(1 for m in request["messages"] if m["role"] == "tool")
        return request["messages"][0]["content"].replace("question", "answer") if n == 1 else "wrong"
    t = evaluate(make_cases(40), run, OPT, exact_judge)
    assert t.regressions == () and t.delta == pytest.approx(1.0)


def test_failures_are_counted_named_and_left_out():
    def run(request):
        if request["messages"][0]["content"] == "question 3":
            raise RuntimeError("boom with private words")
        return echo_run(request)
    t = evaluate(make_cases(40), run, OPT, exact_judge)
    assert (t.n, t.n_errors) == (39, 1) and t.error_kinds == ("RuntimeError",)
    assert "1 failed (RuntimeError)" in t.summary() and "private" not in t.summary()


@pytest.mark.parametrize("bad", [2.0, -0.1, "x", None, [1]])
def test_a_bad_judge_result_counts_as_a_failure(bad):
    t = evaluate(make_cases(3), echo_run, OPT, lambda c, r: bad, min_cases=1)
    assert t.n == 0 and t.n_errors == 3 and t.error_kinds == ("InvalidRequestError",) * 3


def test_a_judge_may_return_numbers_between_zero_and_one():
    t = evaluate(make_cases(4), echo_run, OPT, lambda c, r: 0.25, min_cases=1)
    assert t.mean_baseline == 0.25 and t.mean_optimized == 0.25


def test_lossy_cases_are_counted():
    lossy = Optimizer([steps.trim_tool_output(20)])
    cases = [{"request": {"messages": [{"role": "tool", "content": "x" * 3000 if i % 2 else "ok"}]}, "expected": "a"} for i in range(6)]
    t = evaluate(cases, lambda r: "a", lossy, lambda c, r: True, min_cases=1)
    assert t.lossy_cases == 3
    assert "a lossy step changed the request: 3" in t.summary()


def test_the_interval_is_repeatable_for_a_seed_and_brackets_the_difference():
    def run(request):
        return "wrong" if len(request["messages"]) == 2 and request["messages"][0]["content"].endswith(("1", "2")) else echo_run(request)
    t1 = evaluate(make_cases(60), run, OPT, exact_judge, seed=5)
    t2 = evaluate(make_cases(60), run, OPT, exact_judge, seed=5)
    assert (t1.delta_low, t1.delta_high) == (t2.delta_low, t2.delta_high)
    assert t1.delta_low <= t1.delta <= t1.delta_high and t1.delta_low < t1.delta_high


def test_an_invalid_request_in_a_case_is_a_failure_not_a_crash():
    cases = make_cases(2) + [{"request": "not a request", "expected": "x"}]
    t = evaluate(cases, echo_run, OPT, exact_judge, min_cases=1)
    assert (t.n, t.n_errors) == (2, 1) and t.error_kinds == ("InvalidRequestError",)


@pytest.mark.parametrize("name,bad", [("min_cases", 0), ("min_cases", True), ("min_cases", 1.5), ("bootstrap", 0),
                                      ("bootstrap", True), ("bootstrap", "9")])
def test_bad_arguments_are_refused(name, bad):
    with pytest.raises(InvalidRequestError):
        evaluate(make_cases(1), echo_run, OPT, exact_judge, **{name: bad})


def test_the_bootstrap_really_uses_the_given_seed(monkeypatch):
    seen = []
    real = random.Random

    class Recording(real):
        def __init__(self, *args):
            seen.append(args)
            super().__init__(*args)
    monkeypatch.setattr(random, "Random", Recording)
    evaluate(make_cases(40), echo_run, OPT, exact_judge, seed=123)
    assert seen == [(123,)]
