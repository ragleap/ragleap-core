import json

import pytest
from ragleap_agents import InMemoryStateStore, ResumeError, SQLiteStateStore
from ragleap_security import AuditLog, RateLimiter, RateLimitError, Scope

import ragleap_flows as rf
from conftest import A, B, make, refund_flow


def runner(flow, kit, **kw):
    return rf.FlowRunner(flow, kit, **kw)


def test_a_small_flow_runs_and_returns_its_output(kit, calls):
    r = runner(refund_flow(), kit).run(A, {"amount": 40, "who": "ann@x.test"})
    assert r.status == "done" and r.stop_reason == "completed" and r.output == {"paid": True}
    assert [s["step"] for s in r.steps] == ["look", "big_or_small", "pay"]
    assert ("send", {"to": "ann@x.test", "body": 40}) in calls.log


def test_an_approval_pauses_then_resumes_and_finishes(kit, calls):
    rn = runner(refund_flow(), kit)
    r = rn.run(A, {"amount": 500, "who": "ann@x.test"})
    assert r.status == "awaiting_approval" and r.pending == {"step": "ask", "prompt": "Approve this refund?", "show": {"amount": 500}, "forced": False}
    assert not [c for c in calls.log if c[0] == "send"]                      # nothing outbound before approval
    done = rn.resume(A, r.run_id, {"ask": True})
    assert done.status == "done" and done.output == {"paid": True}
    assert [c for c in calls.log if c[0] == "send"] == [("send", {"to": "ann@x.test", "body": 500})]


def test_a_rejection_ends_the_run_without_running_later_steps(kit, calls):
    rn = runner(refund_flow(), kit)
    r = rn.run(A, {"amount": 500, "who": "x@x.test"})
    done = rn.resume(A, r.run_id, {"ask": False})
    assert done.status == "rejected" and done.steps[-1] == {"step": "ask", "type": "approval", "status": "rejected"}
    assert not [c for c in calls.log if c[0] == "send"]


@pytest.mark.parametrize("decisions,msg", [({}, "no decision"), ({"other": True}, "no decision"), ({"ask": True, "x": True}, "not pending"),
                                           ({"ask": "yes"}, "True or False"), ({"ask": 1}, "True or False"), (None, "no decision")])
def test_bad_decisions_are_refused_and_the_run_stays_paused(kit, decisions, msg):
    rn = runner(refund_flow(), kit)
    r = rn.run(A, {"amount": 500, "who": "x@x.test"})
    with pytest.raises(ResumeError, match=msg):
        rn.resume(A, r.run_id, decisions)
    assert rn.resume(A, r.run_id, {"ask": True}).status == "done"           # positive control: still resumable


def test_a_second_resume_is_refused(kit, calls):
    rn = runner(refund_flow(), kit)
    r = rn.run(A, {"amount": 500, "who": "x@x.test"})
    rn.resume(A, r.run_id, {"ask": True})
    with pytest.raises(ResumeError, match="not awaiting"):
        rn.resume(A, r.run_id, {"ask": True})
    assert len([c for c in calls.log if c[0] == "send"]) == 1


def test_concurrent_resumes_run_the_tool_once(kit, calls, tmp_path):
    import threading

    rn = runner(refund_flow(), kit, store=SQLiteStateStore(tmp_path / "s.db"))
    r = rn.run(A, {"amount": 500, "who": "x@x.test"})
    outcomes = []

    def go():
        try:
            outcomes.append(rn.resume(A, r.run_id, {"ask": True}).status)
        except ResumeError as e:
            outcomes.append(str(e))

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert outcomes.count("done") == 1 and len([c for c in calls.log if c[0] == "send"]) == 1


def test_a_tool_failure_stops_the_run_and_later_steps_do_not_run(kit, calls):
    f = make([{"id": "a", "type": "tool", "tool": "boom", "next": "b"}, {"id": "b", "type": "tool", "tool": "echo"}])
    r = runner(f, kit).run(A)
    assert r.status == "failed" and r.stop_reason == "tool_failed:a" and r.steps == [{"step": "a", "type": "tool", "status": "failed", "error": "nope"}]
    assert [c[0] for c in calls.log] == ["boom"]


