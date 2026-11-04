import copy
import json
import random

import pytest

from ragleap_optimizer import (InvalidRequestError, Optimizer, count_request, estimate_tokens, steps)
from ragleap_security import AuditLog, LimitError, Scope, ScopeError, verify

BIG = "x" * 300
SECRET = "very private words"


def request():
    return {"messages": [{"role": "system", "content": "be brief"}, {"role": "tool", "content": BIG},
                         {"role": "user", "content": "hi"}, {"role": "tool", "content": BIG}], "model": "m"}


def test_report_counts_match_the_counter_and_the_request_is_not_changed():
    r = request()
    before = copy.deepcopy(r)
    out = Optimizer([steps.dedupe_context()]).run(r)
    assert r == before
    assert out.report.tokens_before == count_request(before)
    assert out.report.tokens_after == count_request(out.request) < out.report.tokens_before
    assert out.report.tokens_saved == out.report.tokens_before - out.report.tokens_after
    assert [s.name for s in out.report.steps] == ["dedupe_context"] and out.report.steps[0].changed


def test_steps_run_in_order_and_each_reports_its_own_tokens():
    long_tool = {"messages": [{"role": "tool", "content": "h" + "m" * 5000 + "t"}]}
    out = Optimizer([steps.minify_whitespace(), steps.trim_tool_output(100)]).run(long_tool)
    a, b = out.report.steps
    assert (a.name, b.name) == ("minify_whitespace", "trim_tool_output")
    assert a.tokens_after == b.tokens_before and b.tokens_after < b.tokens_before


def test_lossy_is_true_only_when_a_lossy_step_really_changed_something():
    cut = Optimizer([steps.trim_tool_output(50)]).run({"messages": [{"role": "tool", "content": "q" * 3000}]})
    assert cut.report.lossy is True and cut.report.steps[0].lossy is True
    nothing = Optimizer([steps.trim_tool_output(50)]).run({"messages": [{"role": "tool", "content": "short"}]})
    assert nothing.report.lossy is False
    lossless = Optimizer([steps.dedupe_context()]).run(request())
    assert lossless.report.lossy is False


def test_a_step_that_claims_lossy_but_changes_nothing_is_not_reported_lossy():
    def liar(request, counter):
        return request, True
    liar.step_name = "liar"
    assert Optimizer([liar]).run(request()).report.lossy is False


def test_a_step_that_makes_the_request_longer_is_discarded_and_reported():
    def bloat(request, counter):
        out = copy.deepcopy(request)
        out["messages"].append({"role": "user", "content": "padding " * 50})
        return out, False
    bloat.step_name = "bloat"
    r = request()
    out = Optimizer([bloat]).run(r)
    assert out.request == r and out.report.tokens_after == out.report.tokens_before
    s = out.report.steps[0]
    assert s.skipped and not s.changed and not s.lossy


def test_a_lossless_pipeline_never_makes_a_request_longer():
    rng = random.Random(7)
    opt = Optimizer([steps.dedupe_context(20), steps.minify_whitespace(), steps.stable_prefix(),
                     steps.trim_tool_output(60)])
    words = ["alpha", "beta\n\n\n\n", "{\"a\": 1,  \"b\": [1, 2]}", "x" * 50, "  ", "\u00e9\u00e8"]
    for _ in range(200):
        msgs = [{"role": rng.choice(["system", "user", "tool", "assistant"]),
                 "content": " ".join(rng.choices(words, k=rng.randint(1, 80)))} for _ in range(rng.randint(0, 8))]
        out = opt.run({"messages": msgs})
        assert out.report.tokens_after <= out.report.tokens_before
        assert all(s.tokens_after <= s.tokens_before for s in out.report.steps)


def test_custom_counter_is_used():
    out = Optimizer([steps.dedupe_context()], counter=lambda t: len(t)).run(request())
    assert out.report.tokens_before == count_request(request(), len)


@pytest.mark.parametrize("bad", [-1, True, 1.5, "3", None])
def test_a_bad_counter_result_is_refused(bad):
    with pytest.raises(InvalidRequestError):
        Optimizer([], counter=lambda t: bad).run(request())


def test_bad_steps_and_options_are_refused():
    with pytest.raises(InvalidRequestError):
        Optimizer(["nope"])
    with pytest.raises(InvalidRequestError):
        Optimizer([lambda r, c: (r, False)])             # a plain function has no step_name
    with pytest.raises(InvalidRequestError):
        Optimizer([steps.minify_whitespace()] * 51)
    with pytest.raises(InvalidRequestError):
        Optimizer([], counter="x")
    for name in ("max_request_bytes", "max_messages"):
        for bad in (0, -1, True, 1.5):
            with pytest.raises(InvalidRequestError):
                Optimizer([], **{name: bad})


@pytest.mark.parametrize("bad", [None, [], "x", {"messages": "no"}, {"messages": ["x"]}, {"messages": [{"content": "no role"}]},
                                 {"messages": [{"role": 5}]}, {"messages": [], "x": {1, 2}}])
def test_bad_requests_are_refused(bad):
    with pytest.raises(InvalidRequestError):
        Optimizer([]).run(bad)


def test_size_and_message_limits():
    with pytest.raises(InvalidRequestError):
        Optimizer([], max_messages=2).run({"messages": [{"role": "user", "content": "a"}] * 3})
    Optimizer([], max_messages=3).run({"messages": [{"role": "user", "content": "a"}] * 3})
    with pytest.raises(LimitError):
        Optimizer([], max_request_bytes=100).run({"messages": [{"role": "user", "content": "a" * 500}]})


def test_audit_needs_a_scope_and_records_counts_not_text(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    opt = Optimizer([steps.dedupe_context(10)], audit=AuditLog(path))
    r = {"messages": [{"role": "tool", "content": SECRET + " " + BIG}, {"role": "tool", "content": SECRET + " " + BIG}]}
    with pytest.raises(ScopeError):
        opt.run(r)
    with pytest.raises(ScopeError):
        opt.run(r, "acme")                                # a string is not a scope
    out = opt.run(r, Scope("acme"))
    assert verify(path).ok
    rec = json.loads(open(path).read().splitlines()[-1])
    assert rec["event"] == "optimizer.run"
    assert rec["data"] == {"tokens_before": out.report.tokens_before, "tokens_after": out.report.tokens_after,
                           "steps": ["dedupe_context"], "lossy": False}
    assert SECRET not in open(path).read()


def test_no_audit_means_no_scope_needed():
    Optimizer([steps.dedupe_context()]).run(request())


def test_a_step_that_changes_its_input_in_place_cannot_change_the_callers_request():
    def careless(request, counter):
        request["messages"].append({"role": "user", "content": "x"})   # a badly written step
        return request, False
    careless.step_name = "careless"
    r = request()
    before = copy.deepcopy(r)
    Optimizer([careless]).run(r)
    assert r == before
