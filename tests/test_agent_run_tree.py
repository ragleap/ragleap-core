"""Run records with parents (sub-agents), the run tree, and cancelling a run: the schema upgrade, who is stopped when,
that a pending action cannot run after its run was cancelled, and that a live loop stops. Real test database only."""
import json
import pathlib
import uuid

import pytest

from core import agent_loop, permissions
from core.employees._db import get_connection

ROOT = pathlib.Path(__file__).resolve().parent.parent
Q = "TEST-TREE"
DONE = json.dumps({"tool": "done"})


def plan(tool, target="", content="x"):
    return json.dumps({"tool": tool, "target": target, "content": content})


class FakeService:
    primary_config = {"provider": "fake"}

    def __init__(self, replies):
        self.replies = list(replies)

    def _call_provider(self, cfg, prompt, temperature, max_tokens):
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


def mk(parent=None, depth=0, status="running", pending=None, summary=""):
    run = {"id": None, "role": "support", "query": f"{Q}: {uuid.uuid4()}", "context_answer": "", "status": status,
           "steps": [], "tainted": False, "pending_action_id": pending, "summary": summary, "parent_id": parent, "depth": depth}
    agent_loop._save(run)
    return run["id"]


@pytest.fixture(autouse=True)
def clean():
    yield
    sql("DELETE FROM agent_runs WHERE query LIKE %s", (Q + "%",))


@pytest.fixture
def rejected(monkeypatch):
    calls = []
    import core.autonomy as au
    monkeypatch.setattr(au, "resolve_pending", lambda action_id, approve, decided_by=None: calls.append((action_id, approve)) or "ok")
    return calls


# ---------------- schema ----------------

def test_the_new_columns_and_the_cancelled_status_exist():
    rid = mk()
    sql("UPDATE agent_runs SET status = 'cancelled' WHERE id = %s", (rid,))
    assert agent_loop.get_run(rid)["status"] == "cancelled"
    with pytest.raises(Exception):
        sql("UPDATE agent_runs SET status = 'bogus' WHERE id = %s", (rid,))
    got = agent_loop.get_run(rid)
    assert got["parent_id"] is None and got["depth"] == 0 and got["cancel_requested"] is False


def test_the_upgrade_block_changes_an_old_constraint_and_is_safe_to_run_twice():
    text = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    start = text.index("-- A run can have child runs")
    block = text[start:text.index("$$;", text.index("DO $$", start)) + 3]
    old = "CHECK (status IN ('running', 'waiting_approval', 'done', 'failed', 'rejected', 'budget'))"
    sql("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check")
    sql("ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check " + old)
    rid = mk()
    with pytest.raises(Exception):
        sql("UPDATE agent_runs SET status = 'cancelled' WHERE id = %s", (rid,))
    sql(block)
    sql(block)
    sql("UPDATE agent_runs SET status = 'cancelled' WHERE id = %s", (rid,))
    assert agent_loop.get_run(rid)["status"] == "cancelled"
    assert sql("SELECT count(*) FROM pg_constraint WHERE conname = 'agent_runs_status_check'", fetch=True)[0][0] == 1


# ---------------- records and tree ----------------

def test_parent_and_depth_are_stored_and_listed():
    top = mk()
    child = mk(parent=top, depth=1)
    got = agent_loop.get_run(child)
    assert got["parent_id"] == top and got["depth"] == 1 and isinstance(got["parent_id"], str)
    assert child in [r["id"] for r in agent_loop.list_runs(limit=200)]


def test_the_tree_nests_children_and_grandchildren_in_creation_order():
    top = mk()
    c1, c2 = mk(parent=top, depth=1, summary="first"), mk(parent=top, depth=1)
    g = mk(parent=c1, depth=2, status="done")
    t = agent_loop.tree(top)
    assert t["id"] == top and t["depth"] == 0 and [c["id"] for c in t["children"]] == [c1, c2]
    assert t["children"][0]["summary"] == "first" and [x["id"] for x in t["children"][0]["children"]] == [g]
    assert t["children"][0]["children"][0]["status"] == "done" and t["children"][1]["children"] == []
    assert set(t) == {"id", "role", "status", "depth", "tainted", "steps", "summary", "created_at", "cancel_requested", "children"}
    assert agent_loop.tree(c1)["children"][0]["id"] == g


