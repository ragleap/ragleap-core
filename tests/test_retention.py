"""Retention of the audit trail (core/retention.py) and the /retention routes. Real database (db/schema.sql applied);
test rows are removed afterwards."""
import logging
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from core import audit, document_store, retention, users

BOOT = {"x-api-key": "boot-key"}
MARK = "pytest-retention-"
OLD = datetime.now(timezone.utc) - timedelta(days=400)
NEW = datetime.now(timezone.utc) - timedelta(days=1)


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
def clean(monkeypatch):
    for name in ("AUDIT_RETENTION_DAYS", "ACTION_LOG_RETENTION_DAYS", "AGENT_RUN_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)
    users._clear_cache()
    first = _sql("SELECT COALESCE(MAX(id), 0) FROM audit_events", fetch=True)[0][0]
    yield
    _sql("DELETE FROM audit_events WHERE id > %s OR actor_name LIKE %s", (first, MARK + "%"))
    _sql("DELETE FROM autonomy_log WHERE content LIKE %s", (MARK + "%",))
    _sql("DELETE FROM agent_runs WHERE query LIKE %s", (MARK + "%",))
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


def _event(path, when):
    audit.record({"name": MARK + "a", "role": "admin", "kind": "user"}, "POST", path, 200)
    _sql("UPDATE audit_events SET at = %s WHERE path = %s AND actor_name = %s", (when, path, MARK + "a"))


def _run(tag, status, when):
    _sql("INSERT INTO agent_runs (query, status, created_at, updated_at) VALUES (%s, %s, %s, %s)",
         (MARK + tag, status, when, when))


def _log(tag, when):
    _sql("INSERT INTO autonomy_log (action_type, channel, target, content, result, created_at) "
         "VALUES ('send_email', 'email', 'a@b.c', %s, 'sent', %s)", (MARK + tag, when))


def _left(sql, mark_col="query"):
    return sorted(r[0] for r in _sql(sql, (MARK + "%",), fetch=True))


def test_the_days_setting_keeps_everything_unless_it_is_clear(monkeypatch):
    assert retention.days("AUDIT_RETENTION_DAYS") == 0 and not retention.enabled()
    for bad, want in (("", 0), ("0", 0), ("-5", 0), ("abc", 0), ("3", 0), ("6", 0), ("7", 7), (" 180 ", 180)):
        monkeypatch.setenv("AUDIT_RETENTION_DAYS", bad)
        assert retention.days("AUDIT_RETENTION_DAYS") == want
    assert retention.enabled()


def test_nothing_is_deleted_when_no_limit_is_set():
    _event("/old", OLD)
    out = retention.purge()
    assert all(t["deleted"] == 0 for t in out)
    assert _sql("SELECT count(*) FROM audit_events WHERE actor_name = %s", (MARK + "a",), fetch=True)[0][0] == 1


def test_old_audit_rows_go_and_new_ones_stay(monkeypatch):
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
    _event("/old", OLD)
    _event("/new", NEW)
    out = {t["table"]: t for t in retention.purge()}
    assert out["audit_events"]["deleted"] >= 1 and out["audit_events"]["days"] == 30
    rows = _sql("SELECT path FROM audit_events WHERE actor_name = %s", (MARK + "a",), fetch=True)
    assert [r[0] for r in rows] == ["/new"]


def test_finished_runs_go_but_running_and_waiting_runs_never(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_RETENTION_DAYS", "30")
    for tag, status in (("done", "done"), ("failed", "failed"), ("rejected", "rejected"), ("budget", "budget"),
                        ("running", "running"), ("waiting", "waiting_approval")):
        _run(tag, status, OLD)
    _run("recent", "done", NEW)
    retention.purge()
    left = _left("SELECT query FROM agent_runs WHERE query LIKE %s")
    assert left == [MARK + "recent", MARK + "running", MARK + "waiting"]


def test_the_activity_log_is_purged_by_its_own_setting(monkeypatch):
    monkeypatch.setenv("ACTION_LOG_RETENTION_DAYS", "30")
    _log("old", OLD)
    _log("new", NEW)
    retention.purge()
    assert _left("SELECT content FROM autonomy_log WHERE content LIKE %s") == [MARK + "new"]


def test_a_dry_run_counts_and_deletes_nothing(monkeypatch):
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
    _event("/old", OLD)
    out = {t["table"]: t for t in retention.purge(dry_run=True)}
    assert out["audit_events"]["rows_due"] >= 1 and out["audit_events"]["deleted"] == 0
    assert _sql("SELECT count(*) FROM audit_events WHERE actor_name = %s", (MARK + "a",), fetch=True)[0][0] == 1


def test_a_big_backlog_is_taken_in_batches(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_RETENTION_DAYS", "30")
    monkeypatch.setattr(retention, "BATCH", 2)
    for i in range(5):
        _run("b%d" % i, "done", OLD)
    retention.purge()
    assert _left("SELECT query FROM agent_runs WHERE query LIKE %s") == []


def test_a_purge_leaves_an_audit_log_line(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="ragleap.audit")
    monkeypatch.setenv("ACTION_LOG_RETENTION_DAYS", "30")
    _log("old", OLD)
    retention.purge(trigger="api")
    lines = [json.loads(r.getMessage().split("AUDIT ", 1)[1]) for r in caplog.records if r.name == "ragleap.audit"]
    mine = [x for x in lines if x["audit"] == "retention_purge" and x["table"] == "autonomy_log"]
    assert mine and mine[0]["deleted"] >= 1 and mine[0]["trigger"] == "api" and mine[0]["retention_days"] == 30


def test_a_database_error_never_raises(monkeypatch):
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
    monkeypatch.setattr(retention, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    out = {t["table"]: t for t in retention.purge()}
    assert out["audit_events"]["error"] == "RuntimeError" and out["audit_events"]["deleted"] == 0
    assert retention.status()[0].get("error") == "RuntimeError"


def test_the_routes_and_who_may_use_them(client, monkeypatch):
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
    _, admin = person("admin")
    _, operator = person("operator")
    _event("/old", OLD)
    assert client.get("/retention", headers=operator).status_code == 403
    got = client.get("/retention", headers=admin)
    assert got.status_code == 200 and got.json()["min_days"] == 7
    assert {t["table"] for t in got.json()["tables"]} == {"audit_events", "autonomy_log", "agent_runs"}
    assert client.post("/retention/purge", headers=admin).status_code == 403
    dry = client.post("/retention/purge", headers=BOOT)
    assert dry.status_code == 200 and dry.json()["dry_run"] is True
    assert _sql("SELECT count(*) FROM audit_events WHERE actor_name = %s", (MARK + "a",), fetch=True)[0][0] == 1
    real = client.post("/retention/purge?dry_run=false", headers=BOOT)
    assert real.status_code == 200 and real.json()["dry_run"] is False
    assert _sql("SELECT count(*) FROM audit_events WHERE actor_name = %s", (MARK + "a",), fetch=True)[0][0] == 0
