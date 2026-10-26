"""Who did what: the audit events (core/audit.py), the person on approvals and the activity log (core/autonomy.py,
core/office.py), and the GET /audit route. Real database (db/schema.sql applied); test rows are removed afterwards."""
import random
import string
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from core import audit, autonomy, document_store, office, users

BOOT = {"x-api-key": "boot-key"}
MARK = "pytest-audit-"
_BASE = {"id": 0}          # events older than this test run belong to other tests


def _sql(sql, args=(), fetch=False):
    conn = document_store._connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def clean():
    users._clear_cache()
    first = _sql("SELECT COALESCE(MAX(id), 0) FROM audit_events", fetch=True)[0][0]
    _BASE["id"] = first
    yield
    _sql("DELETE FROM audit_events WHERE id > %s", (first,))
    _sql("DELETE FROM autonomy_log WHERE content LIKE %s", (MARK + "%",))
    _sql("DELETE FROM autonomy_pending WHERE content LIKE %s", (MARK + "%",))
    _sql("DELETE FROM users WHERE name LIKE %s", ("pytest-%",))
    users._clear_cache()


@pytest.fixture
def client(monkeypatch):
    from core import api
    monkeypatch.setattr(api, "RAGLEAP_API_KEY", "boot-key")
    return TestClient(api.app)


def person(role):
    u = users.create_user("pytest-" + uuid.uuid4().hex[:8], role)
    return u, {"x-api-key": users.create_key(u["id"])["key"]}


def events(**where):
    sql, args = "SELECT actor_name, actor_role, actor_kind, method, path, status FROM audit_events WHERE id > %s", [_BASE["id"]]
    for k, v in where.items():
        sql += " AND %s = %%s" % k
        args.append(v)
    return _sql(sql + " ORDER BY id", args, True)


