"""The voice WebSocket server needs the secret token in its address: real sockets on 127.0.0.1, with the call handler replaced by a
stub so no speech or model service is involved."""
import asyncio
import re
from pathlib import Path

import pytest
import websockets
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from channels.voice import auth, server

ROOT = Path(__file__).resolve().parent.parent
GOOD = "g00d-voice-t0ken-0123456789abcdef"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("VOICE_WS_TOKEN", raising=False)
    monkeypatch.delenv("VOICE_MAX_CALLS", raising=False)
    monkeypatch.delenv("VOICE_MAX_CALL_MINUTES", raising=False)
    monkeypatch.setattr(server, "_active", 0)
    monkeypatch.setattr(server, "_last_refusal_log", [None])


async def _with_server(body, stub):
    async def handler(ws):
        await server.serve_call(ws)
    original = server.handle_call
    server.handle_call = stub
    try:
        async with websockets.serve(handler, "127.0.0.1", 0, process_request=server.check_request, max_size=auth.MAX_MESSAGE_BYTES) as s:
            port = s.sockets[0].getsockname()[1]
            return await body("ws://127.0.0.1:%d" % port)
    finally:
        server.handle_call = original


def run(body, stub=None):
    async def echo(ws):
        async for message in ws:
            await ws.send("echo:" + message)
    return asyncio.run(_with_server(body, stub or echo))


async def refused_with(url):
    try:
        async with connect(url):
            return None
    except InvalidStatus as e:
        return e.response.status_code


def test_with_no_token_configured_every_call_is_refused(monkeypatch):
    async def body(base):
        return [await refused_with(base + "/anything"), await refused_with(base), await refused_with(base + "/" + GOOD)]
    assert run(body) == [401, 401, 401]


def test_a_token_shorter_than_16_characters_counts_as_not_set(monkeypatch):
    monkeypatch.setenv("VOICE_WS_TOKEN", "short-token")
    assert auth.configured_token() == ""
    async def body(base):
        return await refused_with(base + "/short-token")
    assert run(body) == 401


def test_a_missing_or_wrong_token_never_becomes_a_call(monkeypatch):
    monkeypatch.setenv("VOICE_WS_TOKEN", GOOD)
    async def body(base):
        return [await refused_with(base), await refused_with(base + "/"), await refused_with(base + "/wrong"),
                await refused_with(base + "/" + GOOD.upper()), await refused_with(base + "/" + GOOD + "x"),
                await refused_with(base + "/" + GOOD + "/more"), await refused_with(base + "/more/" + GOOD),
                await refused_with(base + "/?token=" + GOOD)]
    assert run(body) == [401] * 8


def test_the_right_token_opens_a_call(monkeypatch):
    monkeypatch.setenv("VOICE_WS_TOKEN", GOOD)
    async def body(base):
        seen = []
        for suffix in ("/" + GOOD, "/" + GOOD + "/", "/" + GOOD + "?from=twilio"):
            async with connect(base + suffix) as ws:
                await ws.send("hi")
                seen.append(await asyncio.wait_for(ws.recv(), 3))
        return seen
    assert run(body) == ["echo:hi"] * 3


def test_serve_call_checks_again_when_a_connection_skips_the_first_check(monkeypatch):
    monkeypatch.setenv("VOICE_WS_TOKEN", GOOD)
    calls, closed = [], []

    class FakeRequest:
        path = "/wrong"

    class FakeWs:
        request = FakeRequest()
        async def close(self, code=1000, reason=""):
            closed.append(code)

    async def stub(ws):
        calls.append(1)
    original = server.handle_call
    server.handle_call = stub
    try:
        asyncio.run(server.serve_call(FakeWs()))
    finally:
        server.handle_call = original
    assert closed == [1008] and calls == []


def test_too_many_calls_at_once_are_turned_away(monkeypatch):
    monkeypatch.setenv("VOICE_WS_TOKEN", GOOD)
    monkeypatch.setenv("VOICE_MAX_CALLS", "1")
    async def hold(ws):
        async for _ in ws:
            pass
    async def body(base):
        async with connect(base + "/" + GOOD):
            await asyncio.sleep(0.2)
            async with connect(base + "/" + GOOD) as second:
                try:
                    await asyncio.wait_for(second.recv(), 3)
                except ConnectionClosed as e:
                    return e.rcvd.code
        return None
    assert run(body, hold) == 1013


def test_a_call_that_runs_too_long_is_ended(monkeypatch):
    monkeypatch.setenv("VOICE_WS_TOKEN", GOOD)
    monkeypatch.setattr(auth, "max_call_seconds", lambda: 0.3)
    async def forever(ws):
        await asyncio.sleep(30)
    async def body(base):
        async with connect(base + "/" + GOOD) as ws:
            try:
                await asyncio.wait_for(ws.recv(), 5)
            except ConnectionClosed as e:
                return e.rcvd.code
        return None
    assert run(body, forever) == 1000


def test_an_oversized_message_closes_the_connection(monkeypatch):
    monkeypatch.setenv("VOICE_WS_TOKEN", GOOD)
    async def body(base):
        async with connect(base + "/" + GOOD) as ws:
            await ws.send("x" * (auth.MAX_MESSAGE_BYTES + 1000))
            try:
                await asyncio.wait_for(ws.recv(), 3)
            except ConnectionClosed as e:
                return e.rcvd.code
        return None
    assert run(body) == 1009


def test_the_limits_have_sane_bounds(monkeypatch):
    assert auth.max_calls() == 10 and auth.max_call_seconds() == 1800
    monkeypatch.setenv("VOICE_MAX_CALLS", "100000")
    monkeypatch.setenv("VOICE_MAX_CALL_MINUTES", "0")
    assert auth.max_calls() == 100 and auth.max_call_seconds() == 60
    monkeypatch.setenv("VOICE_MAX_CALLS", "junk")
    assert auth.max_calls() == 10


def test_the_token_is_compared_in_constant_time_and_never_logged():
    source = (ROOT / "channels" / "voice" / "auth.py").read_text()
    assert "hmac.compare_digest" in source
    for line in (ROOT / "channels" / "voice" / "server.py").read_text().splitlines():
        if "logger." in line:
            assert "configured_token()" not in line and "request.path" not in line


def test_a_refusal_is_logged_at_most_once_a_minute_without_the_token(monkeypatch, caplog):
    monkeypatch.setenv("VOICE_WS_TOKEN", GOOD)
    import logging
    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        for _ in range(5):
            server._log_refusal()
    assert len([r for r in caplog.records if "refused a connection" in r.getMessage()]) == 1
    assert GOOD not in caplog.text


def test_spoken_feedback_no_longer_changes_shared_memory():
    source = (ROOT / "channels" / "voice" / "router.py").read_text()
    for name in ("detect_feedback_command", "record_role_memory_outcome", "get_last_reply"):
        assert name not in source
    assert "record_last_reply" in source


def test_the_installer_the_example_file_and_the_readme_cover_the_token():
    assert re.search(r'set_env VOICE_WS_TOKEN "\$\(rand_hex 24\)"', (ROOT / "install.sh").read_text())
    assert re.search(r"^VOICE_WS_TOKEN=$", (ROOT / ".env.example").read_text(), re.M)
    readme = (ROOT / "README.md").read_text()
    assert "wss://your-domain.com:8765/<your VOICE_WS_TOKEN>" in readme and "refuses every call" in readme
