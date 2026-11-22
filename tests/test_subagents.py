"""Sub-agents: the switch and limits, the delegate tool, children as run records, taint in both directions, the approval
hold and the resume of the parent, the token, time and capacity caps, and cancel. Real test database, scripted model."""
import json
import pathlib
import threading

import pytest

from core import agent_loop, audit, subagents
from core.employees._db import get_connection

ROOT = pathlib.Path(__file__).resolve().parent.parent
Q = "TEST-SUB: parent request"
TASK = "TEST-SUB task: count the open tickets"
URL = "https://docs.example.com/x"
DONE = json.dumps({"tool": "done"})


def plan(tool, target="", content="x"):
    return json.dumps({"tool": tool, "target": target, "content": content})


class FakeService:
    primary_config = {"provider": "fake"}

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def _call_provider(self, cfg, prompt, temperature, max_tokens):
        self.prompts.append(prompt)
        if not self.replies:
            raise RuntimeError("no scripted reply left")
        return self.replies.pop(0), {}


def sql(statement, params=None, fetch=False):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(statement, params)
        out = cur.fetchall() if fetch else None
        conn.commit()
        cur.close()
        return out
    finally:
        conn.close()


def rows(where="query LIKE 'TEST-SUB%'"):
    return [agent_loop.get_run(str(r[0])) for r in sql(f"SELECT id FROM agent_runs WHERE {where} ORDER BY created_at, depth", fetch=True)]


def parent_and_children():
    all_ = rows()
    return next(r for r in all_ if r["depth"] == 0), [r for r in all_ if r["depth"] > 0]


