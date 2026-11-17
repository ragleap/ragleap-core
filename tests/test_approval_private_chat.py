"""Approvals count only in a private chat, never from a group or server channel (F-CORE-006). No network."""
import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import HTTPException

from core import autonomy
from core.api import app, AutonomySettingsRequest
from channels.telegram import router as tg
from channels.discord import router as dc


def _cfg(channel="telegram", target="424242"):
    return patch.object(autonomy, "get_autonomy_settings", return_value={
        "mode": "semi", "channels": [], "actions": [], "approval_channel": channel, "approval_target": target})


def test_a_private_owner_chat_can_still_approve():
    with _cfg(), patch.object(autonomy, "process_approval_response", return_value="done") as real:
        assert autonomy.process_approval_from("telegram", 424242, "YES ABCD1234") == "done"
    assert real.called


def test_a_group_chat_never_approves_even_when_its_id_is_the_target():
    with _cfg(target="-100123"), patch.object(autonomy, "process_approval_response") as real:
        assert autonomy.process_approval_from("telegram", -100123, "YES ABCD1234", private=False) is None
    real.assert_not_called()


def test_the_person_who_wrote_must_be_the_chat_owner():
    with _cfg(), patch.object(autonomy, "process_approval_response") as real:
        assert autonomy.process_approval_from("telegram", 424242, "YES ABCD1234", actor=999) is None
        real.assert_not_called()
        real.return_value = "done"
        assert autonomy.process_approval_from("telegram", 424242, "YES ABCD1234", actor=424242) == "done"


def test_a_linked_person_cannot_approve_from_a_group_either():
    with _cfg(target="1"), patch.object(autonomy, "_linked_approver", return_value={"name": "x", "role": "operator"}), \
         patch.object(autonomy, "process_approval_response") as real:
        assert autonomy.process_approval_from("telegram", -5, "YES ABCD1234", private=False) is None
    real.assert_not_called()


class _Req:
    def __init__(self, body):
        self.headers = {}
        self._body = body

    async def json(self):
        return self._body


def _tg_hook(message):
    ep = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/webhook/telegram")
    with patch.object(tg, "_verify_webhook_secret", return_value=True), patch.object(tg, "handle_incoming_message") as h:
        asyncio.run(ep(_Req({"message": message})))
    return h


def test_the_telegram_webhook_marks_group_chats_as_not_private():
    h = _tg_hook({"chat": {"id": -100123, "type": "supergroup"}, "from": {"id": 7}, "text": "YES ABCD1234"})
    h.assert_called_once_with(-100123, "YES ABCD1234", private=False)
    h = _tg_hook({"chat": {"id": 424242, "type": "private"}, "from": {"id": 424242}, "text": "YES ABCD1234"})
    h.assert_called_once_with(424242, "YES ABCD1234")
    h = _tg_hook({"chat": {"id": 424242}, "text": "YES ABCD1234"})
    h.assert_called_once_with(424242, "YES ABCD1234", private=False)


def test_the_discord_webhook_marks_server_channels_as_not_private():
    ep = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/webhook/discord")
    with patch.object(dc, "verify_discord_signature", return_value=True), patch.object(dc, "handle_incoming_message") as h:
        asyncio.run(ep(_Req2({"channel_id": "55", "guild_id": "9", "content": "YES ABCD1234"})))
        h.assert_called_once_with("55", "YES ABCD1234", private=False)
    with patch.object(dc, "verify_discord_signature", return_value=True), patch.object(dc, "handle_incoming_message") as h:
        asyncio.run(ep(_Req2({"channel_id": "55", "content": "YES ABCD1234"})))
        h.assert_called_once_with("55", "YES ABCD1234")


class _Req2(_Req):
    async def body(self):
        return b"{}"


def test_a_telegram_group_cannot_be_saved_as_the_approval_target():
    set_fn = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/autonomy" and "POST" in getattr(r, "methods", ()))
    with pytest.raises(HTTPException) as e:
        set_fn(AutonomySettingsRequest(mode="semi", approval_channel="telegram", approval_target="-100123"))
    assert e.value.status_code == 400
