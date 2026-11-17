"""One pending action runs at most once, however many answers arrive at the same time (F-CORE-005)."""
import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import autonomy
from core.employees._db import get_connection


def _pending():
    aid = uuid.uuid4().hex[:8].upper()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO autonomy_pending (action_id, action_type, channel, target, content) "
        "VALUES (%s, 'send_slack', 'slack', 'x', 'atomic-claim-test')",
        (aid,),
    )
    conn.commit()
    cur.close()
    conn.close()
    return aid


def _quiet(monkeypatch, sent):
    monkeypatch.setattr(autonomy, "_send_via_channel", lambda ch, t, c: sent.append((ch, t)) or "ok")
    monkeypatch.setattr(autonomy, "log_autonomous_action", lambda *a, **k: None)
    monkeypatch.setattr(autonomy.employee_learning, "learn_from_owner_approval", lambda *a, **k: None)
    monkeypatch.setattr(autonomy, "_notify_agent_loop", lambda *a, **k: None)


def _race(messages):
    gate = threading.Barrier(len(messages))
    out = [None] * len(messages)

    def go(i, msg):
        gate.wait()
        out[i] = autonomy.process_approval_response(msg)

    ts = [threading.Thread(target=go, args=(i, m)) for i, m in enumerate(messages)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def test_two_simultaneous_approvals_run_the_effect_once(monkeypatch):
    sent = []
    _quiet(monkeypatch, sent)
    for _ in range(40):
        sent.clear()
        aid = _pending()
        out = _race([f"YES {aid}", f"YES {aid}"])
        assert len(sent) == 1, out
        assert sum("approved and executed" in o for o in out) == 1
        assert sum("already processed" in o for o in out) == 1


def test_approve_and_reject_at_once_have_one_winner(monkeypatch):
    sent = []
    _quiet(monkeypatch, sent)
    for _ in range(40):
        sent.clear()
        aid = _pending()
        out = _race([f"YES {aid}", f"NO {aid}"])
        assert sum("already processed" in o for o in out) == 1
        won_yes = any("approved and executed" in o for o in out)
        assert len(sent) == (1 if won_yes else 0)


def test_single_approval_still_executes_once(monkeypatch):
    sent = []
    _quiet(monkeypatch, sent)
    aid = _pending()
    assert "approved and executed" in autonomy.process_approval_response(f"YES {aid}")
    assert len(sent) == 1
    assert "already processed" in autonomy.process_approval_response(f"YES {aid}")
    assert len(sent) == 1