def test_a_tool_that_raises_gives_only_its_type(kit):
    def raises(scope, name, args):
        raise RuntimeError("secret detail sk-123")

    f = make([{"id": "a", "type": "tool", "tool": "echo"}])
    r = rf.FlowRunner(f, dispatch=raises).run(A)
    assert r.status == "failed" and "secret" not in json.dumps(r.steps) and "RuntimeError" in r.steps[0]["error"]


def test_an_invalid_dispatch_result_fails_the_step():
    f = make([{"id": "a", "type": "tool", "tool": "echo"}])
    r = rf.FlowRunner(f, dispatch=lambda s, n, a: "not a result").run(A)
    assert r.status == "failed" and r.stop_reason == "tool_failed:a" and r.steps[0]["error"] == "tool returned an invalid result"
    assert rf.FlowRunner(f, dispatch=lambda s, n, a: type("R", (), {"success": "yes", "result": 1, "error": None})()).run(A).status == "failed"


def test_dispatch_gets_the_scope_the_tool_and_the_resolved_arguments(kit):
    seen = []

    def dispatch(scope, name, args):
        seen.append((scope, name, args))
        return type("R", (), {"success": True, "result": {"ok": 1}, "error": None})()

    f = make([{"id": "a", "type": "tool", "tool": "echo", "arguments": {"v": "$input.n", "lit": "$$x"}}])
    assert rf.FlowRunner(f, dispatch=dispatch).run(B, {"n": 7}).status == "done"
    assert seen == [(B, "echo", {"v": 7, "lit": "$x"})]


def test_a_missing_value_fails_closed(kit, calls):
    f = make([{"id": "a", "type": "tool", "tool": "echo", "arguments": {"value": "$input.nope"}}])
    r = runner(f, kit).run(A, {"other": 1})
    assert r.status == "failed" and r.stop_reason == "missing_value" and calls.log == []


@pytest.mark.parametrize("value,expect", [(150, "ask"), (100, "pay"), (99.5, "pay"), ("150", "pay"), (True, "pay"), (None, "pay")])
def test_branch_comparisons_never_treat_other_types_as_numbers(kit, value, expect):
    r = runner(refund_flow(), kit).run(A, {"amount": value, "who": "x"})
    if expect == "ask":
        assert r.status == "awaiting_approval" and r.pending["step"] == "ask"
    else:
        assert r.status == "done" and [s["step"] for s in r.steps][-1] == "pay"       # a wrong type is a plain 'no', not a crash


def test_all_any_in_exists_and_truthy(kit):
    f = make([
        {"id": "br", "type": "branch", "cases": [
            {"when": {"all": [{"path": "$input.a", "op": "in", "value": [1, 2]}, {"path": "$input.b", "op": "exists"}]}, "goto": "one"},
            {"when": {"any": [{"path": "$input.c", "op": "truthy"}, {"path": "$input.d", "op": "ne", "value": 5}]}, "goto": "two"}],
         "else": "three"},
        {"id": "one", "type": "tool", "tool": "echo", "arguments": {"value": "one"}},
        {"id": "two", "type": "tool", "tool": "echo", "arguments": {"value": "two"}},
        {"id": "three", "type": "tool", "tool": "echo", "arguments": {"value": "three"}},
    ])
    route = lambda i: [s["step"] for s in runner(f, kit).run(A, i).steps][-1]
    assert route({"a": 1, "b": 0}) == "one" and route({"a": True, "b": 0, "c": 1}) == "two"
    assert route({"a": 9, "c": 0, "d": 5}) == "three" and route({"d": 4}) == "two"
    assert route({"a": 1}) == "three"                        # b is missing, so the all() fails; c and d are missing too


def test_a_missing_value_never_satisfies_ne(kit):
    f = make([{"id": "br", "type": "branch", "cases": [{"when": {"path": "$input.x", "op": "ne", "value": 1}, "goto": "yes"}], "else": None},
              {"id": "yes", "type": "tool", "tool": "echo"}])
    assert [s["step"] for s in runner(f, kit).run(A, {}).steps] == ["br"]
    assert [s["step"] for s in runner(f, kit).run(A, {"x": 2}).steps] == ["br", "yes"]


def test_the_output_is_none_when_a_named_step_was_skipped(kit):
    f = make([{"id": "br", "type": "branch", "cases": [{"when": {"path": "$input.x", "op": "truthy"}, "goto": "a"}], "else": None},
              {"id": "a", "type": "tool", "tool": "echo", "arguments": {"value": 1}}], output={"v": "$a.value"})
    skipped = runner(f, kit).run(A, {"x": 0})
    assert skipped.status == "done" and skipped.output is None and skipped.stop_reason == "output_unavailable"
    assert runner(f, kit).run(A, {"x": 1}).output == {"v": 1}


