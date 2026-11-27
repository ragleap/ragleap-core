"""A client that is refused with 503 because every console slot is taken still gets its answer: what it has already sent is read and discarded
by a bounded helper thread (a lingering close), and the accept loop never waits for a client. Checked through server-side counters on a loopback
socket; the connection reset itself that a Windows client can see cannot be forced on every operating system."""
import socket
import time

from ragleap_physical import console

from helpers import make
from test_console import ALICE, Running as ConsoleRunning


def wait_for(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def hold_every_slot(running, slots):
    port = running.server.server_address[1]
    held = [socket.create_connection(("127.0.0.1", port), timeout=30) for _ in range(slots)]
    for h in held:
        h.sendall(b"POST /v1/apply HTTP/1.1\r\n")  # an unfinished request holds its slot
    return port, held


def close_all(sockets):
    for c in sockets:
        try:
            c.close()
        except OSError:
            pass


def test_a_busy_refusal_reads_and_discards_what_the_client_already_sent():
    old = console.MAX_CONNECTIONS
    console.MAX_CONNECTIONS = 2
    held, c = [], None
    try:
        b, _, _ = make()
        with ConsoleRunning(b) as s:
            port, held = hold_every_slot(s, 2)
            c = socket.create_connection(("127.0.0.1", port), timeout=10)
            head = ("POST /v1/apply HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\n"
                    "Content-Length: 3000\r\n\r\n" % (port, ALICE)).encode()
            c.sendall(head + b"x" * 3000)
            assert c.recv(4096).startswith(b"HTTP/1.0 503")
            c.close()
            assert wait_for(lambda: s.server.lingered_calls >= 1), "the refused connection was not lingered"
            assert s.server.lingered_bytes >= 3000
            assert s.server.refused_busy >= 1
            close_all(held)
    finally:
        close_all(held + ([c] if c else []))
        console.MAX_CONNECTIONS = old


def test_a_flood_of_silent_refused_clients_does_not_hold_up_the_accept_loop():
    # Inline lingering would keep the accept loop busy for up to 0.5 s per silent client (30 clients: 15 s); with helpers every refusal is answered at once.
    old = console.MAX_CONNECTIONS
    console.MAX_CONNECTIONS = 2
    held, flood = [], []
    try:
        b, _, _ = make()
        with ConsoleRunning(b) as s:
            port, held = hold_every_slot(s, 2)
            started = time.monotonic()
            for _ in range(30):
                c = socket.create_connection(("127.0.0.1", port), timeout=10)
                flood.append(c)
                assert c.recv(100).startswith(b"HTTP/1.0 503")
            assert time.monotonic() - started < 8.0, "the refusals waited for the lingering of the clients before them"
            assert s.server.refused_busy >= 30
            close_all(flood + held)
    finally:
        close_all(flood + held)
        console.MAX_CONNECTIONS = old


def test_the_lingering_helpers_stay_within_their_cap_and_are_all_given_back():
    old_c, old_h = console.MAX_CONNECTIONS, console.MAX_LINGER_HELPERS
    console.MAX_CONNECTIONS, console.MAX_LINGER_HELPERS = 2, 2
    held, flood = [], []
    try:
        b, _, _ = make()
        with ConsoleRunning(b) as s:
            port, held = hold_every_slot(s, 2)
            for _ in range(10):
                c = socket.create_connection(("127.0.0.1", port), timeout=10)
                flood.append(c)
                assert c.recv(100).startswith(b"HTTP/1.0 503")
            assert 1 <= s.server.lingering_peak <= 2
            close_all(flood + held)
            assert wait_for(lambda: s.server.lingering_now == 0), "a helper is still lingering after every client has gone"
            for _ in range(2):
                assert s.server._helpers.acquire(blocking=False), "a helper slot was not given back"
            for _ in range(2):
                s.server._helpers.release()
    finally:
        close_all(flood + held)
        console.MAX_CONNECTIONS, console.MAX_LINGER_HELPERS = old_c, old_h
