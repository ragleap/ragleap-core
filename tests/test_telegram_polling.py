"""Telegram long polling and the shared update handler (T1). A scripted fake stands in for Telegram; nothing touches the network."""
import asyncio
import collections
import inspect
import logging
import os
import pathlib
import sys
import threading
import time
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from channels.telegram import poller as tp
from channels.telegram import router as tg
from core import setup_status
from core.api import app

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOT = "123456:" + "LEAK-" + "MARKER-VALUE"       # a made-up value, assembled so no scanner takes it for a real one


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}

    def json(self):
        return self._body


class Fake:
    """A scripted Telegram: per-method queues of responses or exceptions; every call is recorded."""

    def __init__(self, **scripts):
        self.calls = []
        self.scripts = {m: collections.deque(v) for m, v in scripts.items()}

    def __call__(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json, timeout, url))
        q = self.scripts.get(method)
        item = q.popleft() if q else (Resp(200, {"ok": True, "result": []}) if method == "getUpdates" else Resp(200, {"ok": True, "result": {"url": ""}}))
        if isinstance(item, Exception):
            raise item
        return item

    def methods(self):
        return [c[0] for c in self.calls]


def updates(*items):
    return Resp(200, {"ok": True, "result": list(items)})


def msg(uid, chat=100, text="hi", ctype="private", from_id=None):
    m = {"chat": {"id": chat, "type": ctype}, "text": text}
    if from_id is not None:
        m["from"] = {"id": from_id}
    return {"update_id": uid, "message": m}


@pytest.fixture(autouse=True)
def _reset_state():
    tp._STATE.update(mode="off", handled=0, last_error="")
    yield


def poller(fake, handler=None, leader=lambda: True, **kw):
    seen = []
    handler = handler or (lambda u: seen.append(u["update_id"]))
    p = tp.TelegramPoller(BOT, handler, leader, http_post=fake, inline=kw.pop("inline", True), **kw)
    p.seen = seen
    return p


# ---------------------------------------------------------------- switch

def test_polling_is_off_by_default_and_needs_the_token(monkeypatch):
    assert tp.enabled() is False
    monkeypatch.setenv("TELEGRAM_POLLING", "true")
    assert tp.enabled() is False                                       # no token
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT)
    assert tp.enabled() is True
    monkeypatch.setenv("TELEGRAM_POLLING", "false")
    assert tp.enabled() is False and tp.start_default() is None


# ---------------------------------------------------------------- one round

def test_a_round_fetches_handles_and_acknowledges_with_the_next_offset():
    fake = Fake(getUpdates=[updates(msg(10), msg(11)), updates()])
    p = poller(fake)
    assert p.step() == 0.0
    assert p.seen == [10, 11] and tp.status()["mode"] == "polling" and tp.status()["handled"] == 2
    p.step()
    first, second = [c for c in fake.calls if c[0] == "getUpdates"]
    assert "offset" not in first[1] and second[1]["offset"] == 12      # acknowledged only after both were handled
    assert first[1]["allowed_updates"] == ["message"] and first[1]["timeout"] == 30 and first[2] == 40


def test_the_webhook_is_checked_once_and_polling_never_starts_while_one_is_registered():
    fake = Fake(getWebhookInfo=[Resp(200, {"ok": True, "result": {"url": "https://example.com/hook"}}),
                                Resp(200, {"ok": True, "result": {"url": ""}})])
    p = poller(fake)
    assert p.step() == 60.0 and tp.status()["mode"] == "webhook_registered"
    assert "getUpdates" not in fake.methods() and "deleteWebhook" not in fake.methods()       # and it never deletes the webhook
    p.step()                                                           # the owner removed it: polling starts
    p.step()
    assert fake.methods().count("getWebhookInfo") == 2 and fake.methods().count("getUpdates") == 2


def test_only_the_leader_polls_and_a_new_leader_checks_the_webhook_again():
    fake = Fake()
    led = {"yes": False}
    p = poller(fake, leader=lambda: led["yes"])
    assert p.step() == 5.0 and fake.calls == [] and tp.status()["mode"] == "waiting_for_leader"
    led["yes"] = True
    p.step()
    assert fake.methods() == ["getWebhookInfo", "getUpdates"]
    led["yes"] = False
    p.step()
    led["yes"] = True
    p.step()
    assert fake.methods().count("getWebhookInfo") == 2


def test_a_conflict_waits_and_checks_the_webhook_again():
    fake = Fake(getUpdates=[Resp(409, {"ok": False})])
    p = poller(fake)
    assert p.step() == 30.0 and tp.status()["mode"] == "conflict"
    p.step()
    assert fake.methods().count("getWebhookInfo") == 2


