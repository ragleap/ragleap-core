"""The operator console serves each connection in its own thread (issue 1367). A client that stalls, or an apply that is still running, never holds a
stop request; the number of connections is capped; the shared counters do not lose updates. Standard library only, loopback only."""
import socket
import sys
import threading
import time

from ragleap_physical import console
from ragleap_physical.adapters import FakeJointAdapter

from helpers import make, propose
from test_console import ALICE, Running as ConsoleRunning, call


def wait_for(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class BlockingAdapter(FakeJointAdapter):
    """Blocks inside apply() until the test lets it go: it stands for an apply that is still running."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def apply(self, target, command, value):
        self.entered.set()
        self.release.wait(20)
        return super().apply(target, command, value)


def approved(bridge, base):
    p = propose(bridge)
    status, _ = call(base, "/v1/approve", {"proposal_id": p.proposal_id, "digest": p.digest()})
    assert status == 200
    return p


def test_a_stop_is_answered_while_an_apply_is_running():
    adapter = BlockingAdapter()
    b, _, _ = make(adapter=adapter)
    with ConsoleRunning(b) as s:
        p = approved(b, s.base)
        result = {}
        t = threading.Thread(target=lambda: result.update(r=call(s.base, "/v1/apply", {"proposal_id": p.proposal_id})), daemon=True)
        t.start()
        try:
            assert adapter.entered.wait(5), "the apply did not start"
            started = time.monotonic()
            status, body = call(s.base, "/v1/stop", {})
            assert status == 200 and body == {"stopped": True}
            assert time.monotonic() - started < 2.0, "the stop waited for the running apply"
        finally:
            adapter.release.set()
            t.join(10)
        assert result["r"][0] == 423, "the apply must end stopped"


def test_other_requests_are_served_while_a_client_stalls():
    b, _, _ = make()
    with ConsoleRunning(b) as s:
        port = s.server.server_address[1]
        stalled = socket.create_connection(("127.0.0.1", port), timeout=30)
        try:
            stalled.sendall(("POST /v1/apply HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\n"
                             "Content-Length: 1000\r\n\r\n" % (port, ALICE)).encode())
            time.sleep(0.1)
            started = time.monotonic()
            status, body = call(s.base, "/v1/pending")
            assert status == 200 and body == {"pending": []}
            assert time.monotonic() - started < 2.0, "a request waited behind a stalled client"
        finally:
            stalled.close()


def test_connections_beyond_the_cap_are_refused_at_once_and_the_slots_come_back():
    old = console.MAX_CONNECTIONS
    console.MAX_CONNECTIONS = 2
    try:
        b, _, _ = make()
        with ConsoleRunning(b) as s:
            port = s.server.server_address[1]
            held = [socket.create_connection(("127.0.0.1", port), timeout=30) for _ in range(2)]
            try:
                for h in held:
                    h.sendall(b"POST /v1/apply HTTP/1.1\r\n")  # an unfinished request holds its slot
                status, started = None, time.monotonic()
                while time.monotonic() - started < 5.0:
                    status, _ = call(s.base, "/v1/pending")
                    if status == 503:
                        break
                    time.sleep(0.05)
                assert status == 503, "the third connection was not refused"
                assert s.server.refused_busy >= 1
            finally:
                for h in held:
                    h.close()
            assert wait_for(lambda: call(s.base, "/v1/pending")[0] == 200), "the slots did not come back"
    finally:
        console.MAX_CONNECTIONS = old


def test_the_counters_are_updated_under_a_lock_and_lose_no_updates():
    b, _, _ = make()
    server = console.make_console(b, {ALICE: "person:alice"})
    old = sys.getswitchinterval()
    try:
        server._counts.acquire()  # the test holds the lock: count() must wait for it
        t = threading.Thread(target=lambda: server.count(lingered_bytes=5, lingered_calls=1), daemon=True)
        t.start()
        try:
            t.join(0.3)
            assert t.is_alive(), "count() did not wait for the lock"
            assert server.lingered_calls == 0
        finally:
            server._counts.release()
        t.join(5)
        assert not t.is_alive() and (server.lingered_calls, server.lingered_bytes) == (1, 5)
        sys.setswitchinterval(1e-6)

        def work():
            for _ in range(2000):
                server.count(drained_bytes=1)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert server.drained_bytes == 16000
    finally:
        sys.setswitchinterval(old)
        server.server_close()


def test_two_parallel_applies_of_one_approval_apply_exactly_once():
    b, adapter, _ = make()
    with ConsoleRunning(b) as s:
        p = approved(b, s.base)
        barrier = threading.Barrier(2)
        out = []

        def go():
            barrier.wait(5)
            out.append(call(s.base, "/v1/apply", {"proposal_id": p.proposal_id})[0])

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert sorted(out) == [200, 409], out
        assert len(adapter.applied) == 1
