"""The request id in log lines: core/request_context.py, the AUDIT lines and the opt-in app log format."""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from core import audit, document_store, request_context

BOOT = {"x-api-key": "boot-key"}


def _lines(caplog):
    return [json.loads(r.getMessage().split("AUDIT ", 1)[1]) for r in caplog.records if r.name == "ragleap.audit"]


@pytest.fixture
def client(monkeypatch):
    from core import api
    monkeypatch.setattr(api, "RAGLEAP_API_KEY", "boot-key")
    return TestClient(api.app)


def test_bind_current_and_unbind():
    assert request_context.current() == ""
    token = request_context.bind("abc123")
    assert request_context.current() == "abc123"
    request_context.unbind(token)
    assert request_context.current() == ""


def test_audit_lines_carry_the_bound_id_and_an_explicit_one_wins(caplog):
    caplog.set_level(logging.INFO, logger="ragleap.audit")
    audit.emit("action_proposed", action_id="a1")
    token = request_context.bind("req-777")
    try:
        audit.emit("action_proposed", action_id="a2")
        audit.emit("action_proposed", action_id="a3", request_id="mine")
    finally:
        request_context.unbind(token)
    plain, bound, explicit = _lines(caplog)
    assert "request_id" not in plain
    assert bound["request_id"] == "req-777"
    assert explicit["request_id"] == "mine"


def test_the_log_filter_and_the_opt_in_format(monkeypatch):
    monkeypatch.delenv("LOG_REQUEST_ID", raising=False)
    assert "format" not in request_context.log_config()
    monkeypatch.setenv("LOG_REQUEST_ID", "true")
    cfg = request_context.log_config()
    assert "%(request_id)s" in cfg["format"]
    handler = cfg["handlers"][0]
    handler.setFormatter(logging.Formatter(cfg["format"]))
    rec = logging.LogRecord("core.api", logging.INFO, __file__, 1, "hello", None, None)
    assert handler.filter(rec)
    assert handler.format(rec) == "INFO:core.api:[-] hello"
    token = request_context.bind("req-42")
    try:
        assert handler.filter(rec) and handler.format(rec) == "INFO:core.api:[req-42] hello"
    finally:
        request_context.unbind(token)


def test_a_call_gets_its_id_back_and_the_audit_line_inside_it_carries_it(client, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="ragleap.audit")
    monkeypatch.setenv("ACTION_LOG_RETENTION_DAYS", "30")
    old = datetime.now(timezone.utc) - timedelta(days=400)
    conn = document_store._connect()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO autonomy_log (action_type, channel, target, content, result, created_at) "
                    "VALUES ('send_email', 'email', 'a@b.c', 'pytest-reqid-old', 'sent', %s)", (old,))
        conn.commit()
    finally:
        conn.close()
    try:
        got = client.post("/retention/purge?dry_run=false", headers=dict(BOOT, **{"x-request-id": "call-0001-abc"}))
        assert got.status_code == 200 and got.headers["X-Request-ID"] == "call-0001-abc"
        mine = [x for x in _lines(caplog) if x["audit"] == "retention_purge" and x["table"] == "autonomy_log"]
        assert mine and mine[0]["request_id"] == "call-0001-abc"
    finally:
        conn = document_store._connect()
        try:
            conn.cursor().execute("DELETE FROM autonomy_log WHERE content LIKE 'pytest-reqid-%'")
            conn.commit()
        finally:
            conn.close()


def test_an_unsafe_incoming_id_is_replaced_and_nothing_leaks_between_calls(client):
    bad = client.get("/health", headers={"x-request-id": "bad id with spaces\t"})
    ok = client.get("/health")
    assert bad.headers["X-Request-ID"] != "bad id with spaces\t" and len(bad.headers["X-Request-ID"]) >= 8
    assert ok.headers["X-Request-ID"] != bad.headers["X-Request-ID"]
    assert request_context.current() == ""