def test_the_tree_of_an_unknown_run_is_none_and_the_size_is_limited(monkeypatch):
    assert agent_loop.tree(str(uuid.uuid4())) is None and agent_loop.tree("not-a-uuid") is None
    top = mk()
    for _ in range(4):
        mk(parent=top, depth=1)
    monkeypatch.setattr(agent_loop, "MAX_TREE_NODES", 2)
    assert len(agent_loop.tree(top)["children"]) == 2


# ---------------- cancel ----------------

def test_cancelling_a_run_waiting_for_approval_ends_it_and_rejects_the_action(rejected):
    rid = mk(status="waiting_approval", pending="ABCD1234")
    out = agent_loop.cancel_run(rid)
    assert out == {"run_id": rid, "cancelled": [rid], "stopping": [], "already_finished": []}
    got = agent_loop.get_run(rid)
    assert got["status"] == "cancelled" and got["pending_action_id"] is None and got["cancel_requested"] is True
    assert got["summary"] == "Stopped: cancelled by the owner."
    assert rejected == [("ABCD1234", False)]
    agent_loop.on_resolved("ABCD1234", True, "executed anyway")     # a late approval must not revive the run
    assert agent_loop.get_run(rid)["status"] == "cancelled"


def test_cancel_goes_down_the_tree_and_leaves_finished_runs_alone(rejected):
    top = mk()
    child = mk(parent=top, depth=1, status="waiting_approval", pending="EFGH5678")
    grand = mk(parent=child, depth=2, status="done")
    out = agent_loop.cancel_run(top)
    assert out["cancelled"] == [child] and out["stopping"] == [top] and out["already_finished"] == [grand]
    assert agent_loop.get_run(top)["status"] == "running" and agent_loop.get_run(top)["cancel_requested"] is True
    assert agent_loop.get_run(child)["status"] == "cancelled"
    done = agent_loop.get_run(grand)
    assert done["status"] == "done" and done["cancel_requested"] is False
    assert rejected == [("EFGH5678", False)]


def test_a_running_run_without_a_live_thread_is_ended_at_once_a_fresh_one_is_only_flagged(rejected):
    stale, fresh = mk(), mk()
    sql("UPDATE agent_runs SET updated_at = now() - interval '20 minutes' WHERE id = %s", (stale,))
    assert agent_loop.cancel_run(stale)["cancelled"] == [stale] and agent_loop.get_run(stale)["status"] == "cancelled"
    out = agent_loop.cancel_run(fresh)
    assert out["stopping"] == [fresh] and agent_loop.get_run(fresh)["status"] == "running"
    assert agent_loop.get_run(fresh)["cancel_requested"] is True


def test_finished_and_unknown_runs(rejected):
    rid = mk(status="failed")
    out = agent_loop.cancel_run(rid)
    assert out["already_finished"] == [rid] and out["cancelled"] == [] and agent_loop.get_run(rid)["status"] == "failed"
    assert agent_loop.get_run(rid)["cancel_requested"] is False
    assert agent_loop.cancel_run(str(uuid.uuid4())) is None and agent_loop.cancel_run("not-a-uuid") is None


def test_a_failing_child_does_not_stop_the_rest(rejected, monkeypatch):
    top = mk()
    child = mk(parent=top, depth=1, status="waiting_approval", pending="IJKL9012")
    real = agent_loop._flag_cancel

    def flaky(run_id):
        if run_id == top:
            raise RuntimeError("db hiccup")
        real(run_id)
    monkeypatch.setattr(agent_loop, "_flag_cancel", flaky)
    out = agent_loop.cancel_run(top)
    assert out["cancelled"] == [child] and agent_loop.get_run(child)["status"] == "cancelled"


# ---------------- a live loop stops ----------------

