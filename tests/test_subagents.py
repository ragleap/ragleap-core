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
    for k, v in (("AGENT_LOOP_ENABLED", "true"), ("CODE_EXEC_ENABLED", "true"), ("SANDBOX_TOKEN", "t"), ("SUBAGENTS_ENABLED", "true"), ("ADDON_ENCRYPTION_KEY", "test-key-for-spawn-records"),
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
    monkeypatch.setattr(agent_loop, "get_start_record", lambda i: {"forced": False})       # the record alone would say: not forced
    assert autonomy.inherited_force_semi({"id": "c", "parent_id": "00000000-0000-0000-0000-000000000000", "role": "support"}) is True

    def down(i):
        raise RuntimeError("database down")
    monkeypatch.setattr(agent_loop, "get_run", down)
    assert autonomy.inherited_force_semi({"id": "c", "parent_id": "x", "role": "support"}) is True


def test_a_run_without_a_parent_does_not_read_the_database_for_inheritance(world, monkeypatch):
    from core import autonomy
    called = []
    monkeypatch.setattr(agent_loop, "get_run", lambda i: called.append(i))
    assert autonomy.inherited_force_semi({"role": "support"}) is False and called == []


def test_the_gate_authorizes_a_spawn_and_audits_it(world):
    from core import autonomy
    d = autonomy.authorize_spawn({"id": None, "depth": 0, "role": "sales"}, "support")
    assert d["allowed"] is True and d["reason"] == "ok" and d["force_approval"] is False and d["spawn_id"]
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
    monkeypatch.setattr(agent_loop, "get_start_record", lambda i: {"forced": False})
    monkeypatch.setattr(agent_loop, "get_run", lambda i: {"id": i, "role": "support", "parent_id": "b" if i == "a" else "a"})
    assert autonomy.inherited_force_semi({"id": "c", "parent_id": "a", "role": "support"}) is True            # a -> b -> a
    monkeypatch.setattr(agent_loop, "get_run", lambda i: {"id": i, "role": "support", "parent_id": i + "x"})
    assert autonomy.inherited_force_semi({"id": "c", "parent_id": "p", "role": "support"}) is True            # longer than any real tree
    monkeypatch.setattr(agent_loop, "get_run", lambda i: {"id": i, "role": "support", "parent_id": None})
    assert autonomy.inherited_force_semi({"id": "c", "parent_id": "p", "role": "support"}) is False           # a normal parent


# ---------------- a signed start record, budgets drawn as the minimum, steps shared by the tree (Phase 0, P0-I) ----------------

def unrecorded_child():
    parent = {"id": None, "role": None, "query": Q, "context_answer": "", "status": "running", "steps": [], "tainted": False,
              "pending_action_id": None, "summary": "", "depth": 0, "tokens_used": 0}
    agent_loop._save(parent)
    child = dict(parent, id=None, query=TASK, parent_id=parent["id"], depth=1, role="support")
    agent_loop._save(child)
    return parent, child


def test_a_started_child_carries_a_valid_signed_start_record(world):
    from core import autonomy
    agent_loop.run(Q, "d", FakeService(basic_replies()), None)
    parent, children = parent_and_children()
    c = children[0]
    rec = agent_loop.get_start_record(c["id"])
    assert rec and rec["spawn_id"] and rec["sig"] and rec["forced"] is False and rec["key_id"] == autonomy._signing_keys()[0]
    assert (rec["run_id"], rec["parent_id"], rec["role"], rec["depth"]) == (c["id"], parent["id"], "support", 1)
    assert autonomy.verify_spawn_record(c) is True                          # still valid after every later save of the row
    ids = {e: f.get("spawn_id") for e, f in world["audit"] if e.startswith("subagent")}
    assert ids["subagent_authorized"] == ids["subagent_started"] == rec["spawn_id"]


def test_the_record_of_a_sensitive_parents_child_says_forced_and_is_valid(world, monkeypatch):
    from core import autonomy
    sensitive(monkeypatch, "legal_intake")
    agent_loop.run(Q, "d", FakeService([plan("delegate", "support", TASK), plan("send_webhook", "ops", "hello"), "Parent summary."]), "legal_intake", False)
    _, children = parent_and_children()
    assert agent_loop.get_start_record(children[0]["id"])["forced"] is True and autonomy.verify_spawn_record(children[0]) is True


def test_a_record_that_was_changed_or_made_with_another_key_does_not_verify(world, monkeypatch):
    from core import autonomy
    sid, rid, pid = "11111111-1111-1111-1111-111111111111", "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444"
    other = "55555555-5555-5555-5555-555555555555"
    rec = autonomy.spawn_record(sid, rid, pid, "support", 1, False)
    run = {"id": rid, "parent_id": pid, "role": "support", "depth": 1}

    def serve(r):
        monkeypatch.setattr(agent_loop, "get_start_record", lambda i: dict(r) if r is not None else None)
    serve(rec)
    assert autonomy.verify_spawn_record(run) is True
    for field, value in (("role", "sales"), ("depth", 2), ("parent_id", other), ("run_id", other), ("forced", True), ("spawn_id", other),
                         ("sig", rec["sig"][:-1]), ("sig", None), ("key_id", "ffffffffffff"), ("key_id", None)):
        serve(dict(rec, **{field: value}))
        assert autonomy.verify_spawn_record(run) is False, (field, value)
    serve(rec)
    for field, value in (("role", "sales"), ("depth", 2), ("parent_id", other), ("id", other)):          # the LIVE row was edited
        assert autonomy.verify_spawn_record(dict(run, **{field: value})) is False, (field, value)
    serve(None)
    assert autonomy.verify_spawn_record(run) is False
    serve(rec)
    monkeypatch.setenv("ADDON_ENCRYPTION_KEY", "another-key")
    assert autonomy.verify_spawn_record(run) is False
    monkeypatch.delenv("ADDON_ENCRYPTION_KEY")
    assert autonomy.verify_spawn_record(run) is False and autonomy.spawn_record(sid, rid, pid, "r", 1, False) is None


def test_a_child_row_without_a_valid_record_is_refused_and_does_nothing(world):
    parent, child = unrecorded_child()
    agent_loop._drive(child, FakeService([plan("send_webhook", "ops", "hi")]), "support")
    assert world["sent"] == [] and agent_loop.get_run(child["id"])["status"] == "failed"


def test_without_a_signing_key_no_sub_agent_starts(world, monkeypatch):
    from core import autonomy
    monkeypatch.delenv("ADDON_ENCRYPTION_KEY")
    d = autonomy.authorize_spawn({"id": None, "depth": 0, "role": "sales"}, "support")
    assert d["allowed"] is False and d["reason"] == "no_signing_key" and d["spawn_id"] is None
    assert agent_loop.run(Q, "d", FakeService([plan("delegate", "support", TASK)]), None) is None
    assert rows() == []


def test_every_authorization_gets_its_own_spawn_id_and_a_refusal_gets_none(world, monkeypatch):
    from core import autonomy
    a = autonomy.authorize_spawn({"id": None, "depth": 0}, "support")["spawn_id"]
    b = autonomy.authorize_spawn({"id": None, "depth": 0}, "support")["spawn_id"]
    assert a and b and a != b
    assert autonomy.authorize_spawn({"id": None, "depth": subagents.MAX_DEPTH}, "support")["spawn_id"] is None


def test_the_recorded_force_flag_cannot_be_lowered_by_the_live_chain(world, monkeypatch):
    from core import autonomy
    monkeypatch.setattr(agent_loop, "get_run", lambda i: {"id": i, "role": "support", "parent_id": None})
    monkeypatch.setattr(agent_loop, "get_start_record", lambda i: {"forced": True})
    assert autonomy.inherited_force_semi({"id": "c", "parent_id": "p"}) is True                 # recorded when the child started
    monkeypatch.setattr(agent_loop, "get_start_record", lambda i: {"forced": False})
    assert autonomy.inherited_force_semi({"id": "c", "parent_id": "p"}) is False
    monkeypatch.setattr(agent_loop, "get_start_record", lambda i: None)
    assert autonomy.inherited_force_semi({"id": "c", "parent_id": "p"}) is True                 # no record at all counts as forced

    def down(i):
        raise RuntimeError("database down")
    monkeypatch.setattr(agent_loop, "get_start_record", down)
    assert autonomy.inherited_force_semi({"id": "c", "parent_id": "p"}) is True


def test_a_missing_parent_is_refused_by_its_own_check_not_by_the_error_handler(world, monkeypatch, caplog):
    import logging
    from core import autonomy
    monkeypatch.setattr(agent_loop, "get_start_record", lambda i: {"forced": False})
    with caplog.at_level(logging.WARNING):
        assert autonomy.inherited_force_semi({"id": "c", "parent_id": "00000000-0000-0000-0000-000000000000", "role": "support"}) is True
    assert "forcing approval" not in caplog.text          # the explicit check answered; the exception path did not run


def test_the_role_budget_left_is_the_smallest_remaining_cap_and_none_without_a_cap(monkeypatch):
    from core import budget
    for k in ("BUDGET_ROLE_DAILY_TOKENS", "BUDGET_ROLE_MONTHLY_TOKENS", "BUDGET_ROLE_DAILY_OVERRIDES", "BUDGET_ROLE_MONTHLY_OVERRIDES",
              "BUDGET_DAILY_TOKENS", "BUDGET_MONTHLY_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    assert budget.remaining("sales") is None and budget.remaining(None) is None
    monkeypatch.setenv("BUDGET_ROLE_DAILY_TOKENS", "1000")
    monkeypatch.setenv("BUDGET_ROLE_MONTHLY_OVERRIDES", "sales=5000")
    monkeypatch.setattr(budget, "_used", lambda role, period: {"day": 400, "month": 4900}[period])
    assert budget.remaining("sales") == 100 and budget.remaining("support") == 600
    monkeypatch.setenv("BUDGET_DAILY_TOKENS", "10")                    # a global cap is not a role's own cap
    assert budget.remaining("support") == 600
    monkeypatch.setattr(budget, "_used", lambda role, period: (_ for _ in ()).throw(RuntimeError("down")))
    assert budget.remaining("sales") is None                           # fails open, like every token budget


def test_a_child_stops_when_its_parents_remaining_budget_is_spent(world, monkeypatch):
    from core import budget
    monkeypatch.setenv("BUDGET_ROLE_DAILY_OVERRIDES", "sales=100000")
    monkeypatch.setattr(budget, "_used", lambda role, period: 99950 if role == "sales" else 0)     # the parent role has 50 tokens left
    svc = FakeService([plan("delegate", "support", TASK), plan("run_code", content="print(1)"), DONE, "Parent summary."])
    agent_loop.run(Q, "d", svc, "sales", False)
    parent, children = parent_and_children()
    assert children[0]["status"] == "budget" and len(children[0]["steps"]) == 1       # one step, then the parent's budget ran out
    assert len(world["sent"]) == 1


def test_a_child_whose_own_role_is_over_budget_does_not_start_work_the_model_call_checks_it(world, monkeypatch):
    monkeypatch.setattr(agent_loop.budget, "check_budget",
                        lambda role=None: {"scope": "role", "role": role, "period": "day", "limit": 1, "used": 1} if role == "support" else None)
    svc = FakeService([plan("delegate", "support", TASK), DONE, "Parent summary."])
    agent_loop.run(Q, "d", svc, None, False)
    _, children = parent_and_children()
    assert children[0]["status"] == "budget" and children[0]["steps"] == [] and world["sent"] == []


def test_the_budget_stop_fails_open_when_it_cannot_read_and_stops_nobody_without_a_parent(world, monkeypatch):
    assert subagents.budget_stop({"steps": []}) is False
    monkeypatch.setattr(agent_loop, "get_run", lambda i: (_ for _ in ()).throw(RuntimeError("down")))
    assert subagents.budget_stop({"parent_id": "p", "role": "support", "steps": []}) is False


def test_the_whole_run_tree_shares_one_step_limit(world, monkeypatch):
    monkeypatch.setenv("AGENT_LOOP_MAX_STEPS", "3")
    svc = FakeService([plan("delegate", "support", TASK), plan("run_code", content="print(1)"), plan("run_code", content="print(2)"),
                       "Child summary.", "Parent summary."])
    agent_loop.run(Q, "d", svc, None)
    parent, children = parent_and_children()
    assert len(parent["steps"]) + len(children[0]["steps"]) == 3 and len(world["sent"]) == 2
    assert parent["status"] == "done" and children[0]["status"] == "done"


def test_a_plain_run_never_reads_the_database_for_the_step_limit_and_the_tree_check_fails_closed(world, monkeypatch):
    monkeypatch.setattr(subagents, "_other_steps", lambda root, me: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert subagents.steps_exhausted({"id": None, "steps": [{}], "tainted": False}, 3) is False
    assert subagents.steps_exhausted({"id": None, "steps": [{}, {}, {}]}, 3) is True
    monkeypatch.setattr(subagents, "_other_steps", lambda root, me: (_ for _ in ()).throw(RuntimeError("down")))
    assert subagents.steps_exhausted({"id": "c", "parent_id": "p", "steps": []}, 3) is True      # an action cap that cannot be read stops


def test_the_docs_page_describes_the_start_record_and_the_shared_budgets():
    text = (ROOT / "docs" / "sub-agents.md").read_text(encoding="utf-8")
    for word in ("spawn_id", "start record", "agent_run_starts", "append-only", "SPAWN_SIGNING_KEY", "SPAWN_SIGNING_KEY_PREVIOUS",
                 "key id", "python -m core.spawn_keys", "safe_to_retire", "database administrator", "smaller of", "step limit",
                 "fails closed", "fail open"):
        assert word in text, word
    assert "{{" not in text and "{%" not in text


def test_the_schema_has_the_append_only_start_table_and_still_only_adds():
    sql_text = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    for col, kind in (("spawn_id", "UUID"), ("spawn_forced", "BOOLEAN"), ("spawn_sig", "TEXT")):      # v0.13.7 columns stay, unused
        assert sql_text.count(f"ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS {col} {kind};") == 1
    assert sql_text.count("CREATE TABLE IF NOT EXISTS agent_run_starts (") == 1
    for needle in ("CREATE OR REPLACE FUNCTION agent_run_starts_append_only()", "BEFORE UPDATE OR DELETE ON agent_run_starts",
                   "BEFORE TRUNCATE ON agent_run_starts", "RAISE EXCEPTION"):
        assert sql_text.count(needle) == 1, needle
    assert "DROP COLUMN" not in sql_text and "DROP TABLE agent_run" not in sql_text


def test_the_start_record_is_in_its_own_table_and_the_run_row_carries_none(world):
    agent_loop.run(Q, "d", FakeService(basic_replies()), None)
    _, children = parent_and_children()
    assert not any(k.startswith("spawn_") for k in children[0])                  # the mutable row holds no start record
    assert agent_loop.get_start_record(children[0]["id"]) is not None
    assert agent_loop.get_start_record("not-a-uuid") is None and agent_loop.get_start_record(None) is None


def test_an_edited_live_row_makes_the_child_refused(world):
    from core import autonomy
    agent_loop.run(Q, "d", FakeService(basic_replies()), None)
    _, children = parent_and_children()
    cid = children[0]["id"]
    assert autonomy.verify_spawn_record(agent_loop.get_run(cid)) is True
    for column, value in (("role", "sales"), ("depth", 0), ("parent_id", "00000000-0000-0000-0000-000000000001")):
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(f"UPDATE agent_runs SET {column} = %s WHERE id = %s", (value, cid))
            conn.commit()
            cur.close()
        finally:
            conn.close()
        assert autonomy.verify_spawn_record(agent_loop.get_run(cid)) is False, column
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE agent_runs SET role = 'support', depth = 1, parent_id = %s WHERE id = %s", (children[0]["parent_id"], cid))
            conn.commit()
            cur.close()
        finally:
            conn.close()
        assert autonomy.verify_spawn_record(agent_loop.get_run(cid)) is True


def test_the_signing_key_can_be_set_apart_from_the_addon_key(monkeypatch):
    from core import autonomy
    monkeypatch.setenv("ADDON_ENCRYPTION_KEY", "addon-key")
    monkeypatch.delenv("SPAWN_SIGNING_KEY", raising=False)
    monkeypatch.delenv("SPAWN_SIGNING_KEY_PREVIOUS", raising=False)
    addon_id = autonomy._signing_keys()[0]
    monkeypatch.setenv("SPAWN_SIGNING_KEY", "signing-key")
    spawn_id = autonomy._signing_keys()[0]
    assert addon_id and spawn_id and addon_id != spawn_id
    monkeypatch.delenv("ADDON_ENCRYPTION_KEY")
    assert autonomy._signing_keys()[0] == spawn_id                               # the signing key alone is enough
    monkeypatch.delenv("SPAWN_SIGNING_KEY")
    assert autonomy._signing_keys() == (None, {})


def test_a_key_id_is_short_and_does_not_reveal_the_key(monkeypatch):
    from core import autonomy
    monkeypatch.setenv("SPAWN_SIGNING_KEY", "super-secret-key-value")
    kid = autonomy._signing_keys()[0]
    assert len(kid) == 12 and all(c in "0123456789abcdef" for c in kid) and "secret" not in kid
    assert kid != autonomy._key_id("another-key")


def test_a_record_signed_with_the_previous_key_verifies_until_that_key_is_retired(world, monkeypatch):
    from core import autonomy
    sid, rid, pid = "11111111-1111-1111-1111-111111111111", "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444"
    run = {"id": rid, "parent_id": pid, "role": "support", "depth": 1}
    monkeypatch.setenv("SPAWN_SIGNING_KEY", "key-A")
    monkeypatch.delenv("SPAWN_SIGNING_KEY_PREVIOUS", raising=False)
    rec_a = autonomy.spawn_record(sid, rid, pid, "support", 1, False)
    monkeypatch.setattr(agent_loop, "get_start_record", lambda i: dict(rec_a))
    assert autonomy.verify_spawn_record(run) is True
    monkeypatch.setenv("SPAWN_SIGNING_KEY", "key-B")                           # rotation: B is current, A is still known
    monkeypatch.setenv("SPAWN_SIGNING_KEY_PREVIOUS", "key-A")
    assert autonomy.verify_spawn_record(run) is True
    rec_b = autonomy.spawn_record(sid, rid, pid, "support", 1, False)
    assert rec_b["key_id"] != rec_a["key_id"] and rec_b["key_id"] == autonomy._signing_keys()[0]     # new records use the new key
    monkeypatch.setenv("SPAWN_SIGNING_KEY_PREVIOUS", "other-old-key, key-A ,")
    assert autonomy.verify_spawn_record(run) is True                           # several previous keys, blanks ignored
    monkeypatch.delenv("SPAWN_SIGNING_KEY_PREVIOUS")                            # retired: the record is refused
    assert autonomy.verify_spawn_record(run) is False
    monkeypatch.setattr(agent_loop, "get_start_record", lambda i: dict(rec_b))
    assert autonomy.verify_spawn_record(run) is True


def test_the_key_report_says_when_the_old_key_is_safe_to_retire(world, monkeypatch):
    from core import autonomy, spawn_keys
    monkeypatch.setenv("SPAWN_SIGNING_KEY", "report-key-A")
    sensitive(monkeypatch, "legal_intake")
    agent_loop.run(Q, "d", FakeService([plan("delegate", "support", TASK), plan("send_webhook", "ops", "hello"), "Parent summary."]), "legal_intake", False)
    _, children = parent_and_children()
    assert children[0]["status"] == "waiting_approval"                          # an unfinished child that depends on key A
    key_a = autonomy._signing_keys()[0]
    monkeypatch.setenv("SPAWN_SIGNING_KEY", "report-key-B")
    monkeypatch.setenv("SPAWN_SIGNING_KEY_PREVIOUS", "report-key-A")
    rep = {k["key_id"]: k for k in spawn_keys.report()["keys"]}
    assert rep[key_a]["unfinished_runs"] >= 1 and rep[key_a]["safe_to_retire"] is False and rep[key_a]["configured"] is True
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE agent_runs SET status = 'cancelled' WHERE query LIKE 'TEST-SUB%'")
        conn.commit()
        cur.close()
    finally:
        conn.close()
    rep = {k["key_id"]: k for k in spawn_keys.report()["keys"]}
    assert rep[key_a]["unfinished_runs"] == 0 and rep[key_a]["safe_to_retire"] is True
    assert spawn_keys.report()["current_key_id"] == autonomy._signing_keys()[0] and rep[autonomy._signing_keys()[0]]["safe_to_retire"] is False
    assert "report-key" not in json.dumps(spawn_keys.report())                # a key is never printed


def test_a_run_that_still_needs_a_key_that_is_no_longer_configured_is_counted(world, monkeypatch):
    from core import spawn_keys
    monkeypatch.setenv("SPAWN_SIGNING_KEY", "orphan-key-A")
    sensitive(monkeypatch, "legal_intake")
    agent_loop.run(Q, "d", FakeService([plan("delegate", "support", TASK), plan("send_webhook", "ops", "hello"), "Parent summary."]), "legal_intake", False)
    monkeypatch.setenv("SPAWN_SIGNING_KEY", "orphan-key-B")                       # rotated WITHOUT keeping A as the previous key
    assert spawn_keys.report()["unfinished_runs_whose_key_is_not_configured"] >= 1


def test_the_signature_itself_covers_every_field_even_when_the_live_row_is_edited_to_agree(world, monkeypatch):
    from core import autonomy
    sid, rid, pid = "11111111-1111-1111-1111-111111111111", "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444"
    other = "55555555-5555-5555-5555-555555555555"
    rec = autonomy.spawn_record(sid, rid, pid, "support", 1, False)
    for field, value in (("role", "sales"), ("run_id", other), ("parent_id", other), ("depth", 2), ("forced", True), ("spawn_id", other)):
        edited = dict(rec, **{field: value})
        live = {"id": edited["run_id"], "parent_id": edited["parent_id"], "role": edited["role"], "depth": edited["depth"]}   # the row agrees
        monkeypatch.setattr(agent_loop, "get_start_record", lambda i, e=edited: dict(e))
        assert autonomy.verify_spawn_record(live) is False, field                 # only the signature can refuse this


def test_the_key_id_is_part_of_what_is_signed_so_a_record_cannot_be_relabelled(world, monkeypatch):
    from core import autonomy
    same_key = autonomy._derive("one-key")
    monkeypatch.setattr(autonomy, "_signing_keys", lambda: ("k1", {"k1": same_key, "k2": same_key}))   # two ids, one key
    sid, rid, pid = "11111111-1111-1111-1111-111111111111", "33333333-3333-3333-3333-333333333333", "44444444-4444-4444-4444-444444444444"
    rec = autonomy.spawn_record(sid, rid, pid, "support", 1, False)
    run = {"id": rid, "parent_id": pid, "role": "support", "depth": 1}
    monkeypatch.setattr(agent_loop, "get_start_record", lambda i: dict(rec))
    assert autonomy.verify_spawn_record(run) is True
    monkeypatch.setattr(agent_loop, "get_start_record", lambda i: dict(rec, key_id="k2"))
    assert autonomy.verify_spawn_record(run) is False
