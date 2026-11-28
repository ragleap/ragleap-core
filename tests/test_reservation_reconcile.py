"""P1-E: a reservation that a dead child left behind is settled or released, with an audit event for each, and a refused spawn says why."""
import os
import sys
import threading
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import agent_loop, subagents
from core.employees._db import get_connection

CAP = 1000


def _exec(sql, params=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params or None)          # no params at all: a literal % in the SQL must not be treated as a placeholder
    conn.commit()
    cur.close()
    conn.close()


def _one(sql, params=()):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    r = cur.fetchone()
    cur.close()
    conn.close()
    return r


def _run(parent_id=None, status="running", used=0):
    r = {"id": None, "role": "sales" if parent_id is None else "support", "query": "q", "context_answer": "", "status": status, "steps": [],
         "tainted": False, "pending_action_id": None, "summary": "", "parent_id": parent_id, "depth": 0 if parent_id is None else 1,
         "tokens_used": used}
    agent_loop._save(r)
    return r["id"]


def _child(pid, status="running", reserved=CAP, used=0, idle=0, pending=None):
    cid = _run(pid, status, used)
    _exec("UPDATE agent_runs SET reserved_tokens = %s, pending_action_id = %s, updated_at = now() - make_interval(secs => %s) WHERE id = %s",
          (reserved, pending, idle, cid))
    return cid


def _parent(reserved):
    pid = _run()
    _exec("UPDATE agent_runs SET reserved_tokens = %s, spawned = 1 WHERE id = %s", (reserved, pid))
    return pid


def _state(rid):
    return _one("SELECT status, reserved_tokens, summary FROM agent_runs WHERE id = %s", (rid,))


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr(subagents, "_emit", lambda event, **f: seen.append((event, f)))
    yield seen
    _exec("DELETE FROM agent_runs WHERE query = 'q'")
    _exec("DELETE FROM autonomy_pending WHERE action_id LIKE 'RECON%'")


def test_an_ended_child_that_still_holds_a_reservation_is_settled_to_its_real_use(events):
    pid = _parent(CAP)
    cid = _child(pid, status="done", reserved=CAP, used=130)
    out = subagents.reconcile_children(pid)
    assert out["settled"] == 1 and out["changed"] and _one("SELECT reserved_tokens FROM agent_runs WHERE id = %s", (pid,))[0] == 130
    assert _state(cid)[1] == 0
    assert [(e, f["reason"], f["child_run"], f["used"]) for e, f in events] == [("subagent_reservation_settled", "child_ended", cid, 130)]


@pytest.mark.parametrize("status", ["failed", "budget", "cancelled"])
def test_every_ended_status_is_settled(events, status):
    pid = _parent(CAP)
    _child(pid, status=status)
    assert subagents.reconcile_children(pid)["settled"] == 1


def test_a_running_child_nobody_works_on_any_more_is_ended_and_released(events):
    pid = _parent(CAP)
    cid = _child(pid, status="running", used=40, idle=agent_loop.CANCEL_STALE_SECONDS + 60)
    out = subagents.reconcile_children(pid)
    assert out["released"] == 1 and out["changed"]
    status, reserved, summary = _state(cid)
    assert status == "failed" and reserved == 0 and "abandoned" in summary
    assert _one("SELECT reserved_tokens FROM agent_runs WHERE id = %s", (pid,))[0] == 40
    assert [(e, f["reason"], f["child_run"]) for e, f in events] == [("subagent_reservation_released", "abandoned_running", cid)]
    assert events[0][1]["idle_seconds"] > agent_loop.CANCEL_STALE_SECONDS


def test_a_live_child_is_never_touched(events):
    pid = _parent(CAP)
    cid = _child(pid, status="running", idle=5)
    out = subagents.reconcile_children(pid)
    assert out == {"settled": 0, "released": 0, "kept": 1, "changed": False} and _state(cid)[:2] == ("running", CAP) and events == []


def test_a_child_just_under_the_stale_limit_is_kept(events):
    pid = _parent(CAP)
    cid = _child(pid, status="running", idle=agent_loop.CANCEL_STALE_SECONDS - 30)
    assert subagents.reconcile_children(pid)["kept"] == 1 and _state(cid)[0] == "running"


def test_a_child_really_waiting_for_an_approval_keeps_its_reservation_however_long(events):
    pid = _parent(CAP)
    aid = "RECON" + uuid.uuid4().hex[:3].upper()
    _exec("INSERT INTO autonomy_pending (action_id, action_type, channel, target, content) VALUES (%s, 'send_slack', 'slack', 'x', 'c')", (aid,))
    cid = _child(pid, status="waiting_approval", idle=100000, pending=aid)
    out = subagents.reconcile_children(pid)
    assert out["kept"] == 1 and _state(cid)[:2] == ("waiting_approval", CAP) and events == []


