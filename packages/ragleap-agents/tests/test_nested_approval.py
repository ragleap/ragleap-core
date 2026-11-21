"""Nested approvals (design: issue #963): a pause anywhere in the delegation tree appears at the top run as ONE pending
approval, and one resume() on the top run decides it. A scripted model replaces the LLM everywhere."""
import json
import threading

import pytest
from ragleap_security import AuditLog, Scope
from ragleap_tools import Tool, ToolResult

from ragleap_agents.context import current_run
from ragleap_agents import (Agent, Budget, DelegateTool, InMemoryStateStore, Policy, ResumeError, SQLiteStateStore,
                            ToolPolicy, delegate_policy, make_delegate_tool)

DONE = json.dumps({"tool": "done"})
ASK = ToolPolicy(taints=False, outbound=True, requires_approval=True)
OPEN = ToolPolicy(taints=False, outbound=True)
READ = ToolPolicy(taints=True, outbound=False)
A, B = Scope("acme"), Scope("globex")


def plan(tool, **arguments):
    return json.dumps({"tool": tool, "arguments": arguments})


class Script:
    def __init__(self, replies):
        self.replies = list(replies)

    def __call__(self, prompt):
        if not self.replies:
            raise RuntimeError("no scripted reply left")
        return self.replies.pop(0)


