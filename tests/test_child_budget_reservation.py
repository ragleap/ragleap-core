"""P1-B: a child's budget is reserved with ONE conditional UPDATE before the child exists. Spawns that look at the same remaining
budget at the same moment cannot both take it. The concurrent tests use real threads, each with its own database connection."""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import agent_loop, subagents
from core.employees._db import get_connection

CAP = 1000


def _new_parent():
    run = {"id": None, "role": "sales", "query": "q", "context_answer": "", "status": "running", "steps": [], "tainted": False,
           "pending_action_id": None, "summary": "", "parent_id": None, "depth": 0, "tokens_used": 0}
    agent_loop._save(run)
    return run["id"]


def _row(run_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT reserved_tokens, spawned FROM agent_runs WHERE id = %s", (run_id,))
    r = cur.fetchone()
    cur.close()
    conn.close()
    return r


def _exec(sql, params=()):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    cur.close()
    conn.close()


def _race(fn, n):
    """Run fn(i) in n threads released at the same moment; return the results."""
    barrier, out = threading.Barrier(n), [None] * n

    def work(i):
        barrier.wait()
        out[i] = fn(i)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    return out


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    _exec("DELETE FROM agent_runs WHERE query = 'q'")


def test_concurrent_spawns_never_take_more_than_the_remaining_budget(monkeypatch):
    monkeypatch.setattr(subagents, "max_children", lambda: 100)
    for _ in range(25):                                    # many rounds: a race that exists shows up in some of them
        pid = _new_parent()
        limit = 3 * CAP + CAP // 2                         # room for exactly 3 reservations
        got = _race(lambda i: subagents.reserve_child(pid, CAP, limit), 12)
        assert sum(1 for g in got if g) == 3, got
        assert _row(pid) == (3 * CAP, 3)


def test_concurrent_spawns_never_start_more_children_than_the_child_limit(monkeypatch):
    monkeypatch.setattr(subagents, "max_children", lambda: 3)
    for _ in range(25):
        pid = _new_parent()
        got = _race(lambda i: subagents.reserve_child(pid, 1, 10 ** 9), 12)
        assert sum(1 for g in got if g) == 3, got
        assert _row(pid) == (3, 3)


def test_the_test_has_teeth_a_read_then_write_reservation_over_allocates_under_the_same_race(monkeypatch):
    """The naive version (read the remaining budget, decide, write) loses the race: every thread reads the same number."""
    monkeypatch.setattr(subagents, "max_children", lambda: 100)
    pid = _new_parent()
    limit = 3 * CAP + CAP // 2
    barrier = threading.Barrier(12)

    def naive(i):
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT reserved_tokens FROM agent_runs WHERE id = %s", (pid,))
        seen = cur.fetchone()[0]
        barrier.wait()                                     # everyone has read the same value before anyone writes
        ok = seen + CAP <= limit
        if ok:
            cur.execute("UPDATE agent_runs SET reserved_tokens = %s, spawned = spawned + 1 WHERE id = %s", (seen + CAP, pid))
        conn.commit()
        cur.close()
        conn.close()
        return ok
    got = [None] * 12
    ts = [threading.Thread(target=lambda i=i: got.__setitem__(i, naive(i))) for i in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert sum(1 for g in got if g) == 12                  # all twelve passed the check: far more than the 3 that fit
    assert _row(pid)[1] == 12


def test_a_refused_reservation_changes_nothing(monkeypatch):
    monkeypatch.setattr(subagents, "max_children", lambda: 5)
    pid = _new_parent()
    assert subagents.reserve_child(pid, CAP, CAP) == {"reserved": CAP, "spawned": 1}
    assert subagents.reserve_child(pid, CAP, CAP) is None and _row(pid) == (CAP, 1)
    assert subagents.reserve_child(pid, CAP, CAP * 2) == {"reserved": 2 * CAP, "spawned": 2}


def test_a_database_error_refuses_the_spawn(monkeypatch):
    import core.employees._db as db
    monkeypatch.setattr(db, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert subagents.reserve_child("00000000-0000-0000-0000-000000000000", CAP, 10 ** 9) is None       # a safety limit fails closed


def _child_of(pid, reserved, used):
    child = {"id": None, "role": "support", "query": "q", "context_answer": "", "status": "done", "steps": [], "tainted": False,
             "pending_action_id": None, "summary": "", "parent_id": pid, "depth": 1, "tokens_used": used}
    agent_loop._save(child)
    _exec("UPDATE agent_runs SET reserved_tokens = %s WHERE id = %s", (reserved, child["id"]))
    return child["id"]


def test_release_turns_the_reservation_into_what_the_child_really_used(monkeypatch):
    monkeypatch.setattr(subagents, "max_children", lambda: 5)
    pid = _new_parent()
    subagents.reserve_child(pid, CAP, 10 ** 9)
    subagents.reserve_child(pid, CAP, 10 ** 9)
    cid = _child_of(pid, CAP, 130)
    assert subagents.release_child(cid) is True
    assert _row(pid) == (CAP + 130, 2) and _row(cid)[0] == 0
    assert subagents.release_child(cid) is False and _row(pid) == (CAP + 130, 2)             # a second release changes nothing


def test_concurrent_releases_of_one_child_settle_it_once(monkeypatch):
    monkeypatch.setattr(subagents, "max_children", lambda: 5)
    pid = _new_parent()
    subagents.reserve_child(pid, CAP, 10 ** 9)
    cid = _child_of(pid, CAP, 200)
    got = _race(lambda i: subagents.release_child(cid), 8)
    assert sum(1 for g in got if g) == 1 and _row(pid) == (200, 1)


def test_a_child_that_used_more_than_its_reservation_is_counted_at_its_real_use(monkeypatch):
    monkeypatch.setattr(subagents, "max_children", lambda: 5)
    pid = _new_parent()
    subagents.reserve_child(pid, CAP, 10 ** 9)
    subagents.release_child(_child_of(pid, CAP, CAP + 500))
    assert _row(pid)[0] == CAP + 500


def test_refund_gives_back_one_slot_and_the_tokens(monkeypatch):
    monkeypatch.setattr(subagents, "max_children", lambda: 5)
    pid = _new_parent()
    subagents.reserve_child(pid, CAP, 10 ** 9)
    subagents.reserve_child(pid, CAP, 10 ** 9)
    subagents.refund_child(pid, CAP)
    assert _row(pid) == (CAP, 1)
    subagents.refund_child(pid, CAP)
    subagents.refund_child(pid, CAP)
    assert _row(pid) == (0, 0)                              # never below zero


def test_saving_the_parent_run_does_not_touch_the_reservation(monkeypatch):
    monkeypatch.setattr(subagents, "max_children", lambda: 5)
    pid = _new_parent()
    subagents.reserve_child(pid, CAP, 10 ** 9)
    run = agent_loop.get_run(pid)
    run["tokens_used"] = 77
    agent_loop._save(run)
    assert _row(pid) == (CAP, 1)


def test_on_child_ended_settles_the_reservation_even_when_no_parent_waits(monkeypatch):
    monkeypatch.setattr(subagents, "max_children", lambda: 5)
    pid = _new_parent()
    subagents.reserve_child(pid, CAP, 10 ** 9)
    cid = _child_of(pid, CAP, 40)
    subagents.on_child_ended(agent_loop.get_run(cid))
    assert _row(pid) == (40, 1) and _row(cid)[0] == 0


def test_the_limit_is_the_tree_cap_minus_the_parents_use_and_never_above_the_roles_remaining_budget(monkeypatch):
    from core import budget
    monkeypatch.setattr(subagents, "tree_tokens", lambda: 30000)
    monkeypatch.setattr(budget, "remaining", lambda role: None)
    assert subagents.reservation_limit({"tokens_used": 4000, "role": "sales"}) == 26000
    monkeypatch.setattr(budget, "remaining", lambda role: 5000)
    assert subagents.reservation_limit({"tokens_used": 4000, "role": "sales"}) == 5000
    monkeypatch.setattr(budget, "remaining", lambda role: (_ for _ in ()).throw(RuntimeError("x")))
    assert subagents.reservation_limit({"tokens_used": 4000, "role": "sales"}) == 26000     # a ledger that cannot be read does not stop spawns


def test_the_schema_adds_the_p1b_columns():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text(encoding="utf-8")
    for line in ("ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS reserved_tokens INTEGER NOT NULL DEFAULT 0;",
                 "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS spawned INTEGER NOT NULL DEFAULT 0;"):
        assert text.count(line) == 1, line