def test_a_refused_token_waits_long_and_a_rate_limit_waits_as_told():
    assert poller(Fake(getWebhookInfo=[Resp(401, {})])).step() == 300.0 and tp.status()["mode"] == "bad_token"
    assert poller(Fake(getUpdates=[Resp(401, {})])).step() == 300.0
    assert poller(Fake(getUpdates=[Resp(429, {"ok": False, "parameters": {"retry_after": 17}})])).step() == 17.0
    assert poller(Fake(getUpdates=[Resp(429, {"ok": False, "parameters": {"retry_after": 9999}})])).step() == 120.0


def test_errors_back_off_and_a_good_round_resets_it():
    fake = Fake(getUpdates=[Resp(500, {}), requests.ConnectionError("x"), Resp(500, {}), updates(msg(1))])
    p = poller(fake)
    assert [p.step(), p.step(), p.step()] == [2.0, 4.0, 8.0]
    assert p.step() == 0.0
    fake.scripts["getUpdates"].append(Resp(500, {}))
    assert p.step() == 2.0
    for _ in range(10):
        fake.scripts["getUpdates"].append(Resp(500, {}))
    assert max(p.step() for _ in range(10)) == 60.0                     # capped


def test_the_bot_token_never_reaches_a_log_or_the_status(caplog):
    boom = requests.ConnectionError("HTTPSConnectionPool(host='api.telegram.org'): /bot%s/getUpdates failed" % BOT)
    with caplog.at_level(logging.DEBUG):
        p = poller(Fake(getUpdates=[boom, updates(msg(1))]), handler=lambda u: (_ for _ in ()).throw(RuntimeError("bad text " + BOT)))
        p.step()
        assert tp.status()["last_error"] == "ConnectionError"
        p.step()
    assert "LEAK-MARKER-VALUE" not in caplog.text and "LEAK-MARKER-VALUE" not in str(tp.status())


def test_a_redelivered_update_is_handled_once():
    fake = Fake(getUpdates=[updates(msg(5)), updates(msg(5), msg(6))])
    p = poller(fake)
    p.step()
    p.step()
    assert p.seen == [5, 6]


def test_a_failing_handler_does_not_block_the_queue_and_is_acknowledged():
    def handler(u):
        if u["update_id"] == 1:
            raise RuntimeError("boom")
        seen.append(u["update_id"])
    seen = []
    fake = Fake(getUpdates=[updates(msg(1), msg(2)), updates()])
    p = poller(fake, handler=handler)
    p.step()
    p.step()
    assert seen == [2] and [c for c in fake.calls if c[0] == "getUpdates"][1][1]["offset"] == 3


def test_malformed_updates_are_skipped():
    p = poller(Fake(getUpdates=[updates({"message": {}}, {"update_id": "x"}, msg(3))]))
    p.step()
    assert p.seen == [3]


# ---------------------------------------------------------------- concurrency

