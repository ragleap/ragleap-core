"""
Who did what: a plain record of every state-changing request made by a signed-in person.

The API key middleware (core/api.py) calls schedule() after each POST, PUT, PATCH and DELETE once it knows who is
calling: the person (or "Owner (shared key)"), their role, the method, the path and the answer status. Denied attempts
(403) are recorded too. Not recorded: reads, requests with no valid key, and the chat routes (a conversation is not a
change, and its text is private). Never recorded: bodies, query strings or keys, only the path.

record() never raises, and schedule() never makes a request wait for the database. If the table is missing (db/schema.sql
not applied yet) the event is dropped with one warning. A person's name is copied into each row, so the history stays
readable after the person is removed. The table is not trimmed: delete old rows yourself if it grows too large.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
NOT_RECORDED_PREFIXES = ("/chat",)
_tasks = set()
_warned = set()


def _connect():
    from core import document_store
    return document_store._connect()


def should_record(method: str, path: str) -> bool:
    return (method or "").upper() in WRITE_METHODS and not any(
        (path or "") == p or (path or "").startswith(p + "/") for p in NOT_RECORDED_PREFIXES)


def record(actor: dict, method: str, path: str, status: int) -> None:
    """Write one event. Never raises."""
    try:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO audit_events (actor_name, actor_role, actor_kind, actor_user, method, path, status) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (str(actor.get("name") or "unknown")[:120], str(actor.get("role") or "unknown"),
                         str(actor.get("kind") or "unknown"), actor.get("user_id"), method.upper(), (path or "")[:300],
                         int(status)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        if "record" not in _warned:
            _warned.add("record")
            logger.warning("could not write an audit event (%s); is db/schema.sql applied?", type(e).__name__)


def schedule(actor: dict, method: str, path: str, status: int) -> None:
    """Record in the background so the request never waits for the database."""
    if not actor or not should_record(method, path):
        return
    try:
        task = asyncio.get_running_loop().create_task(asyncio.to_thread(record, actor, method, path, status))
    except RuntimeError:
        record(actor, method, path, status)
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def list_events(limit: int = 100, actor: str = None) -> list:
    limit = max(1, min(int(limit), 500))
    sql = "SELECT id, at, actor_name, actor_role, actor_kind, actor_user, method, path, status FROM audit_events"
    params = []
    if actor:
        sql += " WHERE actor_name = %s"
        params.append(actor)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "at": r[1].isoformat() if r[1] else None, "actor": r[2], "role": r[3], "kind": r[4],
             "user_id": str(r[5]) if r[5] else None, "method": r[6], "path": r[7], "status": r[8]} for r in rows]
