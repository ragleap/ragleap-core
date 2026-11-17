"""A pending action that waited too long is not run by a late approval (AMQIC review: authorization needs an expiry)."""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import autonomy
from core.employees._db import get_connection


def _pending(age_hours):
    aid = uuid.uuid4().hex[:8].upper()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO autonomy_pending (action_id, action_type, channel, target, content, created_at) "
        "VALUES (%s, 'send_slack', 'slack', 'x', 'expiry-test', now() - make_interval(hours => %s))",
        (aid, age_hours),
    )
    conn.commit()
    cur.close()
    conn.close()
    return aid


def _left(aid):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM autonomy_pending WHERE action_id = %s", (aid,))
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


def _quiet(monkeypatch):
    sent, notified = [], []
    monkeypatch.setattr(autonomy, "_send_via_channel", lambda ch, t, c: sent.append((ch, t)) or "ok")
    monkeypatch.setattr(autonomy, "log_autonomous_action", lambda *a, **k: None)
    monkeypatch.setattr(autonomy.employee_learning, "learn_from_owner_approval", lambda *a, **k: None)
    monkeypatch.setattr(autonomy, "_notify_agent_loop", lambda aid, ok, res, at: notified.append((aid, ok)))
    return sent, notified


def test_an_old_pending_action_is_not_run_by_a_late_approval(monkeypatch):
    monkeypatch.delenv("APPROVAL_EXPIRY_HOURS", raising=False)
    sent, notified = _quiet(monkeypatch)
    aid = _pending(24 * 8)
    out = autonomy.process_approval_response(f"YES {aid}")
    assert "expired" in out and "not run" in out
    assert sent == []
    assert notified == [(aid, False)]
    assert _left(aid) == 0


def test_a_recent_pending_action_still_runs(monkeypatch):
    monkeypatch.delenv("APPROVAL_EXPIRY_HOURS", raising=False)
    sent, _ = _quiet(monkeypatch)
    aid = _pending(24 * 6)
    assert "approved and executed" in autonomy.process_approval_response(f"YES {aid}")
    assert len(sent) == 1


def test_rejecting_an_old_pending_action_still_works(monkeypatch):
    monkeypatch.delenv("APPROVAL_EXPIRY_HOURS", raising=False)
    sent, _ = _quiet(monkeypatch)
    aid = _pending(24 * 30)
    assert "rejected and cancelled" in autonomy.process_approval_response(f"NO {aid}")
    assert sent == []


def test_the_limit_can_be_changed_and_turned_off(monkeypatch):
    sent, _ = _quiet(monkeypatch)
    monkeypatch.setenv("APPROVAL_EXPIRY_HOURS", "2")
    assert "expired" in autonomy.process_approval_response(f"YES {_pending(3)}")
    monkeypatch.setenv("APPROVAL_EXPIRY_HOURS", "0")
    assert "approved and executed" in autonomy.process_approval_response(f"YES {_pending(24 * 400)}")
    assert len(sent) == 1
    monkeypatch.setenv("APPROVAL_EXPIRY_HOURS", "not a number")
    assert autonomy._approval_expiry_hours() == 168
