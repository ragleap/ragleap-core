"""File effects always need approval, whatever label a caller sends (F-CORE-004). No database, no network."""
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import autonomy
from core import api

PAIRS = [("write_file", "files_write"), ("delete_file", "files_delete"), ("request_access", "files_request")]


class _Cur:
    def execute(self, *a, **k):
        pass

    def close(self):
        pass


class _Conn:
    def cursor(self):
        return _Cur()

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def full_mode(monkeypatch):
    monkeypatch.setattr(autonomy, "get_autonomy_settings", lambda: {
        "mode": "full", "actions": [], "channels": [], "approval_channel": "", "approval_target": ""})
    monkeypatch.setattr(autonomy, "get_connection", lambda: _Conn())
    monkeypatch.setattr(autonomy, "request_approval", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(autonomy, "_send_via_channel", lambda *a, **k: sent.append(a) or "ran")
    return sent


@pytest.mark.parametrize("label", ["create_task", "send_slack", "anything"])
@pytest.mark.parametrize("channel", ["files_write", "files_delete", "files_request"])
def test_a_harmless_label_cannot_carry_a_file_channel(full_mode, label, channel):
    res = autonomy.execute_or_request(action_type=label, channel=channel, target="notes", content="x")
    assert res["status"] == "error"
    assert full_mode == []


@pytest.mark.parametrize("label,channel", PAIRS)
def test_a_file_label_cannot_use_another_channel(full_mode, label, channel):
    res = autonomy.execute_or_request(action_type=label, channel="slack", target="notes", content="x")
    assert res["status"] == "error"
    assert full_mode == []


def test_valid_pair_is_not_refused_by_the_pair_check():
    for label, channel in PAIRS:
        assert autonomy.file_pair_error(label, channel) is None
    assert autonomy.file_pair_error("send_slack", "slack") is None


def test_request_access_still_waits_for_approval_in_full_mode(full_mode):
    res = autonomy.execute_or_request(action_type="request_access", channel="files_request", target="notes", content="x")
    assert res["status"] == "pending_approval"
    assert full_mode == []


def test_forced_approval_covers_every_file_channel():
    assert set(autonomy._FORCED_CHANNELS) == {"files_write", "files_delete", "files_request"}
    assert set(autonomy._FORCED_ACTION_TYPES) == {"write_file", "delete_file", "request_access"}


def test_the_execute_route_refuses_a_mismatched_pair():
    req = api.AutonomyActionRequest(action_type="create_task", channel="files_write", target="notes", content="x")
    with pytest.raises(HTTPException) as e:
        api.execute_autonomous_action(req)
    assert e.value.status_code == 400