def test_runner_configuration_is_checked_up_front(kit):
    f = refund_flow()
    for kw in ({"tools": None}, {"dispatch": lambda *a: None, "tools": kit}, {"store": object(), "store_for": lambda t: None},
               {"audit": "x"}, {"rate_limiter": object()}, {"deadline_seconds": 0}, {"deadline_seconds": True}, {"clock": 5},
               {"store_for": 5}, {"dispatch": 5}):
        with pytest.raises(rf.FlowError):
            rf.FlowRunner(f, **{"tools": kit, **kw} if "tools" not in kw else kw)
    with pytest.raises(rf.FlowError):
        rf.FlowRunner("not a flow", kit)
    with pytest.raises(rf.FlowError, match="not in the toolkit"):
        rf.FlowRunner(make([{"id": "a", "type": "tool", "tool": "ghost"}]), kit)
    rf.FlowRunner(f, kit)   # positive control


def test_input_must_be_small_plain_json(kit):
    rn = runner(refund_flow(), kit)
    for bad in ({"x": object()}, {"x": float("nan")}, {"x": "y" * 70_000}):
        with pytest.raises(ValueError):
            rn.run(A, bad)
    with pytest.raises(ValueError):
        rn.run(A, {}, tainted="yes")
    assert rn.run(A, {"amount": 1, "who": "x"}).status == "done"


def test_the_deadline_stops_a_run_between_steps(kit):
    t = [0.0]
    ticks = lambda: t.__setitem__(0, t[0] + 100) or t[0]
    f = make([{"id": "a", "type": "tool", "tool": "echo", "next": "b"}, {"id": "b", "type": "tool", "tool": "echo"}])
    r = rf.FlowRunner(f, kit, clock=ticks, deadline_seconds=150).run(A)
    assert r.status == "stopped" and r.stop_reason == "deadline"
    assert rf.FlowRunner(f, kit, deadline_seconds=None).run(A).status == "done"


def test_big_tool_output_and_unserialisable_output_fail_the_step(kit):
    f = make([{"id": "a", "type": "tool", "tool": "big"}])
    assert runner(f, kit).run(A).stop_reason == "tool_failed:a"
    f2 = make([{"id": "a", "type": "tool", "tool": "echo"}])
    odd = lambda s, n, a: type("R", (), {"success": True, "result": {"x": object()}, "error": None})()
    r = rf.FlowRunner(f2, dispatch=odd).run(A)
    assert r.status == "failed" and r.steps[0]["error"] == "output_not_json"


def test_state_too_large_fails_and_drops_the_data(kit):
    big = lambda s, n, a: type("R", (), {"success": True, "result": "y" * 60_000, "error": None})()
    steps = [{"id": "s%d" % i, "type": "tool", "tool": "echo", "next": "s%d" % (i + 1) if i < 29 else None} for i in range(30)]
    store = InMemoryStateStore()
    r = rf.FlowRunner(make(steps), dispatch=big, store=store).run(A, {"k": "v"})
    assert r.status == "failed" and r.stop_reason == "state_too_large"
    saved = store.load(r.run_id)
    assert saved["outputs"] == {} and saved["input"] == {}


def test_rate_limiter_is_checked_before_anything_else(kit, calls):
    t = lambda: 1000.0
    rl = RateLimiter(1.0, 1, clock=t)
    rn = runner(refund_flow(), kit, rate_limiter=rl)
    rn.run(A, {"amount": 1, "who": "x"})
    n = len(calls.log)
    with pytest.raises(RateLimitError):
        rn.run(A, {"amount": 1, "who": "x"})
    assert len(calls.log) == n and rn.run(B, {"amount": 1, "who": "x"}).status == "done"


def test_the_first_matching_case_wins(kit):
    f = make([{"id": "br", "type": "branch", "cases": [{"when": {"path": "$input.x", "op": "gt", "value": 1}, "goto": "first"},
                                                         {"when": {"path": "$input.x", "op": "gt", "value": 2}, "goto": "second"}]},
              {"id": "first", "type": "tool", "tool": "echo"}, {"id": "second", "type": "tool", "tool": "echo"}])
    assert [s["step"] for s in runner(f, kit).run(A, {"x": 5}).steps] == ["br", "first"]