def wait_for(cond, secs=3.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        if cond():
            return True
        time.sleep(0.05)
    return False


def pending(content="x"):
    action_id = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    _sql("INSERT INTO autonomy_pending (action_id, action_type, channel, target, content, role) "
         "VALUES (%s, 'send_message', 'telegram', 'chat1', %s, 'support')", (action_id, MARK + content))
    return action_id


def log_row(content):
    return _sql("SELECT approved, decided_by, decided_by_role, decided_kind, decided_by_user FROM autonomy_log "
                "WHERE content = %s", (content,), True)


# ---------------- what is recorded ----------------

@pytest.mark.parametrize("method,path,yes", [
    ("POST", "/users", True), ("put", "/settings", True), ("PATCH", "/tasks/1", True), ("DELETE", "/documents/x", True),
    ("GET", "/users", False), ("HEAD", "/users", False), ("OPTIONS", "/users", False),
    ("POST", "/chat", False), ("POST", "/chat/stream", False), ("POST", "/chat/feedback", False),
    ("POST", "/chatty", True), ("POST", "/upload", True),
])
def test_what_is_recorded(method, path, yes):
    assert audit.should_record(method, path) is yes


def test_an_event_has_the_person_the_request_and_the_answer():
    audit.record({"name": MARK + "a", "role": "admin", "kind": "user", "user_id": str(uuid.uuid4())}, "post", "/users", 200)
    got = audit.list_events(limit=5, actor=MARK + "a")
    assert len(got) == 1 and got[0]["actor"] == MARK + "a" and got[0]["role"] == "admin" and got[0]["kind"] == "user"
    assert (got[0]["method"], got[0]["path"], got[0]["status"]) == ("POST", "/users", 200) and got[0]["user_id"]


def test_recording_never_raises(monkeypatch):
    monkeypatch.setattr(audit, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    audit.record({"name": "x", "role": "owner", "kind": "bootstrap"}, "POST", "/users", 200)
    audit.record({}, "POST", "/users", "not a number")


def test_the_list_is_newest_first_and_limited():
    for i in range(4):
        audit.record({"name": MARK + "l", "role": "admin", "kind": "user"}, "POST", "/p%d" % i, 200)
    got = audit.list_events(limit=2, actor=MARK + "l")
    assert [e["path"] for e in got] == ["/p3", "/p2"]
    assert len(audit.list_events(limit=9999)) <= 500 and len(audit.list_events(limit=0)) <= 1


# ---------------- through the API ----------------

def test_a_change_made_with_the_shared_key_is_recorded_and_a_read_is_not(client):
    assert client.get("/users", headers=BOOT).status_code == 200
    made = client.post("/users", json={"name": "pytest-" + uuid.uuid4().hex[:8], "role": "viewer"}, headers=BOOT)
    assert made.status_code == 200
    assert wait_for(lambda: events(path="/users"))
    assert events(path="/users") == [("Owner (shared key)", "owner", "bootstrap", "POST", "/users", 200)]


def test_a_person_is_named_in_what_they_change(client):
    u, h = person("owner")
    client.post("/users", json={"name": "pytest-" + uuid.uuid4().hex[:8], "role": "viewer"}, headers=h)
    assert wait_for(lambda: events(actor_name=u["name"]))
    assert events(actor_name=u["name"]) == [(u["name"], "owner", "user", "POST", "/users", 200)]


def test_a_refused_attempt_is_recorded_too_but_a_wrong_key_is_not(client):
    u, h = person("viewer")
    assert client.post("/users", json={"name": "pytest-no", "role": "owner"}, headers=h).status_code == 403
    assert wait_for(lambda: events(actor_name=u["name"]))
    assert events(actor_name=u["name"]) == [(u["name"], "viewer", "user", "POST", "/users", 403)]
    before = len(events())
    assert client.post("/users", json={"name": "pytest-no", "role": "owner"}, headers={"x-api-key": "wrong"}).status_code == 401
    time.sleep(0.3)
    assert len(events()) == before


def test_a_failing_audit_write_never_breaks_a_request(client, monkeypatch):
    monkeypatch.setattr(audit, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    r = client.post("/users", json={"name": "pytest-" + uuid.uuid4().hex[:8], "role": "viewer"}, headers=BOOT)
    assert r.status_code == 200


def test_the_audit_route_is_for_admins_and_owners(client):
    admin, ha = person("admin")
    _, ho = person("operator")
    client.post("/users", json={"name": "pytest-" + uuid.uuid4().hex[:8], "role": "viewer"}, headers=BOOT)
    assert wait_for(lambda: events(path="/users"))
    assert client.get("/audit", headers=ho).status_code == 403
    got = client.get("/audit?actor=Owner%20(shared%20key)&limit=5", headers=ha)
    assert got.status_code == 200 and got.json()["events"] and all(e["actor"] == "Owner (shared key)" for e in got.json()["events"])
    assert client.get("/audit", headers=BOOT).status_code == 200


# ---------------- who decided on an action ----------------

def decide(client, action, how, headers):
    return client.post("/autonomy/pending/%s/%s" % (action, how), headers=headers)


def test_an_approval_names_the_person_who_gave_it(client, monkeypatch):
    monkeypatch.setattr(autonomy, "_send_via_channel", lambda c, t, content: "sent")
    u, h = person("operator")
    action = pending("approve-by-person")
    assert decide(client, action, "approve", h).status_code == 200
    assert log_row(MARK + "approve-by-person") == [(True, u["name"], "operator", "user", u["id"])]


def test_a_rejection_names_the_person_too(client):
    u, h = person("admin")
    action = pending("reject-by-person")
    assert decide(client, action, "reject", h).status_code == 200
    assert log_row(MARK + "reject-by-person") == [(False, u["name"], "admin", "user", u["id"])]


def test_the_shared_key_is_named_as_such(client, monkeypatch):
    monkeypatch.setattr(autonomy, "_send_via_channel", lambda c, t, content: "sent")
    action = pending("by-shared-key")
    assert decide(client, action, "approve", BOOT).status_code == 200
    assert log_row(MARK + "by-shared-key") == [(True, "Owner (shared key)", "owner", "bootstrap", None)]


def test_an_approval_in_the_owners_chat_is_recorded_as_the_chat(monkeypatch):
    monkeypatch.setattr(autonomy, "_send_via_channel", lambda c, t, content: "sent")
    monkeypatch.setattr(autonomy, "is_owner_sender", lambda channel, sender: True)
    action = pending("by-chat")
    assert autonomy.process_approval_from("telegram", "me", "YES " + action)
    assert log_row(MARK + "by-chat") == [(True, "the owner's telegram chat", "owner", "channel", None)]


def test_an_action_that_ran_on_its_own_has_no_person():
    autonomy.log_autonomous_action("send_message", "telegram", "chat1", MARK + "auto", "sent", approved=True, role="support")
    assert log_row(MARK + "auto") == [(True, None, None, None, None)]


def test_the_activity_log_shows_the_person(client, monkeypatch):
    monkeypatch.setattr(autonomy, "_send_via_channel", lambda c, t, content: "sent")
    u, h = person("operator")
    decide(client, pending("shown"), "approve", h)
    row = [r for r in office.autonomy_log(limit=50) if r["content"] == MARK + "shown"][0]
    assert (row["decided_by"], row["decided_by_role"], row["decided_kind"]) == (u["name"], "operator", "user")
    got = [r for r in client.get("/autonomy/log?limit=50", headers=h).json()["log"] if r["content"] == MARK + "shown"]
    assert got and got[0]["decided_by"] == u["name"]


# ---------------- a database without the new columns ----------------

class _Missing(Exception):
    pass


_Missing.__name__ = "UndefinedColumn"


def test_logging_still_works_before_the_schema_is_applied(monkeypatch):
    ran = []

    class Cur:
        def execute(self, sql, params=()):
            ran.append(sql)
            if "decided_by" in sql:
                raise _Missing("no column")

        def close(self):
            pass

    class Conn:
        rolled = False

        def cursor(self):
            return Cur()

        def rollback(self):
            Conn.rolled = True

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(autonomy, "get_connection", lambda: Conn())
    autonomy.log_autonomous_action("send_message", "telegram", "c", "body", "ok", approved=True,
                                   decided_by={"name": "Ana", "role": "admin", "kind": "user", "user_id": None})
    assert len(ran) == 2 and "decided_by" in ran[0] and "decided_by" not in ran[1] and Conn.rolled


def test_the_activity_log_still_reads_before_the_schema_is_applied(monkeypatch):
    class Cur:
        n = 0

        def execute(self, sql, params=()):
            Cur.n += 1
            if "decided_by" in sql:
                raise _Missing("no column")

        def fetchall(self):
            return [(1, "send_message", "telegram", "c", "body", "ok", True, None, "support")]

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cur()

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(office, "get_connection", lambda: Conn())
    rows = office.autonomy_log(limit=5)
    assert rows[0]["decided_by"] is None and rows[0]["role"] == "support" and Cur.n == 2