def test_a_waiting_child_whose_pending_action_is_gone_is_released_only_when_stale(events):
    pid = _parent(2 * CAP)
    fresh = _child(pid, status="waiting_approval", idle=10, pending="RECON000")
    old = _child(pid, status="waiting_approval", idle=agent_loop.CANCEL_STALE_SECONDS + 10, pending="RECON111")
    out = subagents.reconcile_children(pid)
    assert out["released"] == 1 and out["kept"] == 1
    assert _state(fresh)[0] == "waiting_approval" and _state(old)[0] == "failed"
    assert [f["reason"] for e, f in events] == ["abandoned_waiting_approval"]


def test_only_the_asked_parents_children_are_touched(events):
    mine, other = _parent(CAP), _parent(CAP)
    _child(mine, status="done")
    theirs = _child(other, status="done")
    subagents.reconcile_children(mine)
    assert _state(theirs)[1] == CAP and _one("SELECT reserved_tokens FROM agent_runs WHERE id = %s", (other,))[0] == CAP


def test_a_child_without_a_reservation_is_ignored(events):
    pid = _parent(0)
    _child(pid, status="done", reserved=0)
    assert subagents.reconcile_children(pid) == {"settled": 0, "released": 0, "kept": 0, "changed": False}


def test_concurrent_passes_release_an_orphan_exactly_once(events):
    pid = _parent(CAP)
    _child(pid, status="running", used=70, idle=agent_loop.CANCEL_STALE_SECONDS + 60)
    barrier, out = threading.Barrier(8), [None] * 8

    def work(i):
        barrier.wait()
        out[i] = subagents.reconcile_children(pid)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert sum(o["released"] for o in out) == 1 and _one("SELECT reserved_tokens FROM agent_runs WHERE id = %s", (pid,))[0] == 70


def test_a_database_error_changes_nothing_and_never_raises(events, monkeypatch):
    import core.employees._db as db
    monkeypatch.setattr(db, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    out = subagents.reconcile_children("00000000-0000-0000-0000-000000000000")
    assert out["changed"] is False and out["error"] is True


def test_the_refusal_reason_names_the_real_cause_with_the_figures(events, monkeypatch):
    monkeypatch.setattr(subagents, "max_children", lambda: 3)
    pid = _parent(5000)
    assert subagents.refusal_reason(pid, CAP, 5500) == ("budget_reserved", {"reserved": 5000, "limit": 5500, "cap": CAP, "spawned": 1, "max_children": 3})
    _exec("UPDATE agent_runs SET spawned = 3 WHERE id = %s", (pid,))
    assert subagents.refusal_reason(pid, CAP, 10 ** 9)[0] == "child_limit"
    assert subagents.refusal_reason(pid, CAP, 10 ** 9 + 0)[1]["spawned"] == 3
    _exec("UPDATE agent_runs SET spawned = 1, reserved_tokens = 0 WHERE id = %s", (pid,))
    assert subagents.refusal_reason(pid, CAP, 10 ** 9)[0] == "reservation_unavailable"        # nothing explains a refusal any more
    assert subagents.refusal_reason("00000000-0000-0000-0000-000000000000", CAP, 1)[0] == "reservation_unavailable"
    import core.employees._db as db
    monkeypatch.setattr(db, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert subagents.refusal_reason(pid, CAP, 1) == ("reservation_unavailable", {})


def test_the_refusal_texts_carry_the_code_and_the_figures():
    assert "reason: child_limit" in subagents._refusal_text("child_limit", {"max_children": 3})
    t = subagents._refusal_text("budget_reserved", {"reserved": 24000, "limit": 26000, "cap": 8000})
    assert "fully reserved" in t and "reason: budget_reserved" in t and "24000 of 26000" in t and "needs 8000" in t
    assert "reason: reservation_unavailable" in subagents._refusal_text("reservation_unavailable", {})


def test_abandoning_a_child_only_changes_a_run_that_is_still_stale_and_has_the_status_we_saw(events):
    pid = _parent(CAP)
    fresh = _child(pid, status="running", idle=5)
    assert subagents._abandon_child(fresh, "running", agent_loop.CANCEL_STALE_SECONDS) is False and _state(fresh)[0] == "running"
    old = _child(pid, status="running", idle=agent_loop.CANCEL_STALE_SECONDS + 60)
    assert subagents._abandon_child(old, "waiting_approval", agent_loop.CANCEL_STALE_SECONDS) is False and _state(old)[0] == "running"   # status moved on
    assert subagents._abandon_child(old, "running", agent_loop.CANCEL_STALE_SECONDS) is True and _state(old)[0] == "failed"
    assert subagents._abandon_child(old, "running", agent_loop.CANCEL_STALE_SECONDS) is False                                              # only once