@pytest.mark.parametrize("op,value,ref,expect", [
    ("lt", 1, 2, True), ("lt", 2, 2, False), ("le", 2, 2, True), ("le", 3, 2, False), ("gt", 3, 2, True), ("gt", 2, 2, False),
    ("ge", 2, 2, True), ("ge", 1, 2, False), ("lt", "a", "b", True), ("gt", "b", "a", True), ("lt", 1.5, 2, True),
    ("lt", True, 5, False), ("gt", 5, False, False), ("lt", "1", 5, False), ("gt", 5, "1", False), ("gt", [2], 1, False),
    ("eq", 1, True, False), ("eq", True, True, True), ("eq", 1, 1.0, True), ("ne", True, 1, True), ("in", True, [1], False),
    ("in", 1, [True, 1], True),
])
def test_condition_operators_table(op, value, ref, expect):
    from ragleap_flows.flow import eval_cond

    assert eval_cond({"path": "$input.v", "op": op, "value": ref}, {"input": {"v": value}}) is expect


@pytest.mark.parametrize("path", ["$input.items.5", "$input.items.01", "$input.items.-1", "$input.items.x", "$input.n.0", "$input.nope"])
def test_list_paths_are_strict(kit, path):
    f = make([{"id": "a", "type": "tool", "tool": "echo", "arguments": {"value": path}}])
    r = runner(f, kit).run(A, {"items": ["p", "q"], "n": 3})
    assert r.status == "failed" and r.stop_reason == "missing_value"
    ok = make([{"id": "a", "type": "tool", "tool": "echo", "arguments": {"value": "$input.items.1"}}])
    assert runner(ok, kit).run(A, {"items": ["p", "q"]}).status == "done"


def test_a_state_that_is_not_awaiting_is_refused_even_if_it_still_holds_a_pending_call(kit):
    st = InMemoryStateStore()
    rn = runner(refund_flow(), kit, store=st)
    r = rn.run(A, {"amount": 500, "who": "x"})
    good = st.load(r.run_id)
    st.save(r.run_id, {**good, "status": "running"})            # damaged: pending still set, status moved on
    with pytest.raises(ResumeError, match="not awaiting"):
        rn.resume(A, r.run_id, {"ask": True})


def test_two_resumers_that_both_loaded_the_paused_state_run_the_tool_once(kit, calls, tmp_path):
    import threading

    class Gate:
        """Both threads load the paused state before either one claims it (forces the race)."""
        def __init__(self, inner):
            self.inner, self.barrier = inner, threading.Barrier(2, timeout=5)
            self.save, self.save_if_status = inner.save, inner.save_if_status

        def load(self, run_id):
            st = self.inner.load(run_id)
            try:
                self.barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return st

    gate = Gate(SQLiteStateStore(tmp_path / "s.db"))
    rn = runner(refund_flow(), kit, store=gate)
    gate.barrier.abort()                                        # no waiting for the first run's own bookkeeping
    r = rn.run(A, {"amount": 500, "who": "x"})
    gate.barrier = threading.Barrier(2, timeout=5)
    outcomes = []

    def go():
        try:
            outcomes.append(rn.resume(A, r.run_id, {"ask": True}).status)
        except ResumeError as e:
            outcomes.append(str(e))

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(outcomes) == ["done", "run is not awaiting approval"]
    assert len([c for c in calls.log if c[0] == "send"]) == 1


def test_a_store_without_an_atomic_claim_still_refuses_a_state_that_is_not_awaiting(kit, calls):
    class PlainStore:                                           # only save and load, like a minimal custom store
        def __init__(self):
            self.data = {}

        def save(self, run_id, state):
            self.data[run_id] = json.dumps(state)

        def load(self, run_id):
            return json.loads(self.data[run_id]) if run_id in self.data else None

    st = PlainStore()
    rn = runner(refund_flow(), kit, store=st)
    r = rn.run(A, {"amount": 500, "who": "x"})
    good = st.load(r.run_id)
    st.save(r.run_id, {**good, "status": "running"})
    with pytest.raises(ResumeError, match="not awaiting"):
        rn.resume(A, r.run_id, {"ask": True})
    assert not [c for c in calls.log if c[0] == "send"]
    st.save(r.run_id, good)
    assert rn.resume(A, r.run_id, {"ask": True}).status == "done"      # positive control with the plain store