def _wait_for(cond, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_the_offset_stays_at_the_oldest_unfinished_update():
    release, done2 = threading.Event(), threading.Event()

    def handler(u):
        if u["update_id"] == 1:
            release.wait(5)
        else:
            done2.set()
    fake = Fake(getUpdates=[updates(msg(1, chat=1), msg(2, chat=2)), updates(), updates()])
    p = poller(fake, handler=handler, inline=False, workers=2)
    try:
        p.step()
        assert done2.wait(3) and _wait_for(lambda: p._inflight == {1})
        p.step()
        assert [c for c in fake.calls if c[0] == "getUpdates"][1][1]["offset"] == 1       # update 1 is not acknowledged yet
        release.set()
        assert _wait_for(lambda: not p._inflight)
        p.step()
        assert [c for c in fake.calls if c[0] == "getUpdates"][2][1]["offset"] == 3
    finally:
        release.set()
        p.stop()


def test_messages_of_one_chat_are_handled_in_order_and_other_chats_are_not_held_up():
    gate, order = threading.Event(), []

    def handler(u):
        if u["update_id"] == 1:
            gate.wait(5)
        order.append(u["update_id"])
    fake = Fake(getUpdates=[updates(msg(1, chat=7), msg(2, chat=7), msg(3, chat=8))])
    p = poller(fake, handler=handler, inline=False, workers=4)
    try:
        p.step()
        assert _wait_for(lambda: order == [3])                          # another chat went through while chat 7 waits
        gate.set()
        assert _wait_for(lambda: order == [3, 1, 2])                    # and chat 7 kept its order
    finally:
        gate.set()
        p.stop()


def test_the_thread_starts_and_stops_cleanly():
    def slow(url, json=None, timeout=None):
        time.sleep(0.02)
        return Resp(200, {"ok": True, "result": {"url": ""}}) if url.endswith("getWebhookInfo") else Resp(200, {"ok": True, "result": []})
    p = tp.TelegramPoller(BOT, lambda u: None, lambda: True, http_post=slow, inline=True).start()
    assert _wait_for(lambda: tp.status()["mode"] == "polling")
    p.stop(2.0)
    assert not p._thread.is_alive() and tp.status()["mode"] == "off"


# ---------------------------------------------------------------- the shared handler and the webhook route

def test_handle_update_applies_the_private_chat_rules_for_both_entry_points():
    with patch.object(tg, "handle_incoming_message") as h:
        assert tg.handle_update(msg(1, chat=42, text="a", from_id=42)) is True
        h.assert_called_once_with(42, "a")
    with patch.object(tg, "handle_incoming_message") as h:
        tg.handle_update(msg(1, chat=-5, text="a", ctype="supergroup", from_id=42))
        h.assert_called_once_with(-5, "a", private=False)
    with patch.object(tg, "handle_incoming_message") as h:
        tg.handle_update(msg(1, chat=42, text="a", from_id=99))          # sender is not the chat: not a private conversation
        h.assert_called_once_with(42, "a", private=False)
    with patch.object(tg, "handle_incoming_message") as h:
        assert tg.handle_update({"update_id": 1, "edited_message": {}}) is False and tg.handle_update({}) is False
        h.assert_not_called()


def test_the_poller_hands_updates_to_the_same_function_the_webhook_uses():
    with patch.object(tg, "handle_incoming_message") as h:
        p = tp.TelegramPoller(BOT, tg.handle_update, lambda: True, http_post=Fake(getUpdates=[updates(msg(1, chat=42, text="q", from_id=42))]), inline=True)
        p.step()
    h.assert_called_once_with(42, "q")


class _Req:
    headers = {"x-telegram-bot-api-secret-token": "s3cret"}

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def test_the_webhook_route_no_longer_runs_the_handler_on_the_event_loop():
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/webhook/telegram")
    ident = {}

    def handler(chat_id, text, private=True):
        ident["thread"] = threading.get_ident()
    with patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", "s3cret"), patch.object(tg, "handle_incoming_message", handler):
        out = asyncio.run(endpoint(_Req(msg(1, chat=42, text="x", from_id=42))))
    assert out == {"ok": True} and ident["thread"] != threading.get_ident()


def test_the_webhook_route_still_checks_the_secret_first():
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/webhook/telegram")

    class Bad(_Req):
        headers = {"x-telegram-bot-api-secret-token": "wrong"}
    with patch.object(tg, "TELEGRAM_WEBHOOK_SECRET", "s3cret"), patch.object(tg, "handle_update") as h:
        with pytest.raises(Exception) as e:
            asyncio.run(endpoint(Bad(msg(1))))
        assert getattr(e.value, "status_code", None) == 403
        h.assert_not_called()


# ---------------------------------------------------------------- wiring and docs

def test_the_app_starts_and_stops_the_poller_in_its_lifespan():
    src = inspect.getsource(__import__("core.api", fromlist=["lifespan"]).lifespan)
    assert "_tg_poller.start_default" in src and "tg_poller.stop" in src
    assert src.index("_tg_poller.start_default") < src.index("yield") < src.index("tg_poller.stop")


def test_the_setup_status_says_how_telegram_messages_arrive(monkeypatch):
    def detail():
        return next(i for i in setup_status.build(api_key_set=True)["items"] if i["id"] == "telegram")["detail"]
    assert "Receiving: webhook only" in detail()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT)
    monkeypatch.setenv("TELEGRAM_POLLING", "true")
    assert "Receiving: long polling" in detail() and "LEAK-MARKER" not in detail()


def test_the_docs_page_and_the_example_settings_describe_polling():
    text = (ROOT / "docs" / "telegram-polling.md").read_text(encoding="utf-8")
    for word in ("TELEGRAM_POLLING", "getUpdates", "webhook", "leader", "at-least-once", "TELEGRAM_POLL_TIMEOUT", "TELEGRAM_POLL_WORKERS",
                 "private chat", "never deletes"):
        assert word in text, word
    assert "{{" not in text and "{%" not in text and not text.startswith("---")
    assert "TELEGRAM_POLLING=true" in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_polling_flags_are_cleared_between_tests():
    for name in ("TELEGRAM_POLLING", "TELEGRAM_POLL_TIMEOUT", "TELEGRAM_POLL_WORKERS"):
        assert name not in os.environ