@pytest.fixture
def world(monkeypatch):
    import core.autonomy as au
    import core.employees.sensitivity as sens
    for k, v in (("AGENT_LOOP_ENABLED", "true"), ("CODE_EXEC_ENABLED", "true"), ("SANDBOX_TOKEN", "t"), ("SUBAGENTS_ENABLED", "true"),
                 ("BROWSER_FETCH_ENABLED", "true"), ("BROWSER_ALLOWED_DOMAINS", "docs.example.com"),
                 ("WEBHOOK_TARGETS", "ops=https://hooks.example.com/x")):
        monkeypatch.setenv(k, v)
    for k in ("AGENT_LOOP_MAX_STEPS", "SHELL_EXEC_ENABLED", "MCP_SERVERS", "MCP_ALLOWED_TOOLS", "SLACK_WEBHOOK_URL",
              "EMAIL_ALLOWED_RECIPIENTS", "ACTION_TASKS_ENABLED", "SUBAGENT_MAX_CHILDREN", "SUBAGENT_TREE_TOKENS",
              "SUBAGENT_CHILD_TOKENS", "SUBAGENT_SECONDS", "SUBAGENT_MAX_PARALLEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(agent_loop.budget, "check_budget", lambda role=None: None)
    monkeypatch.setattr(subagents, "_active_roles", lambda: ["support", "sales"])
    monkeypatch.setattr(subagents, "_sem", None)
    st = {"mode": "full", "sent": [], "audit": [], "spawned": 0,
          "results": {"code": "Code run: exit 0\nstdout:\n42\n", "webhook": "Webhook 'ops': sent",
                      "fetch": "Page fetch: https://docs.example.com/x (HTTP 200)\nWelcome"}}
    monkeypatch.setattr(au, "get_autonomy_settings", lambda: {"mode": st["mode"], "channels": [], "actions": [],
                                                              "approval_channel": "telegram", "approval_target": "+10000000000"})
    monkeypatch.setattr(au, "_send_via_channel", lambda ch, t, c: (st["sent"].append((ch, t, c)) or st["results"][ch]))
    monkeypatch.setattr(au, "log_autonomous_action", lambda *a, **k: None)
    monkeypatch.setattr(au, "request_approval", lambda *a, **k: True)
    monkeypatch.setattr(au, "resolve_pending", lambda *a, **k: "ok")
    monkeypatch.setattr(au.employee_learning, "learn_from_owner_approval", lambda *a, **k: None)
    monkeypatch.setattr(sens, "is_sensitive_role", lambda r: False)
    monkeypatch.setattr(audit, "emit", lambda ev, content=None, **f: st["audit"].append((ev, f)))

    def spawn(fn):
        st["spawned"] += 1
        fn()
    monkeypatch.setattr(agent_loop, "_spawn", spawn)
    yield st
    sql("DELETE FROM agent_runs WHERE query LIKE 'TEST-SUB%'")
    sql("DELETE FROM autonomy_pending WHERE action_type IN ('run_code', 'fetch_page', 'send_webhook')")


def basic_replies():
    return [plan("delegate", "support", TASK), plan("run_code", content="print(42)"), DONE, "Child summary: 42 tickets.", DONE, "Parent summary."]


# ---------------- off by default, the tool ----------------

def test_without_the_switch_the_tool_is_not_offered_and_a_delegate_plan_is_refused(world, monkeypatch):
    monkeypatch.delenv("SUBAGENTS_ENABLED")
    svc = FakeService([plan("delegate", "support", TASK)])
    assert agent_loop.run(Q, "d", svc, None) is None
    assert "- delegate:" not in svc.prompts[0] and rows() == []


def test_the_tool_lists_roles_and_limits_and_is_offered_only_to_top_level_runs(world, monkeypatch):
    base = {"run_code": "x"}
    out = subagents.with_delegate(base, {"depth": 0, "id": None})
    assert "delegate" not in base and "support" in out["delegate"] and "sales" in out["delegate"] and "At most 3" in out["delegate"]
    assert "delegate" not in subagents.with_delegate(base, {"depth": 1})
    monkeypatch.setattr(subagents, "_active_roles", lambda: [])
    assert "delegate" not in subagents.with_delegate(base, {"depth": 0})
    monkeypatch.setattr(subagents, "_active_roles", lambda: ["support"])
    monkeypatch.setenv("SUBAGENTS_ENABLED", "false")
    assert subagents.with_delegate(base, {"depth": 0}) is base


def test_the_settings_are_clamped(monkeypatch):
    for name, fn, default, lo, hi in (("SUBAGENT_MAX_CHILDREN", subagents.max_children, 3, 1, 5),
                                      ("SUBAGENT_MAX_PARALLEL", subagents.max_parallel, 2, 1, 4),
                                      ("SUBAGENT_TREE_TOKENS", subagents.tree_tokens, 30000, 1000, 200000),
                                      ("SUBAGENT_CHILD_TOKENS", subagents.child_tokens, 8000, 500, 100000),
                                      ("SUBAGENT_SECONDS", subagents.child_seconds, 120, 10, 600)):
        monkeypatch.delenv(name, raising=False)
        assert fn() == default
        monkeypatch.setenv(name, "-5")
        assert fn() == lo
        monkeypatch.setenv(name, "99999999")
        assert fn() == hi
        monkeypatch.setenv(name, "many")
        assert fn() == default
    monkeypatch.delenv("SUBAGENTS_ENABLED", raising=False)
    assert subagents.enabled() is False


# ---------------- a delegation ----------------

def test_a_delegation_makes_a_child_run_and_returns_its_result_to_the_parent(world):
    svc = FakeService(basic_replies())
    agent_loop.run(Q, "d", svc, None)
    parent, children = parent_and_children()
    assert len(children) == 1
    child = children[0]
    assert parent["status"] == "done" and child["status"] == "done" and child["role"] == "support"
    assert child["parent_id"] == parent["id"] and child["depth"] == 1 and child["query"] == TASK
    step = parent["steps"][0]
    assert step["tool"] == "delegate" and step["child_run_id"] == child["id"] and step["status"] == "executed"
    assert step["observation"] == "Sub-agent support finished (done): Child summary: 42 tickets."
    assert world["sent"] == [("code", "", "print(42)")]
    assert parent["tokens_used"] > 0 and child["tokens_used"] > 0
    assert "- delegate:" in svc.prompts[0] and "- delegate:" not in svc.prompts[1]           # the child cannot delegate
    assert "Child summary: 42 tickets." in svc.prompts[4]                                     # the parent sees the result
    assert [e for e, f in world["audit"] if e.startswith("subagent")] == ["subagent_authorized", "subagent_started", "subagent_finished"]
    t = agent_loop.tree(parent["id"])
    assert [c["id"] for c in t["children"]] == [child["id"]] and t["children"][0]["depth"] == 1


def test_a_child_that_needs_no_tool_answers_the_task_itself(world):
    answer = "A pending approval is an action that waits for a person to say yes or no."
    svc = FakeService([plan("delegate", "support", TASK), DONE, answer, DONE, "Parent summary."])
    agent_loop.run(Q, "d", svc, None)
    parent, children = parent_and_children()
    child = children[0]
    assert child["status"] == "done" and child["steps"] == [] and child["summary"] == answer
    assert parent["steps"][0]["observation"] == "Sub-agent support finished (done): " + answer
    assert answer in svc.prompts[3]                       # the parent's next planning step sees the child's answer
    assert child["tokens_used"] > 0


def test_a_child_that_tries_to_delegate_is_refused_and_no_grandchild_exists(world):
    svc = FakeService([plan("delegate", "support", TASK), plan("delegate", "sales", "TEST-SUB deeper"), DONE, "ok"])
    agent_loop.run(Q, "d", svc, None)
    parent, children = parent_and_children()
    assert [c["depth"] for c in children] == [1] and parent["status"] == "done"


@pytest.mark.parametrize("target,content", [("nobody", TASK), ("support", ""), ("support", "x" * 601), ("", TASK)])
def test_bad_delegations_create_no_child(world, target, content):
    assert agent_loop.run(Q, "d", FakeService([plan("delegate", target, content)]), None) is None
    assert rows() == []


def test_the_number_of_children_is_limited(world, monkeypatch):
    monkeypatch.setenv("SUBAGENT_MAX_CHILDREN", "1")
    svc = FakeService([plan("delegate", "support", TASK), plan("run_code", content="print(1)"), DONE, "Child one.",
                       plan("delegate", "sales", "TEST-SUB task two")])
    agent_loop.run(Q, "d", svc, None)
    parent, children = parent_and_children()
    assert len(children) == 1 and "limit of 1 sub-agents" in parent["summary"]


def test_the_same_delegation_twice_is_refused(world):
    svc = FakeService([plan("delegate", "support", TASK), DONE, "Child answer.", plan("delegate", "support", TASK)])
    agent_loop.run(Q, "d", svc, None)
    parent, children = parent_and_children()
    assert len(children) == 1 and "same action was proposed twice" in parent["summary"]


# ---------------- taint ----------------

def test_a_child_of_a_tainted_parent_is_tainted_and_its_outbound_action_needs_approval(world):
    svc = FakeService([plan("delegate", "support", TASK), plan("send_webhook", "ops", "hello"), "Parent summary."])
    out = agent_loop.run(Q, "d", svc, None, True)
    parent, children = parent_and_children()
    assert children[0]["tainted"] is True and children[0]["status"] == "waiting_approval" and world["sent"] == []
    assert parent["status"] == "waiting_approval" and out["status"] == "pending_approval"


def test_a_parent_becomes_tainted_when_a_child_read_outside_content(world):
    svc = FakeService([plan("delegate", "support", TASK), plan("fetch_page", URL, "read"), DONE, "Child read it.", DONE, "Parent summary."])
    agent_loop.run(Q, "d", svc, None)
    parent, children = parent_and_children()
    assert children[0]["tainted"] is True and parent["tainted"] is True


def test_a_parent_stays_untainted_when_the_child_read_nothing_outside(world):
    agent_loop.run(Q, "d", FakeService(basic_replies()), None)
    parent, children = parent_and_children()
    assert parent["tainted"] is False and children[0]["tainted"] is False


# ---------------- approval hold and resume ----------------

def held(world, monkeypatch, tail):
    world["mode"] = "semi"
    first = FakeService([plan("delegate", "support", TASK), plan("send_webhook", "ops", "hello")])
    out = agent_loop.run(Q, "d", first, None)
    second = FakeService(tail)
    monkeypatch.setattr(agent_loop, "_service", lambda: second)
    return out, second


def test_a_child_needing_approval_puts_the_parent_on_hold(world, monkeypatch):
    out, _ = held(world, monkeypatch, [])
    parent, children = parent_and_children()
    child = children[0]
    assert child["status"] == "waiting_approval" and child["pending_action_id"]
    assert parent["status"] == "waiting_approval" and parent["pending_action_id"] is None
    assert parent["steps"][0]["status"] == "waiting_child" and out["action_id"] == child["pending_action_id"]


def test_approving_the_child_action_finishes_the_child_and_resumes_the_parent(world, monkeypatch):
    out, second = held(world, monkeypatch, [DONE, "Child sent it.", DONE, "Parent summary."])
    agent_loop.on_resolved(out["action_id"], True, "Webhook 'ops': sent")
    parent, children = parent_and_children()
    assert children[0]["status"] == "done" and parent["status"] == "done" and world["spawned"] >= 1
    assert parent["steps"][0]["status"] == "executed"
    assert parent["steps"][0]["observation"] == "Sub-agent support finished (done): Child sent it."
    assert "Child sent it." in second.prompts[-2]


def test_rejecting_the_child_action_lets_the_parent_go_on(world, monkeypatch):
    out, second = held(world, monkeypatch, [DONE, "Parent summary."])
    agent_loop.on_resolved(out["action_id"], False, "")
    parent, children = parent_and_children()
    assert children[0]["status"] == "rejected" and parent["status"] == "done"
    assert parent["steps"][0]["status"] == "failed" and "finished (rejected)" in parent["steps"][0]["observation"]


def test_cancelling_the_parent_while_on_hold_cancels_the_child_and_nothing_resumes(world, monkeypatch):
    out, _ = held(world, monkeypatch, [DONE, "never used"])
    res = agent_loop.cancel_run(parent_and_children()[0]["id"])
    parent, children = parent_and_children()
    assert len(res["cancelled"]) == 2 and parent["status"] == "cancelled" and children[0]["status"] == "cancelled"
    before = world["spawned"]
    agent_loop.on_resolved(out["action_id"], True, "late approval")
    assert world["spawned"] == before and agent_loop.get_run(parent["id"])["status"] == "cancelled"


def test_on_child_ended_does_nothing_without_a_waiting_parent(world):
    subagents.on_child_ended({"id": "x", "parent_id": None})
    subagents.on_child_ended({"id": "x", "parent_id": "00000000-0000-0000-0000-000000000000"})
    assert world["spawned"] == 0


# ---------------- caps ----------------

def test_a_child_over_its_own_token_cap_stops_and_the_parent_goes_on(world, monkeypatch):
    monkeypatch.setattr(subagents, "child_tokens", lambda: 1)
    svc = FakeService([plan("delegate", "support", TASK), plan("run_code", content="print(1)"), DONE, "Parent summary."])
    agent_loop.run(Q, "d", svc, None)
    parent, children = parent_and_children()
    assert children[0]["status"] == "budget" and "finished (budget)" in parent["steps"][0]["observation"]
    assert parent["status"] == "done" and len(world["sent"]) == 1


def test_the_tree_cap_stops_the_whole_tree(world, monkeypatch):
    monkeypatch.setattr(subagents, "tree_tokens", lambda: 1)
    agent_loop.run(Q, "d", FakeService([plan("delegate", "support", TASK)]), None)
    parent, children = parent_and_children()
    assert children[0]["status"] == "budget" and parent["status"] == "budget" and world["sent"] == []
    assert "token cap" in parent["summary"]


def test_a_child_past_its_time_limit_stops_before_working(world, monkeypatch):
    monkeypatch.setattr(subagents, "child_seconds", lambda: -5)
    agent_loop.run(Q, "d", FakeService([plan("delegate", "support", TASK), DONE, "Parent summary."]), None)
    parent, children = parent_and_children()
    assert children[0]["status"] == "failed" and "time limit" in children[0]["summary"] and parent["status"] == "done"


def test_when_there_is_no_capacity_no_child_is_started(world, monkeypatch):
    monkeypatch.setattr(subagents, "CAPACITY_WAIT_SECONDS", 0.05)
    full = threading.BoundedSemaphore(1)
    full.acquire()
    monkeypatch.setattr(subagents, "_sem", full)
    agent_loop.run(Q, "d", FakeService([plan("delegate", "support", TASK), DONE, "Parent summary."]), None)
    parent, children = parent_and_children()
    assert children == [] and parent["steps"][0]["status"] == "failed" and "capacity is busy" in parent["steps"][0]["observation"]
    assert parent["status"] == "done"


def test_the_capacity_is_given_back_after_every_child(world):
    agent_loop.run(Q, "d", FakeService(basic_replies()), None)
    sem = subagents._semaphore()
    assert [sem.acquire(blocking=False) for _ in range(2)] == [True, True] and sem.acquire(blocking=False) is False


# ---------------- docs ----------------

def test_the_docs_page_describes_the_switches_and_the_limits():
    text = (ROOT / "docs" / "sub-agents.md").read_text(encoding="utf-8")
    for word in ("SUBAGENTS_ENABLED", "SUBAGENT_MAX_CHILDREN", "SUBAGENT_MAX_PARALLEL", "SUBAGENT_TREE_TOKENS",
                 "SUBAGENT_CHILD_TOKENS", "SUBAGENT_SECONDS", "/agent-runs/{id}/tree", "tainted"):
        assert word in text
    assert "{{" not in text and "{%" not in text


# ---------------- the gate decides whether a child may start, and what it inherits (Phase 0, P0-H) ----------------

def sensitive(monkeypatch, *names):
    import core.employees.sensitivity as sens
    monkeypatch.setattr(sens, "is_sensitive_role", lambda r: r in names)


def test_a_child_of_a_sensitive_parent_needs_approval_for_every_action_even_in_full_mode(world, monkeypatch):
    sensitive(monkeypatch, "legal_intake")
    svc = FakeService([plan("delegate", "support", TASK), plan("send_webhook", "ops", "hello"), "Parent summary."])
    out = agent_loop.run(Q, "d", svc, "legal_intake", False)
    parent, children = parent_and_children()
    assert world["sent"] == []                                         # nothing went out without the owner
    assert children[0]["status"] == "waiting_approval" and parent["status"] == "waiting_approval"
    assert children[0]["steps"][0]["forced_approval"] is True and out["status"] == "pending_approval"


def test_the_same_child_runs_in_full_mode_when_the_parent_is_not_sensitive(world, monkeypatch):
    sensitive(monkeypatch, "legal_intake")
    svc = FakeService([plan("delegate", "support", TASK), plan("send_webhook", "ops", "hello"), DONE, "Child summary.", DONE, "Parent summary."])
    agent_loop.run(Q, "d", svc, "sales", False)
    assert len(world["sent"]) == 1                                     # the rule is about the parent, not about every child


def test_a_parent_that_cannot_be_read_counts_as_sensitive(world, monkeypatch):
    from core import autonomy
    assert autonomy.inherited_force_semi({"parent_id": "00000000-0000-0000-0000-000000000000", "role": "support"}) is True

    def down(i):
        raise RuntimeError("database down")
    monkeypatch.setattr(agent_loop, "get_run", down)
    assert autonomy.inherited_force_semi({"parent_id": "x", "role": "support"}) is True


def test_a_run_without_a_parent_does_not_read_the_database_for_inheritance(world, monkeypatch):
    from core import autonomy
    called = []
    monkeypatch.setattr(agent_loop, "get_run", lambda i: called.append(i))
    assert autonomy.inherited_force_semi({"role": "support"}) is False and called == []


def test_the_gate_authorizes_a_spawn_and_audits_it(world):
    from core import autonomy
    d = autonomy.authorize_spawn({"id": None, "depth": 0, "role": "sales"}, "support")
    assert d == {"allowed": True, "reason": "ok", "force_approval": False}
    assert "subagent_authorized" in [e for e, _ in world["audit"]]


def test_the_gate_refuses_a_spawn_when_the_switch_is_off_or_the_limits_are_reached(world, monkeypatch):
    from core import autonomy
    monkeypatch.setenv("SUBAGENTS_ENABLED", "false")
    assert autonomy.authorize_spawn({"id": None, "depth": 0}, "support")["reason"] == "sub_agents_off"
    monkeypatch.setenv("SUBAGENTS_ENABLED", "true")
    assert autonomy.authorize_spawn({"id": None, "depth": subagents.MAX_DEPTH}, "support")["reason"] == "depth_limit"
    monkeypatch.setattr(subagents, "_count_children", lambda i: subagents.max_children())
    d = autonomy.authorize_spawn({"id": "11111111-1111-1111-1111-111111111111", "depth": 0}, "support")
    assert d["allowed"] is False and d["reason"] == "child_limit"
    assert [e for e, _ in world["audit"]].count("subagent_denied") == 3


def test_the_gate_fails_closed_when_it_cannot_decide(world, monkeypatch):
    from core import autonomy

    def down(i):
        raise RuntimeError("database down")
    monkeypatch.setattr(subagents, "_count_children", down)
    d = autonomy.authorize_spawn({"id": "11111111-1111-1111-1111-111111111111", "depth": 0}, "support")
    assert d["allowed"] is False and d["reason"] == "check_failed" and d["force_approval"] is True


def test_the_authorization_says_when_the_child_will_be_forced_to_ask(world, monkeypatch):
    from core import autonomy
    sensitive(monkeypatch, "legal_intake")
    assert autonomy.authorize_spawn({"id": None, "depth": 0, "role": "legal_intake"}, "support")["force_approval"] is True
    assert autonomy.authorize_spawn({"id": None, "depth": 0, "role": "sales"}, "support")["force_approval"] is False


def test_a_delegation_the_gate_refuses_creates_no_child(world, monkeypatch):
    from core import autonomy
    monkeypatch.setattr(autonomy, "authorize_spawn", lambda parent, target: {"allowed": False, "reason": "check_failed", "force_approval": True})
    assert agent_loop.run(Q, "d", FakeService([plan("delegate", "support", TASK)]), None) is None
    assert rows() == []


def test_a_child_cannot_use_a_tool_its_parent_does_not_have(world, monkeypatch):
    monkeypatch.setattr(subagents, "parent_tool_names", lambda parent: {"run_code"})
    svc = FakeService([plan("delegate", "support", TASK), plan("send_webhook", "ops", "hello"), "Child summary.", DONE, "Parent summary."])
    agent_loop.run(Q, "d", svc, None, False)
    parent, children = parent_and_children()
    assert world["sent"] == [] and children[0]["steps"] == []          # refused before anything was proposed to the gate
    assert children[0]["status"] == "done"


def test_the_tool_limit_for_a_child_is_the_intersection_and_fails_closed(world, monkeypatch):
    monkeypatch.setattr(agent_loop, "get_run", lambda i: {"id": i, "role": "sales"})
    monkeypatch.setattr(subagents, "parent_tool_names", lambda p: {"a"})
    assert subagents.limit_to_parent({"a": "1", "b": "2"}, {"parent_id": "p"}) == {"a": "1"}
    assert subagents.limit_to_parent({"a": "1", "b": "2"}, {}) == {"a": "1", "b": "2"}     # a top-level run is unchanged
    monkeypatch.setattr(agent_loop, "get_run", lambda i: None)
    assert subagents.limit_to_parent({"a": "1"}, {"parent_id": "p"}) == {}
    monkeypatch.setattr(agent_loop, "get_run", lambda i: (_ for _ in ()).throw(RuntimeError("down")))
    assert subagents.limit_to_parent({"a": "1"}, {"parent_id": "p"}) == {}


def test_a_child_is_started_only_after_the_gate_agreed_and_is_driven_with_the_inherited_limits():
    import inspect
    ds = inspect.getsource(subagents.delegate_step)
    assert ds.index("authorize_spawn(") < ds.index("_drive(")
    drive = inspect.getsource(agent_loop._drive)
    assert "inherited_force_semi(run)" in drive and "limit_to_parent(" in drive


def test_the_docs_page_describes_who_may_start_a_child():
    text = (ROOT / "docs" / "sub-agents.md").read_text(encoding="utf-8")
    for word in ("authorize_spawn", "sensitive", "sub_agents_off", "depth_limit", "child_limit", "check_failed"):
        assert word in text
    assert "{{" not in text and "{%" not in text


def test_a_loop_or_a_very_long_chain_of_parents_counts_as_sensitive(world, monkeypatch):
    from core import autonomy
    monkeypatch.setattr(agent_loop, "get_run", lambda i: {"id": i, "role": "support", "parent_id": "b" if i == "a" else "a"})
    assert autonomy.inherited_force_semi({"parent_id": "a", "role": "support"}) is True            # a -> b -> a
    monkeypatch.setattr(agent_loop, "get_run", lambda i: {"id": i, "role": "support", "parent_id": i + "x"})
    assert autonomy.inherited_force_semi({"parent_id": "p", "role": "support"}) is True            # longer than any real tree
    monkeypatch.setattr(agent_loop, "get_run", lambda i: {"id": i, "role": "support", "parent_id": None})
    assert autonomy.inherited_force_semi({"parent_id": "p", "role": "support"}) is False           # a normal parent
