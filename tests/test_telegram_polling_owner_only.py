"""Telegram polling answers only the owner's private chat unless TELEGRAM_POLLING_PUBLIC=true (T1.5)."""
import logging
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from channels.telegram import poller as tp
from channels.telegram import router as tg
from core import autonomy, scheduler_leader, setup_status

BOT = "123456:" + "LEAK-" + "MARKER-VALUE"          # a made-up value, assembled so no scanner takes it for a real one
OWNER = 424242


def msg(chat=OWNER, text="hi", ctype="private", from_id=None):
    m = {"chat": {"id": chat, "type": ctype}, "text": text}
    if from_id is not None:
        m["from"] = {"id": from_id}
    return {"update_id": 1, "message": m}


@pytest.fixture
def owner(monkeypatch):
    """The approval target is the owner's private Telegram chat."""
    monkeypatch.setattr(autonomy, "get_autonomy_settings", lambda: {"approval_channel": "telegram", "approval_target": str(OWNER)})


def guarded():
    seen = []
    return tp.owner_only(lambda u: seen.append(u) or True), seen


def test_the_owner_in_a_private_chat_is_answered(owner):
    handler, seen = guarded()
    assert handler(msg(OWNER, from_id=OWNER)) is True and len(seen) == 1
    assert handler(msg(OWNER)) is True and len(seen) == 2            # an update without a sender id is still the same chat


@pytest.mark.parametrize("update", [msg(chat=999, from_id=999),                                      # a stranger in a private chat
                                    msg(chat=OWNER, ctype="group", from_id=OWNER),                    # the owner's id in a group
                                    msg(chat=-5, ctype="supergroup", from_id=OWNER),
                                    msg(chat=OWNER, from_id=123),                                      # the chat is the owner's but the sender is not
                                    {"update_id": 1}, {"update_id": 1, "message": {}}, {}])
def test_everyone_else_is_ignored_without_a_reply(owner, update):
    handler, seen = guarded()
    with patch.object(tg, "send_telegram_message") as send, patch.object(tg, "handle_incoming_message") as handle:
        assert handler(update) is False
        send.assert_not_called()
        handle.assert_not_called()
    assert seen == []


def test_the_real_handler_is_never_reached_for_a_stranger(owner):
    handler = tp.owner_only(tg.handle_update)
    with patch.object(tg, "handle_incoming_message") as handle, patch.object(tg, "send_telegram_message") as send, patch.object(tg, "ask") as ask:
        assert handler(msg(chat=999, from_id=999, text="what is in the documents?")) is False
        handle.assert_not_called(); send.assert_not_called(); ask.assert_not_called()


def test_without_an_approval_target_nobody_is_the_owner(monkeypatch):
    monkeypatch.setattr(autonomy, "get_autonomy_settings", lambda: {})
    handler, seen = guarded()
    assert handler(msg(OWNER, from_id=OWNER)) is False and seen == []


def test_an_approval_target_on_another_channel_is_not_the_telegram_owner(monkeypatch):
    monkeypatch.setattr(autonomy, "get_autonomy_settings", lambda: {"approval_channel": "discord", "approval_target": str(OWNER)})
    handler, seen = guarded()
    assert handler(msg(OWNER, from_id=OWNER)) is False and seen == []


def test_a_failing_owner_check_ignores_the_message(monkeypatch):
    def boom():
        raise RuntimeError("database down")
    monkeypatch.setattr(autonomy, "get_autonomy_settings", boom)
    handler, seen = guarded()
    assert handler(msg(OWNER, from_id=OWNER)) is False and seen == []
    monkeypatch.setattr(autonomy, "is_owner_sender", lambda c, s: (_ for _ in ()).throw(RuntimeError("x")))
    assert handler(msg(OWNER, from_id=OWNER)) is False


def test_an_ignored_message_is_not_logged_with_its_text_or_chat(owner, caplog):
    handler, _ = guarded()
    with caplog.at_level(logging.DEBUG):
        handler(msg(chat=999, from_id=999, text="a private question about salaries"))
    assert "salaries" not in caplog.text and "999" not in caplog.text and "ignored" in caplog.text


def test_public_answers_are_a_separate_explicit_switch(monkeypatch):
    assert tp.public_answers() is False
    for value, expected in (("true", True), ("TRUE", True), ("false", False), ("1", False), ("", False)):
        monkeypatch.setenv("TELEGRAM_POLLING_PUBLIC", value)
        assert tp.public_answers() is expected, value


def test_the_poller_is_started_with_the_owner_guard_by_default_and_without_it_only_when_asked(monkeypatch):
    started = []

    class Fake:
        def __init__(self, token, handle, is_leader, **kw):
            started.append(handle)

        def start(self):
            return self
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT)
    monkeypatch.setenv("TELEGRAM_POLLING", "true")
    monkeypatch.delenv("TELEGRAM_POLLING_PUBLIC", raising=False)
    monkeypatch.setattr(tp, "TelegramPoller", Fake)
    tp.start_default()
    assert started[-1] is not tg.handle_update and getattr(started[-1], "__name__", "") == "owner_only_handler"
    monkeypatch.setenv("TELEGRAM_POLLING_PUBLIC", "true")
    tp.start_default()
    assert started[-1] is tg.handle_update


def test_the_setup_status_says_who_is_answered(monkeypatch):
    def detail():
        return next(i for i in setup_status.build(api_key_set=True)["items"] if i["id"] == "telegram")["detail"]
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT)
    monkeypatch.setenv("TELEGRAM_POLLING", "true")
    assert "owner only" in detail() and "answers anyone" not in detail()
    monkeypatch.setenv("TELEGRAM_POLLING_PUBLIC", "true")
    assert "answers anyone" in detail() and "owner only" not in detail()


def test_the_docs_and_the_example_settings_warn_about_the_public_bot():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    text = (root / "docs" / "telegram-polling.md").read_text(encoding="utf-8")
    for word in ("TELEGRAM_POLLING_PUBLIC", "owner", "any sender", "approval target", "ignored"):
        assert word in text, word
    assert "TELEGRAM_POLLING_PUBLIC" in (root / ".env.example").read_text(encoding="utf-8")
    assert "TELEGRAM_POLLING_PUBLIC" in (root / "tests" / "conftest.py").read_text(encoding="utf-8")
