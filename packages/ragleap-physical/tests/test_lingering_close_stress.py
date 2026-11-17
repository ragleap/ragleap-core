"""Linux-reproducible tests of the cause behind the Windows connection reset: closing a connection while request bytes are still unread.

The servers answer a refused request (for example a body that is too large) without reading the body. If the socket is then closed with bytes still
in its receive queue, the operating system resets the connection, and some systems (Windows) throw away the answer that was already sent. Linux keeps
the answer, so the symptom cannot be seen here, but the cause can: just before the server closes the socket we ask the kernel how many bytes are
still unread (FIONREAD). With the lingering close that number is 0 for a body up to the linger limit; without it, it is the size of the body.
Not run where fcntl does not exist (Windows). Timing bounds are wide on purpose."""
import socket
import threading
import time
import urllib.request

import pytest

try:
    import array
    import fcntl
    import termios
except ImportError:  # Windows
    fcntl = None

from ragleap_physical._linger import LINGER_MAX_BYTES

from helpers import make
from test_console import ALICE, Running as ConsoleRunning
from test_mcp_server import Running as McpRunning, setup as mcp_setup

TOKEN = "t" * 20
# The one test with a body above the linger limit: the server stops reading at the limit, so a client that sends more can block in sendall
# when the operating system's buffers are full. Whether that happens depends on the system (on some kernels the receive window grows while the
# server reads and absorbs the whole body), so the test makes it happen on purpose with small buffers on both sides, and bounds the wait itself: the client gives up sending after SEND_TIMEOUT, and the measuring hook waits for
# the client for HOOK_WAIT at most (it must wait longer than the client's send timeout). Without these two bounds the hook waits its full 10 s
# and the next client waits behind it.
SEND_TIMEOUT = 1.0
HOOK_WAIT = 3.0
SMALL_SEND_BUFFER = 4096
SMALL_RECEIVE_BUFFER = 4096  # on the server's listening socket: with an explicit size the receive window does not grow while the server reads
PING = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}'


def unread_bytes(sock):
    buf = array.array("i", [0])
    fcntl.ioctl(sock.fileno(), termios.FIONREAD, buf, True)
    return buf[0]


def watch_closes(server, sent, wait=10.0):
    """Record, for every connection the server closes, how many request bytes were still unread. The measurement waits until the client has
    finished sending (the `sent` event), so a slow client cannot make the number look small."""
    closes, original = [], server.shutdown_request

    def hooked(request):
        sent.wait(wait)
        time.sleep(0.05)
        try:
            closes.append(unread_bytes(request))
        except OSError:
            closes.append(-1)
        original(request)
    server.shutdown_request = hooked
    return closes


def request_bytes(port, path, token, content_length):
    return ("POST %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
            % (path, port, token, content_length)).encode()


def send_body(sock, size, sent, timeout=None):
    """Send `size` body bytes; stop quietly if the server closes first (that is what a body above the linger limit leads to) or, when a
    `timeout` is given, if the operating system does not take more bytes for that long (a timeout is an OSError)."""
    chunk, left = b" " * 65536, size
    previous = sock.gettimeout()
    try:
        if timeout is not None:
            sock.settimeout(timeout)
        while left > 0:
            n = min(left, len(chunk))
            sock.sendall(chunk[:n])
            left -= n
    except OSError:
        pass
    finally:
        sock.settimeout(previous)
        sent.set()


def ping_latency(port):
    started = time.monotonic()
    s = socket.create_connection(("127.0.0.1", port), timeout=30)
    s.sendall(request_bytes(port, "/mcp", TOKEN, len(PING)) + PING)
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    assert data.startswith(b"HTTP/1.0 200"), data[:60]
    return time.monotonic() - started


