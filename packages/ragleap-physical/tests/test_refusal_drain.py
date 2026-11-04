"""A refused request must not leave its body unread: the server drains a small body first, so a client (notably on Windows) gets the
answer and not a connection reset (WinError 10053). Checked through a server-side counter and the Connection header."""
import http.client

from ragleap_physical.console import MAX_BODY as CONSOLE_MAX
from ragleap_physical.mcp_server import MAX_BODY as MCP_MAX

from helpers import make
from test_console import ALICE, Running as ConsoleRunning
from test_mcp_server import Running as McpRunning, setup as mcp_setup


def refused_post(port, path, body_len, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=b"x" * body_len, headers=dict(headers, **{"Content-Length": str(body_len), "Content-Type": "application/json"}))
    r = conn.getresponse()
    status, connection = r.status, r.getheader("Connection")
    r.read()
    conn.close()
    return status, connection


def test_console_drains_a_small_refused_body_and_says_connection_close():
    b, _, _ = make()
    with ConsoleRunning(b) as s:
        port = s.server.server_address[1]
        status, connection = refused_post(port, "/v1/apply", 3000, {"Authorization": "Bearer wrong"})
        assert status == 401 and connection == "close"
        assert s.server.drained_bytes == 3000
        # a body larger than the cap is refused without being read
        before = s.server.drained_bytes
        assert refused_post(port, "/v1/apply", CONSOLE_MAX + 1, {"Authorization": "Bearer " + ALICE})[0] == 413
        assert s.server.drained_bytes == before


def test_console_does_not_read_a_body_twice_when_it_answers_an_error_after_reading_it():
    b, _, _ = make()
    with ConsoleRunning(b) as s:
        port = s.server.server_address[1]
        status, _ = refused_post(port, "/v1/apply", 20, {"Authorization": "Bearer " + ALICE})
        assert status == 400  # not a JSON object: the body was read once, by the parser
        assert getattr(s.server, "drained_bytes", 0) == 0


def test_mcp_server_drains_a_small_refused_body_and_says_connection_close():
    t, _, _ = mcp_setup()
    with McpRunning(t, auth_token="t" * 20) as s:
        port = s.server.server_address[1]
        status, connection = refused_post(port, "/mcp", 3000, {"Authorization": "Bearer wrong"})
        assert status == 401 and connection == "close"
        assert s.server.drained_bytes == 3000
        before = s.server.drained_bytes
        assert refused_post(port, "/mcp", MCP_MAX + 1, {"Authorization": "Bearer " + "t" * 20})[0] == 413
        assert s.server.drained_bytes == before


def test_many_refused_posts_with_bodies_all_get_an_answer():
    b, _, _ = make()
    with ConsoleRunning(b) as s:
        port = s.server.server_address[1]
        for _ in range(100):
            assert refused_post(port, "/v1/apply", 3000, {"Authorization": "Bearer wrong"})[0] == 401
        assert s.server.drained_bytes == 100 * 3000