@pytest.fixture
def world(monkeypatch):
    import core.autonomy as au
    import core.employees.sensitivity as sens
    monkeypatch.setenv("AGENT_LOOP_ENABLED", "true")
    monkeypatch.setenv("CODE_EXEC_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_TOKEN", "t")
    for k in ("AGENT_LOOP_MAX_STEPS", "SHELL_EXEC_ENABLED", "MCP_SERVERS", "MCP_ALLOWED_TOOLS", "SLACK_WEBHOOK_URL",
              "EMAIL_ALLOWED_RECIPIENTS", "ACTION_TASKS_ENABLED", "BROWSER_FETCH_ENABLED", "WEBHOOK_TARGETS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(agent_loop.budget, "check_budget", lambda role=None: None)
    sent = []
    monkeypatch.setattr(au, "get_autonomy_settings", lambda: {"mode": "full", "channels": [], "actions": [],
                                                              "approval_channel": "telegram", "approval_target": "+10000000000"})
    monkeypatch.setattr(au, "_send_via_channel", lambda ch, t, c: (sent.append((ch, t, c)) or "Code run: exit 0\nstdout:\n1\n"))
    monkeypatch.setattr(au, "log_autonomous_action", lambda *a, **k: None)
    monkeypatch.setattr(au, "request_approval", lambda *a, **k: True)
    monkeypatch.setattr(au.employee_learning, "learn_from_owner_approval", lambda *a, **k: None)
    monkeypatch.setattr(sens, "is_sensitive_role", lambda r: False)
    yield sent
    sql("DELETE FROM agent_runs WHERE query LIKE 'TEST-LOOP%'")


def counter(answers):
    seen = []

    def fn(run_id):
        seen.append(run_id)
        return answers[len(seen) - 1] if len(seen) <= len(answers) else True
    return fn


def run_loop():
    svc = FakeService([plan("run_code", content="print(1)"), plan("run_code", content="print(2)"), DONE, "ok"])
    agent_loop.run("TEST-LOOP: cancel me", "d", svc, None)
    return sql("SELECT status, summary, jsonb_array_length(steps) FROM agent_runs WHERE query LIKE 'TEST-LOOP%'", fetch=True)[0]


def test_a_flagged_run_stops_before_its_next_step(world, monkeypatch):
    monkeypatch.setattr(agent_loop, "cancel_requested", counter([True]))
    status, summary, steps = run_loop()
    assert status == "cancelled" and summary.endswith("Stopped: cancelled by the owner.") and steps == 1 and len(world) == 1


def test_a_run_cancelled_while_the_model_was_planning_does_not_execute_the_planned_action(world, monkeypatch):
    monkeypatch.setattr(agent_loop, "cancel_requested", counter([False, True]))
    status, summary, steps = run_loop()
    assert status == "cancelled" and steps == 1 and len(world) == 1


def test_an_unflagged_run_is_unchanged(world, monkeypatch):
    monkeypatch.setattr(agent_loop, "cancel_requested", lambda run_id: False)
    status, summary, steps = run_loop()
    assert status == "done" and steps == 2 and len(world) == 2


# ---------------- routes, rules, retention ----------------

def test_the_routes_and_the_permission_rules(rejected):
    from fastapi import HTTPException
    from core import api
    top = mk(status="waiting_approval", pending="MNOP3456")
    child = mk(parent=top, depth=1)
    assert api.get_agent_run_tree(top)["tree"]["children"][0]["id"] == child
    assert api.get_agent_run(child)["parent_id"] == top
    assert api.cancel_agent_run(top)["cancelled"] == [top]
    assert api.list_agent_runs(status="cancelled")["runs"][0]["status"] == "cancelled"
    assert all(r["status"] == "cancelled" for r in api.list_agent_runs(status="cancelled", limit=200)["runs"])
    for bad in (str(uuid.uuid4()), "not-a-uuid"):
        for call in (api.get_agent_run_tree, api.cancel_agent_run):
            with pytest.raises(HTTPException) as e:
                call(bad)
            assert e.value.status_code == 404
    assert permissions.required_role("POST", "/agent-runs/abc/cancel") == "operator"
    assert permissions.required_role("GET", "/agent-runs/abc/tree") == "operator"


def test_a_cancelled_run_is_finished_for_retention():
    from core import retention
    cond = dict((t[0], t[3]) for t in retention.TABLES)["agent_runs"]
    assert "'cancelled'" in cond and "'running'" not in cond and "'waiting_approval'" not in cond
    assert "cancelled" in agent_loop.RUN_STATUSES and set(agent_loop.FINISHED_STATUSES) <= set(agent_loop.RUN_STATUSES)