def refused_oversize_post(kind, size, read_answer=True, hold=0.0, send_timeout=None, hook_wait=10.0, send_buffer=None, receive_buffer=None):
    """One refused request with `size` body bytes against the MCP server or the console. Returns what the server recorded."""
    sent = threading.Event()
    if kind == "mcp":
        t, _, _ = mcp_setup()
        runner, path, token = McpRunning(t, auth_token=TOKEN), "/mcp", TOKEN
    else:
        b, _, _ = make()
        runner, path, token = ConsoleRunning(b), "/v1/apply", ALICE
    with runner as s:
        if receive_buffer is not None:  # accepted connections inherit it from the listening socket
            s.server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        closes = watch_closes(s.server, sent, hook_wait)
        port = s.server.server_address[1]
        c = socket.create_connection(("127.0.0.1", port), timeout=30)
        if send_buffer is not None:
            c.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, send_buffer)
        try:
            c.sendall(request_bytes(port, path, token, size))
            send_body(c, size, sent, send_timeout)
            if read_answer:  # read the whole answer, as a real client does: closing with unread answer bytes would make the client reset the connection
                answer = b""
                try:
                    while True:
                        chunk = c.recv(65536)
                        if not chunk:
                            break
                        answer += chunk
                except OSError:
                    pass
                assert answer.startswith(b"HTTP/1.0 413"), answer[:60]
            time.sleep(hold)
            # the client is still connected: the next client has to wait for the linger of this one, so the wait shows how long the linger lasts
            second = ping_latency(port) if (kind == "mcp" and not read_answer) else None
        finally:
            c.close()
        end = time.monotonic() + 15.0
        while not closes and time.monotonic() < end:
            time.sleep(0.01)
        first_close, lingered = list(closes[:1]), getattr(s.server, "lingered_bytes", 0)  # only this client's close, not the next client's
        if second is None and kind == "mcp":
            second = ping_latency(port)
    return first_close, lingered, second


def test_nothing_is_left_unread_when_the_mcp_server_closes_after_a_refused_body_up_to_the_limit():
    if fcntl is None:
        pytest.skip("needs fcntl, which Windows does not have")
    for size in (70_000, 300_000, 900_000):
        closes, _, _ = refused_oversize_post("mcp", size)
        assert closes == [0], "%d body bytes: unread bytes at close %s" % (size, closes)


def test_nothing_is_left_unread_when_the_console_closes_after_a_refused_body_up_to_the_limit():
    if fcntl is None:
        pytest.skip("needs fcntl, which Windows does not have")
    for size in (70_000, 300_000, 900_000):
        closes, _, _ = refused_oversize_post("console", size)
        assert closes == [0], "%d body bytes: unread bytes at close %s" % (size, closes)


def test_a_client_that_never_reads_the_answer_and_never_closes_leaves_nothing_unread():
    if fcntl is None:
        pytest.skip("needs fcntl, which Windows does not have")
    for kind in ("mcp", "console"):
        closes, _, second = refused_oversize_post(kind, 300_000, read_answer=False, hold=0.1)
        assert closes == [0], "%s: unread bytes at close %s" % (kind, closes)
        if second is not None:
            assert second < 5.0


def test_a_body_larger_than_the_linger_limit_is_cut_off_at_the_limit_and_the_server_stays_responsive():
    if fcntl is None:
        pytest.skip("needs fcntl, which Windows does not have")
    size = 4 * LINGER_MAX_BYTES
    started = time.monotonic()
    closes, lingered, second = refused_oversize_post("mcp", size, read_answer=False, hold=0.1, send_timeout=SEND_TIMEOUT, hook_wait=HOOK_WAIT,
                                                     send_buffer=SMALL_SEND_BUFFER, receive_buffer=SMALL_RECEIVE_BUFFER)
    elapsed = time.monotonic() - started
    assert elapsed < 8.0, "the exchange took %.1f s: the wait for a client that cannot finish sending is not bounded" % elapsed
    assert len(closes) == 1
    assert lingered <= LINGER_MAX_BYTES + 65536, "the linger read %d bytes, the limit is %d" % (lingered, LINGER_MAX_BYTES)
    assert second < 5.0, "the server did not answer the next client in time"
    # what is left unread above the limit is the known remaining hazard: the lingering close does not cover bodies larger than its limit
