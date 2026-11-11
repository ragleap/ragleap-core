"""A refused request is answered, and then the connection is closed the way HTTP servers are meant to close it (a "lingering close"):
stop sending, read and throw away what the client is still sending for a short, bounded time. Without it, a Windows client that sends a
body the server did not read can get a connection reset (WinError 10053) instead of the answer. Checked through server-side counters
and a socket pair, because the reset itself cannot be forced on every operating system."""
import http.client
import socket
import threading
import time

from ragleap_physical._linger import LINGER_MAX_BYTES, linger
from ragleap_physical.console import MAX_BODY as CONSOLE_MAX
from ragleap_physical.mcp_server import MAX_BODY as MCP_MAX

from helpers import make
from test_console import ALICE, Running as ConsoleRunning
from test_mcp_server import Running as McpRunning, setup as mcp_setup


def post(port, path, body_len, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=b"x" * body_len, headers=dict(headers, **{"Content-Length": str(body_len), "Content-Type": "application/json"}))
    r = conn.getresponse()
    status = r.status
    r.read()
    conn.close()
    return status


def wait_for(predicate, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---- the helper itself, on a socket pair
def pair():
    a, b = socket.socketpair()
    return a, b


def test_linger_discards_what_the_peer_still_sends_and_stops_when_it_closes():
    server_side, client_side = pair()
    try:
        client_side.sendall(b"y" * 100_000)
        client_side.close()
        assert linger(server_side, seconds=2.0) == 100_000
    finally:
        server_side.close()


def test_linger_is_bounded_in_bytes():
    server_side, client_side = pair()
    stop = threading.Event()

    def flood():
        try:
            while not stop.is_set():
                client_side.sendall(b"z" * 65536)
        except OSError:
            pass

    t = threading.Thread(target=flood, daemon=True)
    t.start()
    try:
        started = time.monotonic()
        got = linger(server_side, seconds=5.0)
        assert LINGER_MAX_BYTES <= got < LINGER_MAX_BYTES + 2 * 65536
        assert time.monotonic() - started < 4.0
    finally:
        stop.set()
        server_side.close()
        client_side.close()
        t.join(2)


def test_linger_is_bounded_in_time_when_the_peer_stays_silent():
    server_side, client_side = pair()
    try:
        started = time.monotonic()
        assert linger(server_side, seconds=0.2) == 0
        assert 0.15 <= time.monotonic() - started < 1.5
    finally:
        server_side.close()
        client_side.close()


def test_linger_survives_a_peer_that_is_already_gone():
    server_side, client_side = pair()
    client_side.close()
    assert linger(server_side, seconds=0.5) == 0
    server_side.close()


# ---- both servers
def test_mcp_server_discards_an_oversize_refused_body_after_answering():
    t, _, _ = mcp_setup()
    with McpRunning(t, auth_token="t" * 20) as s:
        port = s.server.server_address[1]
        assert post(port, "/mcp", MCP_MAX + 1000, {"Authorization": "Bearer " + "t" * 20}) == 413
        assert wait_for(lambda: getattr(s.server, "lingered_calls", 0) >= 1)
        assert s.server.lingered_bytes > 0
        assert getattr(s.server, "drained_bytes", 0) == 0  # the body was not read before the answer, only discarded after it


def test_console_discards_an_oversize_refused_body_after_answering():
    b, _, _ = make()
    with ConsoleRunning(b) as s:
        port = s.server.server_address[1]
        # the body must be larger than what the server's read buffer already holds when it has parsed the headers
        assert post(port, "/v1/apply", CONSOLE_MAX + 300_000, {"Authorization": "Bearer " + ALICE}) == 413
        assert wait_for(lambda: getattr(s.server, "lingered_calls", 0) >= 1)
        assert s.server.lingered_bytes > 0


def test_a_small_refused_body_is_still_drained_before_the_answer():
    b, _, _ = make()
    with ConsoleRunning(b) as s:
        port = s.server.server_address[1]
        assert post(port, "/v1/apply", 3000, {"Authorization": "Bearer wrong"}) == 401
        assert s.server.drained_bytes == 3000


def test_an_answer_that_is_not_an_error_does_not_linger():
    t, _, _ = mcp_setup()
    with McpRunning(t, auth_token="t" * 20) as s:
        port = s.server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}'
        conn.request("POST", "/mcp", body=body, headers={"Authorization": "Bearer " + "t" * 20, "Content-Type": "application/json", "Content-Length": str(len(body))})
        r = conn.getresponse()
        r.read()
        conn.close()
        assert r.status < 400
        # the server handles one request at a time: once this second request is answered, the first handler is certainly finished
        assert post(port, "/mcp", 10, {"Authorization": "Bearer wrong"}) == 401
        # a third request: once it is answered, the refusal's handler (and its counter) is certainly finished
        again = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        again.request("POST", "/mcp", body=body, headers={"Authorization": "Bearer " + "t" * 20,
                      "Content-Type": "application/json", "Content-Length": str(len(body))})
        r2 = again.getresponse()
        r2.read()
        again.close()
        assert r2.status < 400
        assert s.server.lingered_calls == 1  # only the refusal lingered, not the successful answers


def test_many_oversize_refusals_all_get_their_answer():
    b, _, _ = make()
    with ConsoleRunning(b) as s:
        port = s.server.server_address[1]
        for _ in range(50):
            assert post(port, "/v1/apply", CONSOLE_MAX + 5000, {"Authorization": "Bearer " + ALICE}) == 413
        assert wait_for(lambda: getattr(s.server, "lingered_calls", 0) >= 50, 10.0)


def test_a_client_that_sends_a_huge_body_cannot_hold_the_server():
    t, _, _ = mcp_setup()
    with McpRunning(t, auth_token="t" * 20) as s:
        port = s.server.server_address[1]
        started = time.monotonic()
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        head = ("POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\n"
                "Content-Length: %d\r\n\r\n" % (port, "t" * 20, 6 * 1024 * 1024)).encode()
        try:
            sock.sendall(head)
            for _ in range(96):
                sock.sendall(b"q" * 65536)
        except OSError:
            pass  # the server cut the sender off, which is the point
        finally:
            sock.close()
        assert time.monotonic() - started < 10.0
        # the server is still answering
        assert post(port, "/mcp", 10, {"Authorization": "Bearer wrong"}) == 401