def _tool(name, log):
    return Tool(name, name, {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
                lambda message: (log.append((name, message)), ToolResult(True, "ok:" + message))[1])


def leaf(replies, store=None, **policies):
    """A child with outbound tools; the keyword is the tool name, the value its policy (default: needs approval)."""
    log, store = [], store or InMemoryStateStore()
    pols = policies or {"send": ASK}
    return Agent(Script(replies), [_tool(n, log) for n in pols], Policy(tools=pols, max_steps=6), store, summarize=False), log, store


def delegating(replies, children, *, max_depth=1, nested=True, store=None, budget=None):
    tool = make_delegate_tool(children, max_depth=max_depth, allow_outbound=True, nested_approval=nested, budget=budget)
    pol = Policy(tools={"delegate": delegate_policy(children, allow_outbound=True)}, max_steps=6)
    return Agent(Script(replies), [tool], pol, store or InMemoryStateStore(), summarize=False)


def one_level(child_replies=None, parent_replies=None, **kw):
    child, log, cstore = leaf(child_replies or [plan("send", message="hi"), DONE])
    pstore = InMemoryStateStore()
    parent = delegating(parent_replies or [plan("delegate", agent="actor", task="send it"), DONE], {"actor": child}, store=pstore, **kw)
    return parent, child, log, pstore, cstore


def two_levels(leaf_replies, store_leaf=None):
    lf, log, lstore = leaf(leaf_replies, store=store_leaf)
    mstore, rstore = InMemoryStateStore(), InMemoryStateStore()
    mid = delegating([plan("delegate", agent="leaf", task="go"), DONE], {"leaf": lf}, max_depth=2, store=mstore)
    root = delegating([plan("delegate", agent="mid", task="go"), DONE], {"mid": mid}, max_depth=2, store=rstore)
    return root, mid, lf, log, rstore, mstore, lstore


def test_a_childs_pause_appears_at_the_top_run_with_the_real_action_and_the_path_to_it():
    parent, child, log, pstore, cstore = one_level()
    r = parent.run("q", scope=A)
    assert r.status == "awaiting_approval" and log == []
    p = r.pending
    assert (p["tool"], p["arguments"], p["forced"]) == ("send", {"message": "hi"}, False)
    assert len(p["via"]) == 1 and p["via"][0]["agent"] == "actor" and p["delegate"]["tool"] == "delegate"
    assert cstore.load(p["via"][0]["run_id"])["status"] == "awaiting_approval" and pstore.load(r.run_id)["status"] == "awaiting_approval"


def test_approve_runs_the_action_exactly_once_and_the_parent_goes_on():
    parent, child, log, pstore, cstore = one_level()
    r = parent.run("q", scope=A)
    done = parent.resume(r.run_id, {r.pending["call_id"]: True}, scope=A)
    assert done.status == "done" and log == [("send", "hi")]
    step = done.steps[0]
    assert step["tool"] == "delegate" and step["status"] == "executed" and json.loads(step["observation"])["answer"]
    assert cstore.load(r.pending["via"][0]["run_id"])["status"] == "done"
    assert pstore.load(r.run_id)["delegation"] == {"delegations": 1, "child_steps": 1}


def test_reject_ends_the_whole_tree_and_the_action_never_runs():
    parent, child, log, pstore, cstore = one_level()
    r = parent.run("q", scope=A)
    done = parent.resume(r.run_id, {r.pending["call_id"]: False}, scope=A)
    assert done.status == "rejected" and log == []
    assert cstore.load(r.pending["via"][0]["run_id"])["status"] == "rejected"
    assert done.steps[0]["status"] == "rejected" and done.steps[0]["executed_before_pause"] == []


def test_a_second_resume_is_refused_and_the_action_stays_at_one_execution():
    parent, child, log, _, _ = one_level()
    r = parent.run("q", scope=A)
    cid = r.pending["call_id"]
    assert parent.resume(r.run_id, {cid: True}, scope=A).status == "done"
    with pytest.raises(ResumeError, match="not awaiting"):
        parent.resume(r.run_id, {cid: True}, scope=A)
    assert log == [("send", "hi")]


@pytest.mark.parametrize("decisions", [{}, {"nope": True}, "x", None])
def test_a_wrong_call_id_or_decision_is_refused_and_the_tree_stays_paused(decisions):
    parent, child, log, _, _ = one_level()
    r = parent.run("q", scope=A)
    with pytest.raises((ResumeError, TypeError)):
        parent.resume(r.run_id, decisions, scope=A)
    with pytest.raises(ResumeError):
        parent.resume(r.run_id, {r.pending["call_id"]: "yes"}, scope=A)
    assert parent.resume(r.run_id, {r.pending["call_id"]: True}, scope=A).status == "done"      # positive control


def test_a_parent_pending_that_does_not_match_the_childs_pending_is_refused_and_nothing_is_claimed():
    parent, child, log, pstore, cstore = one_level()
    r = parent.run("q", scope=A)
    st = pstore.load(r.run_id)
    for field, value in (("arguments", {"message": "other"}), ("tool", "other"), ("sig", ["x", "y"])):
        pstore.save(r.run_id, {**st, "pending": {**st["pending"], field: value}})
        with pytest.raises(ResumeError, match="does not match"):
            parent.resume(r.run_id, {st["pending"]["call_id"]: True}, scope=A)
    pstore.save(r.run_id, st)
    assert log == [] and cstore.load(st["pending"]["via"][0]["run_id"])["status"] == "awaiting_approval"
    assert parent.resume(r.run_id, {st["pending"]["call_id"]: True}, scope=A).status == "done"


@pytest.mark.parametrize("via", ["x", [], [{"agent": "actor"}], [{"agent": 5, "run_id": "r"}], [{"agent": "ghost", "run_id": "r"}],
                                 [{"agent": "actor", "run_id": "no-such-run"}], [{"agent": "actor", "run_id": "r"}] * 4, None])
def test_a_damaged_via_is_refused_on_approve_and_the_action_never_runs(via):
    parent, child, log, pstore, cstore = one_level()
    r = parent.run("q", scope=A)
    st = pstore.load(r.run_id)
    pstore.save(r.run_id, {**st, "pending": {**st["pending"], "via": via}})
    if via:                                                                     # an empty or missing via is not a nested pause
        with pytest.raises(ResumeError, match="does not match"):
            parent.resume(r.run_id, {st["pending"]["call_id"]: True}, scope=A)
    assert log == []


def test_a_lost_child_run_fails_closed_on_approve_but_a_reject_still_closes_the_tree():
    parent, child, log, pstore, cstore = one_level()
    r = parent.run("q", scope=A)
    cid, crun = r.pending["call_id"], r.pending["via"][0]["run_id"]
    del cstore._data[crun]                                                       # the child's store lost the run
    with pytest.raises(ResumeError, match="does not match"):
        parent.resume(r.run_id, {cid: True}, scope=A)
    assert log == [] and pstore.load(r.run_id)["status"] == "awaiting_approval"
    done = parent.resume(r.run_id, {cid: False}, scope=A)
    assert done.status == "rejected" and done.steps[0]["executed_before_pause"] == [
        {"agent": "actor", "run_id": crun, "unavailable": True}]


def test_a_rejected_tree_lists_what_the_child_already_ran():
    child, log, cstore = leaf([plan("note", message="n1"), plan("send", message="s1")], note=OPEN, send=ASK)
    parent = delegating([plan("delegate", agent="actor", task="t"), DONE], {"actor": child})
    r = parent.run("q", scope=A)
    assert r.status == "awaiting_approval" and log == [("note", "n1")]
    done = parent.resume(r.run_id, {r.pending["call_id"]: False}, scope=A)
    entry = done.steps[0]["executed_before_pause"]
    assert [(e["agent"], e["tool"], e["status"]) for e in entry] == [("actor", "note", "executed")]
    assert entry[0]["arguments"] == json.dumps({"message": "n1"}) and log == [("note", "n1")]          # not undone


def test_the_executed_before_pause_list_is_capped():
    from ragleap_agents import RunResult

    steps = [{"tool": "t%d" % i, "status": "executed", "arguments": {"i": i, "pad": "x" * 500}} for i in range(30)]
    res = RunResult("rid", "rejected", "rejected", "", steps, None, False)
    out = Agent._executed_entries("a", res)
    assert len(out) == 21 and out[-1] == {"truncated": True, "omitted": 10} and len(out[0]["arguments"]) <= 200


def test_depth_two_a_grandchilds_approval_passes_through_two_levels():
    root, mid, lf, log, rstore, mstore, lstore = two_levels([plan("send", message="deep"), DONE])
    r = root.run("q", scope=A)
    assert r.status == "awaiting_approval" and log == [] and [v["agent"] for v in r.pending["via"]] == ["mid", "leaf"]
    for store, rid in ((rstore, r.run_id), (mstore, r.pending["via"][0]["run_id"]), (lstore, r.pending["via"][1]["run_id"])):
        assert store.load(rid)["status"] == "awaiting_approval"
    done = root.resume(r.run_id, {r.pending["call_id"]: True}, scope=A)
    assert done.status == "done" and log == [("send", "deep")]
    for store, rid in ((mstore, r.pending["via"][0]["run_id"]), (lstore, r.pending["via"][1]["run_id"])):
        assert store.load(rid)["status"] == "done"


def test_depth_two_a_second_pause_deeper_down_bubbles_up_again():
    root, mid, lf, log, rstore, mstore, lstore = two_levels([plan("send", message="one"), plan("send", message="two"), DONE])
    r = root.run("q", scope=A)
    second = root.resume(r.run_id, {r.pending["call_id"]: True}, scope=A)
    assert second.status == "awaiting_approval" and log == [("send", "one")]
    assert second.pending["arguments"] == {"message": "two"} and second.pending["call_id"] != r.pending["call_id"]
    assert [v["agent"] for v in second.pending["via"]] == ["mid", "leaf"]
    with pytest.raises(ResumeError):
        root.resume(r.run_id, {r.pending["call_id"]: True}, scope=A)                # the old decision is not reusable
    done = root.resume(r.run_id, {second.pending["call_id"]: True}, scope=A)
    assert done.status == "done" and log == [("send", "one"), ("send", "two")]


def test_depth_two_reject_lists_every_level_and_ends_every_run_rejected():
    lf_agent, log, lstore = leaf([plan("note", message="n"), plan("send", message="s")], note=OPEN, send=ASK)
    mstore, rstore = InMemoryStateStore(), InMemoryStateStore()
    mid = delegating([plan("delegate", agent="leaf", task="go"), DONE], {"leaf": lf_agent}, max_depth=2, store=mstore)
    root = delegating([plan("delegate", agent="mid", task="go"), DONE], {"mid": mid}, max_depth=2, store=rstore)
    r = root.run("q", scope=A)
    done = root.resume(r.run_id, {r.pending["call_id"]: False}, scope=A)
    assert done.status == "rejected" and log == [("note", "n")]
    assert [(e["agent"], e["tool"]) for e in done.steps[0]["executed_before_pause"]] == [("leaf", "note")]
    for store, rid in ((mstore, r.pending["via"][0]["run_id"]), (lstore, r.pending["via"][1]["run_id"]), (rstore, r.run_id)):
        assert store.load(rid)["status"] == "rejected"


def test_a_child_tool_forced_by_taint_bubbles_with_forced_true_and_taints_the_parent():
    child, log, _ = leaf([plan("fetch", message="u"), plan("send", message="m"), DONE], fetch=READ, send=OPEN)
    parent = delegating([plan("delegate", agent="actor", task="t"), DONE], {"actor": child})
    r = parent.run("q", scope=A)
    assert r.status == "awaiting_approval" and r.pending["forced"] is True and r.pending["tool"] == "send"
    done = parent.resume(r.run_id, {r.pending["call_id"]: True}, scope=A)
    assert done.status == "done" and done.tainted is True and log == [("fetch", "u"), ("send", "m")]


def test_budgets_hold_across_a_pause_and_resume():
    parent, child, log, pstore, _ = one_level(budget=Budget(max_delegations=1, max_child_steps=1))
    r = parent.run("q", scope=A)
    done = parent.resume(r.run_id, {r.pending["call_id"]: True}, scope=A)
    assert done.status == "done" and pstore.load(r.run_id)["delegation"] == {"delegations": 1, "child_steps": 1}
    again = delegating([plan("delegate", agent="actor", task="a"), plan("delegate", agent="actor", task="b"), DONE],
                       {"actor": leaf([plan("send", message="x"), DONE, plan("send", message="y")])[0]},
                       budget=Budget(max_delegations=1, max_child_steps=5))
    r2 = again.run("q", scope=A)
    out = again.resume(r2.run_id, {r2.pending["call_id"]: True}, scope=A)
    assert out.status == "done" and "budget used up" in out.steps[1]["observation"]


def test_two_threads_that_both_validated_the_tree_run_the_action_once(tmp_path):
    class Gate:
        """Holds both threads at load() until each has loaded, so both validate the paused tree before either claims it."""
        def __init__(self, inner):
            self.inner, self.armed = inner, False
            self.barrier = threading.Barrier(2, timeout=5)
            self.save, self.save_if_status = inner.save, inner.save_if_status

        def load(self, run_id):
            st = self.inner.load(run_id)
            if self.armed:
                try:
                    self.barrier.wait()
                except threading.BrokenBarrierError:
                    pass
            return st

    cgate, pgate = Gate(SQLiteStateStore(tmp_path / "c.db")), Gate(SQLiteStateStore(tmp_path / "p.db"))
    child, log, _ = leaf([plan("send", message="hi"), DONE], store=cgate)
    parent = delegating([plan("delegate", agent="actor", task="t"), DONE], {"actor": child}, store=pgate)
    r = parent.run("q", scope=A)
    cgate.armed = pgate.armed = True
    outcomes = []

    def go():
        try:
            outcomes.append(parent.resume(r.run_id, {r.pending["call_id"]: True}, scope=A).status)
        except ResumeError as e:
            outcomes.append(str(e))

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(outcomes) == ["done", "run is not awaiting approval"] and log == [("send", "hi")]


def test_without_nested_approval_a_childs_pause_still_stops_the_child_as_in_0_5_0():
    parent, child, log, pstore, cstore = one_level(nested=False)
    r = parent.run("q", scope=A)
    assert r.status == "done" and log == [] and "approval_not_available" in r.steps[0]["observation"]
    assert [json.loads(t)["status"] for t in cstore._data.values()] == ["stopped"]      # the child run was closed, never resumable


def test_the_delegate_tool_knows_its_children_and_validates_the_flag():
    child, _, _ = leaf([DONE])
    tool = make_delegate_tool({"actor": child}, allow_outbound=True, nested_approval=True)
    assert isinstance(tool, DelegateTool) and tool.children == {"actor": child} and tool.nested_approval is True
    assert make_delegate_tool({"actor": child}, allow_outbound=True).nested_approval is False
    for bad in ("yes", 1, None):
        with pytest.raises(ValueError):
            make_delegate_tool({"actor": child}, allow_outbound=True, nested_approval=bad)


class TestTenants:
    def test_another_tenant_cannot_decide_a_nested_pause(self):
        parent, child, log, pstore, cstore = one_level()
        r = parent.run("q", scope=A)
        for other in (B, Scope("acme", "alice"), None):
            with pytest.raises(ResumeError) as e:
                parent.resume(r.run_id, {r.pending["call_id"]: True}, scope=other)
            assert str(e.value) == "unknown run"
        assert log == [] and cstore.load(r.pending["via"][0]["run_id"])["scope"] == "tenant=acme"
        assert parent.resume(r.run_id, {r.pending["call_id"]: True}, scope=A).status == "done"

    def test_a_pending_that_points_at_another_tenants_child_run_is_refused_and_that_run_is_untouched(self):
        shared_child_store = InMemoryStateStore()
        child, log, _ = leaf([plan("send", message="a1"), plan("send", message="b1"), DONE, DONE], store=shared_child_store)
        parent = delegating([plan("delegate", agent="actor", task="t"), plan("delegate", agent="actor", task="t"), DONE, DONE],
                            {"actor": child})
        ra, rb = parent.run("q", scope=A), parent.run("q", scope=B)
        victim = rb.pending["via"][0]["run_id"]
        st = parent.store.load(ra.run_id)
        stolen = shared_child_store.load(victim)["pending"]
        # A's run claims B's paused child AND copies B's pending exactly: only the scope check can still refuse it
        st["pending"] = {**st["pending"], "via": [{"agent": "actor", "run_id": victim}], **{k: stolen[k] for k in ("call_id", "tool", "arguments", "sig", "forced")}}
        parent.store.save(ra.run_id, st)
        with pytest.raises(ResumeError, match="does not match"):
            parent.resume(ra.run_id, {stolen["call_id"]: True}, scope=A)
        assert log == [] and shared_child_store.load(victim)["status"] == "awaiting_approval"
        assert parent.resume(rb.run_id, {rb.pending["call_id"]: True}, scope=B).status == "done"     # B is unaffected

    def test_every_run_in_the_tree_carries_the_callers_scope(self):
        root, mid, lf, log, rstore, mstore, lstore = two_levels([plan("send", message="m"), DONE])
        sc = Scope("acme", "bob", "bot", "s1")
        r = root.run("q", scope=sc)
        for store, rid in ((rstore, r.run_id), (mstore, r.pending["via"][0]["run_id"]), (lstore, r.pending["via"][1]["run_id"])):
            assert store.load(rid)["scope"] == sc.key
        with pytest.raises(ResumeError, match="unknown run"):
            root.resume(r.run_id, {r.pending["call_id"]: True}, scope=Scope("acme", "bob"))
        assert root.resume(r.run_id, {r.pending["call_id"]: True}, scope=sc).status == "done"


def test_audit_records_the_nested_pause_and_decision_without_any_text(tmp_path):
    log_a = AuditLog(tmp_path / "audit.jsonl")
    child, log, _ = leaf([plan("send", message="top-secret-message"), DONE])
    parent = delegating([plan("delegate", agent="actor", task="t"), DONE], {"actor": child})
    parent.audit = log_a
    r = parent.run("q", scope=A)
    parent.resume(r.run_id, {r.pending["call_id"]: True}, scope=A)
    raw = open(log_a.path).read()
    assert "top-secret-message" not in raw
    events = [json.loads(l) for l in raw.splitlines()]
    req = [e for e in events if e["event"] == "approval.requested"]
    dec = [e for e in events if e["event"] == "approval.decided"]
    assert req and req[0]["data"]["via_depth"] == 1 and dec and dec[0]["data"]["via_depth"] == 1
    assert log_a.verify().ok


def test_the_resumed_action_runs_with_the_right_depth_scope_taint_and_run_id():
    seen = []

    def send(message):
        ctx = current_run()
        seen.append((ctx.depth, ctx.scope.key if ctx.scope else None, ctx.tainted, ctx.run_id))
        return ToolResult(True, "ok")

    tool = Tool("send", "send", {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}, send)
    lf = Agent(Script([plan("send", message="m"), DONE]), [tool], Policy(tools={"send": ASK}), InMemoryStateStore(), summarize=False)
    mid = delegating([plan("delegate", agent="leaf", task="go"), DONE], {"leaf": lf}, max_depth=2)
    root = delegating([plan("delegate", agent="mid", task="go"), DONE], {"mid": mid}, max_depth=2)
    sc = Scope("acme", "bob")
    r = root.run("q", scope=sc)
    assert seen == []
    assert root.resume(r.run_id, {r.pending["call_id"]: True}, scope=sc).status == "done"
    assert seen == [(2, sc.key, False, r.pending["via"][1]["run_id"])]


class PlainStore:
    """A minimal custom store: save and load only, no atomic claim."""
    def __init__(self):
        self.data = {}

    def save(self, run_id, state):
        self.data[run_id] = json.dumps(state)

    def load(self, run_id):
        return json.loads(self.data[run_id]) if run_id in self.data else None


def test_a_child_run_that_is_not_awaiting_is_refused_even_on_a_store_without_an_atomic_claim():
    cstore = PlainStore()
    child, log, _ = leaf([plan("send", message="hi"), DONE], store=cstore)
    parent = delegating([plan("delegate", agent="actor", task="t"), DONE], {"actor": child})
    r = parent.run("q", scope=A)
    crun = r.pending["via"][0]["run_id"]
    cst = cstore.load(crun)
    cstore.save(crun, {**cst, "status": "running"})                              # the child was already taken
    with pytest.raises(ResumeError, match="does not match"):
        parent.resume(r.run_id, {r.pending["call_id"]: True}, scope=A)
    assert log == []
    cstore.save(crun, cst)
    assert parent.resume(r.run_id, {r.pending["call_id"]: True}, scope=A).status == "done"      # positive control


def test_a_middle_childs_own_via_must_match_the_rest_of_the_path():
    root, mid, lf, log, rstore, mstore, lstore = two_levels([plan("send", message="deep"), DONE])
    r = root.run("q", scope=A)
    mrun = r.pending["via"][0]["run_id"]
    mst = mstore.load(mrun)
    mstore.save(mrun, {**mst, "pending": {**mst["pending"], "via": [{"agent": "leaf", "run_id": "someone-else"}]}})
    with pytest.raises(ResumeError, match="does not match"):
        root.resume(r.run_id, {r.pending["call_id"]: True}, scope=A)
    assert log == []
    mstore.save(mrun, mst)
    assert root.resume(r.run_id, {r.pending["call_id"]: True}, scope=A).status == "done"


def test_the_deepest_child_must_not_claim_to_have_a_child_of_its_own():
    parent, child, log, pstore, cstore = one_level()
    r = parent.run("q", scope=A)
    crun = r.pending["via"][0]["run_id"]
    cst = cstore.load(crun)
    cstore.save(crun, {**cst, "pending": {**cst["pending"], "via": [{"agent": "actor", "run_id": "x"}]}})
    with pytest.raises(ResumeError, match="does not match"):
        parent.resume(r.run_id, {r.pending["call_id"]: True}, scope=A)
    assert log == []


def test_a_child_paused_outside_the_tree_cannot_be_decided_through_a_parent_that_has_nested_approval_off():
    child, log, cstore = leaf([plan("send", message="loose"), DONE, plan("send", message="in-tree"), DONE])
    plain = Tool("plain", "plain", {"type": "object", "properties": {}}, lambda: ToolResult(True, "x"))
    tool = make_delegate_tool({"actor": child}, allow_outbound=True, nested_approval=False)
    pstore = InMemoryStateStore()
    pol = Policy(tools={"delegate": delegate_policy({"actor": child}, allow_outbound=True), "plain": ToolPolicy(taints=False, outbound=False)}, max_steps=6)
    parent = Agent(Script([plan("delegate", agent="actor", task="t"), DONE]), [tool, plain], pol, pstore, summarize=False)
    loose = child.run("x", scope=A)                                              # a paused child run that is not part of any tree
    r = parent.run("q", scope=A)                                                 # nested off: this child is stopped, parent done
    base = pstore.load(r.run_id)
    for tool_name in ("delegate", "plain", "ghost"):
        fake = {**base, "status": "awaiting_approval", "pending": {
            **{k: loose.pending[k] for k in ("call_id", "tool", "arguments", "sig", "forced")},
            "via": [{"agent": "actor", "run_id": loose.run_id}],
            "delegate": {"tool": tool_name, "arguments": {"agent": "actor", "task": "t"}, "sig": ["delegate", "x"], "counted": 0}}}
        pstore.save(r.run_id, fake)
        with pytest.raises(ResumeError, match="does not match"):
            parent.resume(r.run_id, {loose.pending["call_id"]: True}, scope=A)
    assert log == [] and cstore.load(loose.run_id)["status"] == "awaiting_approval"


def test_a_delegate_call_that_itself_needed_approval_can_pause_again_when_its_child_pauses():
    child, log, _ = leaf([plan("send", message="hi"), DONE])
    tool = make_delegate_tool({"actor": child}, allow_outbound=True, nested_approval=True)
    pol = Policy(tools={"delegate": ToolPolicy(taints=False, outbound=False, requires_approval=True)}, max_steps=6)
    parent = Agent(Script([plan("delegate", agent="actor", task="t"), DONE]), [tool], pol, InMemoryStateStore(), summarize=False)
    first = parent.run("q", scope=A)
    assert first.status == "awaiting_approval" and first.pending["tool"] == "delegate" and not first.pending.get("via")
    second = parent.resume(first.run_id, {first.pending["call_id"]: True}, scope=A)
    assert second.status == "awaiting_approval" and second.pending["tool"] == "send" and len(second.pending["via"]) == 1 and log == []
    done = parent.resume(first.run_id, {second.pending["call_id"]: True}, scope=A)
    assert done.status == "done" and log == [("send", "hi")]
