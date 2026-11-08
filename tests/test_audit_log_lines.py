"""Structured audit log lines: one JSON line per agent-audit event, content only as a sha256."""
import hashlib
import json
import logging

from core import audit, autonomy

BODY = "send the invoice to the customer"
BODY_SHA = hashlib.sha256(BODY.encode()).hexdigest()


class FakeCur:
    def execute(self, *a, **k): pass
    def close(self): pass


class FakeConn:
    def cursor(self): return FakeCur()
    def commit(self): pass
    def close(self): pass


def _lines(caplog):
    out = []
    for r in caplog.records:
        if r.name == "ragleap.audit":
            out.append(json.loads(r.getMessage().split("AUDIT ", 1)[1]))
    return out


def test_emit_writes_hash_and_never_the_text(caplog):
    caplog.set_level(logging.INFO, logger="ragleap.audit")
    audit.emit("action_proposed", content=BODY, action_id="AB12CD34", role="sales")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert BODY not in text
    (line,) = _lines(caplog)
    assert line["content_sha256"] == BODY_SHA and line["action_id"] == "AB12CD34"
    assert line["audit"] == "action_proposed" and line["ts"].endswith("Z")


def test_emit_can_be_turned_off_and_never_raises(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="ragleap.audit")
    monkeypatch.setenv("AUDIT_LOG_LINES", "false")
    audit.emit("x", content=BODY)
    assert _lines(caplog) == []
    monkeypatch.delenv("AUDIT_LOG_LINES")
    audit.emit("x", content=object(), weird=object(), n=3, none=None, empty="")
    (line,) = _lines(caplog)
    assert line["n"] == 3 and "none" not in line and "empty" not in line


def test_execution_and_rejection_lines_carry_the_approver(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="ragleap.audit")

    monkeypatch.setattr(autonomy, "get_connection", lambda: FakeConn())
    who = {"name": "Asha", "role": "owner", "kind": "personal_key", "user_id": "u1"}
    autonomy.log_autonomous_action("send_email", "email", "a@b.c", BODY, "sent", approved=True, role="sales",
                                   decided_by=who, action_id="AB12CD34")
    autonomy.log_autonomous_action("send_email", "email", "a@b.c", BODY, "REJECTED by owner", approved=False,
                                   decided_by=who, action_id="EF56GH78")
    autonomy.log_autonomous_action("send_email", "email", "a@b.c", BODY, "ERROR: boom", approved=True)
    ok, rej, err = _lines(caplog)
    assert ok["audit"] == "action_executed" and ok["approver"] == "Asha" and ok["autonomous"] is False
    assert ok["content_sha256"] == BODY_SHA and ok["action_id"] == "AB12CD34"
    assert rej["audit"] == "action_rejected" and rej["action_id"] == "EF56GH78"
    assert err["audit"] == "action_error" and err["autonomous"] is True and "approver" not in err


def test_skip_and_proposal_lines(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="ragleap.audit")
    monkeypatch.setattr(autonomy, "get_autonomy_settings",
                        lambda: {"mode": "off", "actions": [], "channels": []})
    assert autonomy.execute_or_request("send_email", "email", "a@b.c", BODY)["status"] == "skipped"
    monkeypatch.setattr(autonomy, "get_autonomy_settings",
                        lambda: {"mode": "semi", "actions": ["notify"], "channels": []})
    autonomy.execute_or_request("send_email", "email", "a@b.c", BODY)

    monkeypatch.setattr(autonomy, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(autonomy, "request_approval", lambda *a, **k: True)
    monkeypatch.setattr(autonomy, "get_autonomy_settings",
                        lambda: {"mode": "semi", "actions": [], "channels": []})
    res = autonomy.execute_or_request("send_email", "email", "a@b.c", BODY, role="sales")
    off, not_allowed, proposed = _lines(caplog)
    assert off["audit"] == "action_skipped" and off["reason"] == "autonomy off"
    assert not_allowed["reason"] == "action not allowed"
    assert proposed["audit"] == "action_proposed" and proposed["action_id"] == res["action_id"]
    assert proposed["content_sha256"] == BODY_SHA
