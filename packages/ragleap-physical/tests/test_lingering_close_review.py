"""Tests for the lingering close (_linger.py and its two call sites in the MCP server and the console).

Three tests fail on purpose today and describe open points: a refused client that keeps its socket open holds the next client, a stalled client
holds the console's stop, and the standard library's own error answers are not lingered. The others pass and guard the half-close, the time bound,
parallel refusals, a client that never reads the answer, and the console's success path. Linux cannot show the Windows reset itself, so these tests
check its causes and its cost. The servers handle one request at a time: what one client does while it is served is what the next client waits for.
Timing bounds are wide on purpose (shared runners are slow); where a break only changes a small time, the test enlarges the time it checks."""
import socket
import threading
import time
import urllib.request

import pytest

from ragleap_physical import _linger

from ragleap_physical.console import MAX_BODY as CONSOLE_MAX

from helpers import make
from test_console import ALICE, Running as ConsoleRunning
from test_mcp_server import Running as McpRunning, setup as mcp_setup

TOKEN = "t" * 20
PING = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}'


def raw_request(port, path, token, body=b"", content_length=None, method="POST"):
    n = len(body) if content_length is None else content_length
    head = ("%s %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
            % (method, path, port, token, n)).encode()
    return head + body


def ping_latency(port):
    started = time.monotonic()
    s = socket.create_connection(("127.0.0.1", port), timeout=30)
    s.sendall(raw_request(port, "/mcp", TOKEN, PING))
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    assert data.startswith(b"HTTP/1.0 200"), data[:60]
    return time.monotonic() - started


