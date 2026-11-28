"""Test helper: insert a pending action the way the gate does (with its hash, signature and policy digest)."""
from core import autonomy
from core.employees._db import get_connection


def insert_bound(action_id, action_type, channel, target, content, subject="", role=None, age_hours=0):
    h, sig = autonomy.approval_binding(action_id, action_type, channel, target, content, subject, role)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO autonomy_pending (action_id, action_type, channel, target, content, subject, role, action_hash, action_sig, policy_hash, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() - make_interval(hours => %s))",
        (action_id, action_type, channel, target, content, subject, role, h, sig, autonomy.policy_digest(), age_hours),
    )
    conn.commit()
    cur.close()
    conn.close()