def wait_for(predicate, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# Cost of the bounded lingering close: a refused client that keeps its socket open holds the next client for the linger time (head-of-line blocking).
# Risk: with one request at a time, N such clients delay everyone, including a valid request, by N x 0.5 s. (Measured on loopback: 1 client 0.50 s, 5 clients 2.5 s.)
def test_a_refusal_whose_client_keeps_its_socket_open_holds_the_next_client_for_at_most_the_linger_bound():
    t, _, _ = mcp_setup()
    with McpRunning(t, auth_token=TOKEN) as s:
        port = s.server.server_address[1]
        first = socket.create_connection(("127.0.0.1", port), timeout=30)
        try:
            first.sendall(raw_request(port, "/mcp", "wrong" * 5, PING))
            assert first.recv(65536).startswith(b"HTTP/1.0 401")
            assert ping_latency(port) < 5.0, "the second client waited much longer than the linger bound"  # about 0.5 s expected; 10x the bound, so a linger of 10 s still fails
        finally:
            first.close()


# Open point 2 (not caused by the lingering close, found while measuring it): a client that announces a body and never sends it holds the single thread
# until the handler timeout (10 s). The console's stop is behind it. Risk: "stop never waits" does not hold at the console's HTTP layer.
# (Measured on loopback: stop answered in 0.02 s alone, 10.4 s behind such a client.)
@pytest.mark.xfail(strict=True, reason="gap: issue 1367")
def test_f2_the_console_stop_is_not_held_up_by_a_client_that_never_sends_its_body():
    b, _, _ = make()
    with ConsoleRunning(b) as s:
        port = s.server.server_address[1]
        stalled = socket.create_connection(("127.0.0.1", port), timeout=30)
        try:
            stalled.sendall(raw_request(port, "/v1/apply", "wrong" * 6, b"", content_length=1000))
            time.sleep(0.1)
            started = time.monotonic()
            req = urllib.request.Request(s.base + "/v1/stop", data=b"{}", method="POST",
                                         headers={"Authorization": "Bearer " + ALICE, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                assert resp.status == 200
            assert time.monotonic() - started < 2.0, "stop waited behind a stalled client"
        finally:
            stalled.close()


# Open point 3: an answer that does not go through _send (the standard library's own 501 for a method the handler does not define) is not lingered.
# Risk: the same reset can still hit a client that sends a body with such a request. (Measured: lingered_calls stays 0.)
@pytest.mark.xfail(strict=True, reason="gap: issue 1336 (answers that bypass the lingering close)")
def test_f3_a_refusal_for_an_unsupported_method_with_a_body_is_lingered_too_on_both_servers():
    t, _, _ = mcp_setup()
    with McpRunning(t, auth_token=TOKEN) as s:
        port = s.server.server_address[1]
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
        c.sendall(raw_request(port, "/mcp", TOKEN, b" " * 300_000, method="OPTIONS"))
        assert c.recv(65536).startswith(b"HTTP/1.0 501")
        c.close()
        assert wait_for(lambda: getattr(s.server, "lingered_calls", 0) >= 1), "the MCP server did not linger on its 501"
    b, _, _ = make()
    with ConsoleRunning(b) as s:
        port = s.server.server_address[1]
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
        c.sendall(raw_request(port, "/v1/apply", ALICE, b" " * (CONSOLE_MAX + 300_000), method="OPTIONS"))
        assert c.recv(65536).startswith(b"HTTP/1.0 501")
        c.close()
        assert wait_for(lambda: getattr(s.server, "lingered_calls", 0) >= 1), "the console did not linger on its 501"


# Guards that the lingering-close unit tests do not cover ------------------------------------------------------------------------------------------
def test_the_half_close_lets_a_client_that_reads_until_the_server_closes_finish_at_once():
    """A client that reads until the server closes must see the end of the answer at once. The linger time is raised to 5 s for this test:
    without the half-close the client would wait all of it, with it the client is done in milliseconds, so a bound of 2 s cannot flake."""
    saved = _linger.linger.__defaults__
    _linger.linger.__defaults__ = (5.0,) + tuple(saved[1:])
    try:
        t, _, _ = mcp_setup()
        with McpRunning(t, auth_token=TOKEN) as s:
            port = s.server.server_address[1]
            c = socket.create_connection(("127.0.0.1", port), timeout=15)
            started = time.monotonic()
            c.sendall(raw_request(port, "/mcp", "wrong" * 5, PING))
            data = b""
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    break
                data += chunk
            elapsed = time.monotonic() - started
            c.close()
            assert data.startswith(b"HTTP/1.0 401")
            assert elapsed < 2.0, "the client waited for the linger to run out: no half-close was sent"
    finally:
        _linger.linger.__defaults__ = saved


def parallel_refusals(n):
    t, _, _ = mcp_setup()
    with McpRunning(t, auth_token=TOKEN) as s:
        port = s.server.server_address[1]
        results, started = [], time.monotonic()

        def one():
            try:
                c = socket.create_connection(("127.0.0.1", port), timeout=20)
                c.sendall(raw_request(port, "/mcp", TOKEN, b"", content_length=70_000))
                try:
                    c.sendall(b" " * 70_000)
                except OSError:
                    pass
                results.append(c.recv(65536).split(b"\r\n", 1)[0].decode())
                c.close()
            except OSError as exc:
                results.append(type(exc).__name__)

        threads = [threading.Thread(target=one) for _ in range(n)]
        [x.start() for x in threads]
        [x.join(30) for x in threads]
        return results, time.monotonic() - started


def test_ten_parallel_refusals_from_clients_that_close_all_get_their_answer():
    results, elapsed = parallel_refusals(10)
    assert results == ["HTTP/1.0 413 Request Entity Too Large"] * 10, results
    assert elapsed < 20.0


def test_a_client_that_never_reads_the_answer_and_never_closes_costs_at_most_the_linger_time():
    t, _, _ = mcp_setup()
    with McpRunning(t, auth_token=TOKEN) as s:
        port = s.server.server_address[1]
        silent = socket.create_connection(("127.0.0.1", port), timeout=30)
        try:
            silent.sendall(raw_request(port, "/mcp", "wrong" * 5, PING))  # and never reads
            time.sleep(0.05)
            assert ping_latency(port) < 5.0  # absolute, and 10x the 0.5 s bound: it still catches a linger of 10 s; a bound taken from the constant would loosen with it
        finally:
            silent.close()


# The existing test for "a successful answer does not linger" only covers the MCP server; a break in the console's own condition went unnoticed.
def test_the_console_does_not_linger_on_a_successful_answer():
    b, _, _ = make()
    with ConsoleRunning(b) as s:
        for _ in range(3):  # one request at a time: when the last is answered, the handlers of the earlier ones are finished
            req = urllib.request.Request(s.base + "/v1/pending", headers={"Authorization": "Bearer " + ALICE})
            with urllib.request.urlopen(req, timeout=10) as resp:
                assert resp.status == 200
        assert getattr(s.server, "lingered_calls", 0) == 0
